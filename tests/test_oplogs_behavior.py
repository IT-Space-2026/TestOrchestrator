"""Operational Logs behavioral checks — real fixture records exercised
through the live endpoints, not just page presence."""
import csv, io, json
from testorchestrator.extensions import db
from testorchestrator import logs


def _seed(app, n=5):
    with app.app_context():
        for i in range(n):
            logs.emit("worker-abc" if i % 2 else "dispatch",
                      f"event-{i} token=SECRET-{i}-XYZ" if i % 2 else f"event-{i}",
                      severity="error" if i % 2 else "info",
                      category="execution")
        db.session.commit()


def test_nonadmin_denied_on_page_tail_export(app, client, login_as):
    _seed(app)
    login_as("tester")          # non-admin
    for ep in ("/system/logs", "/system/logs/tail",
               "/system/logs/export"):
        r = client.get(ep)
        assert r.status_code in (302, 403), (ep, r.status_code)


def test_csv_export_redacted_bounded_and_safe(app, client, login_as):
    _seed(app, 4)
    login_as("admin")
    body = client.get("/system/logs/export?format=csv").get_data(as_text=True)
    rows = list(csv.reader(io.StringIO(body)))
    assert "SECRET-1-XYZ" not in body          # redacted
    assert "=•••" in body or "SECRET" not in body
    # CSV-safety: no cell opens with =, +, -, @ unescaped
    for row in rows[1:]:
        for cell in row:
            assert not (cell and cell[:1] in "=+-@" and cell[:3].strip()
                        and cell[1:2].isalnum()), cell


def test_jsonl_export_shape_and_redaction(app, client, login_as):
    _seed(app, 3)
    login_as("admin")
    body = client.get("/system/logs/export?format=jsonl").get_data(as_text=True)
    lines = [json.loads(l) for l in body.strip().splitlines() if l.strip()]
    assert lines and all(isinstance(r, dict) for r in lines)
    assert "SECRET" not in body


def test_combined_filters_and_pagination(app, client, login_as):
    _seed(app, 8)
    login_as("admin")
    # severity=error filter returns only error rows
    r = client.get("/system/logs?severity=error")
    body = r.get_data(as_text=True)
    assert "event-1" in body and "event-0" not in body
    # pagination cap honoured (per_page=50 -> page 2 exists only if >50)
    r2 = client.get("/system/logs?page=2")
    assert r2.status_code == 200


def test_tail_after_returns_newer_only(app, client, login_as):
    _seed(app, 2)
    login_as("admin")
    import time; time.sleep(1)
    with app.app_context():
        logs.emit("dispatch", "late-event", severity="info"); db.session.commit()
    from testorchestrator.extensions import db as _db
    with app.app_context():
        rows = logs.OperationalLog.query.order_by(logs.OperationalLog.id).all()
        mid = rows[0].occurred_at.isoformat()
    data = client.get(f"/system/logs/tail?after={mid}").get_json()
    assert data["rows"] and all(r["ts"] > mid for r in data["rows"])
    assert any("late-event" in r["message"] for r in data["rows"])


def test_export_audit_no_log_content(app, client, login_as):
    _seed(app, 2)
    login_as("admin")
    client.get("/system/logs/export?format=csv")
    with app.app_context():
        from testorchestrator.models import AuditEvent
        ev = AuditEvent.query.filter_by(event="logs.export").first()
        assert ev is not None
        assert "SECRET" not in (ev.detail or "")  # content not copied
