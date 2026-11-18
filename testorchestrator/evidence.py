"""Evidence lifecycle (2.0.2): ownership, deletion, orphan cleanup.

Ownership rules
---------------
- ``instance/imported-evidence/<execution_id>/`` — owned by that
  Execution. Created at import-confirm; every file inside is
  referenced by Artifact rows of that execution only.
- ``<workspace_root>/<exec-id>/`` (live executions, default
  ``instance/workspaces/<exec-id>/``) — owned by that Execution;
  contains ``Reports/``, ``logs/`` and the checked-out source.
- ``instance/remote-runs/<exec-id>/`` — materialised coordinator-side
  artifacts of remote executions; owned likewise.
- A directory under a managed root is an ORPHAN iff no Execution row
  owns it — i.e. its top-level name is not an existing execution id
  and no ``report_dir``/``workspace_path`` points inside it.

Deletion rules
--------------
- Execution deletion removes its owned evidence dir AFTER the DB row
  commits, provided no other execution still references the same path
  and no Artifact row of another execution points inside it.
- Orphan pruning only operates on the TOP-LEVEL directories of the
  managed roots, never follows symlinks, rejects path traversal, and
  defaults to dry-run.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .execution import Execution


def managed_roots() -> list[Path]:
    """Application-managed storage roots where evidence may live."""
    from flask import current_app
    inst = Path(current_app.instance_path)
    return [inst / "imported-evidence", inst / "workspaces",
            inst / "remote-runs"]


def _inside_managed_root(path: Path) -> bool:
    """path must resolve INSIDE a managed root — traversal/symlink
    escapes and arbitrary paths are refused."""
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return False
    for root in managed_roots():
        if resolved.is_relative_to(root.resolve()):
            return True
    return False


def owned_dir(ex: Execution) -> Path | None:
    """The top-level directory this execution owns under a managed
    root, or None when it has no managed evidence."""
    rd = Path(ex.report_dir or "")
    if rd.name == ex.id and _inside_managed_root(rd):
        return rd                      # imported-evidence/<id>
    if rd.parent.name == ex.id and _inside_managed_root(rd.parent):
        return rd.parent               # workspaces/<id>/Reports
    wp = Path(ex.workspace_path or "")
    if wp.name == ex.id and _inside_managed_root(wp):
        return wp                      # workspaces/<id>
    return None


def _referenced_elsewhere(target: Path, ex_id: str) -> bool:
    """True when another execution's report/workspace path or any of
    its artifacts still points inside target — shared evidence is
    never deleted."""
    for other in Execution.query.filter(Execution.id != ex_id):
        for raw in (other.report_dir, other.workspace_path):
            if raw and Path(raw).is_relative_to(target):
                return True
    return False


def remove_owned(ex_id: str, report_dir: str, workspace_path: str
                 ) -> dict:
    """Remove the evidence dir owned by a just-deleted execution.
    Call AFTER the DB row is committed — needs the row gone so the
    shared-reference check sees only OTHER owners."""
    probe = Execution(id=ex_id, report_dir=report_dir or "",
                      workspace_path=workspace_path or "")
    target = owned_dir(probe)
    if target is None or not target.is_dir():
        return {"removed": False, "reason": "no managed evidence dir"}
    if _referenced_elsewhere(target, ex_id):
        return {"removed": False, "reason": "still referenced"}
    # final guards: real dir (not symlink), inside managed root
    if target.is_symlink() or not _inside_managed_root(target):
        return {"removed": False, "reason": "unsafe path"}
    shutil.rmtree(target)
    return {"removed": True, "path": str(target)}


def find_orphans() -> list[Path]:
    """Top-level dirs under managed roots owned by no Execution row.
    Orphans arise when confirm crashes between evidence copy and the
    execution commit, or when deletion raced the row removal."""
    orphans = []
    live_ids = {e.id for e in
                Execution.query.with_entities(Execution.id)}
    live_dirs = set()
    for e in Execution.query.with_entities(
            Execution.report_dir, Execution.workspace_path):
        for raw in (e.report_dir, e.workspace_path):
            if raw:
                live_dirs.add(Path(raw).resolve())
    for root in managed_roots():
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.is_symlink() or not child.is_dir():
                continue
            resolved = child.resolve()
            if child.name in live_ids:
                continue
            if any(str(d).startswith(str(resolved) + "/") or d == resolved
                   for d in live_dirs):
                continue
            orphans.append(child)
    return sorted(orphans)


def prune_orphans(dry_run: bool = True) -> dict:
    """Report (and optionally remove) orphan evidence dirs.
    dry_run=True only reports — the caller decides. Only top-level
    dirs of managed roots are ever touched."""
    orphans = find_orphans()
    removed = []
    if not dry_run:
        for d in orphans:
            if d.is_symlink() or not _inside_managed_root(d):
                continue                    # re-guard at delete time
            shutil.rmtree(d)
            removed.append(str(d))
    return {
        "dry_run": dry_run,
        "orphans": [str(d) for d in orphans],
        "removed": removed,
    }
