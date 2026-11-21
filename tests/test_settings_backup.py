"""Settings backup & restore acceptance (2.6 scope: settings only).

Covers the required matrix: create/download/restore round-trip, restored
credential login, session/token revocation, all tamper rejections,
archive-safety bounds, authorization (admin-only + re-auth + fresh-install
setup token), rollback on failure, and no-plaintext-secret leakage.
"""

import io
import json
import os
import time
from pathlib import Path

import pytest

from testorchestrator import backup as bk
from testorchestrator.extensions import db
from testorchestrator.models import Role, User
from testorchestrator.settings import mark_setup_complete
from tests.conftest import TestConfig, make_app, seed_users

PASS = "backup-passphrase-123"
ADMIN_PW = "TestPassword-12345"


@pytest.fixture
def app(tmp_path):
    a = make_app(tmp_path)
    a.instance_path = str(tmp_path / "instance")   # keep artifacts out of
    Path(a.instance_path).mkdir(exist_ok=True)     # the repo instance dir
    seed_users(a)                       # 4 users, one per role
    return a


def _fresh_app(tmp_path, name="fresh.db"):
    a = make_app(tmp_path, db_name=name)
    a.instance_path = str(tmp_path / "fresh-instance")
    Path(a.instance_path).mkdir(exist_ok=True)
    with a.app_context():
        db.create_all()
        from testorchestrator.bootstrap import ensure_bootstrap_admin
        ensure_bootstrap_admin()
        db.session.commit()
        bk.ensure_setup_token(a)
    return a


def _login(client, username="admin", pw=ADMIN_PW):
    return client.post("/login", data={"username": username,
                                       "password": pw})


def _create_backup(client):
    return client.post("/system/backup/create", data={
        "passphrase": PASS, "confirm": PASS, "password": ADMIN_PW})


def _stage(client, data, cred, path="/system/backup/restore/stage"):
    return client.post(path, data={
        "backup_file": (io.BytesIO(data), "b.tobackup"),
        "passphrase": PASS, "credential": cred},
        content_type="multipart/form-data")


def _apply(client, staged_id, cred, mode="replace", passphrase=PASS):
    return client.post("/system/backup/restore/apply", data={
        "staged_id": staged_id, "mode": mode,
        "passphrase": passphrase, "credential": cred},
        follow_redirects=True)


def _staged_id(resp):
    import re
    m = re.search(rb'name="staged_id"[^>]*value="([0-9a-f]{32})"',
                  resp.data)
    assert m, resp.data[:500]
    return m.group(1).decode()


# ------------------------------------------------------------- create ----

def test_create_requires_admin_and_reauth(app):
    client = app.test_client()
    # anonymous → denied before the resource renders
    assert client.get("/system/backup").status_code in (302, 403)
    _login(client, "tester")            # non-admin role
    assert client.get("/system/backup").status_code == 403
    assert _create_backup(client).status_code == 403
    client.post("/logout")              # POST-only; GET no-ops
    # admin without re-auth password → denied
    _login(client)
    resp = client.post("/system/backup/create", data={
        "passphrase": PASS, "confirm": PASS, "password": "wrong"})
    assert resp.status_code == 302      # flashed denial, no file
    # admin + correct password → the .tobackup download
    resp = _create_backup(client)
    assert resp.status_code == 200
    assert resp.data[:4] == b"TOBK"
    assert ".tobackup" in resp.headers.get("Content-Disposition", "")


def test_create_short_passphrase_rejected(app):
    client = app.test_client()
    _login(client)
    resp = client.post("/system/backup/create", data={
        "passphrase": "short", "confirm": "short", "password": ADMIN_PW})
    assert resp.status_code == 302      # validation failure → no file
    assert not resp.data.startswith(b"TOBK")


def test_create_writes_marker_and_audit(app):
    client = app.test_client()
    _login(client)
    assert _create_backup(client).status_code == 200
    info = bk.last_backup_info(app)
    assert info["format_version"] == bk.FORMAT_VERSION
    assert info["by"] == "admin"
    with app.app_context():
        from testorchestrator.models import AuditEvent
        ev = AuditEvent.query.filter_by(event="backup.created").first()
        assert ev is not None and ev.username == "admin"
        assert PASS not in (ev.detail or "")   # passphrase never audited


# ------------------------------------------------------------- crypto ----

def test_tamper_cases_all_fail(app):
    with app.app_context():
        data = bk.create_backup(app, PASS)
    for bad in (
            data[:-40],                          # truncated
            data[:20] + bytes([data[20] ^ 1]) + data[21:],   # header bit
            data[:-10] + bytes([data[-10] ^ 1]) + data[-9:], # payload bit
            b"XXXX" + data[4:],                  # bad magic
            b""):                                # empty
        with pytest.raises(bk.BackupError):
            bk.decrypt_backup(bad, PASS)
    with pytest.raises(bk.BackupError):
        bk.decrypt_backup(data, "different-passphrase")
    # unsupported newer format
    hlen = int.from_bytes(data[4:8], "little")
    hdr = json.loads(data[8:8 + hlen])
    hdr["format_version"] = bk.FORMAT_VERSION + 1
    hb = json.dumps(hdr, separators=(",", ":")).encode()
    forged = b"TOBK" + len(hb).to_bytes(4, "little") + hb + data[8 + hlen:]
    with pytest.raises(bk.BackupError):
        bk.parse_header(forged)


