"""Git persistence (§10–§11).

Two directions, both through the git_backend facade (no custom Git
transport, no GitPython):

1. Source bindings — clone/fetch Katalon repositories into per-binding
   worktrees for read-only indexing. The worktree keeps its .git so
   updates stay incremental; project code is never executed during
   indexing and submodules are not auto-initialized (LFS/submodule
   policies remain explicit).

2. Destination export — versioned definitions and permitted actual
   reports/evidence under `TestOrchestrator/` in a configured repository.
   Local DB stays authoritative; export state is per-execution
   (saved/pending/pushed/conflict/failed). Secrets, users, sessions,
   private keys, licences, and the live DB are NEVER exported — the writer
   whitelists artifact kinds rather than filtering after the fact.
"""

import json
import shutil
from pathlib import Path

from . import git_backend
from . import settings as settings_mod
from .execution import Execution, GitBinding, utcnow
from .extensions import db

EXPORT_ROOT = "TestOrchestrator"


class NotConfigured(RuntimeError):
    pass


class SyncError(RuntimeError):
    pass


# ---------- credentials (§12) -----------------------------------------

def _binding_auth(binding: GitBinding):
    """Resolve the binding's SecretRef to an https (user, pass) pair.

    Secret format: 'user:password' splits at the first colon; a bare
    token maps to (token, '') — the PAT-as-username convention. SSH URLs
    rely on the host ssh/agent (dulwich subprocess ssh, or system ssh via
    the cli backend) — no password is ever placed on the URL."""
    if not binding.credential_ref:
        return None
    url = (binding.url or "").lower()
    if not url.startswith("http://") and not url.startswith("https://"):
        return None
    try:
        from flask import current_app
        from .secrets_store import SecretError, read_secret
        secret = read_secret(current_app._get_current_object(),
                             binding.credential_ref)
    except SecretError as exc:
        raise SyncError(f"credential {binding.credential_ref!r}: {exc}")
    if ":" in secret:
        return tuple(secret.split(":", 1))
    return (secret, "")


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
        auth = _binding_auth(binding)
        if worktree.exists() and (worktree / ".git").exists():
            # Selected backend update — failure surfaces honestly, never
            # a silent fallback to the other backend (§8).
            git_backend.run("update", worktree, binding.ref or "",
                            auth=auth)
        else:
            worktree.parent.mkdir(parents=True, exist_ok=True)
            git_backend.run("clone", binding.url, worktree,
                            branch=binding.ref or None, depth=1,
                            auth=auth)
        sha = git_backend.run("resolve", worktree, "HEAD")
        binding.worktree_path = str(worktree)
        binding.last_synced_sha = sha
        binding.status = "synced"
        binding.last_error = ""
        db.session.commit()
        discover_project(binding)          # non-fatal to sync status
        return {"sha": sha, "worktree": str(worktree)}
    except Exception as exc:
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

def _export_repo() -> Path:
    """The export repository path — created on first use, .git always
    preserved (commits/merges/pushes need real history). Repo-local
    identity is configured for product commits (never global config)."""
    path = settings_mod.get("git_export_repo") or ""
    if not path:
        raise NotConfigured("git_export_repo is not configured")
    repo_path = Path(path)
    git_backend.run("init", repo_path)
    git_backend.run("identity", repo_path,
                    git_backend.IDENTITY_NAME, git_backend.IDENTITY_EMAIL)
    return repo_path


