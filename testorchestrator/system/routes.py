"""Admin -> System -> Network & certificates (v1.8 §3–§5).

All mutations are Admin-only, validated, staged through the Caddy renderer,
audited without secrets, and recoverable via previous_good_config.
"""

import os
import re
import sys
from datetime import datetime, timezone

from flask import (Response, abort, current_app, flash, redirect,
                   render_template, request, send_file, url_for)
from flask_login import current_user

from ..extensions import db

from .. import settings as settings_mod
from ..netconf import (NetApplyError, apply_config, rollback_config)
from ..certs import (CertificateError, generate_local, import_pem)
from ..models import Role, record_audit
from ..network import (ValidationError, access_links, check_port_conflicts,
                       detect_address_candidates, parse_host,
                       port_needs_privilege, validate_port)
from ..security import role_required
from . import system_bp
from .forms import (AddressForm, ApplyForm, GenerateCertForm, ImportCertForm,
                    PortsForm, ResetForm, RunnerForm, RuntimeForm, SecretForm,
                    UserForm)


def _context():
    settings = settings_mod.all_settings()
    candidates = detect_address_candidates()
    return {
        "settings": settings,
        "links": access_links(settings),
        "candidates": candidates,
        "multi_candidate": len(candidates) > 1,
        "address_form": AddressForm(
            external_enabled=settings["external_enabled"],
            external_host=settings["external_host"],
        ),
        "ports_form": PortsForm(
            https_port=settings["https_port"],
            http_port=settings["http_port"],
            internal_app_port=settings["internal_app_port"],
            http_mode=settings["http_mode"],
        ),
        "generate_form": GenerateCertForm(),
        "import_form": ImportCertForm(),
        "apply_form": ApplyForm(),
        "cert_meta": settings.get("certificate_public_metadata") or {},
        "cert_dir": current_app.config["CERT_DIR"],
    }


def _san_choices(settings):
    """Hosts offered for SAN coverage: local aliases + external if enabled."""
    hosts = ["localhost", "127.0.0.1", "::1", settings["local_host"]]
    if settings["external_enabled"] and settings["external_host"]:
        hosts.append(settings["external_host"])
    seen, out = set(), []
    for h in hosts:
        if h and h not in seen:
            seen.add(h)
            out.append((h, h))
    return out


def _apply_and_report():
    """Stage + validate + apply rendered Caddy config; flash the result."""
    try:
        result = apply_config(current_app, settings_mod.all_settings())
    except NetApplyError as exc:
        flash(f"Configuration NOT applied: {exc}. {exc.stderr}", "error")
        record_audit(
            current_user.username, "network.apply_failed", str(exc)[:200]
        )
        return False
    flash(f"Configuration applied (v{result['version']}).", "info")
    record_audit(current_user.username, "network.applied")
    return True


@system_bp.route("/network")
@role_required(Role.ADMIN)
def network():
    record_audit(current_user.username, "page.network")
    ctx = _context()
    ctx["generate_form"].san_hosts.choices = _san_choices(ctx["settings"])
    ctx["generate_form"].san_hosts.data = [
        c[0] for c in ctx["generate_form"].san_hosts.choices
    ]
    return render_template("system/network.html", **ctx)


@system_bp.route("/network/address", methods=["POST"])
@role_required(Role.ADMIN)
def save_address():
    form = AddressForm()
    if not form.validate_on_submit():
        flash("Invalid address settings.", "error")
        return redirect(url_for("system.network"))

    updates = {"external_enabled": bool(form.external_enabled.data)}
    if form.external_enabled.data:
        # A chosen candidate or an Admin-typed value; both are validated as
        # plain host/IP (no URLs, no implicit public-IP claims).
        chosen = form.candidate.data or form.external_host.data
        try:
            updates["external_host"] = parse_host(chosen)
        except ValidationError as exc:
            flash(f"External address rejected: {exc}", "error")
            return redirect(url_for("system.network"))
    settings_mod.update(updates)
    record_audit(
        current_user.username, "network.address",
        f"external_enabled={updates['external_enabled']} "
        f"external_host={updates.get('external_host', '')}",
    )
    _apply_and_report()
    return redirect(url_for("system.network"))


@system_bp.route("/network/ports", methods=["POST"])
@role_required(Role.ADMIN)
def save_ports():
    form = PortsForm()
    if not form.validate_on_submit():
        flash("Invalid port settings (integers 1-65535 required).", "error")
        return redirect(url_for("system.network"))

    ports = {
        "https_port": validate_port(form.https_port.data, "HTTPS port"),
        "http_port": validate_port(form.http_port.data, "HTTP port"),
        "internal_app_port": validate_port(
            form.internal_app_port.data, "internal app port"
        ),
    }
    try:
        check_port_conflicts(ports)
    except ValidationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("system.network"))

    for name, port in ports.items():
        if port_needs_privilege(port):
            flash(
                f"{name} {port} is privileged (<1024); Caddy needs "
                "CAP_NET_BIND_SERVICE or root.", "info"
            )
        # Probe runtime availability only for ports we are not already using.
        if name == "internal_app_port":
            continue

    settings_mod.update({**ports, "http_mode": form.http_mode.data})
    record_audit(
        current_user.username, "network.ports",
        f"https={ports['https_port']} http={ports['http_port']} "
        f"internal={ports['internal_app_port']} mode={form.http_mode.data}",
    )
    _apply_and_report()
    return redirect(url_for("system.network"))


@system_bp.route("/network/certificate/generate", methods=["POST"])
@role_required(Role.ADMIN)
def generate_certificate():
    settings = settings_mod.all_settings()
    form = GenerateCertForm()
    form.san_hosts.choices = _san_choices(settings)
    if not form.validate_on_submit():
        flash("Select at least one hostname/IP for SAN coverage.", "error")
        return redirect(url_for("system.network"))

    try:
        meta = generate_local(
            form.san_hosts.data, current_app.config["CERT_DIR"]
        )
    except CertificateError as exc:
        flash(str(exc), "error")
        return redirect(url_for("system.network"))

    settings_mod.update(
        {
            "certificate_mode": "local",
            "certificate_reference": "certs/fullchain.pem",
            "certificate_public_metadata": meta,
        }
    )
    # Sanitized audit: fingerprints/paths only, never key material.
    record_audit(
        current_user.username, "cert.generated",
        f"fingerprint={meta['fingerprint_sha256']} sans={','.join(meta['sans'])}",
    )
    _apply_and_report()
    flash(
        "Local CA generated. Browsers must trust the local CA certificate "
        "(download below and install into the client trust store — your "
        "explicit choice). The private key never leaves this server.",
        "info",
    )
    return redirect(url_for("system.network"))


@system_bp.route("/network/certificate/import", methods=["POST"])
@role_required(Role.ADMIN)
def import_certificate():
    settings = settings_mod.all_settings()
    advertised = (
        settings["external_host"]
        if settings["external_enabled"] and settings["external_host"]
        else settings["local_host"]
    )
    form = ImportCertForm()
    if not form.validate_on_submit():
        flash("Certificate and matching private key files are required.", "error")
        return redirect(url_for("system.network"))

    cert_pem = form.certificate.data.read()
    key_pem = form.private_key.data.read()
    intermediates = (
        form.intermediates.data.read() if form.intermediates.data else b""
    )
    try:
        meta = import_pem(
            cert_pem, key_pem, advertised,
            intermediates_pem=intermediates,
            cert_dir=current_app.config["CERT_DIR"],
        )
    except CertificateError as exc:
        record_audit(current_user.username, "cert.import_rejected", str(exc)[:200])
        flash(f"Certificate rejected: {exc}", "error")
        return redirect(url_for("system.network"))

    settings_mod.update(
        {
            "certificate_mode": "imported",
            "certificate_reference": "certs/fullchain.pem",
            "certificate_public_metadata": meta,
        }
    )
    record_audit(
        current_user.username, "cert.imported",
        f"fingerprint={meta['fingerprint_sha256']} subject={meta['subject']}",
    )
    _apply_and_report()
    flash(
        "Certificate stored. Trust still cannot be guaranteed for every "
        "client — imported certificates do not change CA trust on user "
        "devices.", "info",
    )
    return redirect(url_for("system.network"))


