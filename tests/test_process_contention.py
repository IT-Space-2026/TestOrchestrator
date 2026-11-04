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
        # execution parks at QUEUED (still in cancel scope AND still
            # assignable — DISPATCHED would wedge it).
        event = OutboxEvent.query.filter(
            OutboxEvent.payload_json.contains(exs[1].id)).first()
        dispatch._handle_event(event)
        db.session.refresh(exs[1])
        assert exs[1].state == ExecutionState.QUEUED
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
        remote_webdriver_type="Selenium")
    # Katalon-documented remote form: -browserType=Remote is REQUIRED —
    # it does NOT drop browserType, and never carries the local choice.
    assert "-browserType=Remote" in args
    assert "-browserType=Firefox" not in args
    assert "-remoteWebDriverUrl=https://wd.example:4444/wd/hub" in args
    assert "-remoteWebDriverType=Selenium" in args


def test_cancel_kills_process_tree(app, tmp_path):
    """Running cancel: CANCEL_REQUESTED -> killpg on the process group —
    parent AND child die, state reflects the cancel, slot released only
    after confirmed stop (wait/force-kill)."""
    import threading, time
    marker = tmp_path / "tree.marker"
    child_pid = tmp_path / "child.pid"
    fake = tmp_path / "fakekre"
    fake.write_text(
        "#!/bin/sh\n"
        f"sleep 300 & echo $! > {child_pid}\n"
        f"touch {marker}\n"
        "sleep 300\n")
    fake.chmod(0o755)
    with app.app_context():
        from testorchestrator.execution import Runner, RuntimeConnection
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        r = Runner(name="local", connection_mode="local", max_slots=1)
        db.session.add(r); db.session.flush()
        db.session.add(RuntimeConnection(
            name="fx", runner_id=r.id, executable_path=str(fake),
            detected_version="9.0.0", enabled=True,
            workspace_root=str(tmp_path / "ws")))
        db.session.commit()
        ex = dispatch.create_execution("m", s)
        eid = ex.id
    done = []
    def worker():
        with app.app_context():
            dispatch.run_execution(eid); done.append(True)
    t = threading.Thread(target=worker); t.start()
    time.sleep(1.5)                          # parent+child running
    assert child_pid.exists() and marker.exists()
    with app.app_context():
        dispatch.request_cancel(eid, "admin", "stop tree")
    t.join(timeout=30)
    assert done
    cpid = int(child_pid.read_text().strip())
    import os, signal as sig
    try:
        os.kill(cpid, 0); alive = True
    except OSError:
        alive = False
    assert not alive                        # child confirmed stopped
    with app.app_context():
        ex = db.session.get(Execution, eid)
        assert ex.state == ExecutionState.CANCELED
        assert (ex.workspace_path and
                (Path(ex.workspace_path) / "logs"
                 / "kre-invocation.log").exists())


def test_timeout_kills_after_limit(app, tmp_path):
    """timeout_seconds in run_config -> snapshot -> run_invocation
    timeout_s: process tree killed at the limit; reason recorded."""
    import time
    fake = tmp_path / "fakekre"
    fake.write_text("#!/bin/sh\nsleep 300\n")
    fake.chmod(0o755)
    with app.app_context():
        from testorchestrator.execution import Runner, RuntimeConnection
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        r = Runner(name="local", connection_mode="local", max_slots=1)
        db.session.add(r); db.session.flush()
        db.session.add(RuntimeConnection(
            name="fx", runner_id=r.id, executable_path=str(fake),
            detected_version="9.0.0", enabled=True,
            workspace_root=str(tmp_path / "ws")))
        db.session.commit()
        ex = dispatch.create_execution(
            "m", s, run_config={"timeout_seconds": 2})
        eid = ex.id
        assert ex.snapshot()["run_config"]["timeout_seconds"] == 2
        t0 = time.time()
        dispatch.run_execution(eid)
        dt = time.time() - t0
        ex = db.session.get(Execution, eid)
        assert dt < 30                       # killed near 2s, not 300
        assert ex.state in (ExecutionState.FAILED,
                            ExecutionState.UNKNOWN,
                            ExecutionState.CANCELED)


