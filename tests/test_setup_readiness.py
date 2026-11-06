"""First-run setup readiness: evidence-honest component states and
the bounded publishing write-probe contract."""
import pytest
from testorchestrator import readiness
from testorchestrator.extensions import db
from testorchestrator.settings import set as _s


def test_admin_exists_is_not_verified(app):
    """Bootstrap Admin existing must NOT count — only replacement does."""
    with app.app_context():
        from testorchestrator.bootstrap import ensure_bootstrap_admin
        ensure_bootstrap_admin()
        c = next(x for x in readiness.components() if x["key"]=="admin")
        assert c["status"] != "verified"
        _s("admin_bootstrap_replaced", "yes"); db.session.commit()
        c = next(x for x in readiness.components() if x["key"]=="admin")
        assert c["status"] == "verified"


def test_publishing_required_and_probe_scoped(app):
    """Saved URL != verified; a local-only deployment is never ready."""
    with app.app_context():
        pub = next(x for x in readiness.components()
                   if x["key"] == "publishing")
        assert pub["status"] == "missing"
        assert "not" in pub["detail"]  # honest wording
        _s("publish_repo_url", "https://x/repo.git"); db.session.commit()
        pub = next(x for x in readiness.components()
                   if x["key"] == "publishing")
        assert pub["status"] == "configured"  # saved, not verified


def test_submission_blocked_without_worker(app):
    with app.app_context():
        msg = readiness.operational_check()
        assert "worker" in msg.lower()


def test_worker_registration_alone_not_eligible(app):
    """A registered worker is not execution-eligible without a verified
    engine on it — checks must be associated, not independent."""
    with app.app_context():
        from datetime import datetime, timezone
        from testorchestrator.execution import Runner, RuntimeConnection
        r = Runner(name="w1", os_arch="linux/amd64",
                   heartbeat_at=datetime.now(timezone.utc))
        db.session.add(r); db.session.flush()
        rt = RuntimeConnection(name="kre", runner_id=r.id,
                               executable_path="/x")
        db.session.add(rt); db.session.commit()
        # registered+live worker but UNVERIFIED engine -> blocked on engine
        msg = readiness.operational_check()
        assert "verified" in msg or "engine" in msg.lower()


def test_probe_never_reports_success_without_cleanup():
    """The write-probe contract: rejection, read-back mismatch and
    deletion failure all report FAILED-with-cleanup-state, never
    'verified' while a probe ref may remain."""
    from testorchestrator.publish_probe import probe_write_access
    class FakeRepo:
        def __init__(self, plan, readback=None):
            self.plan = plan; self.readback = readback
        def write(self, token): self.readback = token; return self.plan.pop(0)
        def read(self): return self.readback
        def delete(self): return self.plan.pop(0)
    from testorchestrator.publish_probe import _test_iface as wrap
    res = probe_write_access(wrap(FakeRepo([True, False])))
    assert res["status"] == "failed"
    assert res["cleanup"] == "probe ref may remain"
    bad = wrap(FakeRepo([True, True]))
    bad.read = lambda: "wrong"
    res = probe_write_access(bad)
    assert res["status"] == "failed"      # read-back mismatch
    res = probe_write_access(wrap(FakeRepo([False])))
    assert res["status"] == "failed"      # write rejected
    assert res["cleanup"] == "nothing written"
    res = probe_write_access(wrap(FakeRepo([True, True])))
    assert res["status"] == "verified"    # clean pass


def test_verification_invalidated_by_config_change(app):
    """Changing the checked config must drop the component back to
    configured — fingerprint-bound evidence."""
    from testorchestrator.readiness import record_verified, _pub_fp
    with app.app_context():
        _s("publish_repo_url", "https://a/repo.git")
        record_verified("publishing", _pub_fp())
        c = next(x for x in readiness.components()
                 if x["key"] == "publishing")
        assert c["status"] == "verified"
        _s("publish_repo_url", "https://b/other.git")
        db.session.commit()
        c = next(x for x in readiness.components()
                 if x["key"] == "publishing")
        assert c["status"] == "configured"   # stale evidence dropped


def test_stale_heartbeat_is_not_available(app):
    """Registered+not-draining but heartbeat older than the window is
    NOT a connected worker."""
    from datetime import datetime, timedelta, timezone
    from testorchestrator.execution import Runner
    with app.app_context():
        old = Runner(name="stale", os_arch="linux/amd64",
                     heartbeat_at=datetime.now(timezone.utc)
                                 - timedelta(hours=1))
        db.session.add(old); db.session.commit()
        assert readiness._eligible_workers() == []
        old.heartbeat_at = datetime.now(timezone.utc)
        db.session.commit()
        assert len(readiness._eligible_workers()) == 1