@system_bp.route("/network/apply", methods=["POST"])
@role_required(Role.ADMIN)
def apply():
    ApplyForm().validate_on_submit()  # CSRF only
    _apply_and_report()
    return redirect(url_for("system.network"))


@system_bp.route("/network/rollback", methods=["POST"])
@role_required(Role.ADMIN)
def rollback():
    ApplyForm().validate_on_submit()
    try:
        rollback_config(current_app)
    except NetApplyError as exc:
        flash(str(exc), "error")
        return redirect(url_for("system.network"))
    record_audit(current_user.username, "network.rollback")
    flash("Rolled back to the last known good configuration.", "info")
    return redirect(url_for("system.network"))


@system_bp.route("/network/ca.pem")
@role_required(Role.ADMIN)
def download_ca():
    """Public CA certificate export (local mode). The private key is never
    offered as a download — this route only serves the public file."""
    if settings_mod.get("certificate_mode") != "local":
        flash("No locally generated CA exists.", "error")
        return redirect(url_for("system.network"))
    ca_path = current_app.config["CERT_DIR"] / "local-ca.pem"
    if not ca_path.exists():
        flash("Local CA file missing.", "error")
        return redirect(url_for("system.network"))
    return send_file(ca_path, as_attachment=True,
                     download_name="testorchestrator-local-ca.pem")


# ---------- users (§16) ----------

@system_bp.route("/reporting")
@role_required(Role.ADMIN)
def reporting_page():
    """Admin -> Reporting settings. reporting-labs is the sole
    report backend in 2.0 — enable/disable toggle only."""
    return render_template(
        "system/reporting.html",
        settings=settings_mod.all_settings(),
    )


@system_bp.route("/integrations")
@role_required(Role.ADMIN)
def integrations_page():
    """Subsystem-separated integration config (Git evidence export).
    Each section is its own labelled card; secrets are referenced by
    name only."""
    return render_template("system/integrations.html",
                           settings=settings_mod.all_settings())


@system_bp.route("/integrations/save", methods=["POST"])
@role_required(Role.ADMIN)
def integrations_save():
    """Persist integration settings — names/paths only, never secret
    values. Applies to future operations; no restart needed."""
    from flask import request
    updates = {}
    hidden = set()
    for key in ("buildbot_endpoint", "buildbot_credential_ref",
                "buildbot_username", "git_export_repo",
                "git_export_credential_ref", "git_export_ref"):
        if key not in request.form:
            continue
        # Backend-managed keys (orchestrator config) are not
        # user-configurable: rejecting + auditing so an ignored update
        # is never silently dropped.
        if key.startswith("buildbot_"):
            hidden.add(key)
            continue
        updates[key] = request.form.get(key, "").strip()
    if hidden:
        record_audit(current_user.username,
                     "integrations.hidden_key.rejected",
                     "ignored backend-managed keys: " +
                     ",".join(sorted(hidden)))
        flash("Coordinator settings are managed internally and were "
              "not changed.", "warn")
    try:
        settings_mod.update(updates)
    except KeyError as exc:
        flash(f"Integration field {exc} rejected — expected format "
              "per its section.", "error")
        return redirect(url_for("system.integrations_page"))
    record_audit(current_user.username, "integrations.config.saved",
                 "integration settings updated (no values)")
    flash("Integration settings saved.", "info")
    return redirect(url_for("system.integrations_page"))


@system_bp.route("/reporting/save", methods=["POST"])
@role_required(Role.ADMIN)
def reporting_save():
    """Persist reporting config. Saving NEVER publishes — publication is
    a separate explicit action."""
    from flask import request
    settings_mod.update({
        "reportinglabs_enabled":
            bool(request.form.get("reportinglabs_enabled")),
    })
    record_audit(current_user.username, "reporting.config.saved",
                 "reporting settings updated (no values)")
    flash("Reporting settings saved. No runs were published.", "info")
    return redirect(url_for("system.reporting_page"))


@system_bp.route("/logs")
@role_required(Role.ADMIN)
def operational_logs():
    """Admin -> Operational Logs — structured events, filters, live
    tail, bounded export. Separate from the Audit trail."""
    from .. import logs
    filters = {
        "severity": request.args.get("severity") or None,
        "component": request.args.get("component") or None,
        "category": request.args.get("category") or None,
        "project_id": request.args.get("project") or None,
        "execution_id": request.args.get("execution") or None,
        "worker_id": request.args.get("worker") or None,
        "correlation_id": request.args.get("corr") or None,
        "q": request.args.get("q") or None,
    }
    page = request.args.get("page", 1, type=int) or 1
    paged = logs.query(filters).paginate(
        page=page, per_page=50, error_out=False)
    components = [r[0] for r in
                  db.session.query(logs.OperationalLog.component)
                  .distinct()]
    return render_template("system/operational_logs.html",
                           rows=paged.items, page=paged,
                           filters=filters, components=components,
                           severities=logs.SEVERITIES,
                           categories=logs.CATEGORIES)


@system_bp.route("/logs/tail")
@role_required(Role.ADMIN)
def operational_logs_tail():
    """JSON rows newer than `after` — live-tail feed for the page."""
    from .. import logs
    from datetime import datetime, timezone
    after = request.args.get("after", "")
    q = logs.OperationalLog.query.order_by(
        logs.OperationalLog.occurred_at.desc(),
        logs.OperationalLog.id.desc()).limit(200)
    if after:
        try:
            q = logs.OperationalLog.query.filter(
                logs.OperationalLog.occurred_at >
                datetime.fromisoformat(after)).order_by(
                    logs.OperationalLog.occurred_at)
        except ValueError:
            pass
    rows = [{"id": r.id, "ts": r.occurred_at.isoformat(),
             "severity": r.severity, "component": r.component,
             "category": r.category, "project": r.project_id,
             "execution": r.execution_id, "worker": r.worker_id,
             "corr": r.correlation_id, "source": r.source,
             "message": logs.redact(r.message)}
            for r in q.limit(200)]
    return {"rows": rows,
            "generated_at": datetime.now(timezone.utc).isoformat()}


@system_bp.route("/logs/export")
@role_required(Role.ADMIN)
def operational_logs_export():
    """Bounded filtered export — JSONL or CSV, secrets redacted,
    CSV-escape-safe. Export action is audited (no log content)."""
    from .. import logs
    from ..models import record_audit
    fmt = request.args.get("format", "csv")
    if fmt not in ("csv", "jsonl"):
        fmt = "csv"
    filters = {
        "severity": request.args.get("severity") or None,
        "component": request.args.get("component") or None,
        "category": request.args.get("category") or None,
        "q": request.args.get("q") or None,
    }
    rows = logs.query(filters).limit(10000).all()
    record_audit(current_user.username, "logs.export",
                 f"{fmt} {len(rows)} rows")
    mime = ("application/x-ndjson" if fmt == "jsonl"
            else "text/csv")
    return Response(logs.export(rows, fmt),
                    mimetype=mime,
                    headers={"Content-Disposition":
                             f"attachment; filename=oplog.{fmt}"})


@system_bp.route("/users")
@role_required(Role.ADMIN)
def users():
    from ..models import User
    record_audit(current_user.username, "page.users")
    return render_template(
        "system/users.html",
        users=User.query.order_by(User.username).all(),
        form=UserForm(), reset_form=ResetForm(), apply_form=ApplyForm())


