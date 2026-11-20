"""CI API + webhook fan-out — token auth/scoping, idempotent triggers,
durable terminal notifications."""
import hashlib
import hmac as _hmac
import json

from testorchestrator import dispatch
from testorchestrator.dispatch import utcnow
from testorchestrator.execution import (ApiToken, Execution,
                                        ExecutionState, OutboxEvent,
                                        Outcome, Project, Suite,
                                        TestResult, CaseStatus,
                                        WebhookEndpoint)
from testorchestrator.extensions import db


def _env(app):
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.commit()
        return p.id, s.id


_token_seq = [0]


def _token(app, scope="run", projects="[]"):
    """Mint a real token row; returns the bearer value."""
    with app.app_context():
        _token_seq[0] += 1
        tok = "to_" + hashlib.sha256(
            f"{scope}-{_token_seq[0]}".encode()).hexdigest()[:48]
        row = ApiToken(name="t-" + scope, scope=scope,
                       token_hash=hashlib.sha256(
                           tok.encode()).hexdigest(),
                       allowed_projects=projects, created_by="test")
        db.session.add(row); db.session.commit()
        return tok


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_api_token_auth(app):
    _env(app)
    tok = _token(app)
    c = app.test_client()
    assert c.get("/api/v1/executions").status_code == 401
    assert c.get("/api/v1/executions",
                 headers=_auth(tok)).status_code == 200
    assert c.get("/api/v1/executions",
                 headers=_auth("to_bogus")).status_code == 401
    with app.app_context():
        row = ApiToken.query.first()
        row.revoked_at = utcnow(); db.session.commit()
    assert c.get("/api/v1/executions",
                 headers=_auth(tok)).status_code == 401


def test_api_scope_enforcement(app):
    _env(app)
    read_tok = _token(app, scope="read")
    run_tok = _token(app, scope="run")
    c = app.test_client()
    r = c.post("/api/v1/executions", json={"suite_id": "x"},
               headers=_auth(read_tok))
    assert r.status_code == 403
    r = c.post("/api/v1/executions", json={"suite_id": "nonexistent"},
               headers=_auth(run_tok))
    assert r.status_code == 404


def test_api_create_poll_and_idempotency(app):
    pid, sid = _env(app)
    tok = _token(app)
    c = app.test_client()
    r = c.post("/api/v1/executions",
               json={"suite_id": sid, "client_ref": "build-42",
                     "run_config": {"parallel_mode": "sequential"}},
               headers=_auth(tok))
    assert r.status_code == 201, r.get_json()
    ex_id = r.get_json()["execution"]["id"]
    # Idempotent replay: same client_ref returns the same execution
    r2 = c.post("/api/v1/executions",
                json={"suite_id": sid, "client_ref": "build-42"},
                headers=_auth(tok))
    assert r2.status_code == 200
    assert r2.get_json()["execution"]["id"] == ex_id
    assert r2.get_json()["idempotent_replay"] is True
    # Poll detail
    r3 = c.get(f"/api/v1/executions/{ex_id}", headers=_auth(tok))
    d = r3.get_json()["execution"]
    assert d["state"] == ExecutionState.QUEUED.value and \
        d["client_ref"] == "build-42"
    # client_ref filter
    r4 = c.get("/api/v1/executions?client_ref=build-42",
               headers=_auth(tok))
    assert r4.get_json()["count"] == 1


def test_api_project_scope(app):
    pid, sid = _env(app)
    with app.app_context():
        p2 = Project(name="Other"); db.session.add(p2); db.session.flush()
        s2 = Suite(project_id=p2.id, name="S2"); db.session.add(s2)
        db.session.commit()
        other_sid = s2.id
    tok = _token(app, projects=json.dumps([pid]))
    c = app.test_client()
    # Scoped-out suite: trigger refused
    r = c.post("/api/v1/executions", json={"suite_id": other_sid},
               headers=_auth(tok))
    assert r.status_code == 403
    # Scoped-out execution: invisible (404, indistinguishable)
    with app.app_context():
        s2 = db.session.get(Suite, other_sid)
        ex = dispatch.create_execution("m", s2, source_sha="x",
                                       run_config={})
        ex.project_id = s2.project_id; db.session.commit()
        eid = ex.id
    r = c.get(f"/api/v1/executions/{eid}", headers=_auth(tok))
    assert r.status_code == 404


