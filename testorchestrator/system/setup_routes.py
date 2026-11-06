"""First-run setup wizard — same validators and component checks as
Settings; progress persisted so a failed step can be resumed."""
from flask import (Blueprint, render_template, request, redirect,
                   url_for, flash, current_app)
from flask_login import current_user
from ..security import role_required
from ..models import Role
from ..settings import get as _get, set as _set
from ..extensions import db
from .. import readiness

setup_bp = Blueprint("setup", __name__, url_prefix="/system/setup")

STEPS = ["admin", "coordinator", "worker", "engine", "source",
         "publishing", "security", "summary"]


def _progress() -> dict:
    import json
    try:
        return json.loads(_get("setup_state", "{}"))
    except Exception:
        return {}


def _save_progress(state: dict) -> None:
    import json
    _set("setup_state", json.dumps(state))
    db.session.commit()


@setup_bp.route("/")
@role_required(Role.ADMIN)
def index():
    comps = readiness.components()
    state = _progress()
    first_open = next(
        (c["key"] for c in comps
         if c["key"] not in ("security",) and
         c["status"] != "verified" and not state.get(c["key"])), None)
    return render_template("system/setup.html", components=comps,
                           ready=readiness.ready(),
                           state=state, first_open=first_open)


STEP_DEFS = {
 "admin": {"purpose": "Replace the bootstrap credential so the portal "
           "is not left with a shipped default password.",
           "inputs": ["new admin password (>= 12 chars)"],
           "configure_label": "the Users page",
           "verify_note": "Passes when the bootstrap password has been "
           "replaced — existence alone never counts.",
           "auto": ""},
 "coordinator": {"purpose": "The bundled Dagu coordinator runs every "
           "dispatch — required, not optional.",
           "inputs": [],
           "configure_label": "Infrastructure & components",
           "verify_note": "Health-checks the coordinator endpoint "
           "bound to the current endpoint+credential values.",
           "auto": "endpoint, ports, worker token (written at install)"},
 "worker": {"purpose": "At least one Worker must be connected for any "
           "execution — remote machines get the Dagu worker download.",
           "inputs": ["worker install + registration (remote) or local "
                      "worker"],
           "configure_label": "Workers",
           "verify_note": "Passes only with a fresh heartbeat on a "
           "non-draining worker — registration alone never counts.",
           "auto": "worker download links + coordinator URL"},
 "engine": {"purpose": "KRE is the execution engine used by a worker — "
           "external and separately licensed.",
           "inputs": ["KRE executable path on the target worker"],
           "configure_label": "KRE installations",
           "verify_note": "Presence+version probe ON an eligible "
           "worker. License is never inferred — stays 'not tested'.",
           "auto": ""},
 "source": {"purpose": "At least one project source binding must "
           "resolve through the real dispatch path.",
           "inputs": ["repository URL", "ref", "credentials "
                      "(per selected auth method)"],
           "configure_label": "Projects",
           "verify_note": "Clone + ref resolution via the backend a "
           "worker uses — a saved URL is not verification.",
           "auto": ""},
 "publishing": {"purpose": "Report publishing repository — REQUIRED. A "
           "local-artifact-only install is not a ready full system.",
           "inputs": ["publishing repository address", "credentials"],
           "configure_label": "Reporting settings",
           "verify_note": "Explicit write probe: one marker ref "
           "created, read back, deleted. Nothing else is touched.",
           "auto": "local artifact store (storage/recovery only)"},
 "security": {"purpose": "Listening address and TLS for the portal.",
           "inputs": ["bind address", "TLS cert (external exposure "
                      "only)"],
           "configure_label": "Network & certificates",
           "verify_note": "Handshake + reachability on the bound "
           "address. Loopback development is allowed.",
           "auto": "loopback default"},
}


@setup_bp.route("/step/<key>")
@role_required(Role.ADMIN)
def step(key):
    order = [s for s in STEPS if s != "summary"]
    if key not in order:
        abort(404)
    comp = next(c for c in readiness.components() if c["key"] == key)
    i = order.index(key)
    return render_template(
        "system/setup_step.html",
        step=dict(key=key, label=comp["label"], **STEP_DEFS[key]),
        comp=comp, idx=i + 1,
        prev_step=order[i - 1] if i else None,
        next_step=order[i + 1] if i + 1 < len(order) else None)


@setup_bp.route("/verify/<key>", methods=["POST"])
@role_required(Role.ADMIN)
def verify(key):
    """Run the component's real check; outcome is persisted, never
    assumed from a saved value."""
    result = _run_check(key)
    state = _progress()
    state[key] = result
    _save_progress(state)
    flash(f"{key}: {result['status']} — {result['detail']}",
          "success" if result["status"] == "verified" else "error")
    return redirect(url_for("setup.index"))


def _run_check(key: str) -> dict:
    """Per-component verification. Where a check needs an external
    resource (worker probe, repo write probe) it currently reports
    'configured' — the real probe requires the authorized operational
    action and is flagged accordingly."""
    if key == "publishing":
        url = _get("publish_repo_url", "")
        if not url:
            return {"status": "missing",
                    "detail": "no publishing repository configured"}
        if _get("publish_repo_verified", "") == "ok":
            return {"status": "verified", "detail": "write probe passed"}
        return {"status": "configured",
                "detail": "saved — write probe requires an "
                          "explicit authorized action (creates + "
                          "deletes one marker ref, nothing else)"}
    if key == "engine":
        ok = any(getattr(r, "verify_state", "") == "verified"
                 for r in readiness._runtimes())
        return {"status": "verified" if ok else "missing",
                "detail": "verified on eligible worker" if ok else
                          "no verified engine on a connected worker"}
    comp = next((c for c in readiness.components()
                 if c["key"] == key), None)
    if comp is None:
        return {"status": "missing", "detail": "unknown component"}
    return {"status": comp["status"], "detail": comp["detail"]}
