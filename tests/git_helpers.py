"""Git fixture helpers via the app backend (dulwich) — no git CLI.

Tests that need a real remote/working-repo pair use these so the suite
runs with no git executable on PATH — matching the production
TO_GIT_BACKEND=dulwich default."""
from pathlib import Path

from dulwich import porcelain
from dulwich.repo import Repo

from testorchestrator import git_backend

_IDENT = git_backend._IDENT
_dulwich_repo = git_backend._dulwich_repo

IDENT = ("fixture", "fixture@test")


def bare_repo(path: Path) -> Path:
    """A bare origin repo (receives pushes)."""
    path.mkdir(parents=True, exist_ok=True)
    porcelain.init(str(path), bare=True)
    return path


def clone(url: str, dest: Path) -> Path:
    git_backend.run("clone", url, dest)
    git_backend.run("identity", dest, *IDENT)
    return dest


def seed_repo(url: str, wc: Path, files: dict, msg: str = "seed",
              branch: str = "master") -> str:
    """Clone url into wc, write files, commit-all, push to branch."""
    clone(url, wc)
    for rel, content in files.items():
        f = wc / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    sha = git_backend.run("commit_all", wc, msg)
    git_backend.run("push", wc, branch)
    return sha


def repo_show(repo: Path, ref: str, path: str) -> str | None:
    """Contents of `path` at `ref` in a (possibly bare) repo."""
    r = Repo(str(repo))
    obj = r[git_backend.run("resolve", repo, ref).encode()]
    tree = r[obj.tree] if hasattr(obj, "tree") else obj
    for part in path.split("/"):
        _mode, oid = tree[part.encode()]
        tree = r[oid]
    return tree.data.decode() if hasattr(tree, "data") else None


def repo_ls(repo: Path, ref: str, prefix: str = "") -> list:
    """`git ls-tree <ref> <path>` — returns `<mode> <type> <oid>\t<path>`
    for the object AT that path (a tree prints itself, not children).
    Empty output for empty or missing paths."""
    r = Repo(str(repo))
    sha = git_backend.run("resolve", repo, ref).encode()
    tree = r[r[sha].tree]
    if not prefix:
        raise RuntimeError("empty pathspec")  # real git: rc 128
    entry = tree
    try:
        for part in prefix.strip("/").split("/"):
            m, oid = entry[part.encode()]
            entry = r[oid]
    except KeyError:
        return []
    kind = "tree" if m & 0o40000 else "blob"
    return [f"{m:06o} {kind} {oid.decode()}\t{prefix}"]

from types import SimpleNamespace


def cli(*args, check=False):
    """`git <args>` emulation via the app backend/dulwich — same
    (returncode, stdout, stderr) shape as subprocess for test asserts.
    Supports: init [--bare], clone, -C <dir> config|add|commit|push|
    show|rev-parse|ls-tree."""
    try:
        out = _run_args(list(args))
        return SimpleNamespace(returncode=0, stdout=out or "", stderr="")
    except Exception as exc:
        if check:
            raise
        return SimpleNamespace(returncode=128, stdout="", stderr=str(exc))


def _run_args(args):
    cwd = None
    if args[:2] == ["-C", args[1] if len(args) > 1 else ""]:
        cwd = Path(args[1]); args = args[2:]
    cmd, rest = args[0], args[1:]
    if cmd == "init":
        bare = "--bare" in rest
        paths = [a for a in rest if not a.startswith("-")]
        path = Path(paths[-1]) if paths else cwd
        porcelain.init(str(path), bare=bare)
    elif cmd == "clone":
        dest = Path(rest[-1]); git_backend.run("clone", rest[-2], dest)
        git_backend.run("identity", dest, *IDENT)
    elif cmd == "config":  # user.email/user.name only in fixtures
        git_backend.run("identity", cwd, *IDENT)
    elif cmd == "add":
        pass                            # commit_all stages everything
    elif cmd == "commit":
        msg = rest[rest.index("-m") + 1]
        if "--allow-empty" in rest:
            r = _dulwich_repo(cwd)
            return r.get_worktree().commit(
                message=msg.encode(), author=_IDENT, committer=_IDENT,
                no_verify=True).decode()
        return git_backend.run("commit_all", cwd, msg)
    elif cmd == "push":
        git_backend.run("push", cwd, rest[-1])
    elif cmd == "pull":
        git_backend.run("fetch", cwd)
        br = git_backend.run("branch", cwd)
        if git_backend.run("remote_ref", cwd, br):
            git_backend.run("merge", cwd, br)
    elif cmd == "show":
        ref, _, path = rest[0].partition(":")
        return repo_show(cwd, ref, path) or ""
    elif cmd == "rev-parse":
        return git_backend.run("resolve", cwd, rest[0]) + "\n"
    elif cmd == "ls-tree":
        ref, prefix = rest[0], (rest[1] if len(rest) > 1 else "")
        return "\n".join(repo_ls(cwd, ref, prefix))
    else:
        raise RuntimeError(f"unsupported git shim cmd: {cmd}")
    return ""
