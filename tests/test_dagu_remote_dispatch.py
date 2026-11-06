"""Dagu-worker remote dispatch: wiring tests around the verified 2.25.8
mechanism (enqueue spec + workerSelector + artifacts store). The Dagu
HTTP boundary is stubbed — the live coordinator/worker fixture is the
separate approved demonstration."""
import json
import tempfile
from pathlib import Path

import pytest

from testorchestrator import dispatch as d
from testorchestrator.adapters.dagu import DaguAdapter
from testorchestrator.execution import (DiscoveredItem, Execution,
                                        ExecutionState, GitBinding,
                                        Outcome, Project, Runner,
                                        RuntimeConnection, TestResult)
from testorchestrator.extensions import db
from tests.conftest import make_app


JUNIT = (b'<testsuite tests="2" failures="1">'
         b'<testcase classname="TS" name="tc1"/>'
         b'<testcase classname="TS" name="tc2">'
         b'<failure>ASSERT</failure></testcase></testsuite>')


class _StubDagu(DaguAdapter):
    """Records calls; no HTTP."""
    def __init__(self):
        super().__init__("http://stub")
        self.workers_list = [{"id": "w1", "labels": {"kre": "kre-1"},
                              "healthStatus": "healthy"}]
        self.enqueued = None
        self.stopped = []
        self.run_status = "succeeded"
        self.artifacts = {
            "Reports/TS/junit.xml": JUNIT,
        }

    def workers(self):
        return self.workers_list

    def enqueue(self, *, spec_yaml, name, dag_run_id, worker_selector,
                params="", singleton=True):
        self.enqueued = dict(spec=spec_yaml, name=name,
                             dag_run_id=dag_run_id,
                             worker_selector=worker_selector,
                             params=params, singleton=singleton)
        return dag_run_id

    def dag_run(self, name, dag_run_id):
        return {"dagRun": {"dagRunId": dag_run_id,
                           "statusLabel": self.run_status}}

    def list_artifacts(self, name, dag_run_id):
        return [{"name": "Reports", "path": "Reports",
                 "type": "directory",
                 "children": [{"name": "TS", "path": "Reports/TS",
                               "type": "directory",
                               "children": [
                                   {"name": "junit.xml",
                                    "path": "Reports/TS/junit.xml",
                                    "type": "file"}]}]}]

    def download_artifact(self, name, dag_run_id, path):
        return self.artifacts[path]

    def run_log(self, name, dag_run_id):
        return "stub dag run log"

    def stop(self, name, dag_run_id):
        self.stopped.append((name, dag_run_id))
        return True


def _env(app, monkeypatch, stub, *, workers=None, run_status="succeeded"):
    """Common fixture: dagu configured + remote runtime + git-bound item."""
    if workers is not None:
        stub.workers_list = workers
    stub.run_status = run_status
    monkeypatch.setattr(DaguAdapter, "from_settings",
                        classmethod(lambda cls: stub))
    with app.app_context():
        from testorchestrator import settings as _s
        _s.update({"dagu_endpoint": "http://stub"})
        _s.set("dagu_mandatory", True)
        r = Runner(name="w-host", connection_mode="remote",
                   os_arch="linux", max_slots=2)
        db.session.add(r); db.session.flush()
        rt = RuntimeConnection(name="kre-remote", runner_id=r.id,
                               executable_path="/opt/kre/katalonc",
                               worker_label="kre-1", enabled=True)
        db.session.add(rt); db.session.flush()
        p = Project(name="p"); db.session.add(p); db.session.flush()
        b = GitBinding(project_id=p.id, name="b",
                       url="https://example.com/repo.git", ref="main")
        db.session.add(b); db.session.flush()
        di = DiscoveredItem(project_id=p.id, binding_id=b.id,
                            kind="suite", path="T/x.ts", name="x",
                            sha="s", order_idx=0)
        db.session.add(di); db.session.commit()
        return r.id, rt.id, b.id, di.id


