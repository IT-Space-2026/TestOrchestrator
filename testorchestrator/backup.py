"""Settings backup & restore (2.6 scope: settings only).

One encrypted ``.tobackup`` container per backup:

    TOBK | u32le header_len | header_json | AES-256-GCM ciphertext

- ``header_json`` is plaintext (needed before decryption): format version,
  creating app version, db revision, platform, timestamp, KDF parameters
  (scrypt salt/n/r/p/length) and the AEAD nonce. It is bound to the
  ciphertext as GCM additional-authenticated-data, so header edits fail
  the authentication tag.
- The plaintext payload is a single JSON document: ``manifest`` (section
  names, row counts, per-section/per-file SHA-256, exclusion list),
  ``sections`` (row dicts per config table) and ``files`` (currently only
  ``secret.key`` — the Fernet key travels INSIDE the encrypted payload so
  restored secrets stay decryptable on the new host).
- No tar/zip: the whole payload is one bounded JSON document, so there is
  no archive extraction surface (no traversal/links/bombs); section names
  and file names are validated against a fixed allowlist anyway.
- Credentials never appear in plaintext outside the AEAD envelope. The
  passphrase is never stored or logged. Anyone holding file+passphrase
  can read the included secrets — documented in the Admin Manual.

Excluded by design (see ``EXCLUDED``): executions/test_results/artifacts/
outbox/import sessions (evidence & history stay with the instance), audit
events, api_tokens/submit_tokens (revoked, not resurrected), resource
reservations/locks, session data, exports, logs, temp files, TLS cert
*files* (host-specific), the SQLite db file itself, and live process
state.
"""

import base64
import enum
import hashlib
import json
import os
import platform
import secrets as _secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .extensions import db

FORMAT_VERSION = 1
MAGIC = b"TOBK"
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024   # aligned with MAX_CONTENT_LENGTH
MAX_HEADER_BYTES = 64 * 1024
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_ROWS_PER_SECTION = 100_000
# scrypt parameters — stored in the header; these defaults are the
# interactive-login strength recommended by RFC 7914-era guidance
# (n=2^15, r=8, p=1 ⇒ ~32 MiB, <1s on modest hardware).
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2 ** 15, 8, 1, 32
MIN_PASSPHRASE = 12
SETUP_TOKEN_TTL = 3600          # one-time fresh-install token lifetime
STAGE_TTL = 1800                # staged restore file lifetime
REAUTH_WINDOW = 300             # recent-auth window for sensitive actions


class BackupError(RuntimeError):
    pass


# ---------------------------------------------------------------- tables --

def _config_models():
    """Config tables in FK-safe insert order (parents before children).
    Imported lazily so this module stays importable pre-app."""
    from . import execution as ex
    from .models import User
    from .settings import Setting
    return [
        ("settings", Setting),
        ("users", User),
        ("projects", ex.Project),
        ("runners", ex.Runner),
        ("runtime_connections", ex.RuntimeConnection),
        ("git_bindings", ex.GitBinding),
        ("suites", ex.Suite),
        ("suite_entries", ex.SuiteEntry),
        ("plans", ex.Plan),
        ("plan_entries", ex.PlanEntry),
        ("releases", ex.Release),
        ("release_items", ex.ReleaseItem),
        ("schedules", ex.Schedule),
        ("secret_refs", ex.SecretRef),
    ]


ALLOWED_FILES = ("secret.key",)

EXCLUDED = [
    ("executions/test_results/artifacts",
     "immutable evidence & run history — stays with the instance"),
    ("outbox_events/import_sessions/resource_reservations",
     "live process state — meaningless on another host"),
    ("api_tokens/submit_tokens",
     "bearer credentials are revoked on restore, never resurrected"),
    ("audit_events", "audit history is never rewritten by a restore"),
    ("TLS certificate files, exports, logs, session data, locks",
     "host-specific or temporary — regenerated on the new host"),
]


def _cell(value):
    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    if isinstance(value, enum.Enum):
        return {"__enum__": value.value}
    if isinstance(value, (bytes, bytearray)):
        return {"__b64__": base64.b64encode(bytes(value)).decode()}
    return value


def _restore_cell(value, col_type):
    if not isinstance(value, dict):
        return value
    if "__dt__" in value:
        return datetime.fromisoformat(value["__dt__"])
    if "__enum__" in value:
        return col_type.enum_class(value["__enum__"])
    if "__b64__" in value:
        return base64.b64decode(value["__b64__"])
    return value


def _row_dict(row):
    return {c.name: _cell(getattr(row, c.name)) for c in row.__table__.columns}


