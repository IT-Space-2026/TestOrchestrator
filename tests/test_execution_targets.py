"""Execution-target management routes (Admin → Infrastructure):

- create per target_type with the right persisted shape — bb-worker
  generates workername + secret ref and never stores the plaintext;
  ssh requires host/username/credential and stores key material only
  in the secrets store;
- test/toggle/remove semantics — disable never retargets; remove is
  refused while bound runtimes or live executions reference the
  target;
- host-key approval re-fetches and pins the displayed fingerprint;
- non-admin roles cannot manage targets.
"""
import json

from testorchestrator.execution import (Execution, ExecutionState,
                                        Runner, RuntimeConnection)
from testorchestrator.extensions import db
from testorchestrator.secrets_store import read_secret

from tests.conftest import login


def _create(app, client, **fields):
    data = {"name": "t1", "description": "", "os_arch": "linux/amd64",
            "target_type": "local", "max_slots": "1"}
    data.update(fields)
    return client.post("/system/infrastructure/runners", data=data,
                       follow_redirects=True)


def test_create_buildbot_worker_issues_credentials(app, client,
                                                   monkeypatch, tmp_path):
    login(client, "admin")
    with app.app_context():
        pass
    resp = _create(app, client, target_type="buildbot-worker",
                   name="win-ci-01", os_arch="windows/amd64")
    assert resp.status_code == 200
    with app.app_context():
        r = Runner.query.filter_by(name="win-ci-01").one()
        assert r.target_type == "buildbot-worker"
        assert r.connection_mode == "remote"
        assert r.bb_worker_name.startswith("bbw-")
        assert r.bb_credential_ref.startswith("buildbot-worker-")
        # The password is shown ONCE in the creation flash — the row
        # carries only the ref, and the value lives in the secrets
        # store as ciphertext.
        pw = read_secret(app, r.bb_credential_ref)
        assert pw and pw.encode() in resp.data
        assert not hasattr(r, "bb_password")
        raw = db.session.execute(
            db.text("SELECT encrypted FROM secret_refs "
                    "WHERE name = :n"), {"n": r.bb_credential_ref}
        ).scalar()
        assert pw.encode() not in raw


def test_create_ssh_requires_host_user_credential(app, client,
                                                  monkeypatch, tmp_path):
    login(client, "admin")
    resp = _create(app, client, target_type="ssh", name="ssh-h1",
                   ssh_host="", ssh_username="", ssh_key="")
    with app.app_context():
        assert Runner.query.filter_by(name="ssh-h1").first() is None
    resp = _create(app, client, target_type="ssh", name="ssh-h1",
                   ssh_host="10.0.0.5", ssh_username="to-runner",
                   ssh_workdir="/var/lib/to-runs",
                   ssh_key="-----BEGIN KEY-----\nfake\n-----END KEY-----")
    with app.app_context():
        r = Runner.query.filter_by(name="ssh-h1").one()
        assert r.target_type == "ssh"
        assert r.ssh_host == "10.0.0.5" and r.ssh_port == 22
        assert r.ssh_username == "to-runner"
        assert r.ssh_credential_ref
        # Key material is in the secrets store — never on the row.
        assert "BEGIN KEY" in read_secret(app, r.ssh_credential_ref)
        row = json.dumps({c.name: getattr(r, c.name)
                          for c in r.__table__.columns}, default=str)
        assert "BEGIN KEY" not in row


