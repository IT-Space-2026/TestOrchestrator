"""Regression: the dagu-dispatch outbox path must commit runner
assignment BEFORE submitting the workflow run. The workflow's agent
step polls /api/agent/tasks the moment the run starts — a task not yet
assigned is invisible to that single poll, the lease then decays and
the execution reconciles to UNKNOWN without ever being claimed
(observed: execution stranded UNKNOWN with no agent pickup)."""
import pytest

from testorchestrator import dispatch as d
from testorchestrator import settings as _s
from testorchestrator.adapters.dagu import DaguAdapter
from testorchestrator.execution import (DiscoveredItem, Execution,
                                        ExecutionState, GitBinding,
                                        OutboxEvent, Outcome, Project,
                                        Runner, RuntimeConnection)
from testorchestrator.extensions import db


class _OrderProbe(DaguAdapter):
    """Records what the committed DB row looks like AT SUBMIT TIME —
    the exact instant the dagu run (and its agent step) begins."""
    def __init__(self):
        super().__init__("http://stub")
        self.submits = 0
        self.row_at_submit = None
        self.fail = False

    def submit(self, payload):
        self.submits += 1
        if self.fail:
            raise RuntimeError("coordinator down")
        ex = db.session.get(Execution, payload["execution_id"])
        self.row_at_submit = dict(state=ex.state, runner_id=ex.runner_id,
                                  runtime_id=ex.runtime_id,
                                  lease=ex.lease_expires_at)
        return "run-1"


def _env(app, monkeypatch, stub):
    monkeypatch.setattr(DaguAdapter, "from_settings",
                        classmethod(lambda cls: stub))
    with app.app_context():
        _s.update({"dagu_endpoint": "http://stub"})
        _s.set("dagu_mandatory", True)
        r = Runner(name="local", connection_mode="local",
                   os_arch="linux", max_slots=2)
        db.session.add(r); db.session.flush()
        rt = RuntimeConnection(name="kre", runner_id=r.id,
                               executable_path="/opt/kre",
                               enabled=True)
        db.session.add(rt); db.session.flush()
        p = Project(name="p"); db.session.add(p); db.session.flush()
        b = GitBinding(project_id=p.id, name="b", url="/x")
        db.session.add(b); db.session.flush()
        di = DiscoveredItem(project_id=p.id, binding_id=b.id,
                            kind="suite", path="T/x.ts", name="x",
                            sha="s", order_idx=0)
        db.session.add(di); db.session.commit()
        return rt.id, di.id


def _event_for(ex):
    return OutboxEvent.query.filter(
        OutboxEvent.kind == "dagu-dispatch",
        OutboxEvent.payload_json.contains(ex.id)).first()


def test_task_claimable_at_submit_time(app, monkeypatch):
    """At the instant the dagu run starts, the execution must already
    be DISPATCHED to its runner — the agent's first /tasks poll sees
    it, no window for a stranded UNKNOWN."""
    stub = _OrderProbe()
    with app.app_context():
        rtid, diid = _env(app, monkeypatch, stub)
        ex = d.create_execution(
            "u", None, run_config={"runtime_id": rtid},
            source_item=db.session.get(DiscoveredItem, diid))
        d._handle_event(_event_for(ex))
        assert stub.submits == 1
        assert stub.row_at_submit["state"] == ExecutionState.DISPATCHED
        assert stub.row_at_submit["runner_id"]
        assert stub.row_at_submit["runtime_id"]
        assert stub.row_at_submit["lease"] is not None
        db.session.refresh(ex)
        assert ex.state == ExecutionState.DISPATCHED
        assert ex.dispatch_id == "run-1"


def test_reprocessed_event_does_not_resubmit(app, monkeypatch):
    """Idempotency: a second pass over the same event finds the
    assignment CAS already lost (state != QUEUED) and returns before
    submit — no duplicate dagu run for one execution."""
    stub = _OrderProbe()
    with app.app_context():
        rtid, diid = _env(app, monkeypatch, stub)
        ex = d.create_execution(
            "u", None, run_config={"runtime_id": rtid},
            source_item=db.session.get(DiscoveredItem, diid))
        ev = _event_for(ex)
        d._handle_event(ev)
        d._handle_event(ev)
        assert stub.submits == 1


