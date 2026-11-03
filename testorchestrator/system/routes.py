"""Admin -> System -> Network & certificates (v1.8 §3–§5).

All mutations are Admin-only, validated, staged through the Caddy renderer,
audited without secrets, and recoverable via previous_good_config.
"""

from flask import (current_app, flash, redirect, render_template,
                   send_file, url_for)
from flask_login import current_user

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
                    PortsForm)


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
