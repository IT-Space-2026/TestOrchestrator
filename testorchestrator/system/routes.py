"""Admin -> System -> Network & certificates (v1.8 §3–§5).

All mutations are Admin-only, validated, staged through the Caddy renderer,
audited without secrets, and recoverable via previous_good_config.
"""

import sys
from datetime import datetime, timezone

from flask import (abort, current_app, flash, redirect, render_template,
                   request, send_file, url_for)
from flask_login import current_user

from ..extensions import db

from .. import settings as settings_mod
from ..caddyconf import (CaddyApplyError, apply_config, render_caddyfile,
                         rollback_config)
from ..certs import (CertificateError, generate_local, import_pem)
from ..models import Role, record_audit
from ..network import (ValidationError, access_links, check_port_conflicts,
                       detect_address_candidates, parse_host, port_available,
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
    except CaddyApplyError as exc:
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
        result = rollback_config(current_app)
    except CaddyApplyError as exc:
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
    """Admin -> Reporting settings (consolidated §2). Configures the
    OPTIONAL Allure backend only — Java path, probe, readiness."""
    from .. import allure_runtime
    return render_template(
        "system/reporting.html",
        settings=settings_mod.all_settings(),
        allure=allure_runtime.status(),
    )


@system_bp.route("/integrations")
@role_required(Role.ADMIN)
def integrations_page():
    """Subsystem-separated integration config (Dagu orchestrator, Git
    evidence export). Each section is its own labelled card; secrets are
    referenced by name only."""
    return render_template("system/integrations.html",
                           settings=settings_mod.all_settings())


@system_bp.route("/integrations/save", methods=["POST"])
@role_required(Role.ADMIN)
def integrations_save():
    """Persist integration settings — names/paths only, never secret
    values. Applies to future operations; no restart needed."""
    from flask import request
    updates = {}
    for key in ("dagu_endpoint", "dagu_credential_ref", "dagu_username",
                "git_export_repo", "git_export_credential_ref",
                "git_export_ref"):
        if key in request.form:
            updates[key] = request.form.get(key, "").strip()
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
    java_home = request.form.get("allure_java_home", "").strip()
    settings_mod.update({
        "allure_enabled": bool(request.form.get("allure_enabled")),
        "allure_java_home": java_home,
    })
    record_audit(current_user.username, "reporting.config.saved",
                 "reporting settings updated (no values)")
    flash("Reporting settings saved. No runs were published.", "info")
    if java_home:
        # Selecting a runtime validates it immediately with an actual
        # generation probe — never assumed compatible from the path.
        from .. import allure_runtime
        rec = allure_runtime.select_and_validate(java_home)
        if rec["ok"]:
            flash(f"Java validated — generated a report with Allure "
                  f"{allure_runtime.version()} "
                  f"(Java {rec.get('java_version', '?')}).", "info")
        else:
            flash(f"Java at {java_home} FAILED the generation probe: "
                  f"{rec.get('error', '')} — Allure stays not-ready.",
                  "error")
    return redirect(url_for("system.reporting_page"))


@system_bp.route("/reporting/check", methods=["POST"])
@role_required(Role.ADMIN)
def reporting_check():
    """Readiness checks, reported verbatim — never a fabricated ready.
    The Java check is a REAL generation probe with the pinned bundled
    Allure, run as the service account would run it."""
    from .. import allure_runtime
    home, _src = allure_runtime.selected_java()
    rec = allure_runtime.select_and_validate(
        str(home) if home else "")
    a = allure_runtime.status()
    if a["ready"]:
        flash(f"Allure: ready — generator {a['version']}, Java "
              f"{rec.get('java_version', '?')} at {a['java_home']} "
              f"(probe {rec.get('checked_at', '')})", "info")
    else:
        flash(f"Allure: NOT ready — {a['reason']}", "error")
    return redirect(url_for("system.reporting_page"))


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
    ApplyForm().validate_on_submit()
    if delete_secret(name):
        record_audit(current_user.username, "secret.deleted", name)
    return redirect(url_for("system.secrets"))


# ---------- runners & runtime connections (§5–§6) ----------

def _dagu_worker_assets() -> list:
    """Bundled, pinned Dagu worker archives with manifest checksums —
    served from the installed package, not an external link."""
    import re
    from pathlib import Path
    d = Path(current_app.root_path).parent / "3rdparty" / "dagu"
    assets = []
    manifest = {}
    sums = d / "dagu.sha256"
    if sums.exists():
        for line in sums.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2:
                manifest[parts[1]] = parts[0]
    for path in sorted(d.glob("dagu_*_amd64.tar.gz")):
        m = re.match(r"dagu_(\d+\.\d+\.\d+)_(\w+)_amd64\.tar\.gz",
                     path.name)
        assets.append({
            "file": path.name,
            "version": m.group(1) if m else "?",
            "os": (m.group(2) if m else "?"),
            "arch": "amd64",
            "sha256": manifest.get(path.name, ""),
            "size": path.stat().st_size,
        })
    return assets


@system_bp.route("/infrastructure/dagu-worker/<filename>")
@role_required(Role.ADMIN)
def dagu_worker_download(filename):
    """Serve a bundled Dagu worker archive — pinned version, checksum
    published alongside on the page. No credentials in the URL."""
    import re
    from pathlib import Path
    if not re.fullmatch(
            r"dagu_\d+\.\d+\.\d+_(linux|windows)_amd64\.tar\.gz",
            filename):
        abort(404)
    path = (Path(current_app.root_path).parent
            / "3rdparty" / "dagu" / filename)
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

MANAGED_UNITS = ("testorchestrator-web", "testorchestrator-worker",
                 "testorchestrator-dagu", "testorchestrator-caddy")
_RESTART_FLAG = "restart-request.json"


def _service_status() -> dict:
    """Status of application-managed services — systemd units when the
    platform package installed them; otherwise an honest unmanaged
    report (isolated/dev installs have no service manager)."""
    import shutil, subprocess
    out = {"managed": shutil.which("systemctl") is not None,
           "services": []}
    for u in MANAGED_UNITS:
        if not out["managed"]:
            out["services"].append({"unit": u, "state": "unmanaged"})
            continue
        r = subprocess.run(["systemctl", "is-active", u + ".service"],
                           capture_output=True, text=True)
        state = (r.stdout.strip() or "unknown") if r.returncode == 0 \
            else (r.stdout.strip() or "inactive")
        out["services"].append({"unit": u, "state": state})
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


@system_bp.route("/health")
@role_required(Role.ADMIN)
def system_health():
    status = _service_status()
    marker = _restart_marker()
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
    import json as _j, subprocess
    status = _service_status()
    if not status["managed"]:
        flash("No managed services — this installation runs without "
              "systemd units (isolated/dev mode). Nothing to restart.",
              "error")
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
    units = [s["unit"] + ".service" for s in status["services"]]
    marker.write_text(_j.dumps({
        "requested_by": current_user.username,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "units": units, "result": "pending"}))
    record_audit(current_user.username, "services.restart_requested",
                 ", ".join(units))
    # Independent completion: systemd performs the restart; the helper
    # only reports the outcome into the marker afterwards.
    helper = (
        "import json,subprocess,sys,time\n"
        f"m={str(marker)!r}\n"
        "time.sleep(2)\n"
        f"units={units!r}\n"
        "r=subprocess.run(['systemctl','restart',*units])\n"
        "ok=r.returncode==0\n"
        "for u in units:\n"
        "    subprocess.run(['systemctl','is-active','--quiet',u])\n"
        "d=json.loads(m.read_text())\n"  # noqa: E501
        "d['result']='done' if ok else 'failed'\n"
        "m.write_text(json.dumps(d))\n"
        "sys.exit(0 if ok else 1)\n")
    try:
        subprocess.Popen(
            [sys.executable or "python3", "-c", helper],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        marker.unlink(missing_ok=True)
        flash(f"Restart could not be detached: {exc}", "error")
        return redirect(url_for("system.system_health"))
    flash("Restart requested — services will be restarted by systemd. "
          "The browser will disconnect briefly; reconnect and check "
          "System health for the result.", "info")
    return redirect(url_for("system.system_health"))


@system_bp.route("/infrastructure")
@role_required(Role.ADMIN)
def infrastructure():
    from ..execution import Runner, RuntimeConnection
    from ..execution import Project
    record_audit(current_user.username, "page.infrastructure")
    runner_form = RunnerForm()
    runtime_form = RuntimeForm()
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
    return render_template(
        "system/infrastructure.html",
        runners=runners, runtimes=runtimes,
        per_runner=per_runner, unbound=unbound,
        fixture_runners=fixture_runners,
        runner_form=runner_form, runtime_form=runtime_form,
        runner_choices=[(r.id, r.name) for r in Runner.query.all()],
        dagu_assets=_dagu_worker_assets(),
    )


@system_bp.route("/infrastructure/runners", methods=["POST"])
@role_required(Role.ADMIN)
def create_runner():
    import json
    from ..execution import Project, Runner
    form = RunnerForm()
    form.allowed_projects.choices = [
        (p.id, p.name) for p in Project.query.all()]
    if form.validate_on_submit():
        db.session.add(Runner(
            name=form.name.data.strip(),
            description=form.description.data or "",
            os_arch=form.os_arch.data or "",
            connection_mode=form.connection_mode.data,
            workspace_root=form.workspace_root.data or "",
            max_slots=form.max_slots.data or 1,
            allowed_projects=json.dumps(form.allowed_projects.data or []),
        ))
        db.session.commit()
        record_audit(current_user.username, "runner.created",
                     form.name.data.strip())
    return redirect(url_for("system.infrastructure"))


@system_bp.route("/infrastructure/runtimes", methods=["POST"])
@role_required(Role.ADMIN)
def create_runtime():
    from ..execution import Runner, RuntimeConnection
    form = RuntimeForm()
    is_remote = request.form.get("exec_location") == "remote"
    worker_label = (form.worker_label.data or "").strip()
    if is_remote:
        # A remote installation is bound to exactly one Dagu worker —
        # the label must match the worker's --worker.labels kre=<tag>
        # and must be unique across installations (dispatch refuses
        # ambiguous matches).
        if not worker_label:
            flash("Remote registration requires the Dagu worker label "
                  "— the value passed to the worker via "
                  "--worker.labels kre=<label>.", "error")
            return redirect(url_for("system.infrastructure"))
        clash = RuntimeConnection.query.filter_by(
            worker_label=worker_label, enabled=True).first()
        if clash:
            flash(f"Worker label {worker_label!r} is already bound to "
                  f"installation {clash.name!r} — labels must be "
                  "unique per installation.", "error")
            return redirect(url_for("system.infrastructure"))
    _runner = Runner.query.filter_by(
        connection_mode=("remote" if is_remote else "local")).first()
    if _runner is None:
        _runner = Runner(
            name=(f"dagu-worker:{worker_label}" if is_remote
                  else "local"),
            connection_mode=("remote" if is_remote else "local"),
            os_arch=("dagu-worker" if is_remote else "local"),
            max_slots=4)
        db.session.add(_runner); db.session.flush()
    if form.validate_on_submit():
        db.session.add(RuntimeConnection(
            name=form.name.data.strip(),
            description=form.description.data or "",
            runner_id=_runner.id,
            transport=("dagu-worker" if is_remote else "local"),
            executable_path=form.executable_path.data.strip(),
            workspace_root=form.workspace_root.data or "",
            credential_ref=(form.credential_ref.data or "").strip(),
            worker_label=(worker_label if is_remote else ""),
            concurrency=form.concurrency.data or 1,
        ))
        db.session.commit()
        record_audit(current_user.username, "runtime.registered",
                     form.name.data.strip())
    return redirect(url_for("system.infrastructure"))


@system_bp.route("/infrastructure/runtimes/<runtime_id>/probe",
                 methods=["POST"])
@role_required(Role.ADMIN)
def probe_runtime(runtime_id):
    rt = db.session.get(RuntimeConnection, runtime_id)
    if rt and rt.runner and rt.runner.connection_mode != "local":
        flash("Remote installation — the portal-host check cannot "
              "verify a remote worker's KRE. Remote verification "
              "through the agent is not implemented.", "error")
        return redirect(url_for("system.infrastructure"))
        return redirect(url_for("system.infrastructure"))
    """Test connection: executable presence + version probe only. Does not
    install anything, change licensing, or run project tests (§5)."""
    from ..adapters.kre import KreError, probe_version
    from ..execution import RuntimeConnection
    ApplyForm().validate_on_submit()
    rt = db.session.get(RuntimeConnection, runtime_id)
    if rt is None:
        abort(404)
    try:
        version = probe_version(rt.executable_path)
        rt.detected_version = version
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


@system_bp.route("/infrastructure/runners/<runner_id>/issue-token",
                 methods=["POST"])
@role_required(Role.ADMIN)
def issue_token(runner_id):
    """Issue a new agent bearer token for this runner (§6, §12). The
    plaintext is shown ONCE in the flash below — only its SHA-256 digest
    is persisted. Re-issuing replaces the token (old agents lose access).
    Required for BOTH modes: the Dagu-launched local agent and the
    remote agent daemon authenticate with it."""
    from ..agent_api import issue_agent_token
    from ..execution import Runner
    ApplyForm().validate_on_submit()
    runner = db.session.get(Runner, runner_id)
    if runner is None:
        abort(404)
    token = issue_agent_token(runner)
    record_audit(current_user.username, "runner.agent_token_issued",
                 runner.name)
    flash(
        f"Agent token for {runner.name} — copy it now, it is never stored "
        f"or shown again: {token}", "info")
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
    if (runner.os_arch != old_arch or
            runner.workspace_root != old_ws):
        from ..execution import RuntimeConnection
        for rt in RuntimeConnection.query.filter_by(
                runner_id=runner.id):
            rt.detected_version = ""
            from ..execution import utcnow
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


