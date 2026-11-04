"""Repository backend interface — GitPython-free (§8, §11).

All Orchestrator git operations go through ``run()`` — a backend is
selected explicitly (``TO_GIT_BACKEND=dulwich|cli``; default ``dulwich``).
A backend failure is surfaced honestly — never a silent fallback to the
other backend or to system git.

The ``cli`` backend shells out to ``git_runtime.executable()`` (bundled
git when shipped, else the declared host-git prerequisite). It exists as
the fallback path for source builds; the frozen candidate defaults to
``dulwich``, which needs no external git, no system ssh for https, and no
merge3 — merge is a conservative blob-level three-way that merges
disjoint-path divergence (the export workflow's real case) and reports
same-path conflicting writes as MergeConflictError.

Op surface: init, identity, clone, fetch, update, checkout, resolve,
head, branch, commit_all, has_remote, remote_ref, merge, push.
"""

import os
import subprocess
from pathlib import Path

from . import git_runtime


class GitBackendError(Exception):
    pass


class MergeConflictError(GitBackendError):
    """Diverged same-path writes — caller surfaces, never force-pushes."""

    def __init__(self, paths):
        self.paths = list(paths)
        shown = ", ".join(
            p.decode(errors="replace") if isinstance(p, bytes) else p
            for p in self.paths[:10])
        super().__init__(f"merge conflict in {shown}")


IDENTITY_NAME = "TestOrchestrator"
IDENTITY_EMAIL = "testorchestrator@localhost"
_IDENT = f"{IDENTITY_NAME} <{IDENTITY_EMAIL}>".encode()


# ---------- CLI backend (subprocess; no GitPython) --------------------

def _cli(args: list, cwd: Path = None, env: dict = None) -> str:
    res = subprocess.run([git_runtime.executable(), *args], cwd=cwd,
                         env=env or git_runtime.env(), capture_output=True,
                         text=True, timeout=300)
    if res.returncode != 0:
        raise GitBackendError((res.stderr or res.stdout).strip()[:400])
    return res.stdout.strip()


def cli_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        _cli(["init", str(path)])


def cli_identity(path: Path, name: str, email: str) -> None:
    _cli(["config", "user.name", name], cwd=path)
    _cli(["config", "user.email", email], cwd=path)


def cli_clone(url: str, dest: Path, branch: str = None,
              depth: int = None, auth: tuple = None) -> None:
    args = ["clone"]
    if branch:
        args += ["--branch", branch]
    if depth:
        args += ["--depth", str(depth)]
    _cli([*args, "--", url, str(dest)])


def cli_fetch(repo: Path, remote: str = "origin",
              auth: tuple = None) -> None:
    _cli(["fetch", "--", remote], cwd=repo)


def cli_update(repo: Path, ref: str = "", auth: tuple = None) -> None:
    _cli(["fetch", "--", "origin"], cwd=repo)
    _cli(["checkout", f"origin/{ref}" if ref else "HEAD"], cwd=repo)


def cli_checkout(repo: Path, sha: str, dest: Path) -> Path:
    """Isolated worktree at exact sha — concurrent checkouts never share
    a working directory (adds a git worktree, detached)."""
    dest.mkdir(parents=True, exist_ok=True)
    _cli(["worktree", "add", "--detach", str(dest), sha], cwd=repo)
    return dest


def cli_resolve(repo: Path, ref: str = "HEAD") -> str:
    return _cli(["rev-parse", ref], cwd=repo)


def cli_head(repo: Path) -> str:
    try:
        return _cli(["rev-parse", "--verify", "HEAD"], cwd=repo)
    except GitBackendError:
        return ""


def cli_branch(repo: Path) -> str:
    return _cli(["symbolic-ref", "--short", "HEAD"], cwd=repo)


def cli_commit_all(repo: Path, message: str) -> str:
    _cli(["add", "-A"], cwd=repo)
    head = cli_head(repo)
    if head:
        # Staged tree identical to HEAD -> nothing to write.
        res = subprocess.run(
            [git_runtime.executable(), "diff-index", "--quiet", "HEAD",
             "--"], cwd=repo, env=git_runtime.env())
        if res.returncode == 0:
            return head
    _cli(["commit", "-m", message], cwd=repo)
    return cli_head(repo)


def cli_has_remote(repo: Path, name: str = "origin") -> bool:
    return name in _cli(["remote"], cwd=repo).split()


def cli_remote_ref(repo: Path, branch: str,
                   remote: str = "origin") -> str:
    try:
        return _cli(["rev-parse", "--verify",
                     f"refs/remotes/{remote}/{branch}"], cwd=repo)
    except GitBackendError:
        return ""