def test_toggle_and_remove_semantics(app, client):
    login(client, "admin")
    _create(app, client, name="edge-1")
    with app.app_context():
        r = Runner.query.filter_by(name="edge-1").one()
        rid = r.id
    resp = client.post(f"/system/infrastructure/runners/{rid}/toggle",
                       follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(Runner, rid).enabled is False
    # Bound runtime blocks removal.
    with app.app_context():
        db.session.add(RuntimeConnection(
            name="kre", runner_id=rid,
            executable_path="/opt/kre/katalonc"))
        db.session.commit()
    resp = client.post(f"/system/infrastructure/runners/{rid}/remove",
                       follow_redirects=True)
    assert b"still bound" in resp.data
    with app.app_context():
        assert db.session.get(Runner, rid) is not None
        rt = RuntimeConnection.query.filter_by(runner_id=rid).one()
        db.session.delete(rt)
        db.session.commit()
    # A live execution also blocks removal — nothing is abandoned.
    with app.app_context():
        ex = Execution(project_id="p", state=ExecutionState.DISPATCHED,
                       runner_id=rid, created_by="t")
        db.session.add(ex)
        db.session.commit()
    resp = client.post(f"/system/infrastructure/runners/{rid}/remove",
                       follow_redirects=True)
    assert b"still reference it" in resp.data
    with app.app_context():
        ex = Execution.query.filter_by(runner_id=rid).one()
        ex.state = ExecutionState.COMPLETED
        db.session.commit()
    resp = client.post(f"/system/infrastructure/runners/{rid}/remove",
                       follow_redirects=True)
    with app.app_context():
        assert db.session.get(Runner, rid) is None


def test_non_admin_cannot_manage_targets(app, client):
    login(client, "tester")
    resp = _create(app, client, name="nope-1")
    assert resp.status_code in (302, 403)
    with app.app_context():
        r = Runner(name="r", target_type="local")
        db.session.add(r)
        db.session.commit()
        rid = r.id
    resp = client.post(
        f"/system/infrastructure/runners/{rid}/remove",
        follow_redirects=False)
    assert resp.status_code in (302, 403)
    resp = client.post(
        f"/system/infrastructure/runners/{rid}/test",
        follow_redirects=False)
    assert resp.status_code in (302, 403)
    with app.app_context():
        assert db.session.get(Runner, rid) is not None


def test_local_target_test_ok(app, client):
    login(client, "admin")
    _create(app, client, name="local-1")
    with app.app_context():
        rid = Runner.query.filter_by(name="local-1").one().id
    client.post(f"/system/infrastructure/runners/{rid}/test",
                follow_redirects=True)
    with app.app_context():
        r = db.session.get(Runner, rid)
        assert r.check_status == "ok"


def test_bb_worker_test_unregistered(app, client, monkeypatch,
                                     tmp_path):
    login(client, "admin")
    _create(app, client, target_type="buildbot-worker", name="w2")
    with app.app_context():
        r = Runner.query.filter_by(name="w2").one()
        rid = r.id

    class _NoWorkers:
        @classmethod
        def from_settings(cls):
            return cls()
        def workers(self):
            return []

    from testorchestrator.adapters.buildbot import BuildbotAdapter
    monkeypatch.setattr(BuildbotAdapter, "from_settings",
                        classmethod(lambda cls: _NoWorkers()))
    client.post(f"/system/infrastructure/runners/{rid}/test",
                follow_redirects=True)
    with app.app_context():
        r = db.session.get(Runner, rid)
        assert r.check_status == "unregistered"
        assert r.health == "unreachable"


def test_approve_hostkey_pins_displayed_fingerprint(app, client,
                                                  monkeypatch):
    login(client, "admin")
    _create(app, client, target_type="ssh", name="ssh-pk",
            ssh_host="10.0.0.9", ssh_username="u",
            ssh_credential_ref="dummy-ref")
    with app.app_context():
        r = Runner.query.filter_by(name="ssh-pk").one()
        rid = r.id
        r.check_status = "unverified"
        r.check_error = "SHA256:Displayed"
        db.session.commit()

    import testorchestrator.ssh_check as sc
    monkeypatch.setattr(sc, "probe_host_key",
                        lambda h, p: ("ssh-ed25519", "SHA256:Displayed"))
    client.post(
        f"/system/infrastructure/runners/{rid}/approve-hostkey",
        data={"fingerprint": "SHA256:Displayed"},
        follow_redirects=True)
    with app.app_context():
        r = db.session.get(Runner, rid)
        assert r.ssh_host_key_fingerprint == "SHA256:Displayed"
        assert r.ssh_host_key_alg == "ssh-ed25519"

    # A key swapped between display and approval is rejected.
    monkeypatch.setattr(sc, "probe_host_key",
                        lambda h, p: ("ssh-ed25519", "SHA256:Changed"))
    with app.app_context():
        r = db.session.get(Runner, rid)
        r.ssh_host_key_fingerprint = ""
        r.check_status = "unverified"
        r.check_error = "SHA256:Displayed"
        db.session.commit()
    client.post(
        f"/system/infrastructure/runners/{rid}/approve-hostkey",
        data={"fingerprint": "SHA256:Displayed"},
        follow_redirects=True)
    with app.app_context():
        assert db.session.get(Runner, rid).ssh_host_key_fingerprint \
            == ""


def test_ssh_identity_edit_requires_reapproval(app, client,
                                               monkeypatch, tmp_path):
    login(client, "admin")
    _create(app, client, target_type="ssh", name="ssh-ed",
            ssh_host="10.0.0.7", ssh_username="u",
            ssh_key="keymaterial")
    with app.app_context():
        r = Runner.query.filter_by(name="ssh-ed").one()
        rid = r.id
        r.ssh_host_key_fingerprint = "SHA256:Old"
        r.ssh_host_key_alg = "ssh-rsa"
        db.session.commit()
    client.post(f"/system/runners/{rid}/edit",
                data={"name": "ssh-ed", "ssh_host": "10.0.0.8",
                      "ssh_username": "u", "ssh_port": "22",
                      "ssh_workdir": "/x"},
                follow_redirects=True)
    with app.app_context():
        r = db.session.get(Runner, rid)
        assert r.ssh_host == "10.0.0.8"
        assert r.ssh_host_key_fingerprint == ""
        assert r.check_status == "unverified"
