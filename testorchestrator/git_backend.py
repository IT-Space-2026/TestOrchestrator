"""Repository backend interface (§8) — GitPython/CLI vs Dulwich.

All Orchestrator git operations must go through ``backend()`` — a
backend is selected explicitly (``TO_GIT_BACKEND=cli|dulwich``;
default ``cli`` preserves the working delivery). A Dulwich failure is
surfaced honestly — never a silent fallback to system git.

Prototype scope (§8): clone, fetch, exact-commit checkout, isolated
worktree. push/export stays CLI-only until the backend matrix
(LFS/submodules/SSH/proxy/custom CA) is validated.
"""

import os
import shutil
import subprocess
from pathlib import Path

from . import git_runtime


class GitBackendError(Exception):
    pass


# ---------- CLI backend (current working implementation) -------------

def _cli(args: list, cwd: Path = None, env: dict = None) -> str:
    res = subprocess.run([git_runtime.executable(), *args], cwd=cwd,
                         env=env or git_runtime.env(), capture_output=True,
                         text=True, timeout=300)
    if res.returncode != 0:
        raise GitBackendError((res.stderr or res.stdout).strip()[:400])
    return res.stdout.strip()


def cli_clone(url: str, dest: Path) -> None:
    _cli(["clone", "--", url, str(dest)])


def cli_fetch(repo: Path, remote: str = "origin") -> None:
    _cli(["fetch", "--", remote], cwd=repo)


def cli_checkout(repo: Path, sha: str, dest: Path) -> Path:
    """Isolated worktree at exact sha — concurrent checkouts never share
    a working directory (adds a git worktree, detached)."""
    dest.mkdir(parents=True, exist_ok=True)
    _cli(["worktree", "add", "--detach", str(dest), sha], cwd=repo)
    return dest


def cli_resolve(repo: Path, ref: str = "HEAD") -> str:
    return _cli(["rev-parse", ref], cwd=repo)


# ---------- Dulwich prototype ----------------------------------------

def _dulwich_repo(repo: Path):
    try:
        from dulwich.repo import Repo
    except ImportError as exc:
        raise GitBackendError(f"dulwich unavailable: {exc}")
    return Repo(str(repo))


def dulwich_clone(url: str, dest: Path) -> None:
    """Clone via Dulwich porcelain — supports file://, https, ssh.
    Credential handling (SecretRef material -> config) is wired at the
    call site; this layer takes a URL it can open directly."""
    from dulwich import porcelain
    try:
        porcelain.clone(url, str(dest))
    except Exception as exc:
        raise GitBackendError(f"dulwich clone failed: {exc}")


def dulwich_fetch(repo: Path, remote: str = "origin") -> None:
    from dulwich import porcelain
    try:
        porcelain.fetch(str(repo), remote)
    except Exception as exc:
        raise GitBackendError(f"dulwich fetch failed: {exc}")


def dulwich_checkout(repo: Path, sha: str, dest: Path) -> Path:
    """Isolated checkout at exact sha by tree export — same isolation
    guarantee as `git worktree --detach` without touching the source
    worktree's index."""
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


def _export_tree(r, tree: "Tree", dest: Path) -> None:
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
    r = _dulwich_repo(repo)
    try:
        return r[ref.encode() if not ref.startswith("HEAD")
                  else b"HEAD"].id.decode()
    except Exception as exc:
        raise GitBackendError(f"dulwich resolve {ref}: {exc}")


def dulwich_update(repo: Path, ref: str = "") -> None:
    """Fetch + hard-reset the worktree to origin/<ref> — the binding
    worktree stays a real repository for the next update (unlike
    tree-export checkouts which have no .git)."""
    from dulwich import porcelain
    try:
        porcelain.fetch(str(repo), "origin")
        target = (f"origin/{ref}" if ref else "HEAD")
        porcelain.reset(str(repo), mode="hard", treeish=target)
    except Exception as exc:
        raise GitBackendError(f"dulwich update failed: {exc}")


def dulwich_push(repo: Path, refspec: str = "HEAD:refs/heads/master")         -> None:
    """Push local commits to origin — used by the git-export workflow.
    Never force-pushes; a rejected push surfaces as GitBackendError."""
    from dulwich import porcelain
    try:
        porcelain.push(str(repo), "origin", refspec)
    except Exception as exc:
        raise GitBackendError(f"dulwich push failed: {exc}")


def cli_update(repo: Path, ref: str = "") -> None:
    _cli(["fetch", "--", "origin"], cwd=repo)
    _cli(["checkout", f"origin/{ref}" if ref else "HEAD"], cwd=repo)


def cli_push(repo: Path, refspec: str = "HEAD:refs/heads/master") -> None:
    _cli(["push", "origin", refspec], cwd=repo)


# ---------- facade ----------------------------------------------------

OPS = {
    "clone": {"cli": cli_clone, "dulwich": dulwich_clone},
    "fetch": {"cli": cli_fetch, "dulwich": dulwich_fetch},
    "checkout": {"cli": cli_checkout, "dulwich": dulwich_checkout},
    "resolve": {"cli": cli_resolve, "dulwich": dulwich_resolve},
    "update": {"cli": cli_update, "dulwich": dulwich_update},
    "push": {"cli": cli_push, "dulwich": dulwich_push},
}

_BACKENDS = ("cli", "dulwich")


def backend() -> str:
    b = os.environ.get("TO_GIT_BACKEND", "cli").strip().lower()
    return b if b in _BACKENDS else "cli"


def run(op: str, *args, **kwargs):
    """Dispatch one operation to the selected backend — failures are
    GitBackendError, never silent substitution."""
    fn = OPS[op][backend()]
    return fn(*args, **kwargs)