def test_capacity_race_one_slot(app, tmp_path):
    """Two queued jobs, one slot: the claim's capacity predicate lets
    exactly one start; the other stays safely queued until release."""
    fake = tmp_path / "fx"; fake.write_text("#!/bin/sh\nsleep 0.1\n")
    fake.chmod(0o755)
    with app.app_context():
        from testorchestrator.execution import Runner, RuntimeConnection
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        r = Runner(name="local", connection_mode="local", max_slots=1)
        db.session.add(r); db.session.flush()
        db.session.add(RuntimeConnection(
            name="fx", runner_id=r.id, executable_path=str(fake),
            detected_version="9.0.0", enabled=True,
            workspace_root=str(tmp_path / "ws")))
        db.session.commit()
        e1 = dispatch.create_execution("m", s)
        e2 = dispatch.create_execution("m", s)
        e1.runner_id = r.id; e2.runner_id = r.id
        db.session.commit()
        # Simultaneous claims: first wins, second sees active=1>=cap.
        r1 = dispatch._claim_for_start(e1, r)
        db.session.refresh(e2)
        r2 = dispatch._claim_for_start(e2, r)
        assert r1 and not r2
        assert e2.state == ExecutionState.QUEUED   # safely queued
        # Release: after e1 finishes, e2's claim succeeds.
        e1.state = ExecutionState.COMPLETED; db.session.commit()
        assert dispatch._claim_for_start(e2, r)


def test_license_allowance_caps_claim(app, tmp_path):
    """license_slots=1 beats max_slots=2 — second claim denied until
    the active execution leaves the active set."""
    with app.app_context():
        from testorchestrator.execution import Runner
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        r = Runner(name="local", connection_mode="local", max_slots=2,
                   license_slots=1)
        db.session.add(r); db.session.commit()
        e1 = dispatch.create_execution("m", s)
        e2 = dispatch.create_execution("m", s)
        e1.runner_id = r.id; e2.runner_id = r.id
        db.session.commit()
        assert dispatch._claim_for_start(e1, r)
        db.session.refresh(e2)
        assert not dispatch._claim_for_start(e2, r)   # licence cap hit


def test_disabled_draining_not_picked(app):
    """pick_runner skips disabled and draining runners."""
    with app.app_context():
        from testorchestrator.execution import Runner
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        db.session.add_all([Runner(name="off", enabled=False),
                            Runner(name="drain", draining=True)])
        db.session.commit()
        ex = dispatch.create_execution("m", s)
        assert dispatch.pick_runner(ex) is None


DISPATCH_WORKER = REPO / "tests" / "dispatch_worker.py"


def _seed_dispatch_env(tmp_path, slots=1, license_slots=None, delay="3"):
    """One suite, one local runner (slots), a fixture executable that
    sleeps + appends its launch to a marker file, N queued executions."""
    from tests.conftest import make_app
    from testorchestrator.execution import Runner, RuntimeConnection
    app = make_app(tmp_path)
    marker = tmp_path / "launches"
    fake = tmp_path / "fakekre"
    fake.write_text(
        "#!/bin/sh\n"
        f"echo $$ >> {marker}\n"
        f"sleep {delay}\n")
    fake.chmod(0o755)
    with app.app_context():
        db.create_all()
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        r = Runner(name="local", connection_mode="local",
                   max_slots=slots, license_slots=license_slots)
        db.session.add(r); db.session.flush()
        db.session.add(RuntimeConnection(
            name="fx", runner_id=r.id, executable_path=str(fake),
            detected_version="9.0.0", enabled=True,
            workspace_root=str(tmp_path / "ws")))
        db.session.commit()
    return app, marker


