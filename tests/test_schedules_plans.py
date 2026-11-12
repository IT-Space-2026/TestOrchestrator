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


def _gating_env(tmp_path):
    p, s = _project_suite()
    from testorchestrator.execution import Runner, RuntimeConnection
    runner = Runner(name="local", connection_mode="local", max_slots=1)
    db.session.add(runner); db.session.flush()
    rt = RuntimeConnection(
        name="fixture-kre", runner_id=runner.id,
        executable_path="/bin/true", detected_version="8.0.0",
        workspace_root=str(tmp_path))
    db.session.add(rt); db.session.commit()
    return s


def test_gating_rejects_unsupported_option(app, tmp_path):
    """An explicitly requested unsupported option REJECTS the launch —
    the requested execution semantics would change silently otherwise.
    The decision is evidence (kre-gating.log) + a clear failure reason."""
    with app.app_context():
        s = _gating_env(tmp_path)
        ex = dispatch.create_execution(
            "manager", s,
            run_config={"kre_options": {"retryStrategy": "x"}})
        dispatch.run_execution(ex.id)
        db.session.refresh(ex)
        assert ex.state == ExecutionState.FAILED
        assert ex.error_kind == "infrastructure"
        assert "option gating rejected" in ex.error_text
        gating = tmp_path / ex.id / "logs" / "kre-gating.log"
        text = gating.read_text()
        assert "retryStrategy" in text and "reject" in text


def test_gating_allow_drop_acknowledges_and_runs(app, tmp_path):
    """Operator opt-in (kre_options_allow_drop) continues without the
    option — acknowledged in evidence, never silently."""
    with app.app_context():
        s = _gating_env(tmp_path)
        ex = dispatch.create_execution(
            "manager", s,
            run_config={"kre_options": {"retryStrategy": "x"},
                        "kre_options_allow_drop": True})
        dispatch.run_execution(ex.id)
        db.session.refresh(ex)
        assert ex.state == ExecutionState.COMPLETED  # fixture ran anyway
        text = (tmp_path / ex.id / "logs"
                / "kre-gating.log").read_text()
        assert "operator-acknowledged" in text


def test_gating_unknown_version_rejected(app, tmp_path):
    """Unknown/unparsable runtime version: every gated option is
    rejected — conservative default, never a guessed flag."""
    with app.app_context():
        s = _gating_env(tmp_path)
        from testorchestrator.execution import RuntimeConnection
        rt = RuntimeConnection.query.first()
        rt.detected_version = "n/a-build-x"
        db.session.commit()
        ex = dispatch.create_execution(
            "manager", s,
            run_config={"kre_options": {"retry": 1},
                        "kre_options_allow_drop": True})
        dispatch.run_execution(ex.id)
        db.session.refresh(ex)
        assert ex.state == ExecutionState.COMPLETED  # dropped, acknowledged
        assert "version unknown" in (
            tmp_path / ex.id / "logs" / "kre-gating.log").read_text()


def _one_slot_env(tmp_path):
    """A single-KRE-slot environment: runner max_slots=1 AND
    license_slots=1 (capacity = min of both), runtime on it."""
    p, s = _project_suite()
    from testorchestrator.execution import Runner, RuntimeConnection
    runner = Runner(name="one-slot", connection_mode="local",
                    max_slots=1, license_slots=1)
    db.session.add(runner); db.session.flush()
    rt = RuntimeConnection(
        name="fixture-kre", runner_id=runner.id,
        executable_path="/bin/true", detected_version="8.0.0",
        workspace_root=str(tmp_path))
    db.session.add(rt); db.session.commit()
    return p, s, runner, rt


def _due_once(p, s, name):
    sched = Schedule(
        project_id=p.id, name=name, target_type="suite",
        suite_id=s.id, kind="once",
        next_run_at=utcnow() - timedelta(minutes=1),
        created_by="manager")
    db.session.add(sched); db.session.commit()
    return sched


def test_three_schedules_share_one_slot(app, tmp_path):
    """Three overlapping due occurrences on a single-KRE-slot runner:
    all fire into QUEUED; exactly one holds the slot at a time; the
    rest wait (stay QUEUED) until it frees."""
    with app.app_context():
        p, s, runner, rt = _one_slot_env(tmp_path)
        for n in ("s1", "s2", "s3"):
            _due_once(p, s, n)
        assert dispatch.fire_due_schedules() == 3
        exs = Execution.query.order_by(Execution.created_at).all()
        assert len(exs) == 3
        assert {e.state for e in exs} == {ExecutionState.QUEUED}
        # occupy the sole slot — a running KRE-equivalent
        exs[0].state = ExecutionState.RUNNING
        exs[0].runner_id = runner.id
        db.session.commit()
        # the other two cannot acquire it — they stay visibly QUEUED
        for e in exs[1:]:
            dispatch.run_execution(e.id)
            db.session.refresh(e)
            assert e.state == ExecutionState.QUEUED
            assert e.runner_id is None
        # first finishes -> exactly one waiter is admitted
        exs[0].state = ExecutionState.COMPLETED
        db.session.commit()
        dispatch.run_execution(exs[1].id)
        db.session.refresh(exs[1])
        assert exs[1].state == ExecutionState.COMPLETED
        # third still queued only until slot is free again
        dispatch.run_execution(exs[2].id)
        db.session.refresh(exs[2])
        assert exs[2].state == ExecutionState.COMPLETED


