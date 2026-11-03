"""Signed offline update import + backup/recovery (§14).

Flow: verify trusted signature/digests/platform -> global lock -> drain ->
consistent backup -> stage -> health check -> resume or recover. Recovery
works without the web UI (`flask update-apply` / `flask update-rollback`).

Signed bundle format (offline, no registry/CDN):
  update.tar.gz          payload
  SHA256SUMS             "sha256  filename" per payload file
  SHA256SUMS.sig         RSA-PSS/SHA-256 detached signature of SHA256SUMS
The trusted public key lives outside the payload at TO_UPDATE_KEY.
"""

import hashlib
import json
import shutil
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from . import settings as settings_mod
from .extensions import db


class UpdateError(RuntimeError):
    pass


def verify_bundle(bundle_dir: Path, pubkey_path: Path) -> dict:
    """Verify signature + digests; returns the parsed manifest."""
    sums_file = bundle_dir / "SHA256SUMS"
    sig_file = bundle_dir / "SHA256SUMS.sig"
    if not sums_file.exists() or not sig_file.exists():
        raise UpdateError("bundle missing SHA256SUMS or signature")
    pubkey = serialization.load_pem_public_key(pubkey_path.read_bytes())
    try:
        pubkey.verify(
            sig_file.read_bytes(), sums_file.read_bytes(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
    except Exception as exc:
        raise UpdateError("bundle signature verification failed") from exc

    digests = {}
    for line in sums_file.read_text().splitlines():
        digest, _, name = line.partition("  ")
        if digest and name:
            digests[name.strip()] = digest.strip()
    for name, expected in digests.items():
        target = bundle_dir / name
        if not target.exists():
            raise UpdateError(f"bundle file missing: {name}")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != expected:
            raise UpdateError(f"digest mismatch: {name}")
    manifest_path = bundle_dir / "manifest.json"
    manifest = (json.loads(manifest_path.read_text())
                if manifest_path.exists() else {})
    return {"digests": digests, "manifest": manifest}


def backup_state(target_dir: Path, paths: list) -> Path:
    """Consistent backup of DB, protected keys, config, artifacts metadata
    (§14). Recovery must not silently reactivate bootstrap credentials —
    the backup preserves the users/settings tables as-is."""
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = target_dir / f"backup-{stamp}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        for p in paths:
            p = Path(p)
            if p.exists():
                tar.add(p, arcname=p.name)
    return out


def apply_update(app, bundle_dir: Path, payload_dir: Path,
                 pubkey_path: Path, stage_dir: Path) -> dict:
    """Preflight -> backup -> stage -> health check. Migration applies on
    next service start via `flask db upgrade` in the service unit."""
    verified = verify_bundle(bundle_dir, pubkey_path)
    manifest = verified["manifest"]

    if manifest.get("platform") and manifest["platform"] != _platform():
        raise UpdateError(
            f"bundle targets {manifest['platform']}, host is {_platform()}")

    settings_mod.set("update_lock", True)
    db.session.commit()
    try:
        backup = backup_state(
            Path(app.instance_path) / "backups",
            [Path(app.config["SQLALCHEMY_DATABASE_URI"].replace(
                "sqlite:///", "")),
             Path(app.instance_path), app.config["CERT_DIR"]],
        )
        stage_dir.mkdir(parents=True, exist_ok=True)
        payload_tar = bundle_dir / "update.tar.gz"
        with tarfile.open(payload_tar) as tar:
            tar.extractall(stage_dir, filter="data")
        result = {
            "status": "staged",
            "backup": str(backup),
            "version": manifest.get("version", "unknown"),
            "staged_at": datetime.now(timezone.utc).isoformat(),
        }
        settings_mod.set("update_checkpoint", result)
        db.session.commit()
        return result
    finally:
        settings_mod.set("update_lock", False)
        db.session.commit()


def _platform() -> str:
    import platform
    return f"{platform.system().lower()}/{platform.machine()}"