def cli_merge(repo: Path, branch: str, remote: str = "origin") -> str:
    try:
        _cli(["merge", "--no-edit", f"{remote}/{branch}"], cwd=repo)
    except GitBackendError as exc:
        try:
            _cli(["merge", "--abort"], cwd=repo)
        except GitBackendError:
            pass
        raise MergeConflictError([f"{remote}/{branch}".encode()]) \
            if "conflict" in str(exc).lower() or "CONFLICT" in str(exc) \
            else exc
    return cli_head(repo)


def cli_push(repo: Path, refspec: str, auth: tuple = None,
             set_upstream: bool = False) -> None:
    if set_upstream:
        branch = refspec.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
        _cli(["push", "--set-upstream", "origin", branch], cwd=repo)
    else:
        _cli(["push", "origin", refspec], cwd=repo)


# ---------- Dulwich backend (no external git) -------------------------

def _dulwich_repo(repo: Path):
    try:
        from dulwich.repo import Repo
    except ImportError as exc:
        raise GitBackendError(f"dulwich unavailable: {exc}")
    return Repo(str(repo))


def _repo_config_write(repo_path: Path, section, name: str, value) -> None:
    r = _dulwich_repo(repo_path)
    cfg = r.get_config()
    cfg.set(section, name, value)
    with open(repo_path / ".git" / "config", "wb") as f:
        cfg.write_to_file(f)


def dulwich_init(path: Path) -> None:
    from dulwich import porcelain
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        porcelain.init(str(path))


def dulwich_identity(path: Path, name: str, email: str) -> None:
    _repo_config_write(path, (b"user",), b"name", name.encode())
    _repo_config_write(path, (b"user",), b"email", email.encode())


def dulwich_clone(url: str, dest: Path, branch: str = None,
                  depth: int = None, auth: tuple = None) -> None:
    """Clone via Dulwich porcelain — supports file://, https, ssh (host
    ssh for ssh URLs). ``auth`` is (username, password) for https."""
    from dulwich import porcelain
    kwargs = {}
    if branch:
        kwargs["branch"] = branch.encode()
    if depth:
        kwargs["depth"] = depth
    if auth:
        kwargs["username"], kwargs["password"] = auth
    try:
        with open(os.devnull, "wb") as sink:
            porcelain.clone(url, str(dest), errstream=sink, **kwargs)
    except Exception as exc:
        raise GitBackendError(f"dulwich clone failed: {exc}")


def dulwich_fetch(repo: Path, remote: str = "origin",
                  auth: tuple = None) -> None:
    from dulwich import porcelain
    kwargs = {"username": auth[0], "password": auth[1]} if auth else {}
    try:
        with open(os.devnull, "wb") as sink:
            porcelain.fetch(str(repo), remote, errstream=sink, **kwargs)
    except Exception as exc:
        raise GitBackendError(f"dulwich fetch failed: {exc}")


def dulwich_update(repo: Path, ref: str = "", auth: tuple = None) -> None:
    """Fetch + hard-reset the worktree to origin/<ref> — the binding
    worktree stays a real repository (keeps .git) for the next update."""
    from dulwich import porcelain
    kwargs = {"username": auth[0], "password": auth[1]} if auth else {}
    try:
        with open(os.devnull, "wb") as sink:
            porcelain.fetch(str(repo), "origin", errstream=sink, **kwargs)
        target = (f"origin/{ref}" if ref else "HEAD")
        porcelain.reset(str(repo), mode="hard", treeish=target)
    except Exception as exc:
        raise GitBackendError(f"dulwich update failed: {exc}")


def dulwich_checkout(repo: Path, sha: str, dest: Path) -> Path:
    """Isolated checkout at exact sha by tree export — same isolation
    guarantee as `git worktree --detach` without touching the source
    worktree's index. Export-only destination (no .git needed there)."""
    from dulwich.objects import Tree
    r = _dulwich_repo(repo)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        tree = r[sha.encode()] if isinstance(sha, str) else r[sha]
        tree = r[tree.tree] if hasattr(tree, "tree") else tree
        _export_tree(r, tree, dest)
    except Exception as exc:
        raise GitBackendError(f"dulwich checkout {sha[:8]} failed: {exc}")
    (dest / ".checkout-sha").write_text(sha)
    return dest