def _db_revision() -> str:
    try:
        from sqlalchemy import text
        row = db.session.execute(
            text("SELECT version_num FROM alembic_version")).first()
        return row[0] if row else "unknown"
    except Exception:
        return "unknown"


def _app_version(app) -> str:
    return str(app.config.get("RELEASE_METADATA", {}).get("version") or "dev")


def _fernet_key_path(app) -> Path:
    from .secrets_store import _key_path
    return _key_path(app)


# ------------------------------------------------------------- manifest ---

def section_counts() -> dict:
    """Live counts per section — powers the pre-download preview."""
    return {name: model.query.count() for name, model in _config_models()}


def _payload(app) -> dict:
    """Consistent snapshot: all config rows read inside ONE transaction,
    so a concurrent edit cannot produce a half-saved file."""
    sections, manifest_sections = {}, {}
    # The surrounding request already holds a transaction; pinning the
    # connection keeps every SELECT on the same snapshot.
    for name, model in _config_models():
        rows = [_row_dict(r) for r in model.query.all()]
        if len(rows) > MAX_ROWS_PER_SECTION:
            raise BackupError(f"section {name} exceeds row bound")
        blob = json.dumps(rows, sort_keys=True).encode()
        sections[name] = rows
        manifest_sections[name] = {
            "count": len(rows), "sha256": hashlib.sha256(blob).hexdigest()}
    files, manifest_files = {}, {}
    key_path = _fernet_key_path(app)
    if key_path.exists():
        raw = key_path.read_bytes()
        files["secret.key"] = base64.b64encode(raw).decode()
        manifest_files["secret.key"] = hashlib.sha256(raw).hexdigest()
    return {
        "manifest": {
            "format_version": FORMAT_VERSION,
            "app_version": _app_version(app),
            "db_revision": _db_revision(),
            "platform": f"{platform.system()}/{platform.machine()}",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "sections": manifest_sections,
            "files": manifest_files,
            "excluded": [{"items": i, "reason": r} for i, r in EXCLUDED],
        },
        "sections": sections,
        "files": files,
    }


# ---------------------------------------------------------------- crypto --

def _derive(passphrase: str, kdf: dict) -> bytes:
    if kdf.get("name") != "scrypt":
        raise BackupError("unsupported KDF")
    return Scrypt(salt=base64.b64decode(kdf["salt"]), length=kdf["length"],
                  n=kdf["n"], r=kdf["r"], p=kdf["p"]).derive(
                      passphrase.encode())


def create_backup(app, passphrase: str) -> bytes:
    if len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(f"passphrase must be ≥{MIN_PASSPHRASE} characters")
    payload = json.dumps(_payload(app), separators=(",", ":")).encode()
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise BackupError("payload exceeds size bound")
    salt, nonce = os.urandom(16), os.urandom(12)
    header = {
        "format_version": FORMAT_VERSION,
        "app_version": _app_version(app),
        "db_revision": _db_revision(),
        "platform": f"{platform.system()}/{platform.machine()}",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "kdf": {"name": "scrypt", "salt": base64.b64encode(salt).decode(),
                "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
                "length": SCRYPT_LEN},
        "cipher": "AES-256-GCM",
        "nonce": base64.b64encode(nonce).decode(),
    }
    hbytes = json.dumps(header, separators=(",", ":")).encode()
    key = _derive(passphrase, header["kdf"])
    ct = AESGCM(key).encrypt(nonce, payload, hbytes)
    return MAGIC + len(hbytes).to_bytes(4, "little") + hbytes + ct


def parse_header(data: bytes) -> dict:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise BackupError("file exceeds maximum size")
    if len(data) < 8 or data[:4] != MAGIC:
        raise BackupError("not a TestOrchestrator backup file")
    hlen = int.from_bytes(data[4:8], "little")
    if hlen < 1 or hlen > MAX_HEADER_BYTES or 8 + hlen >= len(data):
        raise BackupError("corrupt header length")
    try:
        header = json.loads(data[8:8 + hlen])
    except ValueError:
        raise BackupError("corrupt header")
    if header.get("format_version", 0) > FORMAT_VERSION:
        raise BackupError(
            f"unsupported format version {header['format_version']} — this "
            f"application reads up to v{FORMAT_VERSION}; upgrade first")
    if header.get("cipher") != "AES-256-GCM":
        raise BackupError("unsupported cipher")
    header["_offset"] = 8 + hlen
    return header


