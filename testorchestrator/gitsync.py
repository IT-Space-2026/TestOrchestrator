"""Git persistence (§10–§11).

Two directions, both through GitPython (no custom Git transport):

1. Source bindings — clone/fetch Katalon repositories into per-binding
   worktrees for read-only indexing. Project code is never executed during
   indexing; submodules are not auto-initialized (LFS/submodule policies
   remain explicit).

2. Destination export — versioned definitions and permitted actual
   reports/evidence under `TestOrchestrator/` in a configured repository.
   Local DB stays authoritative; export state is per-execution
   (saved/pending/pushed/conflict/failed). Secrets, users, sessions,
   private keys, licences, and the live DB are NEVER exported — the writer
   whitelists artifact kinds rather than filtering after the fact.
"""

import hashlib
import json
import shutil
from pathlib import Path

import git

from . import settings as settings_mod
from .execution import Execution, GitBinding, utcnow
from .extensions import db

EXPORT_ROOT = "TestOrchestrator"


class NotConfigured(RuntimeError):
    pass


class SyncError(RuntimeError):
    pass


# ---------- source binding indexing ----------

def binding_worktree(binding_id: str) -> Path:
    from flask import current_app
    return Path(current_app.instance_path) / "bindings" / binding_id


def sync_binding(binding_id: str) -> dict:
    """Clone or fetch a source binding and record the resolved SHA.

    Indexing is read-only: file listing + .prj discovery only. No build
    steps, hooks, or project code are executed (§11)."""
    binding = db.session.get(GitBinding, binding_id)
    if binding is None:
        raise SyncError(f"unknown binding {binding_id}")
    worktree = binding_worktree(binding_id)
    try:
        if worktree.exists() and (worktree / ".git").exists():
            repo = git.Repo(worktree)
            repo.remotes.origin.fetch(binding.ref)
            repo.git.checkout(f"origin/{binding.ref}" if binding.ref
                              else "HEAD")
        else:
            worktree.parent.mkdir(parents=True, exist_ok=True)
            repo = git.Repo.clone_from(
                binding.url, worktree, branch=binding.ref or None,
                depth=1,  # shallow index; full history not required
            )
        sha = repo.head.commit.hexsha
        binding.worktree_path = str(worktree)
        binding.last_synced_sha = sha
        binding.status = "synced"
        binding.last_error = ""
        db.session.commit()
        return {"sha": sha, "worktree": str(worktree)}
    except git.GitCommandError as exc:
        binding.status = "failed"
        binding.last_error = str(exc)[:2000]
        db.session.commit()
        raise SyncError(f"git sync failed: {exc}") from exc


def list_test_cases(binding: GitBinding) -> list:
    """Repo-relative case paths under the bound project — used by the suite
    builder/import. Read-only listing, never content execution."""
    if not binding.worktree_path:
        return []
    base = Path(binding.worktree_path)
    if binding.subdirectory:
        base = base / binding.subdirectory
    cases_dir = base / "Test Cases"
    root = cases_dir if cases_dir.exists() else base
    return sorted(
        str(p.relative_to(base)) for p in root.rglob("*")
        if p.is_file() and not p.name.startswith(".")
    )


def list_test_suites(binding: GitBinding) -> list:
    if not binding.worktree_path:
        return []
    base = Path(binding.worktree_path)
    if binding.subdirectory:
        base = base / binding.subdirectory
    suites_dir = base / "Test Suites"
    if not suites_dir.exists():
        return []
    return sorted(
        str(p.relative_to(base)) for p in suites_dir.rglob("*.ts")
        if p.is_file()
    )


# ---------- destination export (§11 layout) ----------

def _export_repo() -> git.Repo:
    path = settings_mod.get("git_export_repo") or ""
    if not path:
        raise NotConfigured("git_export_repo is not configured")
    repo_path = Path(path)
    if not (repo_path / ".git").exists():
        repo_path.mkdir(parents=True, exist_ok=True)
        git.Repo.init(repo_path)
    repo = git.Repo(repo_path)
    # Repo-local identity for product commits/merges (never global config).
    with repo.config_writer() as cfg:
        if not cfg.has_option("user", "name"):
            cfg.set_value("user", "name", "TestOrchestrator")
            cfg.set_value("user", "email", "testorchestrator@localhost")
    return repo


def _commit(repo: git.Repo, message: str) -> str:
    repo.git.add(A=True)
    if repo.head.is_valid() and not repo.index.diff("HEAD"):
        return repo.head.commit.hexsha  # clean tree; nothing to write
    return repo.index.commit(message).hexsha


# Whitelist: only these artifact kinds are exportable (§11 evidence policy).
EXPORTABLE_KINDS = {"junit", "log", "screenshot", "html"}


def _serializable(value):
    import enum
    if isinstance(value, enum.Enum):
        return value.value
    return value


