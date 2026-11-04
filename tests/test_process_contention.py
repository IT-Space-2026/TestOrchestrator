"""Independent-PROCESS contention + injected-failure tests (delta pass).

- Two real subprocesses race the same due schedule on one sqlite file:
  exactly one execution must result.
- A failure injected BEFORE execution creation leaves the schedule
  recoverable (still due).
- A failure injected AFTER the claim+execution commit cannot produce a
  duplicate on the next tick.
- Cancel-vs-dispatch is order-exact: whichever lands first determines
  the (always legal) outcome, and the audit trail proves which.
"""

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from testorchestrator import dispatch
from testorchestrator.execution import (Execution, ExecutionState, Outcome,
                                        OutboxEvent, Plan, PlanEntry, Project,
                                        Schedule, Suite, utcnow)
from testorchestrator.extensions import db
from testorchestrator.models import AuditEvent

REPO = Path(__file__).resolve().parent.parent
WORKER = REPO / "tests" / "contention_worker.py"


def _seed_due_schedule(tmp_path):
    from tests.conftest import make_app
    app = make_app(tmp_path)
    with app.app_context():
        db.create_all()
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        db.session.add(Schedule(
            project_id=p.id, name="s1", target_type="suite",
            suite_id=s.id, kind="once",
            next_run_at=utcnow() - timedelta(minutes=1),
            created_by="m"))
        db.session.commit()
    return app