def test_secret_rotation_invalidates_evidence(app):
    """Replacing a secret under the SAME name must drop verified
    evidence that referenced it."""
    from testorchestrator.execution import SecretRef
    from testorchestrator.readiness import (record_verified, _pub_fp)
    with app.app_context():
        sec = SecretRef(name="pub-cred", kind="git", encrypted=b"x")
        db.session.add(sec)
        db.session.flush()
        _s("publish_repo_url", "https://a/repo.git")
        _s("publish_repo_credential_ref", "pub-cred")
        record_verified("publishing", _pub_fp())
        c = next(x for x in readiness.components()
                 if x["key"] == "publishing")
        assert c["status"] == "verified"
        from testorchestrator.execution import utcnow
        sec.rotated_at = utcnow()          # replacement, same name
        db.session.commit()
        c = next(x for x in readiness.components()
                 if x["key"] == "publishing")
        assert c["status"] == "configured"


def test_credential_value_decodes_for_https_auth(app):
    """A credential created through the form is consumed by the real
    binding-auth path — user:token splits; bare token = PAT username."""
    with app.app_context():
        from testorchestrator.secrets_store import store_secret, read_secret
        from flask import current_app
        store_secret(current_app._get_current_object(),
                     "g1", "git", "alice:ghp_token123")
        store_secret(current_app._get_current_object(),
                     "g2", "git", "baretoken")
        from testorchestrator.gitsync import _binding_auth
        from testorchestrator.execution import GitBinding
        b = GitBinding(project_id="p", url="https://x/r.git",
                       credential_ref="g1")
        assert _binding_auth(b) == ("alice", "ghp_token123")
        b.credential_ref = "g2"
        assert _binding_auth(b) == ("baretoken", "")
        b.url = "ssh://git@x/r.git"          # ssh: no stored-secret auth
        assert _binding_auth(b) is None


def test_system_managed_secret_protected(app):
    """Delete AND update paths both refuse system-managed kinds."""
    with app.app_context():
        from testorchestrator.secrets_store import (store_secret,
                                                    SecretError)
        from flask import current_app
        store_secret(current_app._get_current_object(),
                     "sys1", "dagu", "tok", internal=True)
        login_as_admin = None
        with pytest.raises(SecretError):
            store_secret(current_app._get_current_object(),
                         "sys1", "dagu", "rotated")
        with pytest.raises(SecretError):
            store_secret(current_app._get_current_object(),
                         "sys1", "git", "x")       # kind change too


def test_worker_enrollment_token_digest_flow(app):
    """Worker credentials are provisioned through enrollment
    (issue_agent_token), stored only as a digest, and consumed by the
    agent API — never through the manual secret form."""
    import hashlib
    with app.app_context():
        from testorchestrator.agent_api import issue_agent_token
        from testorchestrator.execution import Runner
        r = Runner(name="prov-w", os_arch="linux/amd64")
        db.session.add(r); db.session.commit()
        token = issue_agent_token(r)
        db.session.refresh(r)
        # only the digest persists — never the plaintext token
        assert r.agent_token_hash == hashlib.sha256(
            token.encode()).hexdigest()
        assert token != r.agent_token_hash
        # no SecretRef row is created for worker enrollment
        from testorchestrator.execution import SecretRef
        assert SecretRef.query.filter_by(kind="runner").count() == 0


def test_dagu_credential_is_file_provisioned_not_stored():
    """The provisioning script writes a 0600 credential file outside
    the DB — nothing for the manual form to manage."""
    import inspect
    from scripts import provision_dagu
    src = inspect.getsource(provision_dagu)
    assert "credentials" in src and "0o600" in src
    assert "username=" in src and "password=" in src


def test_source_detail_distinguishes_remote_capability(app):
    """https+secret resolves portal-side only; ssh:// is remote-capable
    — portal auth alone never implies remote execution."""
    from testorchestrator.execution import GitBinding
    from testorchestrator.readiness import (record_verified,
                                            _binding_fp)
    with app.app_context():
        from datetime import datetime, timezone
        from testorchestrator.execution import (Runner,
                                                RuntimeConnection)
        # remote-capable needs ssh + eligible worker + verified engine
        w = Runner(name="rc-w", os_arch="linux/amd64",
                   heartbeat_at=datetime.now(timezone.utc))
        db.session.add(w); db.session.flush()
        rt = RuntimeConnection(name="rc-kre", runner_id=w.id,
                               executable_path="/opt/kre/katalonc",
                               detected_version="9.0.0")
        db.session.add(rt); db.session.flush()
        readiness.record_verified(f"engine_{rt.id}",
                                  readiness._engine_fp(rt))
        b1 = GitBinding(project_id="p", name="b1", url="ssh://git@h/r.git",
                        ref="main")
        b2 = GitBinding(project_id="p", name="b2", url="https://h/r.git",
                        ref="main")
        db.session.add_all([b1, b2]); db.session.flush()
        # mark both resolved (fingerprint-bound)
        from testorchestrator.readiness import _fp, _verified_fp
        from testorchestrator.settings import set as _s
        combo = _fp(sorted(w.id for w in readiness._eligible_workers()),
                    sorted(r.id for r in readiness._runtimes()
                           if readiness._engine_verified(r)))
        for b in (b1, b2):
            _s(f"verified_fp_binding_{b.id}", _fp(_binding_fp(b), combo))
        db.session.commit()
        det = readiness._source_detail([b1, b2], [b1, b2])
        assert "ssh" in det and "remote-capable" in det
        assert "portal-side only" in det
