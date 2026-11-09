"""Protected secret store (§12).

Values are encrypted with Fernet (cryptography library — no custom crypto)
using a key file stored OUTSIDE the database (``TO_SECRET_KEY_FILE``,
default ``instance/secret.key``, mode 0600). Only the reference *name*
appears in settings, snapshots, audit logs, or API responses. A missing key
or credential yields an actionable not-configured status — never a
fabricated success. Rotation touches metadata only.
"""

import json
import os
import secrets as _secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .extensions import db
from .execution import SecretRef


class SecretError(RuntimeError):
    pass


def _key_path(app) -> Path:
    return Path(
        os.environ.get(
            "TO_SECRET_KEY_FILE",
            str(Path(app.instance_path) / "secret.key"),
        )
    )


def _load_fernet(app) -> Fernet:
    path = _key_path(app)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, Fernet.generate_key())
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
    return Fernet(path.read_bytes().strip())


def store_secret(app, name: str, kind: str, value: str,
                 meta: dict = None, internal: bool = False) -> None:
    """Create or rotate a named credential. The plaintext is accepted,
    encrypted, and never persisted anywhere else."""
    fernet = _load_fernet(app)
    row = SecretRef.query.filter_by(name=name).first()
    if row is None:
        row = SecretRef(name=name, kind=kind)
        db.session.add(row)
    else:
        if row.kind in ("dagu", "runner") and not internal:
            raise SecretError(
                f"credential {name!r} is system-managed — it can only "
                "be rotated by its provisioning flow")
        if kind != row.kind and not internal:
            raise SecretError(
                f"credential {name!r} kind cannot change")
        from .execution import utcnow
        row.rotated_at = utcnow()
    row.kind = kind
    row.encrypted = fernet.encrypt(value.encode("utf-8"))
    row.meta_json = json.dumps(meta or {})
    db.session.commit()


def read_secret(app, name: str) -> str:
    """Resolve a credential reference. Raises SecretError — callers surface
    an actionable not-configured status instead of fabricating success."""
    row = SecretRef.query.filter_by(name=name).first()
    if row is None:
        raise SecretError(f"credential {name!r} is not configured")
    try:
        return _load_fernet(app).decrypt(row.encrypted).decode("utf-8")
    except (InvalidToken, OSError) as exc:
        raise SecretError(
            f"credential {name!r} cannot be decrypted (key rotated/lost)"
        ) from exc


def delete_secret(name: str) -> bool:
    row = SecretRef.query.filter_by(name=name).first()
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def list_secret_meta() -> list:
    """Metadata for Admin UI/audit — names, kinds, rotation; never bytes."""
    return [
        {
            "name": row.name,
            "kind": row.kind,
            "meta": json.loads(row.meta_json or "{}"),
            "created_at": row.created_at.isoformat() if row.created_at else "",
            "rotated_at": row.rotated_at.isoformat() if row.rotated_at else "",
        }
        for row in SecretRef.query.order_by(SecretRef.name).all()
    ]


def ensure_coordinator_credential(app) -> bool:
    """Provision the internal coordinator credential on fresh installs.

    Idempotent: returns False (and leaves the existing secret + its
    consumers untouched) when already provisioned — never regenerates
    on restart, repair, upgrade, or installer rerun. The value is
    never surfaced to settings, logs, exports, or command examples.
    """
    import secrets as _secrets
    name = "coordinator-agent"
    if SecretRef.query.filter_by(name=name).first() is not None:
        return False
    token = _secrets.token_urlsafe(32)
    store_secret(app, name, "dagu", token,
                 {"purpose": "coordinator enrollment", "auto": True},
                 internal=True)
    # Point the backend-managed ref at the provisioned credential so the
    # dispatch adapter resolves it — without user-visible wiring.
    from . import settings as settings_mod
    if not settings_mod.get("dagu_credential_ref"):
        settings_mod.update({"dagu_credential_ref": name})
    return True