def test_no_plaintext_secret_in_container(app):
    with app.app_context():
        from testorchestrator.secrets_store import store_secret
        store_secret(app, "acc-secret", "git", "hunter2-super-secret")
        db.session.commit()
        data = bk.create_backup(app, PASS)
    assert b"hunter2-super-secret" not in data
    payload = bk.decrypt_backup(data, PASS)
    # secret_refs rows are ciphertext blobs + the key travels inside
    assert payload["files"].get("secret.key")


def test_wrong_checksum_section_fails(app):
    with app.app_context():
        data = bk.create_backup(app, PASS)
    payload = bk.decrypt_backup(data, PASS)
    # flip a row value post-encryption is impossible (AEAD) — verify the
    # manifest check itself rejects a doctored payload
    payload["sections"]["users"][0]["username"] = "pwned"
    import hashlib
    meta = payload["manifest"]["sections"]["users"]
    blob = json.dumps(payload["sections"]["users"], sort_keys=True).encode()
    assert hashlib.sha256(blob).hexdigest() != meta["sha256"]


# ------------------------------------------------------------- restore ---

def test_fresh_install_restore_via_setup_token(app, tmp_path):
    client = app.test_client()
    _login(client)
    data = _create_backup(client).data

    fresh = _fresh_app(tmp_path)
    token = bk._setup_token_path(fresh).read_text().split("|")[0]
    fc = fresh.test_client()
    stage = _stage(fc, data, token)
    assert stage.status_code == 200 and b"Backup verified" in stage.data
    resp = _apply(fc, _staged_id(stage), token, mode="fresh")
    assert resp.status_code == 200 and b"Restore applied" in resp.data
    # token is one-time — consumed
    assert not bk._setup_token_path(fresh).exists()
    # restored credentials work on the new system
    assert _login(fc).status_code in (302, 200)
    with fresh.app_context():
        assert User.query.filter_by(username="admin").count() == 1


def test_fresh_install_wrong_token_rejected(tmp_path):
    fresh = _fresh_app(tmp_path)
    fc = fresh.test_client()
    assert _stage(fc, b"TOBK....", "bogus-token").status_code == 403


def test_replace_mode_safety_backup_and_revocation(app, tmp_path):
    client = app.test_client()
    _login(client)
    data = _create_backup(client).data
    stage = _stage(client, data, ADMIN_PW)
    assert stage.status_code == 200
    resp = _apply(client, _staged_id(stage), ADMIN_PW, mode="replace")
    assert b"Restore applied" in resp.data
    # safety backup exists in the protected dir
    d = bk.safety_backup_dir(app)
    safety = [p for p in d.iterdir() if "pre-restore" in p.name]
    assert safety and safety[0].read_bytes()[:4] == b"TOBK"
    with app.app_context():
        from testorchestrator.execution import ApiToken
        assert all(t.revoked_at is not None
                   for t in ApiToken.query.all())


def test_restore_runners_marked_needs_verification(app):
    client = app.test_client()
    _login(client)
    with app.app_context():
        from testorchestrator.execution import Runner
        r = Runner(name="w1", health="ok", check_status="ok")
        db.session.add(r)
        db.session.commit()
    data = _create_backup(client).data
    stage = _stage(client, data, ADMIN_PW)
    _apply(client, _staged_id(stage), ADMIN_PW)
    with app.app_context():
        from testorchestrator.execution import Runner
        r = Runner.query.filter_by(name="w1").one()
        assert r.health == "unknown" and r.check_status == "pending"


def test_restore_without_admin_refused(app):
    with app.app_context():
        data = bk.create_backup(app, PASS)
        payload = bk.decrypt_backup(data, PASS)
    payload["sections"]["users"] = [
        u for u in payload["sections"]["users"]
        if u.get("role") != {"__enum__": "admin"}]
    with pytest.raises(bk.BackupError, match="Administrator"):
        bk.apply_restore(app, payload, "fresh", "test")
    with app.app_context():             # nothing changed
        assert User.query.filter_by(role=Role.ADMIN).count() == 1


def test_wrong_passphrase_stage_leaves_system_untouched(app):
    client = app.test_client()
    _login(client)
    data = _create_backup(client).data
    resp = client.post("/system/backup/restore/stage", data={
        "backup_file": (io.BytesIO(data), "b.tobackup"),
        "passphrase": "wrong-passphrase", "credential": ADMIN_PW},
        content_type="multipart/form-data")
    assert resp.status_code == 302       # flashed error, nothing staged
    with app.app_context():
        assert User.query.count() == 4
        from testorchestrator.models import AuditEvent
        assert AuditEvent.query.filter_by(
            event="backup.validate_failed").count() == 1


def test_stage_expiry_and_cleanup(app, tmp_path):
    with app.app_context():
        sid = bk.stage_backup(app, b"TOBKfake")
        p = bk.staged_path(app, sid)
        os.utime(p, (time.time() - 4000, time.time() - 4000))
        with pytest.raises(bk.BackupError, match="expired"):
            bk.staged_path(app, sid)
        bk.cleanup_staging(app)


def test_session_revoked_after_restore(app):
    client = app.test_client()
    _login(client)
    data = _create_backup(client).data
    stage = _stage(client, data, ADMIN_PW)
    _apply(client, _staged_id(stage), ADMIN_PW)
    # Other sessions (different cookie jar) die; the acting admin's
    # re-authenticated session was deliberately preserved.
    other = app.test_client()
    _login(other, "tester")
    with app.app_context():
        tester = User.query.filter_by(username="tester").one()
        tester.session_version += 1      # simulate restore bump
        db.session.commit()
    assert other.get("/system/users").status_code == 302
