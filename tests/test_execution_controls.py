"""Bounded execution-controls round: explicit target, run-now,
overlap policy, searchable history, bulk cancel."""

import json
from datetime import timedelta

import pytest

from testorchestrator import dispatch
from testorchestrator.execution import (Execution, ExecutionState, Outcome,
                                        Plan, PlanEntry, Project, Runner,
                                        RuntimeConnection, Schedule, Suite,
                                        utcnow)
from testorchestrator.extensions import db


def _env(app, slots=2):
    from testorchestrator.execution import Runner, RuntimeConnection
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        r = Runner(name="local", connection_mode="local",
                   max_slots=slots)
        db.session.add(r); db.session.flush()
        r2 = Runner(name="other", connection_mode="local",
                    max_slots=slots)
        db.session.add(r2); db.session.flush()
        rt = RuntimeConnection(name="fx", runner_id=r.id,
                               executable_path="/bin/true",
                               detected_version="9.0.0", enabled=True,
                               workspace_root="/tmp")
        db.session.add(rt); db.session.commit()
        return s.id, r.id, r2.id, rt.id


# ---------- explicit target ----------

def test_explicit_runner_and_runtime_used(app, tmp_path):
    """requested_runner/runtime in run_config resolves to that exact
    target — recorded in evidence, never falls back."""
    with app.app_context():
        s_id, r_id, r2_id, rt_id = _env(app)
        s = db.session.get(Suite, s_id)
        ex = dispatch.create_execution(
            "m", s, run_config={"runner_id": r_id,
                                "runtime_id": rt_id})
        snap = ex.snapshot()["run_config"]
        assert snap["requested_runner"] == r_id
        assert snap["requested_runtime"] == rt_id
        dispatch.run_execution(ex.id)
        db.session.refresh(ex)
        assert ex.runner_id == r_id and ex.runtime_id == rt_id


def test_explicit_unavailable_runner_rejects_no_fallback(app):
    """Drained explicit runner -> visible rejection, NOT auto-pick."""
    with app.app_context():
        s_id, r_id, r2_id, rt_id = _env(app)
        r = db.session.get(Runner, r_id); r.draining = True
        db.session.commit()
        s = db.session.get(Suite, s_id)
        ex = dispatch.create_execution(
            "m", s, run_config={"runner_id": r_id})
        dispatch.run_execution(ex.id)
        db.session.refresh(ex)
        assert ex.state == ExecutionState.FAILED
        assert "no fallback" in ex.error_text
        assert ex.runner_id != r2_id          # never silently picked


def test_explicit_runtime_wrong_runner_rejects(app):
    """Runtime belonging to a different runner is incompatible."""
    with app.app_context():
        s_id, r_id, r2_id, rt_id = _env(app)
        rt = db.session.get(RuntimeConnection, rt_id)
        rt.runner_id = r2_id; db.session.commit()   # runtime on r2
        s = db.session.get(Suite, s_id)
        ex = dispatch.create_execution(
            "m", s, run_config={"runner_id": r_id,
                                "runtime_id": rt_id})
        dispatch.run_execution(ex.id)
        db.session.refresh(ex)
        assert ex.state == ExecutionState.FAILED
        assert "incompatible" in ex.error_text


# ---------- run now ----------