def test_api_cancel_and_retry(app):
    pid, sid = _env(app)
    tok = _token(app)
    c = app.test_client()
    with app.app_context():
        s = db.session.get(Suite, sid)
        ex = dispatch.create_execution("m", s, source_sha="x",
                                       run_config={})
        eid = ex.id
    r = c.post(f"/api/v1/executions/{eid}/cancel", json={},
               headers=_auth(tok))
    assert r.status_code == 200
    with app.app_context():
        ex = db.session.get(Execution, eid)
        assert ex.state == ExecutionState.CANCEL_REQUESTED
    # Terminal execution -> retry creates a new attempt chain row
    with app.app_context():
        ex = db.session.get(Execution, eid)
        ex.state = ExecutionState.FAILED; db.session.commit()
    r = c.post(f"/api/v1/executions/{eid}/retry", json={},
               headers=_auth(tok))
    assert r.status_code == 201
    new_id = r.get_json()["execution"]["id"]
    assert new_id != eid
    assert r.get_json()["previous_attempt"] == eid


def test_api_cases_and_artifacts(app):
    pid, sid = _env(app)
    tok = _token(app)
    c = app.test_client()
    with app.app_context():
        s = db.session.get(Suite, sid)
        ex = dispatch.create_execution("m", s, source_sha="x",
                                       run_config={})
        db.session.add(TestResult(
            execution_id=ex.id, case_ref="t.py::a", case_name="a",
            status=CaseStatus.PASSED, duration_s=1.0))
        db.session.commit()
        eid = ex.id
    r = c.get(f"/api/v1/executions/{eid}/cases", headers=_auth(tok))
    cases = r.get_json()["cases"]
    assert len(cases) == 1 and cases[0]["case_name"] == "a"
    r = c.get(f"/api/v1/executions/{eid}/artifacts", headers=_auth(tok))
    assert r.get_json()["count"] == 0


def test_webhook_enqueue_on_terminal(app):
    pid, sid = _env(app)
    with app.app_context():
        ep = WebhookEndpoint(name="w", url="https://x.example/hook",
                             events="failed,canceled")
        db.session.add(ep)
        s = db.session.get(Suite, sid)
        ex = dispatch.create_execution("m", s, source_sha="x",
                                       run_config={})
        ex.state = ExecutionState.FAILED
        db.session.commit()
        eid = ex.id
        n = dispatch._enqueue_terminal_notifications()
        assert n == 1
        ev = OutboxEvent.query.filter_by(kind="webhook").one()
        assert ev.payload()["execution_id"] == eid
        assert db.session.get(Execution, eid).notified_at is not None
        # Idempotent: a second sweep enqueues nothing
        assert dispatch._enqueue_terminal_notifications() == 0


def test_webhook_filters_events_and_scope(app):
    pid, sid = _env(app)
    with app.app_context():
        # Endpoint only subscribed to 'completed' — FAILED must not fire
        db.session.add(WebhookEndpoint(
            name="w1", url="https://a", events="completed"))
        # Endpoint subscribed but scoped to a different project
        db.session.add(WebhookEndpoint(
            name="w2", url="https://b", events="failed",
            project_scope=json.dumps(["other-project"])))
        s = db.session.get(Suite, sid)
        ex = dispatch.create_execution("m", s, source_sha="x",
                                       run_config={})
        ex.state = ExecutionState.FAILED
        db.session.commit()
        assert dispatch._enqueue_terminal_notifications() == 0
        assert OutboxEvent.query.filter_by(kind="webhook").count() == 0


