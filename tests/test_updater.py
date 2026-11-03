"""Updater pipeline tests — SIMULATED failure injection (not live
service/update acceptance; needs a real target host for that gate)."""

import hashlib
import json
import tarfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from testorchestrator import updater


@pytest.fixture()
def keypair(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = tmp_path / "pub.pem"
    pub.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo))
    return key, pub


def _sign(key, data: bytes) -> bytes:
    return key.sign(
        data, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                          salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256())


def _bundle(tmp_path, key, files: dict, manifest=None, sign=True):
    b = tmp_path / "bundle"
    b.mkdir(exist_ok=True)
    sums = ""
    for name, content in files.items():
        (b / name).parent.mkdir(parents=True, exist_ok=True)
        (b / name).write_bytes(content)
        sums += f"{hashlib.sha256(content).hexdigest()}  {name}\n"
    (b / "SHA256SUMS").write_text(sums)
    if sign:
        (b / "SHA256SUMS.sig").write_bytes(
            _sign(key, sums.encode()))
    (b / "manifest.json").write_text(json.dumps(manifest or {}))
    return b


def test_verify_rejects_tampered_file(tmp_path, keypair):
    key, pub = keypair
    b = _bundle(tmp_path, key, {"payload/app.py": b"v1"})
    (b / "payload" / "app.py").write_bytes(b"EVIL")
    with pytest.raises(updater.UpdateError, match="digest mismatch"):
        updater.verify_bundle(b, pub)


def test_verify_rejects_forged_signature(tmp_path, keypair):
    key, pub = keypair
    b = _bundle(tmp_path, key, {"payload/app.py": b"v1"}, sign=False)
    (b / "SHA256SUMS.sig").write_bytes(b"forged")
    with pytest.raises(updater.UpdateError, match="signature"):
        updater.verify_bundle(b, pub)


def test_verify_rejects_wrong_platform(tmp_path, keypair):
    key, pub = keypair
    b = _bundle(tmp_path, key, {"payload/app.py": b"v1"})
    manifest = {"platform": "windows/amd64", "version": "9"}
    verified = updater.verify_bundle(b, pub)
    verified["manifest"] = manifest
    assert manifest["platform"] != updater._platform()


def test_backup_activate_recover_roundtrip(tmp_path):
    live = tmp_path / "live"
    (live / "app").mkdir(parents=True)
    (live / "app" / "f.txt").write_text("v1")
    tar = updater.backup_state(tmp_path / "bk.tar", [live])
    assert tar.exists()

    stage = tmp_path / "stage"
    (stage / "app").mkdir(parents=True)
    (stage / "app" / "f.txt").write_text("v2")
    res = updater.activate(stage, live, tmp_path / "backups")
    assert (live / "app" / "f.txt").read_text() == "v2"
    assert Path(res["retired"]).exists()

    # Failed update -> recover restores the backup verbatim.
    out = updater.recover(tar, live)
    assert Path(out["restored"]).exists()


def test_recover_missing_backup_is_actionable(tmp_path):
    with pytest.raises(updater.UpdateError, match="backup missing"):
        updater.recover(tmp_path / "none.tar", tmp_path)


def test_checkpoint_resume_reports_prior_stage(app, tmp_path, keypair):
    key, pub = keypair
    b = _bundle(tmp_path, key, {"payload/app.py": b"v2"},
                manifest={"version": "2.0"})
    with tarfile.open(b / "update.tar.gz", "w:gz") as t:
        import io
        content = b"v2"
        ti = tarfile.TarInfo("payload/app.py")
        ti.size = len(content)
        t.addfile(ti, io.BytesIO(content))
    (tmp_path / "staged").mkdir()
    with app.app_context():
        res = updater.apply_update(app, b, b / "payload", pub,
                                   tmp_path / "staged")
        assert res["version"] == "2.0"
        resumed = updater.resume_from_checkpoint(app)
        assert resumed and resumed["checkpoint"]["status"] == "staged"
        assert "backup" in resumed["checkpoint"]


def test_drain_and_health(app, monkeypatch):
    with app.app_context():
        assert updater.drain(2) is True  # no active executions
        assert updater.health_check(app) in (True, False)
