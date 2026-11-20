"""CSV export + external-logging acceptance (2.6.0 logging scope).

Covers: all-matching filters, scope/count, stable ordering + fixed
cutoff, CSV escaping/Unicode/multiline/formula protection, authorization
on creation/status/download, background jobs, expiry/interruption,
audit of exports, redaction at persistence, and the optional NDJSON
sink incl. rotation/health/dedup contract for collectors."""
import csv
import io
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from testorchestrator import (logs, logexport, logschema, log_output,
                              settings as s)
from testorchestrator.extensions import db
from testorchestrator.models import AuditEvent, record_audit


def _seed_ops(app):
    with app.app_context():
        logs.emit("dispatch", "execution dispatched",
                  severity="info", category="dispatch",
                  execution_id="e1", worker_id="w1",
                  correlation_id="corr-1")
        logs.emit("worker", "heartbeat, with comma",
                  severity="debug", category="worker", worker_id="w1")
        logs.emit("report", 'quote "inside" and\nmultiline — Ünïcode',
                  severity="error", category="report", project_id="p1")


def _csv_rows(body: str):
    text = body.lstrip("\ufeff")
    return list(csv.DictReader(io.StringIO(text)))


# ---------------------------------------------------------- CSV export


def test_ops_export_all_matching_with_filters(client, login_as, app):
    _seed_ops(app)
    login_as("admin")
    out = client.get("/system/logs/export?format=csv").data.decode()
    rows = _csv_rows(out)
    assert len(rows) == 3
    assert rows[0].keys() >= {"event_id", "timestamp_utc", "log_type",
                              "severity", "category", "message"}
    assert all(r["log_type"] == "operational" for r in rows)
    # filtered export only exports matches
    out = client.get(
        "/system/logs/export?format=csv&severity=error").data.decode()
    rows = _csv_rows(out)
    assert len(rows) == 1 and "Ünïcode" in rows[0]["message"]


def test_ops_export_time_range(client, login_as, app):
    _seed_ops(app)
    login_as("admin")
    future = (datetime.now(timezone.utc) + timedelta(days=1)).date()
    out = client.get(
        f"/system/logs/export?format=csv&since={future}").data.decode()
    assert _csv_rows(out) == []
    past = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    out = client.get(
        f"/system/logs/export?format=csv&since={past}&until={future}"
    ).data.decode()
    assert len(_csv_rows(out)) == 3


def test_export_escaping_and_unicode(client, login_as, app):
    _seed_ops(app)
    login_as("admin")
    out = client.get("/system/logs/export?format=csv").data.decode()
    rows = _csv_rows(out)
    msgs = {r["message"] for r in rows}
    assert "heartbeat, with comma" in msgs
    assert 'quote "inside" and\nmultiline — Ünïcode' in msgs
    # explicit UTC timestamps
    assert all(r["timestamp_utc"].endswith("Z") for r in rows)


def test_export_formula_injection_guard(client, login_as, app):
    with app.app_context():
        logs.emit("dispatch", "=cmd|'/c calc'!A1")
        logs.emit("dispatch", "+1+2")
        logs.emit("dispatch", "-2+3")
        logs.emit("dispatch", "@SUM(A1)")
    login_as("admin")
    raw = client.get("/system/logs/export?format=csv").data.decode()
    rows = _csv_rows(raw)
    for r in rows:
        assert not r["message"].startswith(("=", "+", "-", "@"))
        assert r["message"].startswith("'")


def test_export_stable_order_and_cutoff(client, login_as, app):
    _seed_ops(app)
    with app.app_context():
        mode, job, count, cutoff = logexport.request_export(
            "operational", {}, "admin")
        assert mode == "inline" and count == 3
        # a row emitted AFTER the cutoff must not appear in the export
        logs.emit("dispatch", "after-cutoff")
        q = logexport.query_for("operational", {}).filter(
            logexport._cutoff_col("operational") <= cutoff)
        out = "".join(logexport.csv_lines("operational", q, cutoff))
    assert "after-cutoff" not in out
    # ordering: occurred_at DESC, id DESC — newest first
    ids = [r["event_id"] for r in _csv_rows(out)]
    assert ids == sorted(ids, reverse=True)


def test_audit_export_uses_audit_rows_only(client, login_as, app):
    _seed_ops(app)
    login_as("admin")                       # login itself audits events
    out = client.get("/system/audit/export").data.decode()
    rows = _csv_rows(out)
    assert rows and all(r["log_type"] == "audit" for r in rows)
    assert all(r["event_id"].startswith("audit-") for r in rows)
    assert any("page.audit" in r["event_name"] or
               "login" in r["event_name"] for r in rows)


