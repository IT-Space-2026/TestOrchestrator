"""Parallel unit executions: durable parent + identifiable children,
frozen per-unit assignments, idempotent aggregation, cancellation
propagation, and no dispatch of the parent itself."""
import json

from testorchestrator import dispatch
from testorchestrator.execution import (Execution, ExecutionState,
                                        Outcome, Project, Suite)
from testorchestrator.extensions import db


def _env(app):
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.commit()
        return s.id


def _mk_units(app, sid, n=2):
    with app.app_context():
        s = db.session.get(Suite, sid)
        parent, exs = dispatch.create_unit_run(
            "m", [{"index": i, "key": f"shard {i + 1}/{n}",
                   "shard": f"{i + 1}/{n}"} for i in range(n)],
            suite=s, source_sha="abc")
        return parent.id, [e.id for e in exs]


def test_unit_run_parent_and_children(app):
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 3)
    with app.app_context():
        p = db.session.get(Execution, pid)
        assert p.role == "parent" and p.unit_count == 3
        assert p.state == ExecutionState.QUEUED
        kids_db = sorted(
            Execution.query.filter_by(parent_id=pid).all(),
            key=lambda e: e.unit_index)
        assert [e.id for e in kids_db] == kids
        for i, e in enumerate(kids_db):
            assert e.role == "unit" and e.unit_index == i
            assert e.unit_key == f"shard {i + 1}/3"
            snap = e.snapshot()
            assert snap["parent_id"] == pid
            assert snap["unit"]["shard"] == f"{i + 1}/3"


def test_parent_never_dispatched(app):
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 2)
    with app.app_context():
        # The redrive sweep must not launch the aggregation parent —
        # it owns no process.
        dispatch._redrive_queued_remote()
        p = db.session.get(Execution, pid)
        assert p.state == ExecutionState.QUEUED and not p.dispatch_id


def _finish(app, eid, outcome, state=ExecutionState.COMPLETED):
    e = db.session.get(Execution, eid)
    e.state = state
    e.outcome = outcome
    db.session.commit()


def test_aggregate_all_passed(app):
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 2)
    with app.app_context():
        for k in kids:
            _finish(app, k, Outcome.PASSED)
        dispatch.aggregate_parent_runs()
        p = db.session.get(Execution, pid)
        assert p.state == ExecutionState.COMPLETED
        assert p.outcome == Outcome.PASSED
        assert p.finished_at is not None
        # Idempotent: a second sweep changes nothing.
        dispatch.aggregate_parent_runs()
        assert p.state == ExecutionState.COMPLETED


def test_aggregate_failed_child_blocks_success(app):
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 2)
    with app.app_context():
        _finish(app, kids[0], Outcome.PASSED)
        # Still running — parent must NOT report success early.
        dispatch.aggregate_parent_runs()
        p = db.session.get(Execution, pid)
        assert p.state == ExecutionState.RUNNING
        assert p.outcome == Outcome.NONE
        _finish(app, kids[1], Outcome.FAILED)
        dispatch.aggregate_parent_runs()
        db.session.refresh(p)
        assert p.state == ExecutionState.COMPLETED
        assert p.outcome == Outcome.FAILED


def test_aggregate_infrastructure_child_incomplete(app):
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 2)
    with app.app_context():
        _finish(app, kids[0], Outcome.PASSED)
        _finish(app, kids[1], Outcome.INCOMPLETE,
                ExecutionState.FAILED)
        dispatch.aggregate_parent_runs()
        p = db.session.get(Execution, pid)
        assert p.state == ExecutionState.COMPLETED
        assert p.outcome == Outcome.INCOMPLETE
        assert p.error_kind == "infrastructure"
        assert kids[1][:8] in p.error_text


def test_parent_cancel_propagates_and_blocks_queued(app):
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 3)
    with app.app_context():
        # One running, two still queued.
        c0 = db.session.get(Execution, kids[0])
        c0.state = ExecutionState.DISPATCHED
        c0.dispatch_id = "bb:1"
        db.session.commit()
        assert dispatch.request_cancel(pid, "op", "stop all")
        kids_db = Execution.query.filter_by(parent_id=pid).all()
        assert all(e.state == ExecutionState.CANCEL_REQUESTED
                   for e in kids_db)
        # Terminate children as the cancel path would.
        for e in kids_db:
            e.state = ExecutionState.CANCELED
            e.outcome = Outcome.INCOMPLETE
        db.session.commit()
        dispatch.aggregate_parent_runs()
        p = db.session.get(Execution, pid)
        assert p.state == ExecutionState.CANCELED
        assert p.outcome == Outcome.INCOMPLETE


def test_unit_results_stay_distinct(app):
    """Identical unit rows must never merge: each child keeps its own
    execution identity, snapshot assignment and result rows."""
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 2)
    with app.app_context():
        from testorchestrator.execution import CaseStatus, TestResult
        for i, k in enumerate(kids):
            db.session.add(TestResult(
                execution_id=k, classname="c", case_name="t",
                status=CaseStatus.PASSED, attempt=1))
            _finish(app, k, Outcome.PASSED)
        db.session.commit()
        dispatch.aggregate_parent_runs()
        n = TestResult.query.filter(
            TestResult.execution_id.in_(kids)).count()
        assert n == 2   # one per unit — aggregation never collapses