def _commit(repo_path: Path, message: str) -> str:
    return git_backend.run("commit_all", repo_path, message)


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
    repo_path = _export_repo()
    base = (repo_path / EXPORT_ROOT / "projects"
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
        "artifacts": [
            {
                "rel_path": a.rel_path, "kind": a.kind,
                "sha256": a.sha256, "size": a.size,
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

    sha = _commit(repo_path, f"execution {execution.id} evidence")
    return {"commit": sha, "path": str(base)}


def export_definitions(project) -> dict:
    """Versioned suite/plan/release definitions under the project dir."""
    repo_path = _export_repo()
    base = repo_path / EXPORT_ROOT / "projects" / project.id
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
    sha = _commit(repo_path, f"project {project.id} definitions")
    return {"commit": sha, "path": str(base)}


# ---------- push, conflict handling, restore ----------

def push_export(credential_ref: str = "") -> str:
    """Push the export repo to its configured remote (§11). Reconciles
    external edits via fetch+merge — NEVER force-push. Returns a status
    string; conflicts surface honestly rather than being overwritten."""
    repo_path = _export_repo()
    if not git_backend.run("has_remote", repo_path):
        raise NotConfigured("export repo has no remote 'origin'")
    auth = _remote_auth(credential_ref)
    branch = git_backend.run("branch", repo_path)
    refspec = f"HEAD:refs/heads/{branch}"
    git_backend.run("fetch", repo_path, auth=auth)
    if git_backend.run("remote_ref", repo_path, branch):
        # Fast-forward/merge only — conflicts surface, never force-push.
        try:
            git_backend.run("merge", repo_path, branch)
        except git_backend.MergeConflictError as exc:
            raise SyncError(
                "remote has conflicting edits — reconcile manually in "
                f"{repo_path}; automatic force-push is forbidden "
                f"({exc})") from exc
        git_backend.run("push", repo_path, refspec, auth=auth)
    else:
        # First push: establish the upstream branch.
        git_backend.run("push", repo_path, refspec, auth=auth,
                        set_upstream=True)
    return "pushed"


def _remote_auth(credential_ref: str = ""):
    """Export-remote credential: the git_export_credential setting holds a
    SecretRef name; https remotes get (user, pass), ssh relies on the
    host ssh/agent — never embedded in URLs or logs."""
    name = credential_ref or settings_mod.get("git_export_credential", "")
    if not name:
        return None
    try:
        from flask import current_app
        from .secrets_store import SecretError, read_secret
        secret = read_secret(current_app._get_current_object(), name)
    except SecretError as exc:
        raise SyncError(f"credential {name!r}: {exc}")
    if ":" in secret:
        return tuple(secret.split(":", 1))
    return (secret, "")


def restore_definitions(project_id: str) -> dict:
    """Restore suite/plan/release definitions from the export clone (A08).
    Reads exported JSON back into the DB — the inverse of
    export_definitions. Only definition objects are restored; users,
    secrets, sessions, keys and audit data are never in the export."""
    repo_path = _export_repo()
    base = repo_path / EXPORT_ROOT / "projects" / project_id
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
            row = db.session.get(model, data["id"])
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


# ---------- Katalon project discovery (read-only) ----------

_SKIP_DIRS = {".git", ".cache", "Keywords", "Scripts", "Libs", "bin",
              "build", "Reports", "__pycache__"}
_MAX_FILE = 512 * 1024                       # bound definition sizes


def _xml_root(path):
    """Parse just the root tag — bounded, no external entities."""
    import xml.etree.ElementTree as ET
    try:
        if path.stat().st_size > _MAX_FILE:
            return None, "oversized"
        for _ev, el in ET.iterparse(str(path), events=("start",)):
            return el.tag.split("}")[-1], None
    except Exception as exc:
        return None, f"malformed: {type(exc).__name__}"
    return None, "malformed: empty"


def discover_project(binding) -> dict:
    """Typed inventory discovery under the selected Katalon project
    root — .prj root detection, .tc cases, suites vs collections by
    XML root tag, profile names. Read-only; never executes repo code.
    Replaces the current inventory ATOMICALLY per SHA; a failure keeps
    the last good set (stale, not destroyed)."""
    from .execution import DiscoveredItem
    import xml.etree.ElementTree as ET
    out = {"cases": 0, "suites": 0, "collections": 0, "profiles": 0,
           "warnings": []}
    sha = binding.last_synced_sha or ""
    try:
        base = Path(binding.worktree_path or "")
        if binding.subdirectory:
            base = base / binding.subdirectory
        if not base.is_dir():
            raise SyncError("bound worktree/subdirectory missing")
        # .prj detection within the bound scope — bounded walk, no
        # symlink escapes outside the checkout.
        prjs = [p for p in base.rglob("*.prj")
                if not any(part.startswith(".") or
                           part in _SKIP_DIRS for part in p.parts)]
        prjs = [p for p in prjs if p.resolve().is_relative_to(
            base.resolve())]                      # containment
        if not prjs:
            raise SyncError("no Katalon .prj file found under the "
                            "bound path — not a Katalon project root")
        if len(prjs) > 1:
            names = [str(p.relative_to(base)) for p in prjs]
            raise SyncError("multiple .prj files — select one "
                            "explicitly: " + ", ".join(names[:5]))
        root = prjs[0].parent
        items = []
        # Cases: .tc under Test Cases/ only (never groovy/scripts)
        tc_dir = root / "Test Cases"
        base_r = base.resolve()
        for p in sorted(tc_dir.rglob("*.tc")) if tc_dir.exists() else []:
            if not p.resolve().is_relative_to(base_r):
                out["warnings"].append(
                    f"{p.relative_to(base)}: symlink escape skipped")
                continue
            rel = str(p.relative_to(root))
            items.append(DiscoveredItem(
                binding_id=binding.id, project_id=binding.project_id,
                sha=sha, kind="case", path=rel,
                name=p.stem, parent=str(p.parent.relative_to(root))))
        # Suites vs collections: content root tag, not filename.
        ts_dir = root / "Test Suites"
        for p in sorted(ts_dir.rglob("*.ts")) if ts_dir.exists() else []:
            if not p.resolve().is_relative_to(base_r):
                out["warnings"].append(
                    f"{p.relative_to(base)}: symlink escape skipped")
                continue
            rel = str(p.relative_to(root))
            tag, warn = _xml_root(p)
            if warn:
                out["warnings"].append(f"{rel}: {warn}")
            kind = ("collection" if tag == "TestSuiteCollectionEntity"
                    else "suite")
            dynamic = False
            members = []
            if tag:
                try:
                    r = ET.parse(str(p)).getroot()
                    if kind == "suite":
                        members = [t.text.strip() for t in
                                   r.findall(".//testCaseLink/testCaseId")
                                   if t.text]
                        # Explicit metadata first (authoritative);
                        # name-match is an uncertain heuristic label.
                        explicit = bool(r.findall(".//testCaseRunType"))
                        name_hint = "dynamic" in (
                            r.findtext("name") or "").lower()
                        dynamic = explicit
                        if name_hint and not explicit:
                            warn = (warn or "") + \
                                " name suggests dynamic (unverified)"
                    else:
                        members = [t.text.strip() for t in
                                   r.findall(".//TestSuiteRunConfiguration"
                                             "/testSuiteEntity") if t.text]
                except Exception:
                    warn = (warn or "") + " members unparseable"
            items.append(DiscoveredItem(
                binding_id=binding.id, project_id=binding.project_id,
                sha=sha, kind=kind, path=rel, name=p.stem,
                parent=str(p.parent.relative_to(root)),
                dynamic=dynamic,
                members_json=json.dumps(members),
                warnings=(warn or "")))
            out["suites" if kind == "suite" else "collections"] += 1
        # Profiles: names only — never parse/render values.
        prof_dir = root / "Profiles"
        for p in sorted(prof_dir.rglob("*.glbl")) if prof_dir.exists() else []:
            if not p.resolve().is_relative_to(base_r):
                out["warnings"].append(
                    f"{p.relative_to(base)}: symlink escape skipped")
                continue
            items.append(DiscoveredItem(
                binding_id=binding.id, project_id=binding.project_id,
                sha=sha, kind="profile",
                path=str(p.relative_to(root)), name=p.stem))
            out["profiles"] += 1
        out["cases"] = sum(1 for i in items if i.kind == "case")
        # Atomic replace: delete prior set + insert new in one commit.
        DiscoveredItem.query.filter_by(binding_id=binding.id).delete()
        db.session.add_all(items)
        binding.discovered_sha = sha
        binding.discovery_status = ("ok" if items else "none")
        binding.discovery_error = ""
        binding.discovery_at = utcnow()
        binding.project_root = str(root.relative_to(base)) or "."
        db.session.commit()
        return out
    except Exception as exc:
        # Failed discovery: keep prior inventory, mark stale — never
        # erase the last good set.
        binding.discovery_status = "failed"
        binding.discovery_error = str(exc)[:512]
        db.session.commit()
        out["error"] = str(exc)[:512]
        return out