def test_failure_and_cancel_release_capacity(app, tmp_path):
    with app.app_context():
        p, s, runner, rt = _one_slot_env(tmp_path)
        from testorchestrator.execution import Runner
        run = Runner.query.get(runner.id)
        e1 = dispatch.create_execution("m", s)
        e2 = dispatch.create_execution("m", s)
        # fail: slot frees
        e1.runner_id = run.id
        e1.state = ExecutionState.RUNNING; db.session.commit()
        assert dispatch.effective_slots(run) == 0
        e1.state = ExecutionState.FAILED; db.session.commit()
        assert dispatch.effective_slots(run) == 1
        # cancel-requested holds the slot (still active); CANCELED frees
        e2.runner_id = run.id
        e2.state = ExecutionState.CANCEL_REQUESTED; db.session.commit()
        assert dispatch.effective_slots(run) == 0
        e2.state = ExecutionState.CANCELED; db.session.commit()
        assert dispatch.effective_slots(run) == 1


def test_unknown_holds_slot_no_unsafe_release(app, tmp_path):
    """UNKNOWN counts active — an uncertain execution may still have a
    live process; the slot is NOT freed on uncertainty."""
    with app.app_context():
        p, s, runner, rt = _one_slot_env(tmp_path)
        e = dispatch.create_execution("m", s)
        e.runner_id = runner.id
        e.state = ExecutionState.UNKNOWN
        db.session.commit()
        assert dispatch.effective_slots(runner) == 0
        e2 = dispatch.create_execution("m", s)
        dispatch.run_execution(e2.id)
        db.session.refresh(e2)
        assert e2.state == ExecutionState.QUEUED


def test_assign_runner_race_only_one_wins(app, tmp_path):
    """Two dispatchers racing a single slot: the atomic conditional
    UPDATE lets exactly one QUEUED->DISPATCHED transition win."""
    with app.app_context():
        p, s, runner, rt = _one_slot_env(tmp_path)
        e1 = dispatch.create_execution("m", s)
        e2 = dispatch.create_execution("m", s)
        assert dispatch._assign_runner(e1, runner, rt) is True
        db.session.refresh(e2)
        assert dispatch._assign_runner(e2, runner, rt) is False
        assert e2.state == ExecutionState.QUEUED


def test_license_slots_caps_below_hardware(app, tmp_path):
    """capacity belongs to the RUNNER as min(max_slots, license_slots)
    — a second runtime record cannot multiply concurrent sessions."""
    with app.app_context():
        p, s, runner, rt = _one_slot_env(tmp_path)
        runner.max_slots = 3; db.session.commit()
        # hardware says 3, licence says 1 -> still 1 concurrent
        assert dispatch.capacity_limit(runner) == 1
        e = dispatch.create_execution("m", s)
        e.runner_id = runner.id
        e.state = ExecutionState.RUNNING
        db.session.commit()
        e2 = dispatch.create_execution("m", s)
        dispatch.run_execution(e2.id)
        db.session.refresh(e2)
        assert e2.state == ExecutionState.QUEUED


def test_interval_overlap_skip_policy(app, tmp_path):
    """Recurring schedule due while its own previous execution is
    active: 'skip' policy drops the occurrence (audit), never doubles
    or queues an unbounded backlog."""
    with app.app_context():
        p, s, runner, rt = _one_slot_env(tmp_path)
        sched = Schedule(
            project_id=p.id, name="ov", target_type="suite",
            suite_id=s.id, kind="interval", interval_minutes=60,
            overlap_policy="skip",
            next_run_at=utcnow() - timedelta(minutes=5),
            created_by="manager")
        db.session.add(sched); db.session.commit()
        active = dispatch.create_execution("m", s, schedule_id=sched.id)
        active.state = ExecutionState.RUNNING
        active.runner_id = runner.id
        db.session.commit()
        assert dispatch.fire_due_schedules() == 0
        # no new execution created for the skipped occurrence
        assert Execution.query.filter_by(
            schedule_id=sched.id).count() == 1
        from testorchestrator.models import AuditEvent
        assert AuditEvent.query.filter_by(
            event="schedule.overlap_skipped").count() >= 1


def test_repeated_ticks_no_duplicate_occurrence(app, tmp_path):
    """Duplicate scheduler ticks cannot fire a 'once' schedule twice
    nor create a second execution."""
    with app.app_context():
        p, s, runner, rt = _one_slot_env(tmp_path)
        sched = _due_once(p, s, "once1")
        assert dispatch.fire_due_schedules() == 1
        assert dispatch.fire_due_schedules() == 0
        assert Execution.query.filter_by(schedule_id=sched.id).count() == 1


def test_runner_cap_delegates_to_capacity_limit(app):
    """_runner_cap delegates to capacity_limit — same result for
    unset, zero, and positive license_slots (no duplicated logic)."""
    from testorchestrator.dispatch import capacity_limit, _runner_cap
    with app.app_context():
        from testorchestrator.execution import Runner as _R
        for lic in (None, 0, 1, 3):
            r = _R(name=f"c{lic}", connection_mode="local",
                   max_slots=4, license_slots=lic)
            db.session.add(r); db.session.flush()
            assert _runner_cap(r) == capacity_limit(r) == min(
                4, lic or 4)


def test_restart_helper_returns_bool_no_dead_feedback(monkeypatch,
                                                      tmp_path):
    """_launch_restart_helper returns a label/False — the request
    handler (not the helper) owns flash/redirect feedback."""
    from testorchestrator.system.routes import _launch_restart_helper
    import shutil
    monkeypatch.setattr(shutil, "which", lambda x: None)
    hp = tmp_path / "helper.py"; hp.write_text("pass")
    assert _launch_restart_helper(hp, "system") is False
