"""Caddy configuration renderer + staged apply (v1.8 §4–§5).

Renders a Caddyfile purely from stored settings — never from arbitrary user
strings (no Caddyfile injection) and never reflects the request Host. Only
the explicitly selected listeners are opened: HTTPS on the configured port,
plus the HTTP redirect-only listener when ``http_mode == 'redirect'`` (its
target is the configured HTTPS address, not the inbound Host).

Staged apply: render candidate -> `caddy validate` with the bundled binary ->
on success atomically install as active and snapshot the previous active
config into settings.previous_good_config (rollback: `flask caddy-rollback`
or the Admin UI button). Apply failures leave the last good config running.
"""

import shutil
import subprocess
from pathlib import Path

from . import settings as settings_mod
from .network import bracket_host, build_url, effective_allowed_hosts


class CaddyApplyError(RuntimeError):
    def __init__(self, message, stderr=""):
        super().__init__(message)
        self.stderr = stderr


def tls_directive(settings: dict, cert_dir: Path) -> str:
    """`tls` directive for the selected certificate mode.

    Manual-certificate pattern per Caddy docs:
        tls /protected/fullchain.pem /protected/private-key.pem
    Paths come from our managed cert dir — never arbitrary user input.
    """
    mode = settings["certificate_mode"]
    if mode in ("local", "imported"):
        chain = cert_dir / "fullchain.pem"
        key = cert_dir / "private-key.pem"
        return f"tls {chain} {key}"
    # Unset/fallback: Caddy internal CA (unique per installation data dir).
    return "tls internal"


def render_caddyfile(settings: dict, cert_dir: Path, app_addr: str) -> str:
    https_port = int(settings["https_port"])
    http_port = int(settings["http_port"])
    local_host = settings["local_host"]
    tls = tls_directive(settings, cert_dir)

    hosts = [local_host]
    if settings["external_enabled"] and settings["external_host"]:
        hosts.append(settings["external_host"])

    site_labels = ", ".join(
        f"https://{bracket_host(h)}:{https_port}"
        if https_port != 443 else f"https://{bracket_host(h)}"
        for h in hosts
    )

    blocks = [
        "{\n\tadmin off\n\tauto_https disable_redirects\n}",
        f"{site_labels} {{\n\t{tls}\n\treverse_proxy {app_addr}\n\t"
        "header {\n\t\tX-Content-Type-Options nosniff\n\t\t"
        "Referrer-Policy same-origin\n\t\t-Server\n\t}\n}",
    ]

    if settings["http_mode"] == "redirect":
        # Redirect-only listener: never serves login or mutations (NET04).
        # The target is the configured HTTPS address — not the Host header.
        target = build_url(local_host, https_port, "https")
        redirect_labels = ", ".join(
            f"http://{bracket_host(h)}:{http_port}" for h in hosts
        )
        blocks.append(
            f"{redirect_labels} {{\n\t"
            f"redir {target.rstrip('/')}{{uri}} permanent\n}}"
        )
    # 'disabled' and 'development-insecure' modes: no HTTP block is emitted —
    # only explicitly selected listeners exist in the rendered config.

    return "\n\n".join(blocks) + "\n"


def validate_caddyfile(caddy_bin: str, candidate: Path) -> tuple:
    """Run `caddy validate` against a candidate file. Returns (ok, output)."""
    bin_path = Path(caddy_bin)
    if not bin_path.exists():
        return False, f"caddy binary not found at {bin_path}"
    try:
        proc = subprocess.run(
            [str(bin_path), "validate", "--config", str(candidate),
             "--adapter", "caddyfile"],
            capture_output=True, text=True, timeout=30,
        )
        output = (proc.stdout + proc.stderr).strip()
        return proc.returncode == 0, output
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def apply_config(app, settings: dict) -> dict:
    """Stage, validate, and install a new Caddyfile.

    Returns a result dict for the UI/audit log (no secrets). On validation
    failure the active configuration is untouched and the previous good
    snapshot remains for rollback (NET05).
    """
    config_dir: Path = app.config["CADDY_CONFIG_DIR"]
    cert_dir: Path = app.config["CERT_DIR"]
    app_addr = f"{settings['internal_app_host']}:{settings['internal_app_port']}"

    rendered = render_caddyfile(settings, cert_dir, app_addr)
    config_dir.mkdir(parents=True, exist_ok=True)
    candidate = config_dir / "Caddyfile.candidate"
    active = config_dir / "Caddyfile"
    candidate.write_text(rendered, encoding="utf-8")

    ok, output = validate_caddyfile(app.config["CADDY_BIN"], candidate)
    if not ok:
        candidate.unlink(missing_ok=True)
        raise CaddyApplyError("caddy validate rejected the staged config", output)

    if active.exists():
        shutil.copy2(active, config_dir / "Caddyfile.previous")
    active.write_text(rendered, encoding="utf-8")
    candidate.unlink(missing_ok=True)

    snapshot = {
        "https_port": settings["https_port"],
        "http_port": settings["http_port"],
        "http_mode": settings["http_mode"],
        "local_host": settings["local_host"],
        "external_enabled": settings["external_enabled"],
        "external_host": settings["external_host"],
        "certificate_mode": settings["certificate_mode"],
        "network_config_version": settings.get("network_config_version", 0),
    }
    settings_mod.set("previous_good_config", snapshot)
    settings_mod.set(
        "network_config_version",
        int(settings.get("network_config_version", 0)) + 1,
    )
    settings_mod.db.session.commit()

    return {
        "status": "applied",
        "config": str(active),
        "validate_output": output,
        "version": settings_mod.get("network_config_version"),
    }


def rollback_config(app) -> dict:
    """Restore the last known good snapshot + Caddyfile (local recovery)."""
    prev = settings_mod.get("previous_good_config") or {}
    config_dir: Path = app.config["CADDY_CONFIG_DIR"]
    previous_file = config_dir / "Caddyfile.previous"
    if not prev or not previous_file.exists():
        raise CaddyApplyError("no previous good configuration to roll back to")

    shutil.copy2(previous_file, config_dir / "Caddyfile")
    for key, value in prev.items():
        settings_mod.set(key, value)
    settings_mod.set(
        "network_config_version",
        int(settings_mod.get("network_config_version", 0)) + 1,
    )
    settings_mod.db.session.commit()
    return {"status": "rolled-back", "restored": prev}