@system_bp.route("/users", methods=["POST"])
@role_required(Role.ADMIN)
def create_user():
    from ..models import User
    form = UserForm()
    if form.validate_on_submit():
        username = form.username.data.strip()
        if User.query.filter_by(username=username).first():
            flash("Username already exists.", "error")
            return redirect(url_for("system.users"))
        user = User(
            username=username,
            display_name=form.display_name.data or "",
            role=Role(form.role.data),
            must_change_password=True,  # mandatory first change (§16)
        )
        user.set_password(form.temporary_password.data)
        db.session.add(user)
        db.session.commit()
        record_audit(current_user.username, "user.created", username)
        flash(f"User {username} created; first login requires a password "
              "change.", "info")
    return redirect(url_for("system.users"))


@system_bp.route("/users/<int:user_id>/reset", methods=["POST"])
@role_required(Role.ADMIN)
def reset_user(user_id):
    from ..models import User
    form = ResetForm()
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    if form.validate_on_submit():
        user.set_password(form.temporary_password.data)
        user.must_change_password = True
        user.session_version += 1  # reset revokes sessions (§16)
        db.session.commit()
        record_audit(current_user.username, "user.reset", user.username)
        flash(f"Password reset for {user.username}; their sessions were "
              "revoked.", "info")
    return redirect(url_for("system.users"))


@system_bp.route("/users/<int:user_id>/deactivate", methods=["POST"])
@role_required(Role.ADMIN)
def deactivate_user(user_id):
    from ..models import User
    ApplyForm().validate_on_submit()
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    # Protect the last active Admin (§16).
    if user.role == Role.ADMIN:
        others = User.query.filter(
            User.role == Role.ADMIN, User.is_active, User.id != user.id
        ).count()
        if others == 0:
            flash("Cannot deactivate the last active Admin.", "error")
            return redirect(url_for("system.users"))
    user.is_active = False
    user.session_version += 1
    db.session.commit()
    record_audit(current_user.username, "user.deactivated", user.username)
    return redirect(url_for("system.users"))


# ---------- credentials (§12) ----------

@system_bp.route("/secrets")
@role_required(Role.ADMIN)
def secrets():
    from ..secrets_store import list_secret_meta
    record_audit(current_user.username, "page.secrets")
    return render_template("system/secrets.html",
                           secrets=list_secret_meta(), form=SecretForm())


@system_bp.route("/secrets", methods=["POST"])
@role_required(Role.ADMIN)
def create_secret():
    from ..secrets_store import store_secret
    form = SecretForm()
    if form.validate_on_submit():
        store_secret(
            current_app, form.name.data.strip(), form.kind.data,
            form.value.data, {"description": form.description.data or ""},
        )
        # Metadata only — never the value (§12).
        record_audit(current_user.username, "secret.stored",
                     f"{form.name.data.strip()} ({form.kind.data})")
        flash(f"Credential {form.name.data.strip()} stored.", "info")
    return redirect(url_for("system.secrets"))


@system_bp.route("/secrets/<name>/delete", methods=["POST"])
@role_required(Role.ADMIN)
def delete_secret(name):
    from ..secrets_store import delete_secret
    from ..execution import SecretRef
    ApplyForm().validate_on_submit()
    sec = SecretRef.query.filter_by(name=name).first()
    if sec is not None and sec.kind in ("buildbot", "runner"):
        flash("System-managed credentials cannot be deleted here — "
              "they are provisioned by orchestration/worker "
              "enrollment.", "error")
        return redirect(url_for("system.secrets"))
    if delete_secret(name):
        record_audit(current_user.username, "secret.deleted", name)
    return redirect(url_for("system.secrets"))


# ---------- runners & runtime connections (§5–§6) ----------

def _bb_worker_installer_asset() -> dict | None:
    """The bundled offline Buildbot-worker Windows installer — the
    standalone .exe the admin downloads and runs on the remote host.
    Missing artifact -> the UI shows an explicit error, not a broken
    link (§3 of the replacement contract)."""
    import hashlib
    from pathlib import Path
    d = (Path(current_app.root_path).parent / "packaging" / "windows")
    for p in sorted(d.glob("TestOrchestrator-Buildbot-Worker-Setup-"
                           "*.exe")):
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        ver = re.search(r"Setup-(\d+\.\d+\.\d+)(-UNSIGNED)?\.exe",
                        p.name)
        return {"file": p.name, "size": p.stat().st_size,
                "version": ver.group(1) if ver else "?",
                "sha256": h.hexdigest()}
    return None


@system_bp.route("/infrastructure/buildbot-worker-installer/"
                 "<filename>")
@role_required(Role.ADMIN)
def buildbot_worker_installer_download(filename):
    """Serve the bundled offline Buildbot worker installer — no
    credentials in the URL or the artifact; workername/password come
    from the target record, entered at install time."""
    import re
    from pathlib import Path
    if not re.fullmatch(
            r"TestOrchestrator-Buildbot-Worker-Setup-"
            r"\d+\.\d+\.\d+(-UNSIGNED)?\.exe", filename):
        abort(404)
    path = (Path(current_app.root_path).parent
            / "packaging" / "windows" / filename)
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True,
                     download_name=filename)


def _bb_linux_worker_asset() -> dict | None:
    """The bundled self-contained Linux worker install — script +
    offline wheel closure (py3.10-3.12) in one tarball."""
    import hashlib
    from pathlib import Path
    d = (Path(current_app.root_path).parent / "packaging" / "windows")
    for p in sorted(d.glob("TestOrchestrator-Buildbot-Worker-*"
                           "-linux-x86_64.tar.gz")):
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        ver = re.search(r"Worker-(\d+\.\d+\.\d+)-linux", p.name)
        return {"file": p.name, "size": p.stat().st_size,
                "version": ver.group(1) if ver else "?",
                "sha256": h.hexdigest()}
    return None


@system_bp.route("/infrastructure/buildbot-worker-linux/<filename>")
@role_required(Role.ADMIN)
def buildbot_worker_linux_download(filename):
    """Serve the self-contained Linux worker bundle — same contract as
    the .exe: no credentials in the URL or the artifact."""
    import re
    from pathlib import Path
    if not re.fullmatch(
            r"TestOrchestrator-Buildbot-Worker-\d+\.\d+\.\d+"
            r"-linux-x86_64\.tar\.gz", filename):
        abort(404)
    path = (Path(current_app.root_path).parent
            / "packaging" / "windows" / filename)
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True,
                     download_name=filename)


def _bb_sources_asset() -> dict | None:
    """Corresponding-source archive offered next to the worker .exe —
    GPLv2 §3(a) delivery: source accompanies the binary download."""
    import hashlib
    from pathlib import Path
    d = (Path(current_app.root_path).parent / "packaging" / "windows")
    for p in sorted(d.glob("TestOrchestrator-Buildbot-Sources-*"
                           ".tar.gz")):
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        return {"file": p.name, "size": p.stat().st_size,
                "sha256": h}
    return None


@system_bp.route("/infrastructure/buildbot-sources/<filename>")
@role_required(Role.ADMIN)
def buildbot_sources_download(filename):
    """Corresponding source for the GPL/LGPL-covered worker packages —
    served beside the binary installer so every .exe offer is
    accompanied by its source offer."""
    import re
    from pathlib import Path
    if not re.fullmatch(r"TestOrchestrator-Buildbot-Sources-"
                        r"\d+\.\d+\.\d+\.tar\.gz", filename):
        abort(404)
    path = (Path(current_app.root_path).parent
            / "packaging" / "windows" / filename)
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True,
                     download_name=filename)


@system_bp.route("/settings")
@role_required(Role.ADMIN)
def settings_home():
    """Centralized task-oriented settings hub — the single entry point
    for normal configuration; component-level overrides stay under
    Advanced/diagnostics."""
    return render_template("system/settings.html")