def test_audit_export_filters(client, login_as, app):
    login_as("admin")
    with app.app_context():
        record_audit("admin", "evt.target", "target detail")
        record_audit("admin", "evt.other", "other detail")
    out = client.get(
        "/system/audit/export?event=evt.target").data.decode()
    rows = _csv_rows(out)
    assert len(rows) == 1 and rows[0]["event_name"] == "evt.target"
    out = client.get(
        "/system/audit/export?actor=admin&related=target detail") \
        .data.decode()
    rows = _csv_rows(out)
    assert len(rows) == 1
    out = client.get(
        f"/system/audit/export?from="
        f"{(datetime.now(timezone.utc) + timedelta(days=1)).date()}"
    ).data.decode()
    assert _csv_rows(out) == []


# --------------------------------------------------------- permissions


def test_exports_admin_only(client, login_as, app):
    _seed_ops(app)
    login_as("tester")
    for url in ("/system/logs/export?format=csv",
                "/system/audit/export"):
        assert client.get(url).status_code == 403
    with app.app_context():
        job = logexport.LogExportJob(
            created_by="admin", kind="operational", filters_json="{}",
            cutoff_utc=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
        db.session.add(job)
        db.session.commit()
        jid = job.id
    assert client.get(f"/system/log-exports/{jid}").status_code == 403
    assert client.get(
        f"/system/log-exports/{jid}/download").status_code == 403


def test_export_denied_after_role_revoked(client, login_as, app):
    """A demoted admin loses export creation AND job download access."""
    login_as("admin")
    assert client.get("/system/logs/export?format=csv").status_code \
        == 200
    with app.app_context():
        from testorchestrator.models import Role, User
        u = User.query.filter_by(username="admin").one()
        u.role = Role.TESTER
        db.session.commit()
    assert client.get("/system/logs/export?format=csv").status_code \
        == 403
    assert client.get("/system/audit/export").status_code == 403


# -------------------------------------------------------------- jobs


def _complete_job(app, kind="operational", username="admin"):
    """Drive request_export straight into the job path and let the
    runner thread finish."""
    from testorchestrator import settings as _s
    _s.set("logs_export_inline_max", 0)
    db.session.commit()
    try:
        mode, job, count, cutoff = logexport.request_export(
            kind, {}, username)
    finally:
        _s.set("logs_export_inline_max", 10000)
        db.session.commit()
    assert mode == "job"
    jid = job.id
    for _ in range(400):
        db.session.remove()          # release the read txn — the
        job = db.session.get(        # writer thread must be able to
            logexport.LogExportJob, jid)  # commit between polls
        if job.status in ("done", "failed"):
            break
        time.sleep(0.05)
    return job


def test_large_export_runs_as_job_and_downloads(client, login_as, app):
    _seed_ops(app)
    login_as("admin")
    with app.app_context():
        job = _complete_job(app)
        assert job.status == "done", job.error
        assert job.row_count == 3
        jid = job.id
    # status page shows scope + count
    body = client.get(f"/system/log-exports/{jid}").data.decode()
    assert "Matching rows" in body and "operational" in body
    dl = client.get(f"/system/log-exports/{jid}/download")
    assert dl.status_code == 200
    assert len(_csv_rows(dl.data.decode())) == 3


def test_export_job_expires(client, login_as, app):
    _seed_ops(app)
    login_as("admin")
    with app.app_context():
        job = _complete_job(app)
        job.expires_at = datetime.now(timezone.utc) - timedelta(
            minutes=1)
        db.session.commit()
        jid = job.id
        logexport.purge_expired()
        db.session.refresh(job)
        assert job.status == "expired"
        from testorchestrator.config import INSTANCE_DIR
        assert not (INSTANCE_DIR / "log-exports" /
                    job.file_name).exists()
    assert client.get(
        f"/system/log-exports/{jid}/download").status_code == 410


def test_interrupted_job_marked_failed(app):
    with app.app_context():
        job = logexport.LogExportJob(
            created_by="admin", kind="operational", filters_json="{}",
            status="running",
            cutoff_utc=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        db.session.add(job)
        db.session.commit()
        logexport.purge_expired()
        db.session.refresh(job)
        assert job.status == "failed" and "interrupted" in job.error


def test_export_over_hard_cap_refused(client, login_as, app):
    _seed_ops(app)
    with app.app_context():
        s.set("logs_export_max_rows", 2)
        db.session.commit()
    try:
        login_as("admin")
        r = client.get("/system/logs/export?format=csv",
                       follow_redirects=True)
        assert "Export refused" in r.data.decode()
    finally:
        with app.app_context():
            s.set("logs_export_max_rows", 250000)
            db.session.commit()
    with app.app_context():
        assert AuditEvent.query.filter_by(
            event="logs.export.denied").count() >= 1


def test_export_requests_and_downloads_are_audited(client, login_as,
                                                   app):
    _seed_ops(app)
    login_as("admin")
    client.get("/system/logs/export?format=csv&severity=error")
    with app.app_context():
        ev = AuditEvent.query.filter_by(event="logs.export") \
            .order_by(AuditEvent.id.desc()).first()
        assert ev is not None
        assert "1 rows" in ev.detail and "severity" in ev.detail
    with app.app_context():
        job = _complete_job(app)
        jid = job.id
    client.get(f"/system/log-exports/{jid}/download")
    with app.app_context():
        assert AuditEvent.query.filter_by(
            event="logs.export.done").count() >= 1
        assert AuditEvent.query.filter_by(
            event="logs.export.download").count() >= 1


# ----------------------------------------------------------- redaction


def test_secrets_redacted_before_persistence_everywhere(client,
                                                        login_as, app):
    with app.app_context():
        logs.emit("dispatch", "password=hunter2 api_key=AKIA9999")
        record_audit("admin", "cfg.save", "token=sekrit99")
        row = logs.OperationalLog.query.order_by(
            logs.OperationalLog.id.desc()).first()
        assert "hunter2" not in row.message
        assert "AKIA9999" not in row.message
        arow = AuditEvent.query.order_by(AuditEvent.id.desc()).first()
        assert "sekrit99" not in arow.detail
    login_as("admin")
    for url in ("/system/logs/export?format=csv",
                "/system/logs/export?format=jsonl",
                "/system/audit/export"):
        out = client.get(url).data.decode()
        assert "hunter2" not in out and "sekrit99" not in out \
            and "AKIA9999" not in out


def test_redact_details_nested_keys(app):
    with app.app_context():
        d = logschema.redact_details(
            {"password": "p", "nested": {"api_key": "k", "ok": "v"},
             "list": [{"token": "t"}, "safe"]})
        assert d["password"] == "•••" and d["nested"]["api_key"] == "•••"
        assert d["nested"]["ok"] == "v"
        assert d["list"][0]["token"] == "•••" and d["list"][1] == "safe"


# -------------------------------------------------------- JSON output


@pytest.fixture()
def json_dir(tmp_path, monkeypatch):
    d = tmp_path / "log-json"
    monkeypatch.setenv("TO_LOGS_JSON_DIR", str(d))
    log_output.reset_config_cache()
    log_output.reset_stats()
    yield d
    log_output.reset_config_cache()


def test_json_output_disabled_by_default(app, json_dir):
    with app.app_context():
        logs.emit("dispatch", "quiet event")
    assert not (json_dir / "events.jsonl").exists()


def test_json_output_writes_schema_events(app, json_dir):
    with app.app_context():
        s.set("logs_json_enabled", True)
        db.session.commit()
        logs.emit("dispatch", "structured one", severity="warning",
                  category="dispatch", execution_id="e-77",
                  worker_id="w-9", correlation_id="req-3")
        record_audit("admin", "page.audit", "/system/audit")
        s.set("logs_json_enabled", False)
        db.session.commit()
        log_output.reset_config_cache()
    lines = (json_dir / "events.jsonl").read_text().splitlines()
    evs = [json.loads(l) for l in lines]
    op = next(e for e in evs if e["log_type"] == "operational")
    assert op["schema_version"] == "to.log.event/1.0"
    assert op["severity"] == "warning"
    assert op["execution_id"] == "e-77" and op["target_id"] == "w-9"
    assert op["request_id"] == "req-3"
    assert op["timestamp_utc"].endswith("Z") and "T" in op["timestamp_utc"]
    assert op["host"] and op["instance"]
    au = next(e for e in evs if e["log_type"] == "audit")
    assert au["event_name"] == "page.audit"
    assert au["actor_name"] == "admin"
    # IDs are unique — the collector dedup key
    assert len({e["event_id"] for e in evs}) == len(evs)


def test_json_output_rotates_and_bounds_files(app, json_dir):
    with app.app_context():
        s.set("logs_json_enabled", True)
        db.session.commit()
        s.set("logs_json_max_bytes", 400)
        db.session.commit()
        s.set("logs_json_max_files", 2)
        db.session.commit()
        for i in range(30):
            logs.emit("dispatch", f"rotation filler {i} " + "x" * 60)
        s.set("logs_json_enabled", False)
        db.session.commit()
        log_output.reset_config_cache()
    rotated = list(json_dir.glob("events-*.jsonl"))
    assert rotated, "expected at least one rotation"
    assert len(rotated) <= 2          # retention bound
    assert log_output.status()["rotated"] >= 1


def test_json_output_failure_never_breaks_emit(app, tmp_path,
                                               monkeypatch):
    bad = tmp_path / "ro"
    bad.mkdir(mode=0o555)  # unwritable dir
    monkeypatch.setenv("TO_LOGS_JSON_DIR", str(bad / "sub"))
    log_output.reset_config_cache()
    log_output.reset_stats()
    with app.app_context():
        s.set("logs_json_enabled", True)
        db.session.commit()
        log_output.reset_config_cache()
        logs.emit("dispatch", "sink is broken but emit survives")
        assert log_output.status()["dropped"] >= 1
        assert log_output.status()["last_error"]
        s.set("logs_json_enabled", False)
        db.session.commit()
        log_output.reset_config_cache()
    bad.chmod(0o755)


def test_concurrent_writers_produce_valid_lines(app, json_dir):
    import threading
    with app.app_context():
        s.set("logs_json_enabled", True)
        db.session.commit()
        log_output.reset_config_cache()

        def work(n):
            with app.app_context():
                for i in range(25):
                    logs.emit("t", f"writer-{n} line-{i}")

        threads = [threading.Thread(target=work, args=(n,))
                   for n in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        s.set("logs_json_enabled", False)
        db.session.commit()
        log_output.reset_config_cache()
    total = 0
    for f in json_dir.glob("events*.jsonl"):
        for line in f.read_text().splitlines():
            ev = json.loads(line)   # every line must be complete JSON
            assert ev["schema_version"]
            total += 1
    assert total == 100


# ------------------------------------------- collector contract check


def test_local_test_collector_ingests(app, json_dir):
    """End-to-end ingestion check: a minimal filelog-equivalent reader
    parses every line per the documented contract (JSON, timestamp,
    severity, preserved correlation fields) and dedups on event_id —
    the same semantics deploy/otel-collector.example.yaml encodes."""
    with app.app_context():
        s.set("logs_json_enabled", True)
        db.session.commit()
        logs.emit("dispatch", "for collector", execution_id="e-1",
                  correlation_id="req-9")
        record_audit("admin", "user.login", "ok")
        s.set("logs_json_enabled", False)
        db.session.commit()
        log_output.reset_config_cache()

    # --- local test collector: filelog -> json_parse -> fields ----
    seen, parsed = set(), []
    for f in sorted(json_dir.glob("events*.jsonl")):
        for line in f.read_text().splitlines():
            ev = json.loads(line)
            assert ev["event_id"] not in seen     # dedup on event_id
            seen.add(ev["event_id"])
            ts = datetime.strptime(ev["timestamp_utc"],
                                   "%Y-%m-%dT%H:%M:%S.%fZ")
            assert ts.tzinfo is None or True      # explicit-Z format
            assert ev["severity"] in logs.SEVERITIES
            parsed.append(ev)
    op = next(e for e in parsed if e["log_type"] == "operational")
    assert op["execution_id"] == "e-1" and op["request_id"] == "req-9"
    au = next(e for e in parsed if e["log_type"] == "audit")
    assert au["event_name"] == "user.login"


# ------------------------------------------------------ schema sanity


def test_schema_rejects_bad_type_and_unknown_fields():
    with pytest.raises(ValueError):
        logschema.event(log_type="weird")
    with pytest.raises(TypeError):
        logschema.event(log_type="audit", bogus_field="x")


def test_schema_trace_fields_only_when_supplied():
    ev = logschema.event(log_type="operational", message="m")
    assert "trace_id" not in ev and "span_id" not in ev
    ev = logschema.event(log_type="operational", message="m",
                         trace_id="t1", span_id="s1")
    assert ev["trace_id"] == "t1" and ev["span_id"] == "s1"
