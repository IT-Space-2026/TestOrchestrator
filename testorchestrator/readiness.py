"""Shared readiness assessment — setup, Settings health, and launch
revalidation all read the SAME evidence.

Evidence is FINGERPRINT-BOUND: a check marked verified stays verified
only while the configuration it checked is unchanged. Any relevant
change (endpoint, worker, engine path, repo URL/ref, credential,
secret contents, storage path) drops the component back to
'configured' — historical progress is kept separately in setup_state.
"""
import hashlib
from datetime import datetime, timedelta, timezone
from .extensions import db
from .settings import get as _get, set as _set

_WORKER_STALE = timedelta(seconds=90)   # heartbeat freshness window


def _fp(*parts) -> str:
    return hashlib.sha256(
        "|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def _verified_fp(key: str, current_fp: str) -> bool:
    """True only when recorded verification matches CURRENT config."""
    return _get(f"verified_fp_{key}", "") == current_fp


def record_verified(key: str, fp: str) -> None:
    """Write verification evidence bound to the config fingerprint —
    call ONLY after a real check passes."""
    _set(f"verified_fp_{key}", fp)
    db.session.commit()


def _admin_set() -> bool:
    return _get("admin_bootstrap_replaced", "") == "yes"


def _coord_fp() -> str:
    return _fp(_get("dagu_coordinator_url", ""),
               _get("dagu_credential_ref", ""))


def _coordinator_ok() -> bool:
    return _verified_fp("coordinator", _coord_fp())


def _workers() -> list:
    from .execution import Runner
    return Runner.query.filter_by(enabled=True).all()


def _eligible_workers() -> list:
    """Registered AND fresh heartbeat AND not draining — registration
    alone never implies current availability."""
    now = datetime.now(timezone.utc)
    return [r for r in _workers()
            if not getattr(r, "draining", False)
            and getattr(r, "heartbeat_at", None)
            and (r.heartbeat_at.tzinfo is None
                 and r.heartbeat_at.replace(tzinfo=timezone.utc)
                 or r.heartbeat_at) > now - _WORKER_STALE]


def _engine_fp(rt) -> str:
    return _fp(rt.executable_path, rt.runner_id,
               getattr(rt, "credential_ref", ""),
               getattr(rt, "transport", ""))


def _engine_verified(rt) -> bool:
    """detected_version is evidence only while path/worker unchanged."""
    if not getattr(rt, "detected_version", ""):
        return False
    return _verified_fp(f"engine_{rt.id}", _engine_fp(rt))


def _runtimes() -> list:
    from .execution import RuntimeConnection
    return RuntimeConnection.query.filter_by(enabled=True).all()


def _binding_fp(b) -> str:
    return _fp(b.url, b.ref, getattr(b, "subdirectory", ""),
               getattr(b, "credential_ref", ""))


def _binding_resolved(b) -> bool:
    return _verified_fp(f"binding_{b.id}", _binding_fp(b))


def _bindings() -> list:
    from .execution import GitBinding
    return GitBinding.query.all()


def _pub_fp() -> str:
    return _fp(_get("publish_repo_url", ""),
               _get("publish_repo_credential_ref", ""))


def _reporting_state() -> str:
    from . import allure_runtime
    try:
        homes = allure_runtime.detect_java_homes()
        if homes and allure_runtime._allure() is not None:
            return "verified"
        if allure_runtime._allure() is not None:
            return "configured"
        return "missing"
    except Exception:
        return "missing"


def _storage_state() -> str:
    from flask import current_app
    import os
    try:
        from .execution import Project
        Project.query.first()
        inst = current_app.instance_path
        for d in (inst, os.path.join(inst, "reports"),
                  os.path.join(inst, "artifacts")):
            os.makedirs(d, exist_ok=True)
            if not os.access(d, os.W_OK):
                return "failed"
        return "verified"
    except Exception:
        return "failed"


def components() -> list:
    workers = _workers()
    eligible_w = _eligible_workers()
    runtimes = _runtimes()
    bindings = _bindings()
    verified_rt = [r for r in runtimes if _engine_verified(r)]
    eligible = [r for r in verified_rt
                if any(w.id == r.runner_id for w in eligible_w)]
    resolved = [b for b in bindings if _binding_resolved(b)]
    pub_url = _get("publish_repo_url", "")
    pub_ok = _verified_fp("publishing", _pub_fp()) if pub_url else False
    return [
        {"key": "admin", "label": "Admin credential",
         "status": "verified" if _admin_set() else "missing",
         "detail": "bootstrap replaced" if _admin_set() else
                  "bootstrap password must be replaced",
         "link": "system.settings_home"},
        {"key": "coordinator", "label": "Orchestration (Dagu coordinator)",
         "status": "verified" if _coordinator_ok() else "configured",
         "detail": "health check bound to endpoint+credential",
         "link": "system.integrations_page"},
        {"key": "worker", "label": "Worker",
         "status": "verified" if eligible_w else
                  "configured" if workers else "missing",
         "detail": (f"{len(eligible_w)} connected (heartbeat fresh)"
                    if eligible_w else
                    f"{len(workers)} registered — no fresh heartbeat"
                    if workers else "no workers connected"),
         "link": "system.infrastructure"},
        {"key": "engine", "label": "Execution engine (KRE)",
         "status": "verified" if eligible else
                  "configured" if verified_rt or runtimes else "missing",
         "detail": ("verified on eligible worker" if eligible else
                    "not verified on a connected worker"),
         "link": "system.infrastructure"},
        {"key": "source", "label": "Source binding",
         "status": "verified" if resolved else
                  "configured" if bindings else "missing",
         "detail": f"{len(resolved)} resolved" if resolved else
                  "not resolved through the dispatch path",
         "link": "portal.projects"},
        {"key": "publishing", "label": "Report publishing repository",
         "status": "verified" if pub_ok else
                  "configured" if pub_url else "missing",
         "detail": "write probe bound to repo+credential" if pub_url
                  else "required — local-only is not a ready system",
         "link": "system.reporting_page"},
        {"key": "reporting", "label": "Reporting (Allure + JRE)",
         "status": _reporting_state(),
         "detail": "bundled JRE + report pipeline",
         "link": "system.reporting_page"},
        {"key": "storage", "label": "Storage",
         "status": _storage_state(),
         "detail": "DB + instance/report/artifact dirs writable",
         "link": "system.system_health"},
        {"key": "security", "label": "Security / listening address",
         "status": "verified" if _get("bind_verified", "")
                   else "configured",
         "detail": "", "link": "system.network"},
    ]


def ready() -> bool:
    return all(c["status"] == "verified"
               for c in components() if c["key"] != "security")


def blockers() -> list:
    return [c for c in components()
            if c["key"] != "security" and c["status"] != "verified"]


def operational_check() -> "str | None":
    """Submission revalidation — names the first missing blocking
    component, or None. Uses CURRENT evidence only."""
    if not _eligible_workers():
        return "No workers connected — dispatch impossible"
    wids = {w.id for w in _eligible_workers()}
    if not any(_engine_verified(r) and r.runner_id in wids
               for r in _runtimes()):
        return ("No verified KRE installation on a connected worker — "
                "engine check must pass on the target worker")
    if not _verified_fp("publishing", _pub_fp()):
        return ("Publishing repository not verified — reports cannot "
                "be published; complete the publishing setup step")
    return None