def _export_tree(r, tree, dest: Path) -> None:
    from dulwich.objects import Tree as _T, Blob
    for name, mode, oid in tree.iteritems():
        p = dest / name.decode()
        obj = r[oid]
        if isinstance(obj, _T):
            p.mkdir(exist_ok=True)
            _export_tree(r, obj, p)
        elif isinstance(obj, Blob):
            p.write_bytes(obj.data)
            if mode & 0o111:
                p.chmod(0o755)


def dulwich_resolve(repo: Path, ref: str = "HEAD") -> str:
    """Resolve any commit-ish: exact sha, branch, tag, remote ref, or
    `~N` parent-walks (HEAD~1) — exact-revision selection stays
    byte-exact."""
    r = _dulwich_repo(repo)
    base, sep, back = ref.partition("~")
    candidates = [base]
    if not base.startswith("refs/") and base != "HEAD":
        candidates += [f"refs/heads/{base}", f"refs/tags/{base}",
                       f"refs/remotes/{base}"]
    obj = None
    for cand in candidates:
        try:
            obj = r[cand.encode()]
            break
        except (KeyError, ValueError):
            continue
    if obj is None:
        raise GitBackendError(f"cannot resolve {ref!r}")
    while hasattr(obj, "object"):      # annotated tag
        obj = r[obj.object[1]]
    if sep and back.isdigit():
        for _ in range(int(back)):     # first-parent walk
            parents = getattr(obj, "parents", None) or []
            if not parents:
                raise GitBackendError(f"{ref!r}: no parent for ~{back}")
            obj = r[parents[0]]
    return obj.id.decode()


def dulwich_head(repo: Path) -> str:
    r = _dulwich_repo(repo)
    try:
        return r.head().decode()
    except (KeyError, ValueError):
        return ""


def dulwich_branch(repo: Path) -> str:
    r = _dulwich_repo(repo)
    ref = r.refs.read_ref(b"HEAD")
    if ref and ref.startswith(b"ref: refs/heads/"):
        return ref[len(b"ref: refs/heads/"):].decode()
    raise GitBackendError("HEAD is detached — no active branch")


def dulwich_commit_all(repo: Path, message: str) -> str:
    """git add -A + commit-if-dirty. Returns the (possibly unchanged)
    HEAD sha — a clean tree writes no empty commit."""
    from dulwich import porcelain
    r = _dulwich_repo(repo)
    porcelain.add(r)                       # includes deletions
    index = r.open_index(config=r.get_config_stack())
    tree_id = index.commit(r.object_store)
    try:
        head = r.head()
        if r[head].tree == tree_id:
            return head.decode()           # clean — nothing to write
    except (KeyError, ValueError):
        pass                               # unborn HEAD -> first commit
    sha = r.get_worktree().commit(
        message=message.encode(), author=_IDENT, committer=_IDENT,
        no_verify=True)
    return sha.decode()


def dulwich_has_remote(repo: Path, name: str = "origin") -> bool:
    r = _dulwich_repo(repo)
    try:
        r.get_config().get((b"remote", name.encode()), b"url")
        return True
    except KeyError:
        return False


def dulwich_remote_ref(repo: Path, branch: str,
                       remote: str = "origin") -> str:
    r = _dulwich_repo(repo)
    try:
        return r.refs[f"refs/remotes/{remote}/{branch}".encode()].decode()
    except KeyError:
        return ""


def _flatten_tree(r, tree_id, prefix: bytes = b"") -> dict:
    """{path: (mode, sha)} for every blob under a tree."""
    out = {}
    if tree_id is None:
        return out
    from dulwich.objects import Tree
    for name, mode, oid in r[tree_id].iteritems():
        obj = r[oid]
        if isinstance(obj, Tree):
            out.update(_flatten_tree(r, oid, prefix + name + b"/"))
        else:
            out[prefix + name] = (mode, oid)
    return out


def _build_tree(r, entries: dict):
    """Write nested Trees for flat {path: (mode, sha)}; return root id."""
    from dulwich.objects import Tree
    nested = {}
    for path, entry in entries.items():
        parts = path.split(b"/")
        d = nested
        for part in parts[:-1]:
            d = d.setdefault(part, {})
        d[parts[-1]] = entry

    def make(sub):
        t = Tree()
        for name in sorted(sub):
            value = sub[name]
            if isinstance(value, dict):
                t[name] = (0o40000, make(value))
            else:
                t[name] = value
        r.object_store.add_object(t)
        return t.id

    return make(nested)