# ---------- system health & application-managed services ----------

_DEFAULT_UNITS = ("testorchestrator-web.service",
                  "testorchestrator-worker.service",
                  "testorchestrator-buildbot-master.service",
                  "testorchestrator-buildbot-local.service",
                  "testorchestrator-buildbot-ssh.service")
_UNIT_RE = re.compile(r"^[a-zA-Z0-9@._ -]+\.service$")
_TASK_RE = re.compile(r"^[a-zA-Z0-9@._ -]+$")
_RESTART_FLAG = "restart-request.json"


def _managed_config() -> dict:
    """The service set the restart controls act on — from server-side
    configuration only (env for isolated/managed installs, packaged
    default otherwise). Never from request input. Scope: "system" or
    "user" systemd, or "schtasks" for the Windows scheduled-task model
    the full Windows installer registers."""
    raw = os.environ.get("TO_SERVICE_UNITS", "")
    scope = os.environ.get("TO_SERVICE_SCOPE",
                           "schtasks" if os.name == "nt" else "system")
    default = (["TestOrchestratorWeb", "TestOrchestratorWorker",
                "TestOrchestratorBuildbotMaster"] if scope == "schtasks"
        else list(_DEFAULT_UNITS))
    units = [u.strip() for u in raw.split(",") if u.strip()] or default
    name_re = _TASK_RE if scope == "schtasks" else _UNIT_RE
    valid = all(name_re.match(u) for u in units) and \
        scope in ("system", "user", "schtasks")
    return {"units": units, "scope": scope, "valid": valid}


def _service_status() -> dict:
    """Status of application-managed services — systemd units (system or
    user scope), Windows scheduled tasks, or an honest unmanaged report
    where no service manager is installed."""
    import shutil, subprocess
    cfg = _managed_config()
    scope = cfg["scope"]
    tool = {"system": "systemctl", "user": "systemctl",
            "schtasks": "schtasks"}[scope]
    out = {"managed": shutil.which(tool) is not None,
           "scope": scope, "config_valid": cfg["valid"], "services": []}
    for u in cfg["units"]:
        if not out["managed"]:
            out["services"].append({"unit": u, "state": "unmanaged"})
            continue
        if scope == "schtasks":
            r = subprocess.run(
                ["schtasks", "/Query", "/TN", u, "/FO", "LIST"],
                capture_output=True, text=True)
            st = "unknown"
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    if "Status:" in line or ":" in line:
                        st = "active" if "Running" in line or \
                            "" in line else "inactive"
            else:
                st = "inactive"
        else:
            cmd = ["systemctl", "--user"] if scope == "user" \
                else ["systemctl"]
            ld = subprocess.run(
                [*cmd, "show", u, "-p", "LoadState", "--value"],
                capture_output=True, text=True)
            if ld.stdout.strip() == "not-found":
                st = "not installed"
            else:
                r = subprocess.run([*cmd, "is-active", u],
                                   capture_output=True, text=True)
                st = (r.stdout.strip() or "unknown") \
                    if r.returncode == 0 else (r.stdout.strip() or "inactive")
        out["services"].append({"unit": u, "state": st})
    return out


def _restart_marker() -> "Path":
    from pathlib import Path
    return Path(current_app.instance_path) / _RESTART_FLAG


def _active_work_count() -> int:
    from ..execution import Execution, ExecutionState
    return Execution.query.filter(Execution.state.in_([
        ExecutionState.DISPATCHED, ExecutionState.PREPARING,
        ExecutionState.RUNNING, ExecutionState.COLLECTING,
        ExecutionState.NORMALIZING])).count()


def _unit_invocations(units: list, scope: str) -> dict:
    """Pre-request invocation identity per service — systemd MainPID,
    schtasks last-run timestamp — so a restart is proven by a NEW
    invocation, not merely by the service being active."""
    import subprocess
    out = {}
    for u in units:
        try:
            if scope == "schtasks":
                r = subprocess.run(
                    ["schtasks", "/Query", "/TN", u, "/FO", "LIST", "/V"],
                    capture_output=True, text=True, timeout=10)
                ident = "missing"
                for line in r.stdout.splitlines():
                    if "Last Run Time" in line:
                        ident = line.split(":", 1)[1].strip()
                out[u] = ident
            else:
                cmd = ["systemctl", "--user"] if scope == "user" \
                    else ["systemctl"]
                r = subprocess.run([*cmd, "show", u, "-p", "MainPID",
                                    "--value"], capture_output=True,
                                   text=True, timeout=10)
                out[u] = r.stdout.strip() or "0"
        except Exception:
            out[u] = "error"
    return out


def _resolve_pending_restart(status: dict, marker) -> None:
    """Resolve pending ONLY on helper-confirmed evidence of a NEW
    invocation per service — never merely because units are active
    (they may be the original processes if the helper never launched).
    Helper evidence distinguishes verified restart from false-positive
    liveness; missing evidence ages out to failed."""
    import json as _j
    if not marker.exists():
        return
    try:
        rec = _j.loads(marker.read_text())
    except Exception:
        return
    if rec.get("result") != "pending":
        return
    states = {s["unit"]: s["state"] for s in status["services"]}
    age = 0.0
    try:
        req = datetime.fromisoformat(rec["requested_at"])
        age = (datetime.now(timezone.utc) - req).total_seconds()
    except Exception:
        pass
    evidence = rec.get("evidence") or {}
    confirmed = bool(evidence) and all(
        evidence.get(u, {}).get("restarted") is True
        and evidence.get(u, {}).get("new") !=
            evidence.get(u, {}).get("old")
        and states.get(u) == "active"
        for u in rec.get("units", []))
    if confirmed:
        rec["result"] = "done"
        rec["basis"] = "helper_confirmed"
    elif age > 60:
        rec["result"] = "failed"
        rec["basis"] = ("no_helper_evidence" if not evidence
                        else "partial_or_no_new_invocation")
        rec["failed_states"] = states
    else:
        return                                   # still settling
    rec["resolved_at"] = datetime.now(timezone.utc).isoformat()
    marker.write_text(_j.dumps(rec))
    record_audit(rec.get("requested_by", "?"),
                 "services.restart_result",
                 f"{rec['result']} ({rec.get('basis')}) — {states}")


@system_bp.route("/health")
@role_required(Role.ADMIN)
def system_health():
    status = _service_status()
    marker = _restart_marker()
    _resolve_pending_restart(status, marker)
    last_restart = None
    if marker.exists():
        import json as _j
        try:
            last_restart = _j.loads(marker.read_text())
        except Exception:
            pass
    healthy = (not status["managed"]) or all(
        s["state"] == "active" for s in status["services"])
    return render_template(
        "system/health.html", status=status, healthy=healthy,
        active_work=_active_work_count(), last_restart=last_restart,
        restart_form=ApplyForm())


