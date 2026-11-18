"""Live SSH-check coverage against an in-process paramiko server —
exercises the real protocol: first contact (unverified + discovered
fingerprint), post-approval full pass, changed-key rejection, bad
credential, unreachable host, missing runner executable."""
import pytest

paramiko = pytest.importorskip("paramiko", reason="paramiko needed")

from testorchestrator import ssh_check
from testorchestrator.execution import Runner, RuntimeConnection
from testorchestrator.secrets_store import store_secret
from testorchestrator.extensions import db

from tests.ssh_test_server import TestSSHServer, generate_client_key


@pytest.fixture()
def ssh_env(app, tmp_path, monkeypatch):
    server = TestSSHServer(tmp_path / "remote")
    (tmp_path / "remote").mkdir(parents=True)
    return server


def _runner(app, server, tmp_path, fingerprint="", start=True):
    with app.app_context():
        key = generate_client_key(tmp_path / "id_rsa")
        if start:
            server.start(key)
        store_secret(app, "ssh-test", "ssh",
                     (tmp_path / "id_rsa").read_text())
        r = Runner(name="ssh-h", connection_mode="remote",
                   target_type="ssh", ssh_host="127.0.0.1",
                   ssh_port=server.port, ssh_username="to-runner",
                   ssh_credential_ref="ssh-test",
                   ssh_workdir=str(tmp_path / "remote" / "to-runs"),
                   ssh_host_key_fingerprint=fingerprint,
                   ssh_host_key_alg="ssh-rsa" if fingerprint else "")
        db.session.add(r)
        db.session.commit()
        return r.id


def test_first_contact_unverified_then_full_pass(app, ssh_env,
                                                 tmp_path):
    rid = _runner(app, ssh_env, tmp_path)
    with app.app_context():
        r = db.session.get(Runner, rid)
        res = ssh_check.check_target(r)
        assert res.status == "unverified"
        assert res.fingerprint == ssh_env.fingerprint
        assert ("host-key", "unverified") in res.stages
        # after approval the full staged check passes — nothing was
        # installed, no test was launched
        r.ssh_host_key_fingerprint = ssh_env.fingerprint
        r.ssh_host_key_alg = ssh_env.key_alg
        res = ssh_check.check_target(r)
        assert res.status == "ok"
        stages = dict(res.stages)
        for stage in ("reachability", "host-key", "credential",
                      "authentication", "working-directory", "sftp",
                      "runner"):
            assert stages.get(stage) == "ok", (stage, res.stages)


def test_changed_host_key_rejected(app, ssh_env, tmp_path):
    rid = _runner(app, ssh_env, tmp_path)
    with app.app_context():
        r = db.session.get(Runner, rid)
        r.ssh_host_key_fingerprint = "SHA256:Stale"
        r.ssh_host_key_alg = "ssh-rsa"
        res = ssh_check.check_target(r)
        assert res.status == "fail"
        assert res.stage == "host-key"
        assert "changed" in res.error


def test_bad_credential_fails_auth(app, ssh_env, tmp_path):
    rid = _runner(app, ssh_env, tmp_path)
    with app.app_context():
        r = db.session.get(Runner, rid)
        r.ssh_host_key_fingerprint = ssh_env.fingerprint
        r.ssh_host_key_alg = ssh_env.key_alg
        # replace the stored key with a different one
        other = generate_client_key(tmp_path / "id_rsa2")
        store_secret(app, "ssh-test", "ssh",
                     (tmp_path / "id_rsa2").read_text())
        res = ssh_check.check_target(r)
        assert res.status == "fail"
        assert res.stage == "authentication"


def test_unreachable_host_named(app, tmp_path):
    # A port that was bound then released — guaranteed nothing listens.
    import socket as _s
    s = _s.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()
    server = TestSSHServer(tmp_path / "remote")
    server.port = dead_port
    rid = _runner(app, server, tmp_path, start=False)
    with app.app_context():
        r = db.session.get(Runner, rid)
        res = ssh_check.check_target(r)
        assert res.status == "fail"
        assert res.stage == "reachability"


def test_missing_runner_executable_named(app, ssh_env, tmp_path):
    rid = _runner(app, ssh_env, tmp_path)
    with app.app_context():
        r = db.session.get(Runner, rid)
        r.ssh_host_key_fingerprint = ssh_env.fingerprint
        r.ssh_host_key_alg = ssh_env.key_alg
        rt = RuntimeConnection(name="kre", runner_id=rid,
                               executable_path="/nonexistent/katalonc")
        db.session.add(rt)
        db.session.commit()
        res = ssh_check.check_target(r, [rt])
        assert res.status == "fail"
        assert res.stage == "runner"
        assert "kre" in res.error
    server_dir = tmp_path / "remote" / "to-runs"
    assert server_dir.is_dir()   # workdir created by the check