def export_execution(execution: Execution) -> dict:
    """Write snapshot/summary/results/reporting + permitted evidence under
    TestOrchestrator/projects/<project-id>/executions/<execution-id>/."""
    repo = _export_repo()
    base = (Path(repo.working_dir) / EXPORT_ROOT / "projects"
            / execution.project_id / "executions" / execution.id)
    base.mkdir(parents=True, exist_ok=True)

    (base / "snapshot.json").write_text(
        json.dumps(execution.snapshot(), indent=2), encoding="utf-8")
    (base / "summary.json").write_text(json.dumps({
        "execution_id": execution.id,
        "suite_id": execution.suite_id,
        "state": execution.state.value,
        "outcome": execution.outcome.value,
        "source_sha": execution.source_sha,
        "created_by": execution.created_by,
        "created_at": execution.created_at.isoformat()
        if execution.created_at else "",
        "started_at": execution.started_at.isoformat()
        if execution.started_at else "",
        "finished_at": execution.finished_at.isoformat()
        if execution.finished_at else "",
        "exit_status": execution.exit_status,
        "schema": 1,
    }, indent=2), encoding="utf-8")
    (base / "results.json").write_text(json.dumps([
        {
            "case_ref": r.case_ref, "case_name": r.case_name,
            "classname": r.classname, "status": r.status.value,
            "duration_s": r.duration_s, "iteration": r.iteration,
            "attempt": r.attempt, "unresolved": r.unresolved,
            # Failure text is evidence; secrets are never stored in results.
            "message": r.message[:2000],
        } for r in execution.results
    ], indent=2), encoding="utf-8")
    (base / "reporting.json").write_text(json.dumps({
        "reportportal_state": execution.rp_state.value,
        "artifacts": [
            {
                "rel_path": a.rel_path, "kind": a.kind,
                "sha256": a.sha256, "size": a.size,
                "rp_state": a.rp_state.value,
            } for a in execution.artifacts
        ],
        "schema": 1,
    }, indent=2), encoding="utf-8")

    report_root = Path(execution.report_dir) if execution.report_dir else None
    for artifact in execution.artifacts:
        if artifact.kind not in EXPORTABLE_KINDS:
            continue  # HAR/video/debug capture stays local (safety policy)
        if not report_root:
            continue
        src = report_root / artifact.rel_path
        if not src.is_file():
            continue
        dest_dir = base / "evidence" / artifact.kind
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / Path(artifact.rel_path).name
        shutil.copy2(src, dest)
        # Verify integrity on copy — evidence must match its checksum.
        from .results import sha256_file
        if sha256_file(dest) != artifact.sha256:
            dest.unlink(missing_ok=True)
            raise SyncError(
                f"checksum mismatch exporting {artifact.rel_path}"
            )

    sha = _commit(repo, f"execution {execution.id} evidence")
    return {"commit": sha, "path": str(base)}


def export_definitions(project) -> dict:
    """Versioned suite/plan/release definitions under the project dir."""
    repo = _export_repo()
    base = (Path(repo.working_dir) / EXPORT_ROOT / "projects" / project.id)
    for name, rows in (
        ("suites", project.suites),
        ("plans", project.plans),
        ("releases", project.releases),
    ):
        out = base / name
        out.mkdir(parents=True, exist_ok=True)
        for row in rows:
            data = {
                c.name: _serializable(getattr(row, c.name))
                for c in row.__table__.columns
            }
            (out / f"{row.id}.json").write_text(
                json.dumps(data, indent=2, default=str), encoding="utf-8")
    sha = _commit(repo, f"project {project.id} definitions")
    return {"commit": sha, "path": str(base)}


# ---------- push, conflict handling, restore ----------

def push_export() -> str:
    """Push the export repo to its configured remote (§11). Reconciles
    external edits via fetch+merge — NEVER force-push. Returns a status
    string; conflicts surface honestly rather than being overwritten."""
    repo = _export_repo()
    if not repo.remotes:
        raise NotConfigured("export repo has no remote 'origin'")
    try:
        branch = repo.active_branch.name
        remote_ref = f"origin/{branch}"
        repo.remotes.origin.fetch()
        if remote_ref in {str(r) for r in repo.remotes.origin.refs}:
            # Fast-forward/merge only — conflicts surface, never force-push.
            repo.git.merge(remote_ref, no_edit=True)
            repo.remotes.origin.push()
        else:
            # First push: establish the upstream branch.
            repo.git.push("--set-upstream", "origin", branch)
        return "pushed"
    except git.GitCommandError as exc:
        text = str(exc)
        if "CONFLICT" in text or "conflict" in text.lower():
            repo.git.merge("--abort")
            raise SyncError(
                "remote has conflicting edits — reconcile manually in "
                f"{repo.working_dir}; automatic force-push is forbidden"
            )
        raise


def restore_definitions(project_id: str) -> dict:
    """Restore suite/plan/release definitions from the export clone (A08).
    Reads exported JSON back into the DB — the inverse of
    export_definitions. Only definition objects are restored; users,
    secrets, sessions, keys and audit data are never in the export."""
    repo = _export_repo()
    base = (Path(repo.working_dir) / EXPORT_ROOT / "projects" / project_id)
    if not base.exists():
        raise NotConfigured(f"no exported definitions for {project_id}")

    from .execution import Plan, Release, Suite, SuiteEntry
    counts = {"suites": 0, "plans": 0, "releases": 0}
    for name, model, counter in (
        ("suites", Suite, "suites"),
        ("plans", Plan, "plans"),
        ("releases", Release, "releases"),
    ):
        src = base / name
        if not src.exists():
            continue
        for path in src.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            row = model.query.get(data["id"])
            if row is None:
                row = model(id=data["id"], project_id=project_id)
                db.session.add(row)
            from sqlalchemy import DateTime
            coltypes = {c.name: c.type for c in row.__table__.columns}
            for key, value in data.items():
                if key in ("id", "project_id"):
                    continue
                if hasattr(row, key):
                    try:
                        if isinstance(value, str) and isinstance(
                                coltypes.get(key), DateTime):
                            from datetime import datetime
                            value = datetime.fromisoformat(value)
                        setattr(row, key, value)
                    except (AttributeError, TypeError, ValueError):
                        pass
            counts[counter] += 1
    db.session.commit()
    return counts