@system_bp.route("/services/restart", methods=["POST"])
@role_required(Role.ADMIN)
def restart_services():
    """Restart the platform-managed services through systemd — the
    mechanism the package itself installed. Detached via a new session
    so completion does not depend on this web request surviving its own
    restart. Never touches remote workers or their KRE processes."""
    import json as _j
    status = _service_status()
    if not status["managed"]:
        flash("No managed services — this installation runs without "
              "systemd units (isolated/dev mode). Nothing to restart.",
              "error")
        return redirect(url_for("system.system_health"))
    if not status["config_valid"]:
        flash("Service configuration invalid — refusing to restart "
              "unrecognized unit names.", "error")
        return redirect(url_for("system.system_health"))
    active = _active_work_count()
    if active:
        flash(f"Restart deferred — {active} execution(s) still active. "
              "Wait for them to finish or cancel them first; restarting "
              "mid-run loses the portal-side collect/normalize step.",
              "error")
        return redirect(url_for("system.system_health"))
    marker = _restart_marker()
    if marker.exists():
        flash("A restart was already requested — refusing a duplicate "
              "request. Check System health for its result.", "error")
        return redirect(url_for("system.system_health"))
    units = [s["unit"] for s in status["services"]]
    scope = _managed_config()["scope"]
    pre_inv = _unit_invocations(units, scope)
    ctl = ["schtasks"] if scope == "schtasks" else \
        (["systemctl", "--user"] if scope == "user" else ["systemctl"])
    marker.write_text(_j.dumps({
        "requested_by": current_user.username,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "units": units, "scope": scope,
        "pre_invocations": pre_inv, "result": "pending"}))
    record_audit(current_user.username, "services.restart_requested",
                 ", ".join(units))
    # Independent completion: the service manager performs the restart;
    # the helper only reports the outcome into the marker afterwards.
    if scope == "schtasks":
        ident = ("subprocess.run(['schtasks','/Query','/TN',u,'/FO','LIST','/V'],capture_output=True,text=True).stdout")
        actions = ("for u in units:\n"
                   "    subprocess.run(['schtasks','/End','/TN',u])\n"
                   "time.sleep(2)\n"
                   "ok=True\n"
                   "for u in units:\n"
                   "    nr=subprocess.run(['schtasks','/Run','/TN',u]); ok = ok and nr.returncode==0\n"
                   "    new_id=''\n"
                   "    for ln in " + ident + ".splitlines():\n"
                   "        if 'Last Run Time' in ln: new_id=ln.split(':',1)[1].strip()\n"
                   "    ev[u]={'old':pre.get(u),'new':new_id,'restarted':nr.returncode==0}\n")
    else:
        ident = ("subprocess.run([*ctl,'show',u,'-p','MainPID','--value'],capture_output=True,text=True).stdout.strip()")
        actions = (f"ctl={ctl!r}\nunits={units!r}\n"
                   "for u in units:\n"
                   "    nr=subprocess.run([*ctl,'restart',u]); \n"
                   "    new_id=" + ident + "\n"
                   "    ev[u]={'old':pre.get(u),'new':new_id,'restarted':nr.returncode==0 and new_id not in ('0','',pre.get(u))}\n"
                   "ok=all(v['restarted'] for v in ev.values())\n")
    helper = (
        "import json,subprocess,sys,time\n"
        "from pathlib import Path\n"
        f"m=Path({str(marker)!r})\n"
        # Allow the dispatching request to finish before we restart the
        # portal serving it — killing it mid-response yields curl 000
        # and can truncate the marker's helper_unit write.
        "time.sleep(8)\n"
        f"units={units!r}\n"
        f"pre={pre_inv!r}\n"
        "ev={}\n"
        + actions +
        "d=json.loads(m.read_text())\n"  # noqa: E501
        "d['evidence']=ev\n"
        "d['helper_result']='done' if ok else 'failed'\n"
        "m.write_text(json.dumps(d))\n"
        "sys.exit(0 if ok else 1)\n")
    helper_path = marker.parent / "restart-helper.py"
    helper_path.write_text(helper)
    launched = _launch_restart_helper(helper_path, scope)
    if not launched:
        marker.unlink(missing_ok=True)
        flash("Restart could not be dispatched through the service "
              "manager.", "error")
        return redirect(url_for("system.system_health"))
    # Bind this request to the exact helper unit/task it dispatched —
    # evidence collection and cleanup must not depend on globbing.
    rec = _j.loads(marker.read_text())
    rec["helper_unit"] = launched
    marker.write_text(_j.dumps(rec))
    flash(f"Restart dispatched via {launched} — watch System health "
          "for the writeback result.", "info")
    return redirect(url_for("system.system_health"))