def decrypt_backup(data: bytes, passphrase: str) -> dict:
    """Authenticate + decrypt + verify per-section checksums."""
    header = parse_header(data)
    off = header.pop("_offset")
    hbytes = data[8:off]
    try:
        key = _derive(passphrase, header["kdf"])
        raw = AESGCM(key).decrypt(
            base64.b64decode(header["nonce"]), data[off:], hbytes)
    except (InvalidTag, ValueError):
        raise BackupError(
            "decryption failed — wrong passphrase or the file was modified")
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise BackupError("payload exceeds size bound")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise BackupError("corrupt payload")
    manifest = payload.get("manifest", {})
    for name, meta in manifest.get("sections", {}).items():
        rows = payload.get("sections", {}).get(name)
        if rows is None:
            raise BackupError(f"manifest section {name} missing from payload")
        if hashlib.sha256(json.dumps(
                rows, sort_keys=True).encode()).hexdigest() != meta["sha256"]:
            raise BackupError(f"checksum mismatch in section {name}")
    for fname in payload.get("files", {}):
        if fname not in ALLOWED_FILES:
            raise BackupError(f"unexpected file entry {fname!r} in archive")
    return payload


def backup_filename(app) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (f"testorchestrator-settings-{_app_version(app)}-"
            f"{stamp}.tobackup")


# ------------------------------------------------------- maintenance flag --

def _maintenance_flag(app) -> Path:
    return Path(app.instance_path) / "MAINTENANCE"


def maintenance_active(app) -> bool:
    return _maintenance_flag(app).exists()


def _enter_maintenance(app):
    _maintenance_flag(app).write_text(json.dumps(
        {"since": datetime.now(timezone.utc).isoformat()}))


def _exit_maintenance(app):
    _maintenance_flag(app).unlink(missing_ok=True)


# ------------------------------------------------------------ setup token --

def _setup_token_path(app) -> Path:
    return Path(app.instance_path) / ".restore-setup-token"


def unconfigured() -> bool:
    """True while first-run setup has not completed — the bootstrap admin
    row already exists by then, so user count is NOT the signal."""
    from . import settings as settings_mod
    return not settings_mod.setup_completed()


def ensure_setup_token(app) -> None:
    """One-time, local, time-limited authorize-restore credential for a
    fresh install (setup not yet completed). Written mode-0600 — only the
    local OS account running the installer can read it. Consumed on use
    and removed once setup completes."""
    try:
        if not unconfigured():
            _setup_token_path(app).unlink(missing_ok=True)
            return
    except Exception:
        return                       # db not migrated yet — first boot
    path = _setup_token_path(app)
    if path.exists():
        try:
            _, exp = path.read_text().strip().split("|", 1)
            if float(exp) > time.time():
                return               # still valid
        except ValueError:
            pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"{_secrets.token_urlsafe(32)}|{time.time() + SETUP_TOKEN_TTL}")


def check_setup_token(app, token: str) -> bool:
    path = _setup_token_path(app)
    if not token or not path.exists():
        return False
    try:
        stored, exp = path.read_text().strip().split("|", 1)
        return (float(exp) > time.time()
                and _secrets.compare_digest(stored, token))
    except ValueError:
        return False


def consume_setup_token(app) -> None:
    _setup_token_path(app).unlink(missing_ok=True)


# ---------------------------------------------------------------- staging --

def _staging_dir(app) -> Path:
    d = Path(app.instance_path) / "restore-staging"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def stage_backup(app, data: bytes) -> str:
    """Park the ENCRYPTED upload in a protected short-lived location —
    the plaintext payload never touches disk."""
    stage_id = _secrets.token_hex(16)
    path = _staging_dir(app) / f"{stage_id}.tobackup"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return stage_id


def staged_path(app, stage_id: str) -> Path:
    if not stage_id or not all(c in "0123456789abcdef" for c in stage_id):
        raise BackupError("invalid staged id")
    path = _staging_dir(app) / f"{stage_id}.tobackup"
    if not path.exists():
        raise BackupError("staged backup expired — upload it again")
    if time.time() - path.stat().st_mtime > STAGE_TTL:
        path.unlink(missing_ok=True)
        raise BackupError("staged backup expired — upload it again")
    return path


def cleanup_staging(app) -> None:
    """Remove stale staged files — called at boot and on apply."""
    d = Path(app.instance_path) / "restore-staging"
    if not d.exists():
        return
    for p in d.glob("*.tobackup"):
        if time.time() - p.stat().st_mtime > STAGE_TTL:
            p.unlink(missing_ok=True)


# ----------------------------------------------------------------- apply --

