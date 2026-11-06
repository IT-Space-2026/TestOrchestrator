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
           "auto": "",
           "fields": [{"name": "password", "type": "password",
                       "label": "New admin password",
                       "help": "12+ characters"}]},
 "coordinator": {"purpose": "The bundled Dagu coordinator runs every "
           "dispatch — required, not optional.",
           "inputs": [],
           "configure_label": "Infrastructure & components",
           "verify_note": "Health-checks the coordinator endpoint "
           "bound to the current endpoint+credential values.",
           "auto": "endpoint, ports, worker token (written at install)",
           "fields": []},
 "worker": {"purpose": "At least one Worker must be connected for any "
           "execution — remote machines get the Dagu worker download.",
           "inputs": ["worker install + registration (remote) or local "
                      "worker"],
           "configure_label": "Workers",
           "verify_note": "Passes only with a fresh heartbeat on a "
           "non-draining worker — registration alone never counts.",
           "auto": "worker download links + coordinator URL",
           "fields": [{"name": "worker_name", "type": "text",
                       "label": "Register local worker (optional)",
                       "help": "name for an in-process worker on this "
                               "portal host — remote workers register "
                               "from the worker itself"}]},
 "engine": {"purpose": "KRE is the execution engine used by a worker — "
           "external and separately licensed.",
           "inputs": ["KRE executable path on the target worker"],
           "configure_label": "KRE installations",
           "verify_note": "Presence+version probe ON an eligible "
           "worker. License is never inferred — stays 'not tested'.",
           "auto": "",
           "fields": [{"name": "kre_path", "type": "text",
                       "label": "KRE executable path",
                       "help": "e.g. /opt/kre/katalonc (must exist on "
                               "the target worker)"},
                      {"name": "worker_id", "type": "worker_select",
                       "label": "On worker",
                       "help": "the registered worker that hosts this "
                               "KRE"}]},
 "source": {"purpose": "At least one project source binding must "
           "resolve through the real dispatch path.",
           "inputs": ["repository URL", "ref", "credentials "
                      "(per selected auth method)"],
           "configure_label": "Projects",
           "verify_note": "Clone + ref resolution via the backend a "
           "worker uses — a saved URL is not verification.",
           "auto": "",
           "fields": [{"name": "repo_url", "type": "text",
                       "label": "Repository URL",
                       "help": "ssh:// for remote worker execution; "
                               "https:// works portal-side only"},
                      {"name": "repo_ref", "type": "text",
                       "label": "Ref", "help": "branch, tag, or SHA"},
                      {"name": "cred_ref", "type": "text",
                       "label": "Credential name (optional)",
                       "help": "stored credential for HTTPS; ssh uses "
                               "the worker's key path"}]},
 "publishing": {"purpose": "Report publishing repository — REQUIRED. A "
           "local-artifact-only install is not a ready full system.",
           "inputs": ["publishing repository address", "credentials"],
           "configure_label": "Reporting settings",
           "verify_note": "Explicit write probe: one marker ref "
           "created, read back, deleted. Nothing else is touched.",
           "auto": "local artifact store (storage/recovery only)",
           "fields": [{"name": "publish_repo_url", "type": "text",
                       "label": "Publishing repository URL",
                       "help": "where evidence/reports are published "
                               "— required"},
                      {"name": "publish_cred", "type": "text",
                       "label": "Publish credential name",
                       "help": "a stored Git credential with push "
                               "rights"}]},
 "security": {"purpose": "Listening address and TLS for the portal.",
           "inputs": ["bind address", "TLS cert (external exposure "
                      "only)"],
           "configure_label": "Network & certificates",
           "verify_note": "Handshake + reachability on the bound "
           "address. Loopback development is allowed.",
           "auto": "loopback default",
           "fields": [{"name": "bind_addr", "type": "text",
                       "label": "Listen address",
                       "help": "127.0.0.1 for local-only or the "
                               "deployment address"}]},
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
        workers=readiness._workers(),
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


@setup_bp.route("/save/<key>", methods=["POST"])
@role_required(Role.ADMIN)
def save(key):
    """Inline per-step save — writes the same settings/models as the
    Settings forms (no parallel backend). Validation errors return to
    the step with an actionable message; progress persists."""
    state = _progress()
    try:
        _save_step(key)
        state[key] = {"status": "configured",
                      "detail": "saved — run verification"}
        _save_progress(state)
        flash(f"{key}: saved", "success")
    except (ValueError, Exception) as exc:  # noqa: B902 - surfaces to admin
        state[key] = {"status": "failed", "detail": str(exc)}
        _save_progress(state)
        flash(f"{key}: {exc}", "error")
    return redirect(url_for("setup.step", key=key))


def _save_step(key: str) -> None:
    f = request.form
    if key == "admin":
        pw = f.get("password", "")
        if len(pw) < 12:
            raise ValueError("password must be at least 12 characters")
        from ..bootstrap import BOOTSTRAP_USERNAME
        from ..models import User
        # bootstrap account when present, else the admin doing setup
        admin = (User.query.filter_by(username=BOOTSTRAP_USERNAME)
                 .first() or db.session.get(User, current_user.id))
        if admin is None:
            raise ValueError("no admin account to update")
        admin.set_password(pw)
        admin.must_change_password = False
        admin.session_version += 1
        if pw != "Admin":
            _set("admin_bootstrap_replaced", "yes")
        db.session.commit()
    elif key == "worker":
        name = (f.get("worker_name") or "").strip()
        if not name:
            return  # remote workers register themselves
        from ..execution import Runner
        if Runner.query.filter_by(name=name).first():
            raise ValueError(f"worker {name!r} already exists")
        db.session.add(Runner(name=name,
                              connection_mode="local",
                              os_arch="linux/amd64", enabled=True))
        db.session.commit()
    elif key == "engine":
        path = f.get("kre_path", "").strip()
        wid = f.get("worker_id", "").strip()
        if not path or not wid:
            raise ValueError("KRE path and target worker are required")
        from ..execution import RuntimeConnection, Runner
        if Runner.query.get(wid) is None:
            raise ValueError("unknown worker")
        rt = RuntimeConnection(name=f"kre-{wid[:6]}",
                               runner_id=wid, executable_path=path,
                               enabled=True)
        db.session.add(rt)
        db.session.commit()
    elif key == "source":
        url = f.get("repo_url", "").strip()
        ref = f.get("repo_ref", "").strip() or "main"
        cred = f.get("cred_ref", "").strip()
        if not url:
            raise ValueError("repository URL is required")
        from ..execution import GitBinding, Project
        p = Project.query.first() or Project(name="default")
        db.session.add(p); db.session.flush()
        db.session.add(GitBinding(project_id=p.id,
                                  name=url.rsplit("/", 1)[-1],
                                  url=url, ref=ref,
                                  credential_ref=cred))
        db.session.commit()
    elif key == "publishing":
        url = f.get("publish_repo_url", "").strip()
        cred = f.get("publish_cred", "").strip()
        if not url:
            raise ValueError("publishing repository URL is required")
        _set("publish_repo_url", url)
        _set("publish_repo_credential_ref", cred)
        db.session.commit()
    elif key == "security":
        addr = f.get("bind_addr", "").strip() or "127.0.0.1"
        _set("bind_addr", addr)
        db.session.commit()
    else:
        raise ValueError(f"no inline form for {key}")


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