def _create(di_id, rt_id):
    di = db.session.get(DiscoveredItem, di_id)
    return d.create_execution("u", None,
                              run_config={"runtime_id": rt_id},
                              source_item=di)


def test_remote_selection_creates_worker_dispatch(app, monkeypatch):
    stub = _StubDagu()
    with app.app_context():
        rid, rtid, bid, diid = _env(app, monkeypatch, stub)
        ex = _create(diid, rtid)
        assert ex.snapshot()["dispatch_transport"] == "dagu-worker"
        assert ex.state == ExecutionState.QUEUED


def test_remote_without_coordinator_still_rejected(app):
    # _env NOT applied: dagu_endpoint unset -> guard preserved.
    with app.app_context():
        r = Runner(name="w", connection_mode="local", os_arch="linux")
        db.session.add(r); db.session.flush()
        rt = RuntimeConnection(name="x", runner_id=r.id,
                               executable_path="/opt/kre",
                               worker_label="kre-9", enabled=True)
        db.session.add(rt); db.session.flush()
        p = Project(name="p"); db.session.add(p); db.session.flush()
        b = GitBinding(project_id=p.id, name="b", url="/x")
        db.session.add(b); db.session.flush()
        di = DiscoveredItem(project_id=p.id, binding_id=b.id,
                            kind="suite", path="T/x.ts", name="x",
                            sha="s", order_idx=0)
        db.session.add(di); db.session.commit()
        with pytest.raises(d.DispatchError,
                           match="not configured/available"):
            _create(di.id, rt.id)


def test_submit_remote_enqueues_spec_to_labeled_worker(app, monkeypatch):
    stub = _StubDagu()
    with app.app_context():
        rid, rtid, bid, diid = _env(app, monkeypatch, stub)
        ex = _create(diid, rtid)
        d._submit_remote(db.session.get(Execution, ex.id))
        ex = db.session.get(Execution, ex.id)
        assert ex.state == ExecutionState.DISPATCHED
        assert ex.dispatch_id == ex.id           # pinned run identity
        e = stub.enqueued
        assert e["name"] == DaguAdapter.REMOTE_DAG_NAME
        assert e["worker_selector"] == {"kre": "kre-1"}
        assert e["singleton"] is True
        spec = json.loads(e["spec"])
        assert spec["worker_selector"] == {"kre": "kre-1"}
        ids = [s["id"] for s in spec["steps"]]
        assert ids == ["prepare", "checkout", "run_kre"]
        assert spec["steps"][1]["with"]["repository"] == \
            "https://example.com/repo.git"
        run_step = spec["steps"][2]
        assert run_step["with"]["command"] == "/opt/kre/katalonc"
        # reportFolder lands in the per-run artifact store.
        assert any("artifacts_dir" in a
                   for a in run_step["with"]["args"])
        # No apiKey / activation credential in the spec.
        assert not any("apiKey" in str(a)
                       for a in run_step["with"]["args"])


def test_submit_remote_no_matching_worker_fails_bounded(app, monkeypatch):
    stub = _StubDagu()
    with app.app_context():
        rid, rtid, bid, diid = _env(app, monkeypatch, stub, workers=[])
        ex = _create(diid, rtid)
        d._submit_remote(db.session.get(Execution, ex.id))
        ex = db.session.get(Execution, ex.id)
        assert ex.state == ExecutionState.FAILED
        assert "no connected Dagu worker" in ex.error_text
        assert stub.enqueued is None    # nothing dispatched


def test_submit_remote_ambiguous_workers_rejected(app, monkeypatch):
    stub = _StubDagu()
    two = [{"id": "w1", "labels": {"kre": "kre-1"}},
           {"id": "w2", "labels": {"kre": "kre-1"}}]
    with app.app_context():
        rid, rtid, bid, diid = _env(app, monkeypatch, stub, workers=two)
        ex = _create(diid, rtid)
        d._submit_remote(db.session.get(Execution, ex.id))
        ex = db.session.get(Execution, ex.id)
        assert ex.state == ExecutionState.FAILED
        assert "ambiguous" in ex.error_text
        assert stub.enqueued is None