def _merge_entries(base: dict, ours: dict, theirs: dict):
    """Conservative three-way merge at blob granularity. A path changed
    on only one side (or identically on both) merges; paths changed
    differently on both sides are real conflicts — reported, never
    silently resolved."""
    merged, conflicts = {}, []
    for path in set(base) | set(ours) | set(theirs):
        b, o, t = base.get(path), ours.get(path), theirs.get(path)
        if o == t:
            if o is not None:
                merged[path] = o
        elif o == b:
            if t is not None:
                merged[path] = t
        elif t == b:
            if o is not None:
                merged[path] = o
        else:
            conflicts.append(path)
    return merged, conflicts


def dulwich_merge(repo: Path, branch: str, remote: str = "origin") -> str:
    """Merge refs/remotes/<remote>/<branch> into HEAD.

    - remote already contained in HEAD: no-op.
    - fast-forward: HEAD moves to the remote commit.
    - divergence: blob-level three-way merge into a merge commit.
      Same-path conflicting writes -> MergeConflictError with the worktree
      left untouched (equivalent to merge --abort)."""
    from dulwich import porcelain
    from dulwich.graph import find_merge_base
    r = _dulwich_repo(repo)
    ours = r.head()
    try:
        theirs = r.refs[f"refs/remotes/{remote}/{branch}".encode()]
    except KeyError as exc:
        raise GitBackendError(f"no remote ref for {remote}/{branch}") \
            from exc
    if ours == theirs:
        return ours.decode()
    bases = find_merge_base(r, [ours, theirs])
    if theirs in bases:
        return ours.decode()               # remote already merged
    if ours in bases:                      # fast-forward
        porcelain.reset(str(repo), mode="hard", treeish=theirs)
        return theirs.decode()
    base_tree = r[bases[0]].tree if bases else None
    merged, conflicts = _merge_entries(
        _flatten_tree(r, base_tree),
        _flatten_tree(r, r[ours].tree),
        _flatten_tree(r, r[theirs].tree))
    if conflicts:
        raise MergeConflictError(conflicts)
    tree_id = _build_tree(r, merged)
    sha = r.get_worktree().commit(
        message=f"Merge remote '{remote}/{branch}'".encode(),
        author=_IDENT, committer=_IDENT,
        tree=tree_id, merge_heads=[theirs], no_verify=True)
    # Index + worktree follow the merge commit (ref already moved).
    porcelain.reset(str(repo), mode="hard", treeish=sha)
    return sha.decode()


def dulwich_push(repo: Path, refspec: str, auth: tuple = None,
                 set_upstream: bool = False) -> None:
    """Push local commits to origin — never force-pushes; a rejected
    (non-fast-forward) push surfaces as GitBackendError."""
    import os as _os
    from dulwich import porcelain
    kwargs = {"set_upstream": set_upstream}
    if auth:
        kwargs["username"], kwargs["password"] = auth
    try:
        with open(_os.devnull, "wb") as sink:
            porcelain.push(str(repo), "origin", refspec.encode(),
                           outstream=sink, errstream=sink, **kwargs)
    except Exception as exc:
        raise GitBackendError(f"dulwich push failed: {exc}")


# ---------- facade ----------------------------------------------------

OPS = {
    "init": {"cli": cli_init, "dulwich": dulwich_init},
    "identity": {"cli": cli_identity, "dulwich": dulwich_identity},
    "clone": {"cli": cli_clone, "dulwich": dulwich_clone},
    "fetch": {"cli": cli_fetch, "dulwich": dulwich_fetch},
    "update": {"cli": cli_update, "dulwich": dulwich_update},
    "checkout": {"cli": cli_checkout, "dulwich": dulwich_checkout},
    "resolve": {"cli": cli_resolve, "dulwich": dulwich_resolve},
    "head": {"cli": cli_head, "dulwich": dulwich_head},
    "branch": {"cli": cli_branch, "dulwich": dulwich_branch},
    "commit_all": {"cli": cli_commit_all, "dulwich": dulwich_commit_all},
    "has_remote": {"cli": cli_has_remote, "dulwich": dulwich_has_remote},
    "remote_ref": {"cli": cli_remote_ref, "dulwich": dulwich_remote_ref},
    "merge": {"cli": cli_merge, "dulwich": dulwich_merge},
    "push": {"cli": cli_push, "dulwich": dulwich_push},
}

_BACKENDS = ("dulwich", "cli")


def backend() -> str:
    b = os.environ.get("TO_GIT_BACKEND", "dulwich").strip().lower()
    return b if b in _BACKENDS else "dulwich"


def run(op: str, *args, **kwargs):
    """Dispatch one operation to the selected backend — failures are
    GitBackendError, never silent substitution."""
    fn = OPS[op][backend()]
    return fn(*args, **kwargs)
