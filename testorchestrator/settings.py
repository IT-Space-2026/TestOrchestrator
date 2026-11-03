"""Application settings store (v1.8 §6 schema).

Key/value JSON persisted in the database — survives upgrades, restarts,
reinstalls that preserve data (BOOT04). Passwords and private-key bytes are
never stored here; ``certificate_reference``/``certificate_public_metadata``
only hold paths and public certificate fields.
"""

import json

from .extensions import db


class Setting(db.Model):
    __tablename__ = "settings"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text, nullable=False)


DEFAULTS = {
    # Durable setup flag: bootstrap completes only after the Admin password
    # is replaced. Never reset by upgrades/restarts/restores.
    "setup_completed": False,
    # Local access (default https://localhost, HTTPS port 443 omitted in URLs).
    "local_host": "localhost",
    # External/LAN-facing address: disabled until bootstrap completes AND an
    # Admin explicitly enables it. IP literal or DNS hostname only.
    "external_enabled": False,
    "external_host": "",
    "bind_addresses": ["127.0.0.1", "::1"],
    "allowed_hosts": ["localhost", "127.0.0.1", "::1"],
    "advertised_base_url": "https://localhost",
    # Ports (v1.8 §4): public HTTPS 443, public HTTP 8080 (redirect-only),
    # private Waitress loopback 18080.
    "https_port": 443,
    "http_port": 8080,
    "http_mode": "redirect",  # redirect | disabled | development-insecure
    "internal_app_host": "127.0.0.1",
    "internal_app_port": 18080,
    # Certificate setup: "" (unset) | "local" | "imported".
    "certificate_mode": "",
    "certificate_reference": "",
    "certificate_public_metadata": {},
    # Staged-apply bookkeeping: bump on every committed network change.
    "network_config_version": 0,
    # Snapshot of the last successfully applied config for rollback.
    "previous_good_config": {},
    # Integration endpoints (handoff §5–§9, §11). Empty = not configured;
    # adapters report an actionable not-configured status, never a fake
    # success. Values are endpoints/references — secrets live in SecretRef.
    "dagu_endpoint": "",
    "dagu_credential_ref": "",
    # auth.mode: basic username (password lives in the SecretRef). Empty =
    # Bearer token mode (builtin auth API key).
    "dagu_username": "",
    "reportportal_endpoint": "",
    "reportportal_project": "",
    "reportportal_credential_ref": "",
    "git_export_repo": "",
    "git_export_credential_ref": "",
    "git_export_ref": "main",
}


def get(key, default=None):
    row = db.session.get(Setting, key)
    if row is None:
        return DEFAULTS.get(key, default)
    return json.loads(row.value)


def set(key, value) -> None:  # noqa: A001 - deliberate settings API name
    row = db.session.get(Setting, key)
    if row is None:
        row = Setting(key=key)
        db.session.add(row)
    row.value = json.dumps(value)


def all_settings() -> dict:
    """All settings with schema defaults applied (read-only snapshot)."""
    merged = dict(DEFAULTS)
    for row in Setting.query.all():
        merged[row.key] = json.loads(row.value)
    return merged


def update(values: dict) -> None:
    for key, value in values.items():
        if key not in DEFAULTS:
            raise KeyError(f"unknown setting: {key}")
        set(key, value)
    db.session.commit()


def setup_completed() -> bool:
    return bool(get("setup_completed"))


def mark_setup_complete() -> None:
    set("setup_completed", True)
    db.session.commit()