@pytest.mark.parametrize("workers", [2, 3])
def test_schedule_contention_independent_processes(tmp_path, workers):
    """N real processes contend for the same due slot — exactly one
    execution is created regardless of which process wins."""
    app = _seed_due_schedule(tmp_path)
    procs = [subprocess.Popen(
        [sys.executable, str(WORKER), str(tmp_path), str(i)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for i in range(workers)]
    (tmp_path / "go").write_text("go")          # synchronized release
    for p in procs:
        rc = p.wait(timeout=60)
        assert rc == 0, p.stderr.read().decode()[-500:]
    results = [(tmp_path / f"result-{i}").read_text()
               for i in range(workers)]
    assert "TIMEOUT" not in results and not any(
        r.startswith("ERR") for r in results), results
    assert sum(int(r) for r in results) == 1, results
    with app.app_context():
        assert Execution.query.count() == 1


def test_injected_failure_before_creation_stays_due(app):
    """Failure before execution creation rolls the claim back — the
    schedule stays due and fires once the cause is fixed."""
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        sc = Schedule(project_id=p.id, name="x", target_type="suite",
                      suite_id=s.id, kind="once",
                      next_run_at=utcnow() - timedelta(minutes=1),
                      created_by="m")
        db.session.add(sc); db.session.commit()

        orig = dispatch.create_execution
        def boom(*a, **k):
            raise RuntimeError("injected pre-creation failure")
        dispatch.create_execution = boom
        try:
            assert dispatch.fire_due_schedules() == 0
        finally:
            dispatch.create_execution = orig
        db.session.refresh(sc)
        assert sc.enabled and sc.next_run_at is not None
        assert Execution.query.count() == 0
        # Recovered on the next tick.
        assert dispatch.fire_due_schedules() == 1
        assert Execution.query.count() == 1


def test_injected_failure_after_commit_no_duplicate(app):
    """A failure after the claim+execution commit (e.g. audit write)
    cannot produce a duplicate fire on the next tick."""
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        sc = Schedule(project_id=p.id, name="x", target_type="suite",
                      suite_id=s.id, kind="once",
                      next_run_at=utcnow() - timedelta(minutes=1),
                      created_by="m")
        db.session.add(sc); db.session.commit()

        orig = dispatch.record_audit
        def boom(user, event, detail=""):
            if event == "schedule.fired":
                raise RuntimeError("injected post-commit failure")
            return orig(user, event, detail)
        dispatch.record_audit = boom
        try:
            assert dispatch.fire_due_schedules() == 0
        finally:
            dispatch.record_audit = orig
        # The execution WAS created (claim committed with it).
        assert Execution.query.count() == 1
        # Next tick must not duplicate it.
        assert dispatch.fire_due_schedules() == 0
        assert Execution.query.count() == 1


def _plan_pair(app):
    p = Project(name="P"); db.session.add(p); db.session.flush()
    s1 = Suite(project_id=p.id, name="S1")
    s2 = Suite(project_id=p.id, name="S2")
    db.session.add_all([s1, s2]); db.session.flush()
    plan = Plan(project_id=p.id, name="PL"); db.session.add(plan)
    db.session.flush()
    db.session.add_all([
        PlanEntry(plan_id=plan.id, order=0, suite_id=s1.id,
                  config_json=json.dumps({"on_failure": "stop"})),
        PlanEntry(plan_id=plan.id, order=1, suite_id=s2.id)])
    db.session.commit()
    return plan, s1, s2


def test_cancel_then_dispatch_is_skipped_and_audited(app):
    """Order A — enforcement lands first: the cancelled entry's queued
    event is skipped at the dispatch boundary, audited, never launched.
    GUARANTEE: a queued/dispatched entry is always cancelled; the
    cancellation is durable and its event cannot launch it later."""
    with app.app_context():
        plan, s1, s2 = _plan_pair(app)
        rid, exs = dispatch.queue_plan_executions("m", plan)
        exs[0].state = ExecutionState.FAILED
        exs[0].outcome = Outcome.INCOMPLETE
        db.session.commit()
        assert dispatch.enforce_plan_policies() == 1
        db.session.refresh(exs[1])
        assert exs[1].state == ExecutionState.CANCELED
        event = OutboxEvent.query.filter(
            OutboxEvent.payload_json.contains(exs[1].id)).first()
        dispatch._handle_event(event)
        db.session.refresh(exs[1])
        assert exs[1].state == ExecutionState.CANCELED
        assert AuditEvent.query.filter_by(
            event="execution.dispatch_skipped").count() == 1


def test_dispatch_then_cancel_still_cancels_queued_scope(app):
    """Order B — the worker's event lands first but no runner is free:
    the entry reaches DISPATCHED (still cancel scope), enforcement
    then cancels it — outcome identical: never launched, audited.
    GUARANTEE: entries not yet in a running state always end cancelled;
    only entries already running are left to finish."""
    with app.app_context():
        plan, s1, s2 = _plan_pair(app)
        rid, exs = dispatch.queue_plan_executions("m", plan)
        exs[0].state = ExecutionState.FAILED
        exs[0].outcome = Outcome.INCOMPLETE
        db.session.commit()
        # Worker's event lands first — no runner registered, so the
        # execution parks at DISPATCHED (still in cancel scope).
        event = OutboxEvent.query.filter(
            OutboxEvent.payload_json.contains(exs[1].id)).first()
        dispatch._handle_event(event)
        db.session.refresh(exs[1])
        assert exs[1].state == ExecutionState.DISPATCHED
        assert dispatch.enforce_plan_policies() == 1
        db.session.refresh(exs[1])
        assert exs[1].state == ExecutionState.CANCELED
        assert "stop-on-failure" in exs[1].cancel_reason
        assert AuditEvent.query.filter_by(
            event="plan.stop_on_failure_cancel").count() == 1


def test_running_entry_out_of_cancel_scope(app):
    """Order C — an entry already RUNNING when its predecessor fails is
    left to finish (evidence preserved); only queued/dispatched work
    is cancelled. Original run + attempt evidence stays immutable."""
    with app.app_context():
        plan, s1, s2 = _plan_pair(app)
        rid, exs = dispatch.queue_plan_executions("m", plan)
        exs[0].state = ExecutionState.FAILED
        exs[0].outcome = Outcome.INCOMPLETE
        exs[1].state = ExecutionState.RUNNING
        db.session.commit()
        assert dispatch.enforce_plan_policies() == 0
        db.session.refresh(exs[1])
        assert exs[1].state == ExecutionState.RUNNING
        # Evidence: original attempt row untouched, outcome unset.
        assert exs[1].finished_at is None
        assert exs[1].snapshot().get("attempt", 1) == 1


def test_kre_reject_launches_no_process(app, tmp_path):
    """Gated-option rejection: NO KRE process launches (no invocation
    log, no fixture marker), the rejection is durable evidence — a
    registered Artifact for logs/kre-gating.log, accessible on disk."""
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        from testorchestrator.execution import (Artifact, Runner,
                                                RuntimeConnection)
        runner = Runner(name="local", connection_mode="local",
                        max_slots=1)
        db.session.add(runner); db.session.flush()
        marker = tmp_path / "kre-did-run"
        fake = tmp_path / "fakekre"
        fake.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
        fake.chmod(0o755)
        rt = RuntimeConnection(
            name="fx", runner_id=runner.id, executable_path=str(fake),
            detected_version="8.0.0", enabled=True,
            workspace_root=str(tmp_path / "ws"))
        db.session.add(rt); db.session.commit()
        ex = dispatch.create_execution(
            "m", s, run_config={"kre_options": {"retryStrategy": "x"}})
        dispatch.run_execution(ex.id)
        db.session.refresh(ex)
        assert ex.state == ExecutionState.FAILED
        assert "option gating rejected" in ex.error_text
        # No process: invocation log absent, fixture marker untouched.
        assert not marker.exists()
        ws = Path(ex.workspace_path)
        assert not (ws / "logs" / "kre-invocation.log").exists()
        # Evidence: registered artifact + accessible file.
        art = Artifact.query.filter_by(
            execution_id=ex.id, rel_path="logs/kre-gating.log").one()
        assert art.sha256 and art.size > 0
        assert (ws / "logs" / "kre-gating.log").exists()


# ---------- launch-boundary race: claim -> cancel -> launch attempt ----------

def _fixture_env(tmp_path):
    from testorchestrator.execution import Runner, RuntimeConnection
    p = Project(name="P"); db.session.add(p); db.session.flush()
    s = Suite(project_id=p.id, name="S"); db.session.add(s)
    db.session.flush()
    runner = Runner(name="local", connection_mode="local", max_slots=1)
    db.session.add(runner); db.session.flush()
    rt = RuntimeConnection(name="fx", runner_id=runner.id,
                           executable_path="/bin/true",
                           detected_version="9.0.0", enabled=True,
                           workspace_root=str(tmp_path / "ws"))
    db.session.add(rt); db.session.commit()
    return s


def test_cancel_between_claim_and_launch_aborts(app, tmp_path):
    """Synchronized interleave: dispatcher has entered run_execution
    (event claimed), cancellation lands mid-flight, and the atomic
    QUEUED|DISPATCHED->PREPARING claim then fails — the cancelled entry
    is never launched. This is the exact race sequential order tests
    cannot establish."""
    import threading
    with app.app_context():
        s = _fixture_env(tmp_path)
        ex = dispatch.create_execution("m", s)
        ex_id = ex.id

    entered = threading.Event()
    proceed = threading.Event()
    orig = dispatch.pick_runtime

    def blocking_pick(e, r):
        rt = orig(e, r)
        entered.set()
        proceed.wait(timeout=10)
        return rt

    dispatch.pick_runtime = blocking_pick
    done = []
    def worker():
        with app.app_context():
            dispatch.run_execution(ex_id)
            done.append(True)

    t = threading.Thread(target=worker)
    t.start()
    assert entered.wait(timeout=10), "dispatcher never reached seam"
    with app.app_context():
        # Cancellation lands while the dispatcher is mid-flight.
        assert dispatch.request_cancel(ex_id, "admin", "race cancel")
    proceed.set()
    t.join(timeout=30)
    dispatch.pick_runtime = orig
    assert done
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        # The CAS aborted the launch: no PREPARING/RUNNING happened.
        assert ex.state == ExecutionState.CANCEL_REQUESTED
        ws = Path(ex.workspace_path) if ex.workspace_path else None
        assert ws is None or not (ws / "logs" / "kre-invocation.log").exists()


def test_launch_boundary_commit_wins_over_cancel(app, tmp_path):
    """Inverse order: the launch claim commits first -> a subsequent
    enforcement cancel UPDATE matches 0 rows (entry left running)."""
    with app.app_context():
        s = _fixture_env(tmp_path)
        ex = dispatch.create_execution("m", s)
        # Simulate the committed claim boundary.
        assert dispatch._claim_for_start(ex)
        db.session.refresh(ex)
        assert ex.state == ExecutionState.PREPARING
        # Enforcement cancel-scope UPDATE can no longer match it.
        n = db.session.query(Execution).filter(
            Execution.id == ex.id,
            Execution.state.in_([ExecutionState.QUEUED,
                                 ExecutionState.DISPATCHED]),
        ).update({"state": ExecutionState.CANCELED},
                 synchronize_session=False)
        db.session.commit()
        assert n == 0
        db.session.refresh(ex)
        assert ex.state == ExecutionState.PREPARING


def test_non_lock_operational_error_propagates(app):
    """Only sqlite lock/busy contention is retried — unrelated DB
    failures must surface, never be silently skipped."""
    import sqlalchemy.exc as exc_mod
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        db.session.add(Schedule(
            project_id=p.id, name="x", target_type="suite",
            suite_id=s.id, kind="once",
            next_run_at=utcnow() - timedelta(minutes=1),
            created_by="m"))
        db.session.commit()
        orig = db.session.query
        def boom(*a, **k):
            q = orig(*a, **k)
            class Exploding:
                def filter(self, *x): return self
                def update(self, *x, **y):
                    raise exc_mod.OperationalError(
                        "UPDATE schedules", {}, Exception("disk full"))
            return Exploding()
        db.session.query = boom
        try:
            import pytest as _p
            with _p.raises(exc_mod.OperationalError):
                dispatch.fire_due_schedules()
        finally:
            db.session.query = orig
        # Lock phrasing IS swallowed (retried next tick) — assert narrow.
        class Locked:
            def filter(self, *x): return self
            def update(self, *x, **y):
                raise exc_mod.OperationalError(
                    "UPDATE schedules", {}, Exception("database is locked"))
        db.session.query = lambda *a, **k: Locked()
        try:
            assert dispatch.fire_due_schedules() == 0   # skipped, not raised
        finally:
            db.session.query = orig


def test_scheduled_firefox_reaches_kre_args(app, tmp_path):
    """Schedule run_config browser=Firefox -> snapshot -> dispatch ->
    -browserType=Firefox in the fixture KRE invocation (never replaced
    by the default)."""
    import json
    with app.app_context():
        s = _fixture_env(tmp_path)
        # Schedule carries the operator's browser choice.
        sch = Schedule(
            project_id=s.project_id, name="ff", target_type="suite",
            suite_id=s.id, kind="once",
            next_run_at=utcnow() - timedelta(minutes=1),
            run_config_json=json.dumps({"browser": "Firefox"}),
            created_by="t")
        db.session.add(sch); db.session.commit()
        assert dispatch.fire_due_schedules() == 1
        ex = Execution.query.order_by(
            Execution.created_at.desc()).first()
        # Immutable snapshot carries the choice.
        assert ex.snapshot()["run_config"]["browser"] == "Firefox"
        dispatch.run_execution(ex.id)
        db.session.refresh(ex)
        ws = Path(ex.workspace_path)
        log = (ws / "logs" / "kre-invocation.log").read_text()
        assert "-browserType=Firefox" in log
        assert "-browserType=Chrome (headless)" not in log


def test_browser_validation_and_remote_override(app, tmp_path):
    """Unsupported/platform-incompatible choices are rejected at launch;
    a remote WebDriver URL overrides -browserType entirely."""
    from testorchestrator.adapters import kre
    ok, v = kre.validate_browser("Firefox", "linux/amd64")
    assert ok and v == "Firefox"
    ok, v = kre.validate_browser("Safari", "linux/amd64")
    assert not ok                      # platform-incompatible
    ok, v = kre.validate_browser("Netscape", "linux/amd64")
    assert not ok                      # unknown value, never guessed
    args = kre.build_args("/x", "/p", "/s", "/r", browser="Firefox")
    assert "-browserType=Firefox" in args
    args = kre.build_args(
        "/x", "/p", "/s", "/r", browser="Firefox",
        remote_webdriver_url="https://wd.example:4444/wd/hub",
        remote_webdriver_type="chrome")
    assert "-browserType=Firefox" not in args
    assert "-remoteWebDriverUrl=https://wd.example:4444/wd/hub" in args
    assert "-remoteWebDriverType=chrome" in args
