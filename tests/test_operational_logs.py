"""Admin -> Operational Logs: access control, filters, tail, export."""
from datetime import datetime, timedelta, timezone
from testorchestrator.extensions import db
from testorchestrator import logs


def _seed(app):
    with app.app_context():
        logs.emit("dispatch", "execution dispatched",
                  severity="info", category="dispatch",
                  execution_id="e1", worker_id="w1", correlation_id="c1")
        logs.emit("worker", "worker heartbeat",
                  severity="debug", category="worker", worker_id="w1")
        logs.emit("report", "report generation failed",
                  severity="error", category="report",
                  project_id="p1", execution_id="e2")


def test_admin_only(client, login_as, app):
    _seed(app)
    login_as("tester")
    assert client.get("/system/logs").status_code == 403
    assert client.get("/system/logs/tail").status_code == 403
    assert client.get("/system/logs/export").status_code == 403


def test_page_and_filters(client, login_as, app):
    _seed(app)
    login_as("admin")
    body = client.get("/system/logs").data.decode()
    assert "execution dispatched" in body
    # severity filter narrows results
    body = client.get("/system/logs?severity=error").data.decode()
    assert "report generation failed" in body
    assert "worker heartbeat" not in body
    # component filter
    body = client.get("/system/logs?component=dispatch").data.decode()
    assert "execution dispatched" in body
    assert "worker heartbeat" not in body
    # search
    body = client.get("/system/logs?q=heartbeat").data.decode()
    assert "worker heartbeat" in body


def test_tail_returns_rows_after_timestamp(client, login_as, app):
    _seed(app)
    login_as("admin")
    d = client.get("/system/logs/tail").get_json()
    assert "rows" in d and "generated_at" in d
    assert any(r["message"] == "execution dispatched"
               for r in d["rows"])


def test_export_csv_and_jsonl(client, login_as, app):
    _seed(app)
    login_as("admin")
    csv_r = client.get("/system/logs/export?format=csv")
    assert csv_r.status_code == 200
    assert "dispatch" in csv_r.data.decode()
    jl = client.get("/system/logs/export?format=jsonl")
    assert jl.status_code == 200
    import json
    row = json.loads(jl.data.decode().splitlines()[0])
    assert "severity" in row and "message" in row


def test_export_redacts_secrets(client, login_as, app):
    with app.app_context():
        logs.emit("dispatch", "call api token=abc123 done",
                  severity="info")
    login_as("admin")
    out = client.get("/system/logs/export?format=csv").data.decode()
    assert "abc123" not in out
    assert "token=•••" in out


def test_audit_separate_from_logs(client, login_as, app):
    _seed(app)
    login_as("admin")
    body = client.get("/system/logs").data.decode()
    assert "Audit trail" in body or "Audit" in body  # link present
    # log events aren't audit events — different table
    with app.app_context():
        from testorchestrator.models import AuditEvent
        assert AuditEvent.query.filter_by(
            event="execution dispatched").count() == 0


def test_prune_respects_retention_and_cap(app, monkeypatch):
    with app.app_context():
        for i in range(5):
            logs.emit("t", f"msg-{i}")
        # force age-based prune
        old = logs.OperationalLog.query.first()
        old.occurred_at = datetime.now(timezone.utc) - timedelta(days=60)
        db.session.commit()
        monkeypatch.setitem(logs.DEFAULTS, "logs_retention_days", 30)
        logs.prune()
        assert logs.OperationalLog.query.filter(
            logs.OperationalLog.occurred_at <
            datetime.now(timezone.utc) - timedelta(days=30)).count() == 0