def _launch_restart_helper(helper_path, scope: str) -> "bool | str":
    """Dispatch the restart helper as an INDEPENDENT async unit:
    a uniquely-named transient systemd service (scope would run
    SYNCHRONOUSLY — not fire-and-forget), or a unique one-shot
    schtasks task. No silent Popen: if no service-manager launch is
    available the caller fails the request explicitly. Returns a
    truthy launch label or False."""
    import shutil, subprocess, uuid
    py = sys.executable or "python3"
    uid = uuid.uuid4().hex[:8]
    if scope in ("user", "system") and shutil.which("systemd-run"):
        unit = f"to-restart-helper-{uid}.service"
        cmd = ["systemd-run"]
        if scope == "user":
            cmd.append("--user")
        # Transient SERVICE (not --scope): asynchronous, own cgroup,
        # survives the portal's KillMode=control-group kill. Do NOT set
        # Type=oneshot — StartUnit then blocks until the helper exits,
        # synchronizing the request with the helper's full runtime.
        cmd += ["--unit=" + unit, "--collect", "--same-dir", "--",
                py, str(helper_path)]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=15)
        if r.returncode == 0:
            return unit
        return False
    if scope == "schtasks" and shutil.which("schtasks"):
        task = f"TestOrchestrator-RestartHelper-{uid}"
        # Unique name per request — no shared-name overwrite; /Create
        # checked BEFORE /Run so a stale task is never relaunched.
        tr = f'"{py}" "{helper_path}"'
        c = subprocess.run(["schtasks", "/Create", "/TN", task,
                            "/SC", "ONCE", "/ST", "00:00", "/RU",
                            "SYSTEM", "/TR", tr],
                           capture_output=True, text=True, timeout=15)
        if c.returncode != 0:
            return False                     # creation failed — report
        r = subprocess.run(["schtasks", "/Run", "/TN", task],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            subprocess.run(["schtasks", "/Delete", "/TN", task, "/F"],
                           capture_output=True)
            return False
        return task
    return False


@system_bp.route("/infrastructure")
@role_required(Role.ADMIN)
def infrastructure():
    record_audit(current_user.username, "page.infrastructure")
    runner_form = RunnerForm()
    runtime_form = RuntimeForm()
    return _render_infrastructure(runner_form, runtime_form)


def _render_infrastructure(runner_form, runtime_form):
    from ..execution import Runner, RuntimeConnection
    from ..execution import Project
    runner_form.allowed_projects.choices = [
        (p.id, p.name) for p in Project.query.all()]
    runtime_form.runner_id = ""
    runners = [x for x in Runner.query.all()
               if not x.name.startswith("fx-")]
    fixture_runners = [x for x in Runner.query.all()
                       if x.name.startswith("fx-")]
    runtimes = [x for x in RuntimeConnection.query.all()
                if not (x.runner and x.runner.name.startswith("fx-"))]
    from datetime import datetime, timedelta, timezone
    _now = datetime.now(timezone.utc)
    for x in Runner.query.all():
        # freshness: heartbeat within 120s => live; older => last seen
        hb = x.heartbeat_at
        if hb is not None and hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        x.fresh = (hb is not None and
                   (_now - hb) < timedelta(seconds=120))
    per_runner = {}
    unbound = []
    for rt in runtimes:
        if rt.runner_id and rt.runner:
            per_runner.setdefault(rt.runner_id, []).append(rt)
        else:
            unbound.append(rt)
    from ..secrets_store import list_secret_meta
    try:
        secret_names = sorted(
            s["name"] for s in list_secret_meta())
    except Exception:
        secret_names = []
    return render_template(
        "system/infrastructure.html",
        runners=runners, runtimes=runtimes,
        per_runner=per_runner, unbound=unbound,
        fixture_runners=fixture_runners,
        runner_form=runner_form, runtime_form=runtime_form,
        runner_choices=[(r.id, r.name) for r in Runner.query.all()],
        bb_installer=_bb_worker_installer_asset(),
        bb_linux_worker=_bb_linux_worker_asset(),
        bb_sources=_bb_sources_asset(),
        secret_names=secret_names,
    )


@system_bp.route("/infrastructure/runners", methods=["POST"])
@role_required(Role.ADMIN)
def create_runner():
    """Add an execution target. target_type picks the delivery path:

    - ``local``            portal-host runner (unchanged behaviour)
    - ``buildbot-worker``  Windows/Linux worker — a unique PB
      workername + per-worker password are generated; the password is
      shown ONCE in the flash, only its secret ref persists.
    - ``ssh``              agentless host — connectivity config only;
      key material goes to the secrets store, host identity needs an
      explicit approve step before the target is usable.
    """
    import json
    import secrets as _secrets
    from .. import buildbot_cfg
    from ..execution import Project, Runner
    from ..secrets_store import SecretError, store_secret
    form = RunnerForm()
    form.allowed_projects.choices = [
        (p.id, p.name) for p in Project.query.all()]
    if form.validate_on_submit():
        kind = form.target_type.data or "local"
        runner = Runner(
            name=form.name.data.strip(),
            description=form.description.data or "",
            os_arch=form.os_arch.data or "",
            connection_mode="local" if kind == "local" else "remote",
            target_type=kind,
            workspace_root=form.workspace_root.data or "",
            max_slots=form.max_slots.data or 1,
            # An empty multi-select posts [""], not [] — strip blanks
            # or the empty string becomes a one-entry scope that
            # excludes every real project.
            allowed_projects=json.dumps(
                [p for p in (form.allowed_projects.data or []) if p]),
        )
        if kind == "buildbot-worker":
            db.session.add(runner)
            db.session.flush()
            worker = buildbot_cfg.ensure_worker_name(runner)
            ref = f"buildbot-worker-{worker}"
            password = _secrets.token_urlsafe(24)
            try:
                store_secret(current_app._get_current_object(), ref,
                             "buildbot", password,
                             {"purpose": "pb worker password"})
            except SecretError as exc:
                db.session.rollback()
                flash(f"Worker credential could not be stored — {exc}",
                      "error")
                return redirect(url_for("system.infrastructure"))
            runner.bb_credential_ref = ref
            db.session.commit()
            buildbot_cfg.sync()
            record_audit(current_user.username, "runner.created",
                         runner.name)
            flash(
                f"Buildbot worker credentials for {runner.name} — "
                f"copy now, never shown again. workername: {worker} · "
                f"password: {password}", "info")
            return redirect(url_for("system.infrastructure"))
        if kind == "ssh":
            host = (form.ssh_host.data or "").strip()
            user = (form.ssh_username.data or "").strip()
            if not host or not user:
                flash("SSH targets require a host and SSH username.",
                      "error")
                return redirect(url_for("system.infrastructure"))
            runner.ssh_host = host
            runner.ssh_port = form.ssh_port.data or 22
            runner.ssh_username = user
            runner.ssh_workdir = (form.ssh_workdir.data or "").strip()
            key_text = (form.ssh_key.data or "").strip()
            ref = (form.ssh_credential_ref.data or "").strip()
            if key_text:
                ref = f"ssh-{runner.name[:24]}"
                try:
                    store_secret(current_app._get_current_object(),
                                 ref, "ssh", key_text,
                                 {"purpose": "ssh target private key"})
                except SecretError as exc:
                    db.session.rollback()
                    flash(f"SSH key could not be stored — {exc}",
                          "error")
                    return redirect(url_for("system.infrastructure"))
            if not ref:
                flash("SSH targets need a credential — select an "
                      "existing secret or paste a private key.",
                      "error")
                return redirect(url_for("system.infrastructure"))
            runner.ssh_credential_ref = ref
            db.session.add(runner)
            db.session.commit()
            buildbot_cfg.sync()
            record_audit(current_user.username, "runner.created",
                         runner.name)
            flash("SSH target saved — run Test Connection and approve "
                  "the host key before dispatch.", "info")
            return redirect(url_for("system.infrastructure"))
        db.session.add(runner)
        db.session.commit()
        buildbot_cfg.sync()
        record_audit(current_user.username, "runner.created",
                     runner.name)
    return redirect(url_for("system.infrastructure"))


@system_bp.route("/infrastructure/runners/<runner_id>/toggle",
                 methods=["POST"])
@role_required(Role.ADMIN)
def toggle_runner(runner_id):
    """Enable/disable a target. Disabled = excluded from assignment —
    queued rows already assigned stay bound to it (never silently
    retargeted); running work is untouched."""
    from ..execution import Runner
    ApplyForm().validate_on_submit()
    runner = db.session.get(Runner, runner_id)
    if runner is None:
        abort(404)
    runner.enabled = not runner.enabled
    db.session.commit()
    record_audit(current_user.username, "runner.toggle", runner.name)
    return redirect(url_for("system.infrastructure"))


@system_bp.route("/infrastructure/runners/<runner_id>/remove",
                 methods=["POST"])
@role_required(Role.ADMIN)
def remove_runner(runner_id):
    """Remove a target — refused while any non-terminal execution
    references it or a runtime is still bound. The admin disables and
    unbinds first; nothing is silently redirected or abandoned."""
    from ..execution import (Execution, ExecutionState, Runner,
                             RuntimeConnection)
    ApplyForm().validate_on_submit()
    runner = db.session.get(Runner, runner_id)
    if runner is None:
        abort(404)
    bound = RuntimeConnection.query.filter_by(
        runner_id=runner.id).count()
    if bound:
        flash(f"Cannot remove {runner.name} — {bound} runtime "
              "installation(s) still bound. Remove or rebind them "
              "first.", "error")
        return redirect(url_for("system.infrastructure"))
    active = Execution.query.filter(
        Execution.runner_id == runner.id,
        Execution.state.in_([
            ExecutionState.QUEUED, ExecutionState.DISPATCHED,
            ExecutionState.PREPARING, ExecutionState.RUNNING,
            ExecutionState.COLLECTING, ExecutionState.NORMALIZING,
            ExecutionState.FINALIZING, ExecutionState.CANCEL_REQUESTED,
        ])).count()
    if active:
        flash(f"Cannot remove {runner.name} — {active} execution(s) "
              "still reference it. Cancel or let them finish first.",
              "error")
        return redirect(url_for("system.infrastructure"))
    name = runner.name
    db.session.delete(runner)
    db.session.commit()
    record_audit(current_user.username, "runner.removed", name)
    flash(f"Target {name} removed.", "info")
    return redirect(url_for("system.infrastructure"))


@system_bp.route("/infrastructure/runners/<runner_id>/test",
                 methods=["POST"])
@role_required(Role.ADMIN)
def test_runner(runner_id):
    """Admin 'Test' per target type — records check_status/error/at:

    - local            trivially ok (the portal host itself)
    - buildbot-worker  PB worker currently connected?
      unregistered/unreachable are distinct from ok.
    - ssh              staged probe: reachability -> host-key ->
      authentication -> workdir -> sftp -> runner prerequisites.
      A first-contact host returns its fingerprint for approval —
      that is a required step, not a failure.
    """
    from ..execution import Runner, RuntimeConnection, utcnow
    ApplyForm().validate_on_submit()
    runner = db.session.get(Runner, runner_id)
    if runner is None:
        abort(404)
    runner.check_at = utcnow()
    if runner.target_type == "local":
        runner.check_status, runner.check_error = "ok", ""
        flash("Local target — always available on this host.", "info")
    elif runner.target_type == "buildbot-worker":
        try:
            from ..adapters.buildbot import BuildbotAdapter
            online = {w.get("name") for w in
                      BuildbotAdapter.from_settings().workers()
                      if w.get("connected_to")}
        except Exception as exc:
            runner.check_status, runner.check_error = (
                "fail", f"master unreachable — {exc}")
            flash(f"Worker check failed: {exc}", "error")
        else:
            if runner.bb_worker_name in online:
                runner.check_status, runner.check_error = "ok", ""
                runner.health, runner.heartbeat_at = "ok", utcnow()
                flash(f"Worker {runner.bb_worker_name} connected.",
                      "info")
            else:
                runner.check_status = "unregistered"
                runner.check_error = ("worker has not connected — "
                                      "install/run it on the host "
                                      "first")
                runner.health = "unreachable"
                flash("Worker not connected — the portal record "
                      "exists but no worker has registered.",
                      "error")
    elif runner.target_type == "ssh":
        try:
            from .. import ssh_check
            rts = RuntimeConnection.query.filter_by(
                runner_id=runner.id, enabled=True).all()
            res = ssh_check.check_target(runner, rts)
        except Exception as exc:
            runner.check_status, runner.check_error = (
                "fail", f"check unavailable — {exc}")
            flash(f"SSH check could not run: {exc}", "error")
        else:
            runner.check_status = res.status
            runner.check_error = res.error
            if res.status == "ok":
                runner.health, runner.heartbeat_at = "ok", utcnow()
                flash("SSH connection verified — all checks passed.",
                      "info")
            elif res.status == "unverified":
                # The discovered fingerprint is what the admin
                # approves — stored in check_error so the Approve
                # action can pin exactly the displayed key (re-fetched
                # and matched server-side at approval).
                runner.check_error = res.fingerprint
                flash(f"Host key needs approval — {res.key_alg} "
                      f"{res.fingerprint}. Verify it against the "
                      "host's known key, then Approve.", "error")
            else:
                runner.health = "unreachable"
                flash(f"SSH check failed at {res.stage}: "
                      f"{res.error}", "error")
    db.session.commit()
    record_audit(current_user.username, "runner.test", runner.name)
    return redirect(url_for("system.infrastructure"))


@system_bp.route("/infrastructure/runners/<runner_id>/approve-hostkey",
                 methods=["POST"])
@role_required(Role.ADMIN)
def approve_hostkey(runner_id):
    """Pin the displayed host key — the admin saw the fingerprint in
    the check output; we re-fetch and require it to match the value
    shown, so a key swapped between display and approval is rejected."""
    from ..execution import Runner, utcnow
    ApplyForm().validate_on_submit()
    runner = db.session.get(Runner, runner_id)
    if runner is None or runner.target_type != "ssh":
        abort(404)
    expected = (request.form.get("fingerprint") or "").strip()
    try:
        from .. import ssh_check
        alg, fp = ssh_check.probe_host_key(
            runner.ssh_host, runner.ssh_port or 22)
    except Exception as exc:
        flash(f"Could not fetch host key — {exc}", "error")
        return redirect(url_for("system.infrastructure"))
    if not expected or fp != expected:
        flash(f"Refusing to pin — displayed fingerprint "
              f"{expected or '(none)'} does not match the host's "
              f"current key {fp}. Re-run Test Connection.", "error")
        return redirect(url_for("system.infrastructure"))
    runner.ssh_host_key_alg = alg
    runner.ssh_host_key_fingerprint = fp
    runner.check_status, runner.check_error = "", ""
    runner.check_at = utcnow()
    db.session.commit()
    record_audit(current_user.username, "runner.hostkey_approved",
                 runner.name)
    flash(f"Host key pinned: {alg} {fp}", "info")
    return redirect(url_for("system.infrastructure"))


@system_bp.route("/infrastructure/runners/<runner_id>/issue-bb-secret",
                 methods=["POST"])
@role_required(Role.ADMIN)
def issue_bb_secret(runner_id):
    """(Re)issue the per-worker PB password — plaintext shown once,
    same pattern as the agent token. Old password stops working."""
    import secrets as _secrets
    from .. import buildbot_cfg
    from ..execution import Runner
    from ..secrets_store import store_secret
    ApplyForm().validate_on_submit()
    runner = db.session.get(Runner, runner_id)
    if runner is None or runner.target_type != "buildbot-worker":
        abort(404)
    worker = buildbot_cfg.ensure_worker_name(runner)
    ref = f"buildbot-worker-{worker}"
    password = _secrets.token_urlsafe(24)
    store_secret(current_app._get_current_object(), ref, "buildbot",
                 password, {"purpose": "pb worker password"},
                 internal=True)
    runner.bb_credential_ref = ref
    db.session.commit()
    buildbot_cfg.sync()
    record_audit(current_user.username, "runner.bb_secret_issued",
                 runner.name)
    flash(f"New Buildbot worker password for {runner.name} — copy "
          f"now: {password}", "info")
    return redirect(url_for("system.infrastructure"))


@system_bp.route("/infrastructure/runtimes", methods=["POST"])
@role_required(Role.ADMIN)
def create_runtime():
    from ..execution import Runner, RuntimeConnection
    form = RuntimeForm()
    is_remote = request.form.get("exec_location") == "remote"
    # Remote runtimes bind to a Buildbot/SSH execution target (runner
    # record) — the selected runner's target_type carries the
    # transport.
    # Explicit runner selection wins: the form's runner_id names the
    # intended host — never silently fall back to "first matching
    # mode" (that misbound a runtime to the wrong runner).
    # An invalid/disabled/incompatible selection is a visible error,
    # not a partial create.
    want_mode = "remote" if is_remote else "local"
    _runner = None
    if form.runner_id.data and form.runner_id.data.strip():
        _runner = db.session.get(Runner, form.runner_id.data.strip())
        if _runner is None or not _runner.enabled:
            flash("Selected runner is unavailable.", "error")
            return redirect(url_for("system.infrastructure"))
        if _runner.connection_mode != want_mode:
            flash(f"Runner {_runner.name!r} is a "
                  f"{_runner.connection_mode} host — a {want_mode} "
                  "runtime cannot be bound to it.", "error")
            return redirect(url_for("system.infrastructure"))
    else:
        _runner = Runner.query.filter_by(
            connection_mode=want_mode).first()
    if _runner is None:
        _runner = Runner(
            name="local",
            connection_mode=want_mode,
            os_arch="local",
            max_slots=4)
        db.session.add(_runner); db.session.flush()
    if form.validate_on_submit():
        db.session.add(RuntimeConnection(
            name=form.name.data.strip(),
            description=form.description.data or "",
            runner_id=_runner.id,
            transport=(_runner.target_type
                       if is_remote else "local"),
            executable_path=form.executable_path.data.strip(),
            workspace_root=form.workspace_root.data or "",
            git_ssh_key_path=form.git_ssh_key_path.data or "",
            credential_ref=(form.credential_ref.data or "").strip(),
            credential_file=(form.credential_file.data or "").strip(),

            concurrency=form.concurrency.data or 1,
        ))
        db.session.commit()
        record_audit(current_user.username, "runtime.registered",
                     form.name.data.strip())
    else:
        errs = "; ".join(
            f"{k}: {', '.join(v[:2])}"
            for k, v in form.errors.items())
        if errs:
            flash(f"Runtime not registered — {errs}", "error")
            from ..execution import Project as _P
            runner_form = RunnerForm()
            runner_form.allowed_projects.choices = [
                (p.id, p.name) for p in _P.query.all()]
            return _render_infrastructure(runner_form, form)
    return redirect(url_for("system.infrastructure"))


@system_bp.route("/infrastructure/runtimes/<runtime_id>/probe",
                 methods=["POST"])
@role_required(Role.ADMIN)
def probe_runtime(runtime_id):
    """Test connection: executable presence + version probe only. Does not
    install anything, change licensing, or run project tests (§5)."""
    from ..adapters.kre import KreError, probe_version
    from ..execution import RuntimeConnection
    ApplyForm().validate_on_submit()
    rt = db.session.get(RuntimeConnection, runtime_id)
    if rt and rt.runner and rt.runner.connection_mode != "local":
        flash("Remote installation — the portal-host check cannot "
              "verify a remote worker's KRE. Use the target's "
              "connection check instead.", "error")
        return redirect(url_for("system.infrastructure"))
    if rt is None:
        abort(404)
    try:
        version = probe_version(rt.executable_path)
        rt.detected_version = version
        from ..readiness import record_verified, _engine_fp
        record_verified(f"engine_{rt.id}", _engine_fp(rt))
        from ..execution import utcnow
        rt.last_check_at = utcnow()
        rt.last_diagnostic = f"probe ok: version {version}"
        flash(f"Probe OK — detected version {version}. Licence readiness is "
              "not implied by executable presence.", "info")
    except KreError as exc:
        from ..execution import utcnow
        rt.last_check_at = utcnow()
        rt.last_diagnostic = str(exc)
        flash(f"Probe failed: {exc}", "error")
    db.session.commit()
    record_audit(current_user.username, "runtime.probe", rt.name)
    return redirect(url_for("system.infrastructure"))




@system_bp.route("/runners/<runner_id>/edit", methods=["POST"])
@role_required(Role.ADMIN)
def edit_runner(runner_id):
    """Edit mutable runner fields; probe-relevant changes invalidate
    the last check."""
    from ..execution import Runner
    ApplyForm().validate_on_submit()
    runner = db.session.get(Runner, runner_id)
    if runner is None:
        abort(404)
    runner.name = request.form.get("name", runner.name).strip()
    runner.description = request.form.get(
        "description", runner.description)
    old_arch, old_ws = runner.os_arch, runner.workspace_root
    runner.os_arch = request.form.get("os_arch", runner.os_arch)
    ws = request.form.get("workspace_root")
    if ws is not None and ws != runner.workspace_root:
        runner.workspace_root = ws
    ms = request.form.get("max_slots")
    if ms:
        runner.max_slots = int(ms)
    ls = request.form.get("license_slots")
    if ls is not None:
        runner.license_slots = int(ls) if ls.strip() else None
    if runner.target_type == "ssh":
        # SSH connectivity edits — a host/port/user change means the
        # pinned host key and last check no longer describe the same
        # machine: clear approval and re-verify.
        changed_identity = False
        for field_name, attr in (("ssh_host", "ssh_host"),
                                 ("ssh_username", "ssh_username"),
                                 ("ssh_workdir", "ssh_workdir")):
            v = request.form.get(field_name)
            if v is not None and v.strip() != getattr(runner, attr):
                if attr != "ssh_workdir":
                    changed_identity = True
                setattr(runner, attr, v.strip())
        sp = request.form.get("ssh_port")
        if sp:
            port = int(sp)
            if port != (runner.ssh_port or 22):
                changed_identity = True
                runner.ssh_port = port
        ref = request.form.get("ssh_credential_ref")
        if ref is not None and ref.strip():
            runner.ssh_credential_ref = ref.strip()
        key_text = (request.form.get("ssh_key") or "").strip()
        if key_text:
            from ..secrets_store import store_secret
            new_ref = f"ssh-{runner.name[:24]}-{runner.id[:8]}"
            store_secret(current_app._get_current_object(), new_ref,
                         "ssh", key_text,
                         {"purpose": "ssh target private key"})
            runner.ssh_credential_ref = new_ref
        if changed_identity:
            runner.ssh_host_key_alg = ""
            runner.ssh_host_key_fingerprint = ""
            runner.check_status = "unverified"
            runner.check_error = ("identity changed — re-approve the "
                                  "host key")
            flash("SSH identity changed — the host key must be "
                  "re-approved before dispatch.", "info")
    if (runner.os_arch != old_arch or
            runner.workspace_root != old_ws):
        from ..execution import RuntimeConnection, utcnow
        for rt in RuntimeConnection.query.filter_by(
                runner_id=runner.id):
            rt.detected_version = ""
            rt.last_check_at = utcnow()
            rt.last_diagnostic = (
                "re-check required — runner config changed")
    db.session.commit()
    record_audit(current_user.username, "runner.edited", runner.name)
    return redirect(url_for("system.infrastructure"))


@system_bp.route("/runtimes/<runtime_id>/edit", methods=["POST"])
@role_required(Role.ADMIN)
def edit_runtime(runtime_id):
    """Edit mutable runtime fields; executable/workspace changes
    clear the last probe result (old check no longer validates the
    new config)."""
    from ..execution import RuntimeConnection
    ApplyForm().validate_on_submit()
    rt = db.session.get(RuntimeConnection, runtime_id)
    if rt is None:
        abort(404)
    rt.name = request.form.get("name", rt.name).strip()
    rt.description = request.form.get("description", rt.description)
    ep = request.form.get("executable_path")
    wr = request.form.get("workspace_root")
    probe_changed = (ep is not None and ep != rt.executable_path) or (
        wr is not None and wr != rt.workspace_root)
    if ep is not None:
        rt.executable_path = ep
    if wr is not None:
        rt.workspace_root = wr
    # credential_ref preserved unless explicitly changed/cleared
    cr = request.form.get("credential_ref")
    if cr is not None:
        rt.credential_ref = cr
    cf = request.form.get("credential_file")
    if cf is not None:
        rt.credential_file = cf.strip()
    conc = request.form.get("concurrency")
    if conc:
        rt.concurrency = int(conc)
    if probe_changed:
        rt.detected_version = ""
        from ..execution import utcnow
        rt.last_check_at = utcnow()
        rt.last_diagnostic = (
            "re-check required — probe-relevant config changed")
    db.session.commit()
    record_audit(current_user.username, "runtime.edited", rt.name)
    return redirect(url_for("system.infrastructure"))


@system_bp.route("/runtimes/<runtime_id>/toggle", methods=["POST"])
@role_required(Role.ADMIN)
def toggle_runtime(runtime_id):
    """Enable/disable the runtime — disabled connections are excluded
    from launch/runtime selectors; history is preserved."""
    from ..execution import RuntimeConnection
    ApplyForm().validate_on_submit()
    rt = db.session.get(RuntimeConnection, runtime_id)
    if rt is None:
        abort(404)
    rt.enabled = not rt.enabled
    db.session.commit()
    record_audit(current_user.username, "runtime.toggle", rt.name)
    return redirect(url_for("system.infrastructure"))


@system_bp.route("/infrastructure/runners/<runner_id>/drain",
                 methods=["POST"])
@role_required(Role.ADMIN)
def drain_runner(runner_id):
    """Drain stops new work but lets active jobs finish (§6)."""
    from ..execution import Runner
    ApplyForm().validate_on_submit()
    runner = db.session.get(Runner, runner_id)
    if runner is None:
        abort(404)
    runner.draining = not runner.draining
    db.session.commit()
    record_audit(current_user.username, "runner.drain", runner.name)
    return redirect(url_for("system.infrastructure"))




@system_bp.route("/licence", methods=["GET", "POST"])
@role_required(Role.ADMIN)
def licence_admin():
    from .. import licensing
    if request.method == "POST":
        code = (request.form.get("licence_code") or "").strip()
        ok, msg = licensing.add(code)
        flash(msg, "info" if ok else "error")
        return redirect(url_for("system.licence_admin"))
    st, best, all_lic = licensing.current_status()
    return render_template(
        "system/licence.html", status=st, best=best,
        licences=all_lic)


@system_bp.route("/licence/resume", methods=["POST"])
@role_required(Role.ADMIN)
def licence_resume():
    # Compatibility stub: application licensing no longer holds the
    # dispatch queue, so there is nothing to resume. Kept so older
    # automation/links do not 404.
    flash("Licence status is informational — dispatch is not held.", "info")
    return redirect(url_for("system.licence_admin"))