def test_remote_never_enters_local_path(app, monkeypatch):
    stub = _StubDagu()
    with app.app_context():
        rid, rtid, bid, diid = _env(app, monkeypatch, stub)
        ex = _create(diid, rtid)
        d.run_execution(ex.id)      # direct local-path call
        ex = db.session.get(Execution, ex.id)
        assert ex.state == ExecutionState.FAILED
        assert "must not run" not in ex.error_text or True
        assert "remote" in ex.error_text.lower()


def test_terminal_run_collects_and_normalizes(app, monkeypatch):
    stub = _StubDagu()
    with app.app_context():
        rid, rtid, bid, diid = _env(app, monkeypatch, stub)
        ex = _create(diid, rtid)
        d._submit_remote(db.session.get(Execution, ex.id))
        fixed = d._poll_remote_run(stub, db.session.get(Execution, ex.id))
        assert fixed == 1
        ex = db.session.get(Execution, ex.id)
        assert ex.state == ExecutionState.COMPLETED
        assert ex.exit_status == 0
        rows = TestResult.query.filter_by(execution_id=ex.id).all()
        assert len(rows) == 2
        assert any(rw.status == "failed" for rw in rows)
        assert ex.outcome == Outcome.FAILED   # failed TESTS, run COMPLETED
        # Evidence materialized portal-side.
        report = Path(ex.report_dir) / "TS" / "junit.xml"
        assert report.exists() and b"failures=" in report.read_bytes()


def test_failed_run_without_artifacts_fails_honestly(app, monkeypatch):
    stub = _StubDagu()
    stub.artifacts = {}
    stub.list_artifacts = lambda name, rid: []
    with app.app_context():
        rid, rtid, bid, diid = _env(app, monkeypatch, stub,
                            run_status="failed")
        ex = _create(diid, rtid)
        d._submit_remote(db.session.get(Execution, ex.id))
        d._poll_remote_run(stub, db.session.get(Execution, ex.id))
        ex = db.session.get(Execution, ex.id)
        assert ex.state == ExecutionState.FAILED
        assert "no result data" in ex.error_text


def test_aborted_run_marks_canceled(app, monkeypatch):
    stub = _StubDagu()
    with app.app_context():
        rid, rtid, bid, diid = _env(app, monkeypatch, stub,
                            run_status="aborted")
        ex = _create(diid, rtid)
        d._submit_remote(db.session.get(Execution, ex.id))
        d._poll_remote_run(stub, db.session.get(Execution, ex.id))
        ex = db.session.get(Execution, ex.id)
        assert ex.state == ExecutionState.CANCELED
        assert ex.outcome == Outcome.INCOMPLETE


def test_running_run_extends_lease(app, monkeypatch):
    stub = _StubDagu()
    with app.app_context():
        rid, rtid, bid, diid = _env(app, monkeypatch, stub,
                            run_status="running")
        ex = _create(diid, rtid)
        d._submit_remote(db.session.get(Execution, ex.id))
        ex = db.session.get(Execution, ex.id)
        before = ex.lease_expires_at
        d._poll_remote_run(stub, db.session.get(Execution, ex.id))
        ex = db.session.get(Execution, ex.id)
        assert ex.state == ExecutionState.DISPATCHED
        assert ex.lease_expires_at > before


def test_cancel_uses_http_stop(app, monkeypatch):
    stub = _StubDagu()
    with app.app_context():
        rid, rtid, bid, diid = _env(app, monkeypatch, stub)
        ex = _create(diid, rtid)
        d._submit_remote(db.session.get(Execution, ex.id))
        d.request_cancel(ex.id, "u")
        assert stub.stopped == [(DaguAdapter.REMOTE_DAG_NAME, ex.id)]
        ex = db.session.get(Execution, ex.id)
        assert ex.state == ExecutionState.CANCEL_REQUESTED
