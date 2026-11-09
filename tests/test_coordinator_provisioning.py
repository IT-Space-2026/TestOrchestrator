"""ensure_coordinator_credential — fresh-install idempotent provisioning."""
from testorchestrator.extensions import db
from testorchestrator.execution import SecretRef
from testorchestrator.secrets_store import (ensure_coordinator_credential,
                                            read_secret)


def test_provisions_once(app):
    with app.app_context():
        assert ensure_coordinator_credential(app) is True
        # value resolves via the protected mechanism; ref is wired
        assert read_secret(app, "coordinator-agent")
        from testorchestrator import settings as sm
        assert sm.get("dagu_credential_ref") == "coordinator-agent"


def test_idempotent_no_regenerate(app):
    with app.app_context():
        ensure_coordinator_credential(app)
        first = read_secret(app, "coordinator-agent")
        # restart/upgrade/rerun semantics: second call is a no-op
        assert ensure_coordinator_credential(app) is False
        assert read_secret(app, "coordinator-agent") == first


def test_preserves_user_credential_ref(app):
    """An installation that already set a ref keeps it — we do not
    overwrite an existing consumer."""
    from testorchestrator import settings as sm
    with app.app_context():
        sm.update({"dagu_credential_ref": "user-token"})
        ensure_coordinator_credential(app)
        assert sm.get("dagu_credential_ref") == "user-token"


def test_launcher_generation_and_validation(app):
    from testorchestrator.adapters.dagu import worker_launcher_script
    from testorchestrator import settings as sm
    with app.app_context():
        sm.update({"dagu_coordinator_url": "coord.internal:50055"})
        sh = worker_launcher_script(worker_id="wr-1", kre_label="kre-a")
        assert "coord.internal:50055" in sh
        assert "kre=kre-a" in sh and "wr-1" in sh
        win = worker_launcher_script(worker_id="wr-1", kre_label="kre-a",
                                     platform="windows")
        assert "dagu.exe" in win
        import pytest
        with pytest.raises(ValueError):  # unconfigured coordinator
            sm.update({"dagu_coordinator_url": ""})
            worker_launcher_script(worker_id="wr-1", kre_label="kre-a")
        with pytest.raises(ValueError):  # unsafe label rejected
            sm.update({"dagu_coordinator_url": "c:1"})
            worker_launcher_script(worker_id="wr; rm -rf /", kre_label="kre-a")


def test_launcher_endpoint_admin_only(app, client, login_as):
    from testorchestrator import settings as sm
    with app.app_context():
        sm.update({"dagu_coordinator_url": "coord.internal:50055"})
    login_as("tester")
    assert client.get("/system/infrastructure/worker-launcher/wr-1/kre-a"
                      ).status_code in (302, 403)
    login_as("admin")
    r = client.get("/system/infrastructure/worker-launcher/wr-1/kre-a")
    assert r.status_code == 200 and "worker.coordinators" in r.get_data(
        as_text=True)


def test_concurrent_provisioning_race(app):
    """A losing concurrent _provision hits the UNIQUE name constraint;
    the guard rolls back and resolves to the winner's credential — no
    duplicate, no overwrite."""
    import pytest
    from sqlalchemy.exc import IntegrityError
    from testorchestrator import secrets_store as ss
    with app.app_context():
        ss.ensure_coordinator_credential(app)        # winner
        winner = read_secret(app, "coordinator-agent")
        # a losing attempt that reaches _provision directly: store_secret
        # adds a row with the same UNIQUE name -> IntegrityError on commit
        with pytest.raises(Exception):
            ss._provision(app, "coordinator-agent")
        ss.db.session.rollback()
        # after the conflict the original credential is intact + single
        assert ss.SecretRef.query.filter_by(
            name="coordinator-agent").count() == 1
        assert read_secret(app, "coordinator-agent") == winner
        # and a subsequent ensure resolves to it
        assert ss.ensure_coordinator_credential(app) is False


def test_existing_credential_not_overwritten_on_rerun(app):
    from testorchestrator import secrets_store as ss
    with app.app_context():
        ss.ensure_coordinator_credential(app)
        first = read_secret(app, "coordinator-agent")
        ss.ensure_coordinator_credential(app)   # restart/rerun
        ss.ensure_coordinator_credential(app)   # upgrade
        assert read_secret(app, "coordinator-agent") == first


def test_launcher_safety_paths_and_quoting(app):
    """Paths with spaces, unsafe chars rejected; bundled-exe resolution."""
    from testorchestrator.adapters.dagu import worker_launcher_script
    from testorchestrator import settings as sm
    import pytest
    with app.app_context():
        sm.update({"dagu_coordinator_url": "coord.internal:50055"})
        # unsafe worker_id / label rejected (no shell injection)
        for bad in ("w;r;m", "w id", "w`id`", "w$(id)", ""):
            with pytest.raises(ValueError):
                worker_launcher_script(worker_id=bad, kre_label="kre-a")
        for bad in ("l;l", "l l", "l|l", ""):
            with pytest.raises(ValueError):
                worker_launcher_script(worker_id="wr-1", kre_label=bad)
        # coordinator addr present + quoted in both outputs
        sh = worker_launcher_script(worker_id="wr-1", kre_label="kre-a")
        assert 'worker.coordinators="coord.internal:50055"' in sh
        win = worker_launcher_script(worker_id="wr-1", kre_label="kre-a",
                                     platform="windows")
        assert "coord.internal:50055" in win and "dagu.exe" in win
        # unsupported platform rejected
        with pytest.raises(ValueError):
            worker_launcher_script(worker_id="wr-1", kre_label="kre-a",
                                   platform="plan9")


def test_launcher_endpoint_authz(app, client, login_as):
    from testorchestrator import settings as sm
    with app.app_context():
        sm.update({"dagu_coordinator_url": "coord.internal:50055"})
    # unauthenticated -> redirect/deny
    assert client.get(
        "/system/infrastructure/worker-launcher/wr-1/kre-a").status_code in (302,403,401)
    # non-admin -> deny
    login_as("tester")
    assert client.get(
        "/system/infrastructure/worker-launcher/wr-1/kre-a").status_code in (302,403)
    # admin -> 200 + correct content-disposition
    login_as("admin")
    r = client.get("/system/infrastructure/worker-launcher/wr-1/kre-a")
    assert r.status_code == 200 and "attachment" in r.headers.get("Content-Disposition","")
