"""Schedules, plan management, retry attempts, KRE option gating (item 4)."""

import json
from datetime import timedelta

import pytest

from testorchestrator import dispatch
from testorchestrator.execution import (Execution, ExecutionState, Outcome,
                                        Plan, PlanEntry, Project, Schedule,
                                        Suite, utcnow)
from testorchestrator.extensions import db


def _project_suite():
    p = Project(name="P")
    db.session.add(p); db.session.flush()
    s = Suite(project_id=p.id, name="S")
    db.session.add(s); db.session.commit()
    return p, s


def test_schedule_fires_once_then_disables(app):
    with app.app_context():
        p, s = _project_suite()
        sched = Schedule(
            project_id=p.id, name="nightly", target_type="suite",
            suite_id=s.id, kind="once", next_run_at=utcnow() - timedelta(minutes=1),
            created_by="manager")
        db.session.add(sched); db.session.commit()
        assert dispatch.fire_due_schedules() == 1
        db.session.refresh(sched)
        assert sched.enabled is False and sched.next_run_at is None
        ex = Execution.query.filter_by(suite_id=s.id).one()
        assert ex.created_by == "manager"
        assert ex.state == ExecutionState.QUEUED


def test_interval_schedule_reschedules(app):
    with app.app_context():
        p, s = _project_suite()
        sched = Schedule(
            project_id=p.id, name="hourly", target_type="suite",
            suite_id=s.id, kind="interval", interval_minutes=30,
            next_run_at=utcnow() - timedelta(minutes=5),
            created_by="manager")
        db.session.add(sched); db.session.commit()
        assert dispatch.fire_due_schedules() == 1
        db.session.refresh(sched)
        from datetime import timezone
        nxt = sched.next_run_at
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        assert sched.enabled and nxt > utcnow()
        # Not due again yet.
        assert dispatch.fire_due_schedules() == 0


def test_plan_run_id_groups_entries(app):
    with app.app_context():
        p, s = _project_suite()
        plan = Plan(project_id=p.id, name="PL")
        db.session.add(plan); db.session.flush()
        db.session.add(PlanEntry(plan_id=plan.id, order=0, suite_id=s.id))
        db.session.commit()
        rid, exs = dispatch.queue_plan_executions("manager", plan)
        assert rid and len(exs) == 1
        assert exs[0].snapshot()["plan_run_id"] == rid
        assert exs[0].snapshot()["plan_id"] == plan.id


def test_stop_on_failure_cancels_later_entries(app):
    with app.app_context():
        p = Project(name="P")
        db.session.add(p); db.session.flush()
        s1 = Suite(project_id=p.id, name="S1")
        s2 = Suite(project_id=p.id, name="S2")
        db.session.add_all([s1, s2]); db.session.flush()
        plan = Plan(project_id=p.id, name="PL")
        db.session.add(plan); db.session.flush()
        e1 = PlanEntry(plan_id=plan.id, order=0, suite_id=s1.id,
                       config_json=json.dumps({"on_failure": "stop"}))
        e2 = PlanEntry(plan_id=plan.id, order=1, suite_id=s2.id)
        db.session.add_all([e1, e2]); db.session.commit()
        rid, exs = dispatch.queue_plan_executions("manager", plan)
        # Entry 0 fails terminally; entry 1 still queued.
        exs[0].state = ExecutionState.FAILED
        exs[0].outcome = Outcome.INCOMPLETE
        db.session.commit()
        assert dispatch.enforce_plan_policies() == 1
        db.session.refresh(exs[1])
        assert exs[1].state == ExecutionState.CANCELED
        assert "stop-on-failure" in exs[1].cancel_reason
        # Idempotent on the next tick.
        assert dispatch.enforce_plan_policies() == 0


def test_retry_creates_linked_attempt(app):
    with app.app_context():
        p, s = _project_suite()
        ex = dispatch.create_execution("manager", s)
        ex.state = ExecutionState.FAILED
        db.session.commit()
        retry = dispatch.retry_execution("manager", ex)
        snap = retry.snapshot()
        assert snap["retry_of"] == ex.id and snap["attempt"] == 2
        assert retry.state == ExecutionState.QUEUED
        retry.state = ExecutionState.FAILED
        db.session.commit()
        retry2 = dispatch.retry_execution("manager", retry)
        assert retry2.snapshot()["attempt"] == 3
        assert retry2.snapshot()["retry_of"] == retry.id


def test_retry_rejects_non_terminal(app):
    with app.app_context():
        p, s = _project_suite()
        ex = dispatch.create_execution("manager", s)
        with pytest.raises(dispatch.DispatchError):
            dispatch.retry_execution("manager", ex)


def test_kre_option_gating():
    from testorchestrator.adapters import kre
    flags, dropped = kre.gated_flags("10.1.0", {"retry": 2})
    assert flags == ["-retry=2"] and not dropped
    flags, dropped = kre.gated_flags("8.5.0", {"retryStrategy": "x"})
    assert not flags and "retryStrategy" in dropped[0]
    flags, dropped = kre.gated_flags("", {"retry": 1})
    assert not flags and dropped          # unknown version -> conservative
    flags, dropped = kre.gated_flags("10.0", {"bogusOpt": 1})
    assert not flags and "allowlisted" in dropped[0]


def test_gating_note_becomes_evidence(app, tmp_path):
    """A dropped option is recorded in kre-gating.log inside the run's
    log dir — collected as an artifact, never silently ignored."""
    with app.app_context():
        p, s = _project_suite()
        from testorchestrator.execution import Runner, RuntimeConnection
        runner = Runner(name="local", connection_mode="local", max_slots=1)
        db.session.add(runner); db.session.flush()
        rt = RuntimeConnection(
            name="fixture-kre", runner_id=runner.id,
            executable_path="/bin/true", detected_version="8.0.0",
            workspace_root=str(tmp_path))
        db.session.add(rt); db.session.commit()
        ex = dispatch.create_execution(
            "manager", s,
            run_config={"kre_options": {"retryStrategy": "x"}})
        dispatch.run_execution(ex.id)
        db.session.refresh(ex)
        gating = tmp_path / ex.id / "logs" / "kre-gating.log"
        assert gating.exists()
        assert "retryStrategy" in gating.read_text()