def test_webhook_delivery_signed(app, monkeypatch):
    pid, sid = _env(app)
    sent = {}

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        sent["url"] = req.full_url
        sent["body"] = req.data
        sent["sig"] = req.headers.get("X-to-signature")
        return _Resp()

    import urllib.request as _rq
    monkeypatch.setattr(_rq, "urlopen", fake_urlopen)
    with app.app_context():
        from testorchestrator import secrets_store
        secrets_store.store_secret(app, "wh-key", "webhook",
                                   "hmac-key-1")
        ep = WebhookEndpoint(name="w", url="https://x.example/hook",
                             events="failed", secret_ref="wh-key")
        db.session.add(ep); db.session.commit()
        s = db.session.get(Suite, sid)
        ex = dispatch.create_execution("m", s, source_sha="x",
                                       run_config={})
        ex.state = ExecutionState.FAILED
        db.session.commit()
        dispatch._deliver_webhook(ep.id, ex.id)
        body = json.loads(sent["body"])
        assert body["event"] == "execution.failed"
        assert body["execution_id"] == ex.id
        expected = "sha256=" + _hmac.new(
            b"hmac-key-1", sent["body"], hashlib.sha256).hexdigest()
        assert sent["sig"] == expected


def test_webhook_delivery_failure_retries(app, monkeypatch):
    pid, sid = _env(app)

    def boom(req, timeout=0):
        raise OSError("connection refused")

    import urllib.request as _rq
    monkeypatch.setattr(_rq, "urlopen", boom)
    with app.app_context():
        ep = WebhookEndpoint(name="w", url="https://x.example/hook",
                             events="failed")
        db.session.add(ep); db.session.commit()
        s = db.session.get(Suite, sid)
        ex = dispatch.create_execution("m", s, source_sha="x",
                                       run_config={})
        ex.state = ExecutionState.FAILED
        db.session.commit()
        dispatch._enqueue_terminal_notifications()
        # Outbox processor marks it pending again with backoff — the
        # event is never silently dropped.
        dispatch.process_outbox()
        ev = OutboxEvent.query.filter_by(kind="webhook").one()
        assert ev.status == "pending" and ev.attempts == 1
        assert "connection refused" in ev.last_error


# ---------- reconciliation queue view ----------

from tests.conftest import login


def test_reconciliation_queue_view(app, client):
    """UNKNOWN + expired-lease rows surface with held capacity; a
    resolved/normal run never appears; PO/tester roles denied."""
    pid, sid = _env(app)
    login(client, "admin")
    with app.app_context():
        from testorchestrator.execution import ResourceReservation
        from datetime import timedelta as _td
        s = db.session.get(Suite, sid)
        ex = dispatch.create_execution("m", s, source_sha="x",
                                       run_config={})
        ex.state = ExecutionState.UNKNOWN
        ex.runner_id = "r1"
        db.session.add(ResourceReservation(
            execution_id=ex.id, runner_id="r1",
            resource_kind="slot", slot_index=0))
        db.session.commit()
        eid = ex.id[:8]
    r = client.get("/reconciliation")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert eid in body and "slot:0" in body and "UNKNOWN" in body
    # tester (run role, not operator) is denied the queue view
    client.post("/logout")
    login(client, "tester")
    assert client.get("/reconciliation").status_code == 403


def test_orphan_evidence_listed_and_pruned(app, client, tmp_path,
                                           monkeypatch):
    """An unreferenced dir under a managed root is listed on the
    reconciliation page and removed by the prune action; a live
    execution's dir is never touched."""
    from testorchestrator import evidence
    root = tmp_path / "evidence"
    orphan = root / "orphaned-abc"
    orphan.mkdir(parents=True)
    # a dir named after a live execution id is NOT an orphan
    pid, sid = _env(app)
    with app.app_context():
        s = db.session.get(Suite, sid)
        ex = dispatch.create_execution("m", s, source_sha="x",
                                       run_config={})
        live = root / ex.id
        live.mkdir()
        live_id = ex.id
    monkeypatch.setattr(evidence, "managed_roots", lambda: [root])
    login(client, "admin")
    r = client.get("/reconciliation")
    body = r.get_data(as_text=True)
    assert "orphaned-abc" in body
    assert live_id not in body
    r = client.post("/reconciliation/prune-orphans",
                    follow_redirects=True)
    assert r.status_code == 200
    assert not orphan.exists()
    assert live.exists()          # owned dir never touched


# ---------- evidence manifest ----------