def _rearm_schedule(row: dict, now: datetime) -> str:
    """Explicit in-flight/schedule handling: an enabled schedule whose
    fire time passed while the system was down is re-armed into the
    future — it never catch-up storms immediately after restore."""
    nxt = row.get("next_run_at")
    if not row.get("enabled") or not isinstance(nxt, dict):
        return ""
    when = datetime.fromisoformat(nxt["__dt__"])
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if when > now:
        return ""
    if row.get("kind") == "interval" and row.get("interval_minutes"):
        from datetime import timedelta
        row["next_run_at"] = _cell(
            now + timedelta(minutes=row["interval_minutes"]))
        return "re-armed to next interval"
    row["enabled"] = False
    return "disabled — its single fire time passed during the outage"


def apply_restore(app, payload: dict, mode: str,
                  actor: str) -> dict:
    """Row-level restore in ONE transaction — evidence/history tables are
    never touched; a failure rolls everything back atomically."""
    if mode not in ("fresh", "replace"):
        raise BackupError("unknown restore mode")
    from . import execution as ex
    from .models import User, record_audit
    now = datetime.now(timezone.utc)
    report = {"restored": {}, "skipped": [], "attention": []}
    sections = payload["sections"]
    valid_names = {n for n, _ in _config_models()}
    unknown = [n for n in sections if n not in valid_names]
    if unknown:
        report["skipped"].append(
            f"unrecognized sections ignored: {', '.join(unknown)}")

    users = sections.get("users", [])
    if not any((u.get("role") == "admin" or
                (isinstance(u.get("role"), dict) and
                 u["role"].get("__enum__") == "admin"))
               and u.get("is_active", True) for u in users):
        raise BackupError(
            "backup contains no active Administrator — refusing; a "
            "restore must leave at least one working admin. Break-glass: "
            "reinstall to bootstrap a fresh admin instead.")

    _enter_maintenance(app)
    try:
        for name, model in _config_models():
            rows = sections.get(name)
            if rows is None:
                report["skipped"].append(f"{name}: absent in backup")
                continue
            model.query.delete()
            cols = {c.name: c for c in model.__table__.columns}
            inserted = 0
            for raw_row in rows[:MAX_ROWS_PER_SECTION]:
                clean = {}
                for k, v in raw_row.items():
                    if k in cols:
                        clean[k] = _restore_cell(v, cols[k].type)
                if name == "schedules":
                    note = _rearm_schedule(raw_row, now)
                    if note:
                        report["attention"].append(
                            f"schedule {raw_row.get('name', '?')}: {note}")
                        # re-arm already mutated raw_row — re-serialize
                        clean = {k: _restore_cell(v, cols[k].type)
                                 for k, v in raw_row.items() if k in cols}
                db.session.add(model(**clean))
                inserted += 1
            report["restored"][name] = inserted

        # ---- explicit revocation & environment handling -----------------
        for u in User.query.all():
            u.session_version += 1          # every restored session dies
        ex.ApiToken.query.update({ex.ApiToken.revoked_at: now})
        ex.SubmitToken.query.delete()
        ex.ResourceReservation.query.delete()
        for r in ex.Runner.query.all():
            r.health = "unknown"            # environment may differ →
            r.check_status = "pending"      # needs verification, not
            r.check_error = ""              # "healthy"
            r.check_at = None
        record_audit(actor, "backup.restore_applied",
                     f"mode={mode} sections={len(report['restored'])}")
        db.session.commit()
    except Exception:
        db.session.rollback()
        _exit_maintenance(app)
        record_audit(actor, "backup.restore_failed", "rolled back")
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        raise

    # secret.key last — only after the DB commit succeeded.
    key_b64 = payload.get("files", {}).get("secret.key")
    if key_b64:
        kp = _fernet_key_path(app)
        fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64decode(key_b64))
    _exit_maintenance(app)
    return report


def safety_backup_dir(app) -> Path:
    """Pre-restore safety backups — same protection as normal backups
    (encrypted container, 0700 dir). Kept server-side for break-glass
    recovery; never plaintext."""
    d = Path(app.instance_path) / "backups"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def last_backup_info(app) -> dict:
    marker = Path(app.instance_path) / "last-backup.json"
    if marker.exists():
        try:
            return json.loads(marker.read_text())
        except ValueError:
            pass
    return {}


def record_backup_marker(app, actor: str, header: dict) -> None:
    fd = os.open(Path(app.instance_path) / "last-backup.json",
                 os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"created_utc": header["created_utc"],
                   "format_version": header["format_version"],
                   "app_version": header["app_version"],
                   "by": actor}, f)
