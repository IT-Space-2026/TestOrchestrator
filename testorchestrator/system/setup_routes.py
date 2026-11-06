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