def test_submit_failure_after_assignment_fails_not_unknown(app,
                                                           monkeypatch):
    """If the coordinator rejects the submit after assignment, the
    execution fails honestly (slot released) — it must not sit
    DISPATCHED until the lease decays to UNKNOWN."""
    stub = _OrderProbe()
    stub.fail = True
    with app.app_context():
        rtid, diid = _env(app, monkeypatch, stub)
        ex = d.create_execution(
            "u", None, run_config={"runtime_id": rtid},
            source_item=db.session.get(DiscoveredItem, diid))
        d._handle_event(_event_for(ex))
        db.session.refresh(ex)
        assert ex.state == ExecutionState.FAILED
        assert ex.outcome == Outcome.INCOMPLETE
        assert ex.error_kind == "infrastructure"


class _StatusStub(DaguAdapter):
    def __init__(self, label):
        super().__init__("http://stub")
        self.label = label

    def status(self, dispatch_id):
        return {"statusLabel": self.label}


def _cancel_pending_exec(app, monkeypatch, label, cancel=True):
    stub = _StatusStub(label)
    monkeypatch.setattr(DaguAdapter, "from_settings",
                        classmethod(lambda cls: stub))
    rtid, diid = _env(app, monkeypatch, stub)
    ex = d.create_execution(
        "u", None, run_config={"runtime_id": rtid},
        source_item=db.session.get(DiscoveredItem, diid))
    ex.dispatch_id = "run-x"
    if cancel:
        ex.state = ExecutionState.CANCEL_REQUESTED
        ex.cancel_reason = "test cancel"
    else:
        ex.state = ExecutionState.DISPATCHED
    db.session.commit()
    return ex


def test_requested_cancel_acknowledged_by_abort_is_canceled(app,
                                                          monkeypatch):
    """User-requested cancel + coordinator-confirmed abort -> terminal
    CANCELED (same rule as the remote transport) — not UNKNOWN."""
    with app.app_context():
        ex = _cancel_pending_exec(app, monkeypatch, "aborted")
        assert d.reconcile_dagu_runs() == 1
        db.session.refresh(ex)
        assert ex.state == ExecutionState.CANCELED
        assert ex.outcome == Outcome.INCOMPLETE


def test_unrequested_abort_reconciles_unknown(app, monkeypatch):
    """An abort nobody asked for is still ambiguous -> UNKNOWN."""
    with app.app_context():
        ex = _cancel_pending_exec(app, monkeypatch, "aborted",
                                  cancel=False)
        assert d.reconcile_dagu_runs() == 1
        db.session.refresh(ex)
        assert ex.state == ExecutionState.UNKNOWN


def test_requested_cancel_with_failed_run_stays_unknown(app,
                                                        monkeypatch):
    """A 'failed' dagu label under a pending cancel is ambiguous (abort
    or genuine failure) -> UNKNOWN, never silently CANCELED."""
    with app.app_context():
        ex = _cancel_pending_exec(app, monkeypatch, "failed")
        assert d.reconcile_dagu_runs() == 1
        db.session.refresh(ex)
        assert ex.state == ExecutionState.UNKNOWN


def test_explicit_runtime_honored_in_dagu_dispatch(app, monkeypatch):
    """run_config.requested_runtime must reach the assignment — the
    dagu path may not silently pick another eligible runtime."""
    stub = _OrderProbe()
    with app.app_context():
        rtid, diid = _env(app, monkeypatch, stub)
        rt2 = RuntimeConnection(name="kre-explicit", runner_id=db.session
                                .query(Runner).first().id,
                                executable_path="/opt/kre2", enabled=True)
        db.session.add(rt2); db.session.commit()
        ex = d.create_execution(
            "u", None, run_config={"runtime_id": rt2.id},
            source_item=db.session.get(DiscoveredItem, diid))
        d._handle_event(_event_for(ex))
        db.session.refresh(ex)
        assert ex.runtime_id == rt2.id
        assert stub.row_at_submit["runtime_id"] == rt2.id


def test_explicit_runtime_unavailable_fails_visibly(app, monkeypatch):
    """An explicit runtime pick that is valid at queue time but becomes
    unavailable before dispatch rejects visibly — never a retarget to
    another eligible runtime."""
    stub = _OrderProbe()
    with app.app_context():
        rtid, diid = _env(app, monkeypatch, stub)
        rt2 = RuntimeConnection(name="kre-explicit", runner_id=db.session
                                .query(Runner).first().id,
                                executable_path="/opt/kre2", enabled=True)
        db.session.add(rt2); db.session.commit()
        ex = d.create_execution(
            "u", None, run_config={"runtime_id": rt2.id},
            source_item=db.session.get(DiscoveredItem, diid))
        rt2.enabled = False
        db.session.commit()
        d._handle_event(_event_for(ex))
        db.session.refresh(ex)
        assert ex.state == ExecutionState.FAILED
        assert "no fallback" in ex.error_text
        assert stub.submits == 0
