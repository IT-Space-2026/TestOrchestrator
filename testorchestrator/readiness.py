"""Shared readiness assessment — used by first-run setup, the Settings
health panel, and execution-submission revalidation.

Every required component reports a tri-state: missing / configured /
verified / failed. "Verified" means an actual check passed; a saved
value is only ever "configured". Licensed execution and end-to-end
flow are tracked as separate, always-shown lines."""
from .extensions import db
from .settings import get as _get


def _admin_set() -> bool:
    from .auth import User
    return bool(User.query.filter_by(username="Admin").first())


def _coordinator_ok() -> bool:
    # configured when provisioned; verified = settings recorded by the
    # last successful health check (setup/probe writes this key)
    return _get("dagu_coordinator_verified", "") == "ok"


def _workers() -> list:
    from .execution import Runner
    return Runner.query.filter_by(is_active=True).all()


def _runtimes() -> list:
    from .execution import RuntimeConnection
    return RuntimeConnection.query.filter_by(is_active=True).all()


def _bindings() -> list:
    from .execution import GitBinding
    return GitBinding.query.all()


def components() -> list:
    """Ordered blocking components for the readiness summary."""
    workers = _workers()
    runtimes = _runtimes()
    bindings = _bindings()
    verified_rt = [r for r in runtimes
                   if getattr(r, "verify_state", "") == "verified"]
    resolved = [b for b in bindings
                if getattr(b, "last_resolve_state", "") == "resolved"]

    # per-worker eligibility: a verified KRE must sit on a connected worker
    eligible = [r for r in verified_rt
                if any(w.id == r.runner_id for w in workers)]

    pub_url = _get("publish_repo_url", "")
    pub_ok = _get("publish_repo_verified", "") == "ok"

    return [
        {"key": "admin", "label": "Admin credential",
         "status": "verified" if _admin_set() else "missing",
         "detail": "", "link": "system.settings_home"},
        {"key": "coordinator", "label": "Orchestration (Dagu coordinator)",
         "status": ("verified" if _coordinator_ok()
                    else "configured"),
         "detail": "auto-configured at install; health check required",
         "link": "system.integrations_page"},
        {"key": "worker", "label": "Worker",
         "status": "verified" if workers else "missing",
         "detail": f"{len(workers)} connected"
                   if workers else "no workers connected",
         "link": "system.infrastructure"},
        {"key": "engine", "label": "Execution engine (KRE)",
         "status": ("verified" if eligible else
                    "configured" if verified_rt else
                    "configured" if runtimes else "missing"),
         "detail": ("verified on an eligible worker" if eligible else
                    "not verified on any connected worker"
                    if verified_rt else "no KRE installation verified"),
         "link": "system.infrastructure"},
        {"key": "source", "label": "Source binding",
         "status": ("verified" if resolved else
                    "configured" if bindings else "missing"),
         "detail": (f"{len(resolved)} resolved" if resolved else
                    "saved but not resolved through the dispatch path"
                    if bindings else "none registered"),
         "link": "portal.projects"},
        {"key": "publishing", "label": "Report publishing repository",
         "status": ("verified" if pub_ok else
                    "configured" if pub_url else "missing"),
         "detail": ("" if pub_ok else
                    "saved — write probe not passed" if pub_url else
                    "required: a local-artifact-only deployment is not "
                    "a ready full system"),
         "link": "system.reporting_page"},
        {"key": "storage", "label": "Storage",
         "status": _storage_state(),
         "detail": "instance dir + database",
         "link": "system.system_health"},
        {"key": "security", "label": "Security / listening address",
         "status": "verified" if _get("bind_verified", "") else
                   "configured",
         "detail": "", "link": "system.network"},
    ]


def _storage_state() -> str:
    from .execution import Project  # noqa: proves DB is queryable
    try:
        Project.query.first()
        return "verified"
    except Exception:
        return "failed"


def ready() -> bool:
    return all(c["status"] == "verified"
               for c in components() if c["key"] != "security")


def blockers() -> list:
    return [c for c in components()
            if c["key"] != "security" and c["status"] != "verified"]


def operational_check() -> "str | None":
    """Submission-time revalidation — names the first missing blocking
    component, or None when the system can serve an execution now."""
    if not _workers():
        return "No workers connected — dispatch impossible"
    workers = {w.id for w in _workers()}
    if not any(getattr(r, "verify_state", "") == "verified"
               and r.runner_id in workers for r in _runtimes()):
        return ("No verified KRE installation on a connected worker — "
                "engine check must pass on the target worker")
    if not _get("publish_repo_verified", ""):
        return ("Publishing repository not verified — reports cannot "
                "be published; complete the publishing setup step")
    return None