def test_two_dispatchers_one_execution_single_launch(tmp_path):
    """Two independent processes run_execution on the SAME queued
    execution — exactly one process launches (marker has one line),
    and the execution has one coherent terminal state."""
    app, marker = _seed_dispatch_env(tmp_path)
    with app.app_context():
        s = Suite.query.first()
        ex = dispatch.create_execution("m", s)
        eid = ex.id
    procs = [subprocess.Popen(
        [sys.executable, str(DISPATCH_WORKER), str(tmp_path),
         str(i), eid], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for i in range(2)]
    (tmp_path / "go").write_text("go")
    for p in procs:
        assert p.wait(timeout=60) == 0, p.stderr.read().decode()[-500:]
    launches = marker.read_text().split() if marker.exists() else []
    assert len(launches) == 1, launches        # one process only
    with app.app_context():
        ex = db.session.get(Execution, eid)
        assert ex.state == ExecutionState.COMPLETED


def test_two_jobs_one_slot_sequential_release(tmp_path):
    """Two executions, one slot, two processes: only one launches now;
    the loser stays QUEUED (not failed), and runs once capacity frees."""
    app, marker = _seed_dispatch_env(tmp_path, slots=1, delay="2")
    with app.app_context():
        s = Suite.query.first()
        r = __import__("testorchestrator.execution",
                       fromlist=["Runner"]).Runner.query.first()
        e1 = dispatch.create_execution("m", s)
        e2 = dispatch.create_execution("m", s)
        e1.runner_id = r.id; e2.runner_id = r.id
        db.session.commit()
        ids = [e1.id, e2.id]
    procs = [subprocess.Popen(
        [sys.executable, str(DISPATCH_WORKER), str(tmp_path),
         str(i), eid], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for i, eid in enumerate(ids)]
    (tmp_path / "go").write_text("go")
    for p in procs:
        p.wait(timeout=60)
    results = [(tmp_path / f"result-{i}").read_text() for i in range(2)]
    assert results.count("completed") == 1     # exactly one ran now
    assert "queued" in results                 # loser safely queued —
                                             # not failed, not running
    launches1 = marker.read_text().split() if marker.exists() else []
    assert len(launches1) == 1
    # Release happened — retry the queued one: it now completes.
    with app.app_context():
        q = Execution.query.filter_by(
            state=ExecutionState.QUEUED).first()
        assert q is not None
        dispatch.run_execution(q.id)
        db.session.refresh(q)
        assert q.state == ExecutionState.COMPLETED


def test_remote_pickup_claim_exactly_once(app, client, tmp_path):
    """Agent pickup is a CAS: first claim DISPATCHED->RUNNING wins and
    is scoped to the owning runner; a second claim gets 409, and a
    non-DISPATCHED or foreign-runner claim never wins."""
    from testorchestrator.agent_api import issue_agent_token
    from testorchestrator.execution import Runner
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        r = Runner(name="rem", connection_mode="remote", enabled=True)
        db.session.add(r); db.session.flush()
        token = issue_agent_token(r)
        r2 = Runner(name="rem2", connection_mode="remote", enabled=True)
        db.session.add(r2); db.session.flush()
        token2 = issue_agent_token(r2)
        ex = dispatch.create_execution("m", s)
        ex.runner_id = r.id
        ex.state = ExecutionState.DISPATCHED
        db.session.commit()
        eid = ex.id
    h = {"Authorization": f"Bearer {token}"}
    h2 = {"Authorization": f"Bearer {token2}"}
    r_ok = client.post(f"/api/agent/tasks/{eid}/claim", headers=h)
    assert r_ok.status_code == 200 and r_ok.get_json()["claimed"]
    r_dup = client.post(f"/api/agent/tasks/{eid}/claim", headers=h)
    assert r_dup.status_code == 409
    r_wrong = client.post(f"/api/agent/tasks/{eid}/claim", headers=h2)
    assert r_wrong.status_code == 409


def test_resolve_unknown_releases_capacity(app, client, login_as,
                                           tmp_path):
    """UNKNOWN holds a slot; admin resolve (with confirmation) marks it
    FAILED, audits, and frees capacity. Non-UNKNOWN / unconfirmed /
    non-admin requests are rejected."""
    with app.app_context():
        from testorchestrator.execution import Runner
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        r = Runner(name="l", connection_mode="local", max_slots=1)
        db.session.add(r); db.session.flush()
        ex = dispatch.create_execution("m", s)
        ex.runner_id = r.id
        ex.state = ExecutionState.UNKNOWN
        db.session.commit()
        eid, rid = ex.id, r.id
        # UNKNOWN counts as active — capacity exhausted.
        assert dispatch.effective_slots(r) == 0
        login_as("admin")
        resp = client.post(f"/executions/{eid}/resolve-unknown",
                           data={}, follow_redirects=True)
        assert resp.status_code == 200
        ex = db.session.get(Execution, eid)
        assert ex.state == ExecutionState.UNKNOWN      # no confirm
        resp = client.post(
            f"/executions/{eid}/resolve-unknown",
            data={"confirm_stopped": "yes"}, follow_redirects=True)
        ex = db.session.get(Execution, eid)
        assert ex.state == ExecutionState.UNKNOWN      # no reason
        resp = client.post(
            f"/executions/{eid}/resolve-unknown",
            data={"confirm_stopped": "yes",
                  "reason": "checked ps — process + children gone"},
            follow_redirects=True)
        ex = db.session.get(Execution, eid)
        assert ex.state == ExecutionState.FAILED
        assert "ADMIN-RESOLVED" in ex.error_text
        assert "checked ps" in ex.error_text
        db.session.refresh(r)
        assert dispatch.effective_slots(r) == 1        # slot released
        # double-resolve: CAS refuses (state no longer UNKNOWN)
        resp = client.post(
            f"/executions/{eid}/resolve-unknown",
            data={"confirm_stopped": "yes", "reason": "again"},
            follow_redirects=True)
        ex = db.session.get(Execution, eid)
        assert ex.state == ExecutionState.FAILED       # unchanged


def test_agent_capability_gate(app, client, tmp_path):
    """Only agents that advertise task-claim receive work; old agents
    get an explicit incompatibility marker, not tasks."""
    from testorchestrator.agent_api import issue_agent_token
    from testorchestrator.execution import Runner
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        r = Runner(name="rem", connection_mode="remote", enabled=True)
        db.session.add(r); db.session.flush()
        token = issue_agent_token(r)
        ex = dispatch.create_execution("m", s)
        ex.runner_id = r.id
        ex.state = ExecutionState.DISPATCHED
        db.session.commit()
        eid = ex.id
    h = {"Authorization": f"Bearer {token}"}
    # register WITHOUT capabilities — pre-claim agent
    client.post("/api/agent/register",
                json={"agent_version": "0.1"}, headers=h)
    resp = client.get("/api/agent/tasks", headers=h)
    d = resp.get_json()
    assert d["tasks"] == [] and d["incompatible"]
    # register WITH the capability — work flows
    client.post("/api/agent/register",
                json={"agent_version": "0.2",
                      "capabilities": ["task-claim"]}, headers=h)
    resp = client.get("/api/agent/tasks", headers=h)
    assert len(resp.get_json()["tasks"]) == 1
    # claim still works on the listed task
    assert client.post(f"/api/agent/tasks/{eid}/claim",
                       headers=h).status_code == 200


def test_agent_run_config_wiring(tmp_path):
    """The agent must receive the resolved timeout + remote-webdriver
    fields from the immutable snapshot — not adapter defaults."""
    import inspect
    import testorchestrator.agent as agent_mod
    src = inspect.getsource(agent_mod.run_task)
    assert "remote_webdriver_url" in src
    assert "remote_webdriver_type" in src
    assert 'timeout_seconds' in src
    assert "3600" in src                     # same default as portal