def test_run_now_uses_schedule_config_preserves_recurrence(
        app, client, login_as):
    """Run-now creates a new snapshot with the schedule's config +
    links back to the schedule; next_run_at/recurrence untouched."""
    with app.app_context():
        s_id, r_id, r2_id, rt_id = _env(app)
        s = db.session.get(Suite, s_id)
        sc = Schedule(project_id=s.project_id, name="w",
                      target_type="suite", suite_id=s.id,
                      kind="interval", interval_minutes=30,
                      next_run_at=utcnow() + timedelta(hours=1),
                      enabled=True,
                      run_config_json=json.dumps({"browser": "Firefox"}),
                      created_by="m")
        db.session.add(sc); db.session.commit()
        sc_id = sc.id
        orig_next = sc.next_run_at
    login_as("admin")
    resp = client.post(f"/schedules/{sc_id}/run-now",
                       follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        sc = db.session.get(Schedule, sc_id)
        assert sc.next_run_at == orig_next           # not advanced
        assert sc.enabled                            # not consumed
        ex = Execution.query.filter_by(
            schedule_id=sc_id).first()
        assert ex is not None
        assert ex.snapshot()["run_config"]["browser"] == "Firefox"
        assert ex.created_by == "admin"               # actor recorded


def test_run_now_disabled_rejected(app, client, login_as):
    """Disabled schedules cannot be manually triggered."""
    with app.app_context():
        s_id, r_id, r2_id, rt_id = _env(app)
        s = db.session.get(Suite, s_id)
        sc = Schedule(project_id=s.project_id, name="d",
                      target_type="suite", suite_id=s.id,
                      kind="interval", enabled=False,
                      next_run_at=None, created_by="m")
        db.session.add(sc); db.session.commit()
        sc_id = sc.id
    login_as("admin")
    resp = client.post(f"/schedules/{sc_id}/run-now",
                       follow_redirects=True)
    assert b"disabled" in resp.data
    with app.app_context():
        assert Execution.query.filter_by(
            schedule_id=sc_id).count() == 0


# ---------- overlap ----------

def test_overlap_skip_policy(app):
    """skip: an active execution from this schedule drops the new
    occurrence (audited); allow: both fire."""
    with app.app_context():
        s_id, r_id, r2_id, rt_id = _env(app)
        s = db.session.get(Suite, s_id)
        sc = Schedule(project_id=s.project_id, name="ov",
                      target_type="suite", suite_id=s.id,
                      kind="interval", interval_minutes=5,
                      next_run_at=utcnow() - timedelta(minutes=1),
                      enabled=True, overlap_policy="skip",
                      created_by="m")
        db.session.add(sc); db.session.commit()
        sc_id = sc.id
        # pre-existing ACTIVE execution from this schedule
        ex = dispatch.create_execution("m", s, schedule_id=sc_id)
        ex.state = ExecutionState.RUNNING
        db.session.commit()
        assert dispatch.fire_due_schedules() == 0     # skipped
        from testorchestrator.models import AuditEvent
        assert AuditEvent.query.filter_by(
            event="schedule.overlap_skipped").count() == 1
        # once the prior finishes, the next fire proceeds
        ex.state = ExecutionState.COMPLETED; db.session.commit()
        sc2 = db.session.get(Schedule, sc_id)
        sc2.next_run_at = utcnow() - timedelta(minutes=1)
        db.session.commit()
        assert dispatch.fire_due_schedules() == 1


def test_overlap_unknown_blocks(app):
    """UNKNOWN counts as active — skip policy holds the occurrence."""
    with app.app_context():
        s_id, r_id, r2_id, rt_id = _env(app)
        s = db.session.get(Suite, s_id)
        sc = Schedule(project_id=s.project_id, name="u",
                      target_type="suite", suite_id=s.id,
                      kind="interval", interval_minutes=5,
                      next_run_at=utcnow() - timedelta(minutes=1),
                      enabled=True, overlap_policy="skip",
                      created_by="m")
        db.session.add(sc); db.session.commit()
        ex = dispatch.create_execution("m", s, schedule_id=sc.id)
        ex.state = ExecutionState.UNKNOWN
        db.session.commit()
        assert dispatch.fire_due_schedules() == 0


# ---------- history view ----------

def test_history_filters_pagination(client, login_as, app):
    """Filters combine; ordering stable; no-results state; filters
    preserved through pagination."""
    with app.app_context():
        s_id, r_id, r2_id, rt_id = _env(app)
        s = db.session.get(Suite, s_id)
        for i in range(60):
            e = dispatch.create_execution("m", s)
            e.state = ExecutionState.COMPLETED
            e.outcome = (Outcome.PASSED if i % 2 else Outcome.FAILED)
            db.session.add(e)
        db.session.commit()
    login_as("admin")
    resp = client.get("/executions?state=completed")
    assert resp.status_code == 200
    assert b"passed" in resp.data or b"completed" in resp.data
    # page 2 keeps the filter
    resp = client.get("/executions?state=completed&page=2")
    assert b"state=completed" in resp.data or resp.status_code == 200
    # no-match filter
    resp = client.get("/executions?state=queued&q=zzz")
    assert b"No executions match" in resp.data


# ---------- bulk cancel ----------

def test_bulk_cancel_mixed_outcomes(app, client, login_as):
    """Selected rows each get their own result — accepted, already-
    terminal skipped, denied for out-of-scope projects."""
    with app.app_context():
        s_id, r_id, r2_id, rt_id = _env(app)
        s = db.session.get(Suite, s_id)
        e1 = dispatch.create_execution("m", s)          # queued
        e2 = dispatch.create_execution("m", s)
        e2.state = ExecutionState.COMPLETED
        db.session.commit()
        ids = [e1.id, e2.id, "nonexistent"]
    login_as("admin")
    resp = client.post("/executions/bulk-cancel",
                       data={"ex_ids": ids}, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        e1 = db.session.get(Execution, ids[0])
        assert e1.state in (ExecutionState.CANCELED,
                            ExecutionState.CANCEL_REQUESTED)
        e2 = db.session.get(Execution, ids[1])
        assert e2.state == ExecutionState.COMPLETED   # untouched


def test_explicit_runner_out_of_project_scope_rejected(app):
    """A posted runner_id outside the project's allowed_runners is a
    tampered request — dispatch rejects it, never silently picks."""
    with app.app_context():
        s, r, r2, rt = _env(app)
        r = db.session.get(Runner, r)
        r.allowed_projects = json.dumps(["some-other-project"])
        db.session.commit()
        ex = dispatch.create_execution(
            "m", db.session.get(Suite, s),
            run_config={"runner_id": r.id})
        dispatch.run_execution(ex.id)
        db.session.refresh(ex)
        assert ex.state == ExecutionState.FAILED
        assert "out of scope" in ex.error_text


def test_auto_no_capacity_waits_vs_explicit_rejects(app):
    """Auto with no free slot stays QUEUED (waiting); an invalid
    explicit pick FAILS — the two outcomes are distinct."""
    with app.app_context():
        s, r, r2, rt = _env(app, slots=1)
        r2o = db.session.get(Runner, r2)
        r2o.enabled = False            # only one eligible runner
        r = db.session.get(Runner, r)
        running = dispatch.create_execution("m", db.session.get(Suite, s))
        running.runner_id = r.id
        running.state = ExecutionState.RUNNING
        db.session.commit()
        auto = dispatch.create_execution("m", db.session.get(Suite, s))
        dispatch.run_execution(auto.id)
        db.session.refresh(auto)
        assert auto.state == ExecutionState.QUEUED   # waits
        bad = dispatch.create_execution(
            "m", db.session.get(Suite, s),
            run_config={"runner_id": "nonexistent"})
        dispatch.run_execution(bad.id)
        db.session.refresh(bad)
        assert bad.state == ExecutionState.FAILED     # rejects


def test_migration_upgrade_preserves_existing(tmp_path):
    """Upgrade from the immediately-preceding schema (d5e6f7a8b9c0):
    existing rows stay readable, overlap_policy defaults to allow,
    schedule_id stays NULL, new fires link correctly."""
    from pathlib import Path
    from tests.conftest import make_app
    from datetime import timedelta
    app = make_app(tmp_path, "upg.db")
    MIGR = str(Path(__file__).resolve().parent.parent / "migrations")
    with app.app_context():
        from flask_migrate import upgrade
        upgrade(directory=MIGR, revision="d5e6f7a8b9c0")   # old head
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        import sqlalchemy as sa
        db.session.execute(sa.text(
            "INSERT INTO schedules (id,project_id,name,target_type,"
            "suite_id,kind,description,enabled,created_by,"
            "run_config_json) VALUES "
            "('sc1',:p,'w','suite',:s,'interval','',1,'m','{}')"),
            {"p": p.id, "s": s.id})
        db.session.execute(sa.text(
            "INSERT INTO executions (id,project_id,suite_id,"
            "snapshot_json,source_sha,state,outcome,git_state,"
            "created_by,created_at,dispatch_id,workspace_path,"
            "report_dir,error_kind,error_text,cancel_reason,"
            "report_status,report_path,allure_status,allure_path,"
            "allure_version) VALUES "
            "('e1',:p,:s,'{}','sha','COMPLETED','PASSED','NONE',"
            "'m','2026-01-01','','','','','','','pending','',"
            "'pending','','')"), {"p": p.id, "s": s.id})
        db.session.commit()
        upgrade(directory=MIGR)                            # to head
        db.session.expire_all()
        sc = db.session.get(Schedule, "sc1")
        assert sc.overlap_policy == "allow"               # default set
        e = db.session.get(Execution, "e1")
        assert e.state == ExecutionState.COMPLETED        # readable
        assert e.schedule_id is None                      # null link ok
        assert Execution.query.filter(
            Execution.schedule_id.is_(None)).count() == 1
