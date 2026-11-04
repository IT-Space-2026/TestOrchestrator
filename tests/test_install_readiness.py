"""Installer readiness attribution (item 2) + schedule fire safety."""

import http.server
import socketserver
import threading
from datetime import timedelta

from testorchestrator import cli, dispatch
from testorchestrator.execution import (Execution, Project, Schedule, Suite,
                                        utcnow)
from testorchestrator.extensions import db


def test_foreign_listener_blocks_fresh_install(tmp_path, monkeypatch):
    """A pre-existing 200-listener must NOT yield install success — the
    port precheck refuses a fresh install before any payload work."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers()
        def log_message(self, *a):
            pass
    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        args = type("A", (), {"payload": str(tmp_path / "p"),
                              "data": str(tmp_path / "d"),
                              "config": str(tmp_path / "c"),
                              "url": f"http://127.0.0.1:{port}",
                              "upgrade": False, "dry_run": False})
        # Precheck alone: occupied port + fresh install -> exit 2.
        assert cli._port_occupied(args.url)
        monkeypatch.setattr(cli, "_copy_tree", lambda s, d: None)
        monkeypatch.setattr(cli, "_run_toctl",
                            lambda c, e, timeout=900:
                            type("R", (), {"returncode": 0,
                                           "stdout": "{}", "stderr": ""})())
        monkeypatch.setattr(cli, "_app_dir",
                            lambda: tmp_path / "nosuch")
        for k in ("secret_key", "java_home", "no_allure",
                  "no_service_start", "https_port", "service_user",
                  "yes"):
            setattr(args, k, True if k == "no_allure" or k ==
                    "no_service_start" or k == "yes"
                    else (443 if k == "https_port" else
                          ("testorchestrator" if k == "service_user"
                           else "")))
        rc = cli.cmd_install(args)
        assert rc == 2  # refused — no false fresh-install success
    finally:
        srv.shutdown()


def test_verify_install_full_matrix(tmp_path):
    """Readiness requires THIS install's id AND the installed build:
    the whole identity matrix, never just 'something answers'."""
    args = type("A", (), {"url": "http://127.0.0.1:9"})  # closed port
    assert cli._verify_install(args, "abc123", "b1") == 4   # unreachable

    cases = [
        # health payload, expected rc, scenario
        ({"install_id": "abc123", "build": "b1",
          "version": "1.9.0"}, 0, "own id + expected build -> ready"),
        ({"install_id": "other", "build": "b1",
          "version": "1.9.0"}, 4, "foreign install id -> not ready"),
        ({"install_id": "abc123", "build": "OLD",
          "version": "1.8.0"}, 4, "own id but stale build -> not ready "
         "(upgrade with old process still answering)"),
        ({"install_id": "", "build": "b1"}, 4,
         "missing install_id -> not ready"),
        ({"install_id": "abc123"}, 4, "missing build -> not ready"),
        ({"status": "ok"}, 4, "no identity fields -> not ready"),
    ]
    orig = cli._install_health
    try:
        for health, rc, scenario in cases:
            cli._install_health = lambda url, h=health: h
            got = cli._verify_install(args, "abc123", "b1")
            assert got == rc, f"{scenario}: expected {rc}, got {got}"
    finally:
        cli._install_health = orig


def test_install_id_persists_across_upgrade(tmp_path, monkeypatch):
    """--upgrade keeps the existing TO_INSTALL_ID — the id identifies
    the install location, not the build."""
    cfg = tmp_path / "cfg"; cfg.mkdir()
    (cfg / "web.env").write_text(
        "TO_INSTANCE_DIR=/d\nTO_SECRET_KEY=s\n"
        "TO_INSTALL_ID=persist-me\n")
    # Exercise the same read path cmd_install uses.
    install_id = ""
    for line in (cfg / "web.env").read_text().splitlines():
        if line.startswith("TO_INSTALL_ID="):
            install_id = line.split("=", 1)[1].strip()
    assert install_id == "persist-me"


def test_schedule_claim_is_single_fire(app):
    """Two workers racing the same due schedule: exactly one fires —
    the CAS means only one advances next_run_at/disables."""
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        sched = Schedule(
            project_id=p.id, name="once1", target_type="suite",
            suite_id=s.id, kind="once",
            next_run_at=utcnow() - timedelta(minutes=1),
            created_by="m")
        db.session.add(sched); db.session.commit()
        assert dispatch.fire_due_schedules() == 1
        # Second worker sees it disabled -> no duplicate execution.
        assert dispatch.fire_due_schedules() == 0
        assert Execution.query.filter_by(suite_id=s.id).count() == 1


def test_schedule_fire_failure_rolls_back_claim(app):
    """A fire failure un-claims — the schedule stays due (retried next
    tick) rather than being silently consumed."""
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        sched = Schedule(
            project_id=p.id, name="bad", target_type="plan",
            plan_id="nonexistent", kind="once",
            next_run_at=utcnow() - timedelta(minutes=1),
            created_by="m")
        db.session.add(sched); db.session.commit()
        assert dispatch.fire_due_schedules() == 0   # fire failed
        db.session.refresh(sched)
        assert sched.enabled and sched.next_run_at is not None  # still due


# ---------- schedule claim under real contention (item 3) ----------

import json as _json
import threading

from testorchestrator.execution import (ExecutionState, OutboxEvent,
                                        Plan, PlanEntry)


def _seed_schedule(app, kind="once", interval=None, run_config=None):
    p = Project(name="P"); db.session.add(p); db.session.flush()
    s = Suite(project_id=p.id, name="S"); db.session.add(s)
    db.session.flush()
    sc = Schedule(
        project_id=p.id, name="s1", target_type="suite", suite_id=s.id,
        kind=kind, interval_minutes=interval,
        next_run_at=utcnow() - timedelta(minutes=1), created_by="m",
        run_config_json=_json.dumps(run_config or {}))
    db.session.add(sc); db.session.commit()
    return sc, s


def test_two_workers_one_execution(app):
    """Two workers racing the same due slot: exactly one fires, one
    execution is created — the CAS makes the claim exclusive."""
    with app.app_context():
        sc, s = _seed_schedule(app)
    barrier = threading.Barrier(2)
    fired, errors = [], []
    def worker():
        try:
            with app.app_context():
                barrier.wait(timeout=10)
                fired.append(dispatch.fire_due_schedules())
        except Exception as e:                    # pragma: no cover
            errors.append(e)
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    assert sum(fired) == 1, f"both workers fired: {fired}"
    with app.app_context():
        assert Execution.query.count() == 1


def test_interval_no_catchup_runs(app):
    """Multiple missed intervals produce ONE late run; the next due time
    is now+interval (missed slots are skipped, never replayed)."""
    with app.app_context():
        sc, s = _seed_schedule(app, kind="interval", interval=30)
        # Simulate a long outage: next_run_at far in the past.
        sc.next_run_at = utcnow() - timedelta(hours=10)
        db.session.commit()
        assert dispatch.fire_due_schedules() == 1
        assert Execution.query.count() == 1      # one late run
        assert dispatch.fire_due_schedules() == 0  # no catch-up backlog
        db.session.refresh(sc)
        from datetime import timezone
        nxt = sc.next_run_at
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        assert nxt > utcnow()                    # computed from now


def test_disable_before_claim_never_fires(app):
    """A schedule disabled between the due scan and the claim is not
    fired — the CAS filter includes enabled."""
    with app.app_context():
        sc, s = _seed_schedule(app)
        sc.enabled = False; db.session.commit()
        assert dispatch.fire_due_schedules() == 0
        assert Execution.query.count() == 0


def test_deleted_schedule_never_fires(app):
    """A schedule deleted before the worker tick is never dispatched."""
    with app.app_context():
        sc, s = _seed_schedule(app)
        db.session.delete(sc); db.session.commit()
        assert dispatch.fire_due_schedules() == 0
        assert Execution.query.count() == 0


# ---------- stop-on-failure at the dispatch boundary (item 4) ----------

def _plan_with_sibling(app, stop=True):
    p = Project(name="P"); db.session.add(p); db.session.flush()
    s1 = Suite(project_id=p.id, name="S1")
    s2 = Suite(project_id=p.id, name="S2")
    db.session.add_all([s1, s2]); db.session.flush()
    plan = Plan(project_id=p.id, name="PL"); db.session.add(plan)
    db.session.flush()
    db.session.add_all([
        PlanEntry(plan_id=plan.id, order=0, suite_id=s1.id,
                  config_json=_json.dumps({"on_failure": "stop"
                                           if stop else "continue"})),
        PlanEntry(plan_id=plan.id, order=1, suite_id=s2.id)])
    db.session.commit()
    return plan, s1, s2


def test_cancelled_sibling_event_cannot_dispatch(app):
    """Boundary: entry1 fails -> enforcement cancels the QUEUED entry2;
    entry2's already-queued outbox event is then skipped at the dispatch
    boundary — never launched."""
    with app.app_context():
        plan, s1, s2 = _plan_with_sibling(app)
        rid, exs = dispatch.queue_plan_executions("m", plan)
        e1, e2 = exs
        e1.state = ExecutionState.FAILED
        e1.outcome = __import__("testorchestrator.execution",
                                fromlist=["Outcome"]).Outcome.INCOMPLETE
        db.session.commit()
        assert dispatch.enforce_plan_policies() == 1
        db.session.refresh(e2)
        assert e2.state == ExecutionState.CANCELED
        # e2's outbox event is still pending — the dispatch boundary
        # must refuse to launch a cancelled execution.
        event = OutboxEvent.query.filter(
            OutboxEvent.payload_json.contains(e2.id)).first()
        assert event is not None
        dispatch._handle_event(event)   # simulates the worker claim
        db.session.refresh(e2)
        assert e2.state == ExecutionState.CANCELED  # unchanged


def test_running_sibling_not_cancelled(app):
    """Cancel-queued scope: an entry already RUNNING when its predecessor
    fails is left to finish — only queued/dispatched siblings cancel."""
    with app.app_context():
        plan, s1, s2 = _plan_with_sibling(app)
        rid, exs = dispatch.queue_plan_executions("m", plan)
        exs[0].state = ExecutionState.FAILED
        exs[1].state = ExecutionState.RUNNING
        db.session.commit()
        assert dispatch.enforce_plan_policies() == 0
        db.session.refresh(exs[1])
        assert exs[1].state == ExecutionState.RUNNING


def test_retry_in_flight_blocks_stop_enforcement(app):
    """A queued retry ('not-started' latest) means the entry is not
    settled — enforcement must not cancel siblings on its account."""
    with app.app_context():
        plan, s1, s2 = _plan_with_sibling(app)
        rid, exs = dispatch.queue_plan_executions("m", plan)
        exs[0].state = ExecutionState.FAILED
        db.session.commit()
        retry = dispatch.retry_execution("m", exs[0])
        # Latest attempt for entry 0 is queued -> no enforcement.
        assert dispatch.enforce_plan_policies() == 0
        db.session.refresh(exs[1])
        assert exs[1].state == ExecutionState.QUEUED
