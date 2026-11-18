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
        if row.kind in ("buildbot", "runner") and not internal:
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
    """Provision the internal Buildbot API credential on fresh installs.

    Idempotent: returns False (and leaves the existing secret + its
    consumers untouched) when already provisioned — never regenerates
    on restart, repair, upgrade, or installer rerun. When the
    installer already provisioned ``buildbot_home/secrets.json`` its
    api_password is ADOPTED so the master's auth and the portal's
    SecretRef agree without an admin command; otherwise a fresh token
    is generated (the next targets sync writes it into secrets.json
    and reconfigures the master). The value is never surfaced to
    settings, logs, exports, or command examples.
    """
    name = "buildbot-api"
    if SecretRef.query.filter_by(name=name).first() is not None:
        return False
    # Concurrency: the name is UNIQUE — a simultaneous first-provisioning
    # attempt commits first and makes OUR check-then-create raise
    # IntegrityError. Recheck after rollback: the row now exists → we
    # resolve to it (no duplicate, no overwrite) instead of failing.
    try:
        return _provision(app, name)
    except Exception:
        db.session.rollback()
        if SecretRef.query.filter_by(name=name).first() is not None:
            return False            # the other attempt won — adopt it
        raise
    # (reached only when the row is still absent)


def _provision(app, name):
    """Create the Buildbot API credential + wire the credential ref
    (called once by ensure_coordinator_credential under the
    race-recheck guard).

    INSERT-ONLY: builds the SecretRef directly instead of the upserting
    store_secret, so a UNIQUE-name conflict raises IntegrityError — the
    caller's guard rolls back + adopts the winner's credential instead
    of silently overwriting it. Never rotates an existing row."""
    import secrets as _secrets
    from pathlib import Path
    from . import settings as settings_mod
    from .execution import SecretRef
    password = None
    home = settings_mod.get("buildbot_home") or ""
    secrets_file = Path(home) / "secrets.json" if home else None
    if secrets_file and secrets_file.exists():
        try:
            password = (json.loads(secrets_file.read_text())
                        .get("api_password") or None)
        except Exception:
            password = None        # unreadable — generate fresh instead
    fernet = _load_fernet(app)
    row = SecretRef(
        name=name, kind="buildbot",
        encrypted=fernet.encrypt(
            (password or _secrets.token_urlsafe(32)).encode()),
        meta_json=json.dumps({"purpose": "buildbot REST API basic-auth "
                              "password", "auto": True}))
    db.session.add(row)
    db.session.commit()            # UNIQUE(name) -> IntegrityError if racing
    if not settings_mod.get("buildbot_credential_ref"):
        settings_mod.update({"buildbot_credential_ref": name})


def ensure_internal_worker_credentials(app) -> int:
    """Adopt the installer-provisioned controller-worker passwords.

    ``provision_buildbot`` writes ``worker_passwords`` into
    ``buildbot_home/secrets.json`` and bakes the same values into each
    worker's ``buildbot.tac``. ``buildbot_cfg.sync`` sources internal
    workers from the SecretRef store — without adoption the first
    sync would generate fresh passwords and lock the already-running
    workers out of the master ("unauthorized login"). INSERT-ONLY per
    ref: existing SecretRefs always win; missing secrets.json values
    are skipped (sync generates + stores them, and provision then
    owns the tac side). Returns the number of refs adopted."""
    import json
    from pathlib import Path
    from . import settings as settings_mod
    home = settings_mod.get("buildbot_home") or ""
    secrets_file = Path(home) / "secrets.json" if home else None
    if not secrets_file or not secrets_file.exists():
        return 0
    try:
        worker_passwords = (json.loads(secrets_file.read_text())
                            .get("worker_passwords") or {})
    except Exception:
        return 0                  # unreadable — sync regenerates instead
    adopted = 0
    fernet = _load_fernet(app)
    for name, password in worker_passwords.items():
        if not password:
            continue
        ref = f"buildbot-worker-{name}"
        if SecretRef.query.filter_by(name=ref).first() is not None:
            continue              # existing ref wins — never overwrite
        db.session.add(SecretRef(
            name=ref, kind="buildbot",
            encrypted=fernet.encrypt(password.encode()),
            meta_json=json.dumps({"purpose": f"internal worker {name}",
                                  "auto": True})))
        adopted += 1
    if adopted:
        db.session.commit()
    return adopted
    return True