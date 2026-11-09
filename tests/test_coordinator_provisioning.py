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