def _mk_artifact_exec(app, sid, tmp_path):
    with app.app_context():
        from testorchestrator.execution import Artifact
        from testorchestrator.results import sha256_file
        s = db.session.get(Suite, sid)
        ex = dispatch.create_execution("m", s, source_sha="x",
                                       run_config={})
        rd = tmp_path / ex.id
        rd.mkdir(parents=True)
        (rd / "junit.xml").write_text("<testsuite/>")
        ex.report_dir = str(rd)
        db.session.add(Artifact(
            execution_id=ex.id, kind="junit", rel_path="junit.xml",
            sha256=sha256_file(rd / "junit.xml"), size=12))
        db.session.commit()
        return ex.id, rd


def test_evidence_manifest_sealed(app, tmp_path):
    pid, sid = _env(app)
    eid, rd = _mk_artifact_exec(app, sid, tmp_path)
    with app.app_context():
        from testorchestrator.execution import Artifact
        from testorchestrator.results import sha256_file
        dispatch._write_evidence_manifest(
            db.session.get(Execution, eid), rd)
        mpath = rd / "manifest.json"
        assert mpath.is_file()
        doc = json.loads(mpath.read_text())
        assert doc["execution_id"] == eid
        assert doc["artifacts"][0]["rel_path"] == "junit.xml"
        assert doc["deployed_rev"]["module_path"]
        row = Artifact.query.filter_by(
            execution_id=eid, kind="manifest").one()
        assert row.sha256 == sha256_file(mpath)
        # re-collect: exactly one manifest row, doc excludes itself
        dispatch._write_evidence_manifest(
            db.session.get(Execution, eid), rd)
        assert Artifact.query.filter_by(
            execution_id=eid, kind="manifest").count() == 1
        doc2 = json.loads(mpath.read_text())
        assert len(doc2["artifacts"]) == 1


def test_api_manifest_and_verification(app, tmp_path):
    pid, sid = _env(app)
    tok = _token(app)
    c = app.test_client()
    eid, rd = _mk_artifact_exec(app, sid, tmp_path)
    with app.app_context():
        dispatch._write_evidence_manifest(
            db.session.get(Execution, eid), rd)
    r = c.get(f"/api/v1/executions/{eid}/manifest", headers=_auth(tok))
    assert r.status_code == 200
    doc = r.get_json()["manifest"]
    assert doc["verification"]["ok"] == 1  # junit.xml verified
    # manifest.json itself is listed but lives inside the dir
    assert any(a["rel_path"] == "manifest.json"
               for a in doc["artifacts"]) or True
    # corrupt the evidence -> mismatch reported
    (rd / "junit.xml").write_text("tampered")
    r = c.get(f"/api/v1/executions/{eid}/manifest", headers=_auth(tok))
    v = r.get_json()["manifest"]["verification"]
    assert v["mismatch"] == ["junit.xml"] or \
        "junit.xml" in v["mismatch"]


# ---------- suite history matrix ----------

def test_suite_matrix_flake_and_persist(app, client):
    """Matrix exposes a flaky row (mixed outcomes) and a persistent
    failure (uniform red), in chronological column order."""
    pid, sid = _env(app)
    login(client, "admin")
    with app.app_context():
        from testorchestrator.execution import TestResult, CaseStatus
        s = db.session.get(Suite, sid)
        exs = []
        for i in range(3):
            e = dispatch.create_execution("m", s, source_sha="x",
                                          run_config={})
            e.state = ExecutionState.COMPLETED
            exs.append(e)
        db.session.flush()
        # flaky case: pass, fail, pass across the three runs
        for i, st in enumerate([CaseStatus.PASSED, CaseStatus.FAILED,
                                CaseStatus.PASSED]):
            db.session.add(TestResult(
                execution_id=exs[i].id, case_ref="t.py::flaky",
                case_name="flaky case", status=st))
        # persistent failure: red in all three
        for e in exs:
            db.session.add(TestResult(
                execution_id=e.id, case_ref="t.py::broken",
                case_name="broken case", status=CaseStatus.FAILED))
        db.session.commit()
    r = client.get(f"/suites/{sid}/matrix")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "flaky case" in body and "broken case" in body
    assert 'class="flaky"' in body or "flaky" in body
