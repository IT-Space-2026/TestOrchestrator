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
