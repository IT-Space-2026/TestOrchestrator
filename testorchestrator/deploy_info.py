"""Deployed-revision diagnostics.

Acceptance evidence must name the exact code under test — a git sha
alone is not enough when two source trees exist on one host (the
stale-tree trap this diagnostic exists to catch: processes imported
``<root>/testorchestrator/`` while deploys wrote
``<root>/payload/testorchestrator/`` — every "deploy" was a no-op for
the running services).

``deployed_revision()`` reports the resolved import path of this
module plus its git state when the tree is a checkout. It is stamped
into every execution snapshot so each evidence row carries the exact
deployed source state it ran on.
"""

import os
import subprocess
from pathlib import Path

_cache: dict | None = None


def _git(root: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def deployed_revision() -> dict:
    """Identity of the code this process imported, best-effort.

    ``module_path`` is the resolved directory of the running package —
    the value that matters for stale-tree detection. ``git_head`` /
    ``git_branch`` / ``git_dirty`` are empty for packaged payload trees
    (no .git) where ``version_file``/``build_stamp`` apply instead."""
    global _cache
    if _cache is not None:
        return dict(_cache)
    import testorchestrator
    module_dir = Path(testorchestrator.__file__).resolve().parent
    info = {
        "module_path": str(module_dir),
        "git_head": _git(module_dir, "rev-parse", "HEAD"),
        "git_branch": _git(module_dir, "rev-parse",
                           "--abbrev-ref", "HEAD"),
        "git_dirty": "",
        "version_file": "",
        "build_stamp": "",
    }
    if info["git_head"]:
        info["git_dirty"] = _git(module_dir, "status",
                                 "--porcelain", "-uno") and "yes" or "no"
    vf = module_dir / "VERSION"
    if vf.is_file():
        info["version_file"] = vf.read_text().strip()
    bs = module_dir.parent / "BUILD"
    if bs.is_file():
        info["build_stamp"] = bs.read_text().strip()[:200]
    _cache = dict(info)
    return info


def payload_tree(root: Path) -> dict | None:
    """The payload source tree under an install root, when present —
    for dev/synced layouts where the running code may differ from the
    tree deploys write to."""
    p = Path(root) / "payload" / "testorchestrator"
    if not p.is_dir():
        return None
    return {"module_path": str(p.resolve()),
            "git_head": _git(p, "rev-parse", "HEAD")}


def diagnose(root: Path = None) -> dict:
    """Compare the running import against the payload tree. ``mismatch``
    is True when both exist and differ — the exact stale-tree state
    that silently invalidated earlier deployments."""
    running = deployed_revision()
    payload = payload_tree(root or Path.cwd())
    out = {"running": running, "payload": payload, "mismatch": False,
           "warnings": []}
    if payload is None:
        return out
    if os.path.realpath(running["module_path"]) != \
            os.path.realpath(payload["module_path"]):
        out["mismatch"] = True
        out["warnings"].append(
            f"running code is {running['module_path']} but deploys "
            f"target {payload['module_path']} — changes to the "
            "payload tree do not reach this process")
    elif running["git_head"] and payload["git_head"] and \
            running["git_head"] != payload["git_head"]:
        out["mismatch"] = True
        out["warnings"].append(
            f"running tree at {running['git_head'][:12]} but payload "
            f"tree at {payload['git_head'][:12]} — stale deployment")
    return out
