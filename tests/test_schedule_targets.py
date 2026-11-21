"""Regression: schedules whose targets_json names discovered repository
items must (a) stage the TARGET's binding — never the project's first
binding — and (b) persist the claim, execution, schedule.fired audit,
and last_run_at inside the fire transaction (the old bare `continue`
skipped the commit entirely). Duplicate-fire safety must hold across
re-fire attempts and 'once' schedules.
"""
import json
from datetime import timedelta

from testorchestrator import dispatch
from testorchestrator.execution import (DiscoveredItem, Execution,
                                        GitBinding, Project, Schedule,
                                        utcnow)
from testorchestrator.extensions import db
from testorchestrator.models import AuditEvent


def _mk(project, name, sha):
    b = GitBinding(project_id=project.id, name=name,
                   url="/tmp/x", ref="main", last_synced_sha=sha)
    db.session.add(b)
    db.session.flush()
    return b


def _item(project, binding, path="tests/"):
    it = DiscoveredItem(binding_id=binding.id, project_id=project.id,
                        sha=binding.last_synced_sha, kind="suite",
                        path=path, name="it", parent="")
    db.session.add(it)
    db.session.flush()
    return it


def _sched(project, item, binding, kind="interval"):
    sched = Schedule(
        project_id=project.id, name="s", target_type="suite",
        kind=kind, interval_minutes=60,
        next_run_at=utcnow() - timedelta(minutes=1),
        created_by="manager",
        targets_json=json.dumps([
            {"kind": "suite", "path": item.path,
             "source_item_id": item.id,
             "binding_id": binding.id}]))
    db.session.add(sched)
    db.session.commit()
    return sched


def test_targets_fire_uses_target_binding_not_project_first(app):
    """Regression for the bug that staged the project's first binding —
    the scheduled Robot run staged the Playwright repo and produced an
    empty suite."""
    with app.app_context():
        p = Project(name="P")
        db.session.add(p)
        db.session.flush()
        b1 = _mk(p, "first", "aaa111")
        b2 = _mk(p, "second", "bbb222")
        item = _item(p, b2)
        sched = _sched(p, item, b2)
        assert dispatch.fire_due_schedules() == 1
        ex = Execution.query.filter_by(schedule_id=sched.id).one()
        assert ex.binding_id == b2.id
        assert ex.source_sha == "bbb222"
        assert ex.source_sha != b1.last_synced_sha


def test_targets_fire_persists_claim_exec_audit_last_run(app):
    """The old targets-branch `continue` skipped the else-clause commit:
    no schedule.fired audit, no last_run_at, and the claim + executions
    survived only via ambient commits (or rolled back entirely)."""
    with app.app_context():
        p = Project(name="P")
        db.session.add(p)
        db.session.flush()
        b = _mk(p, "b", "sha9")
        item = _item(p, b)
        sched = _sched(p, item, b)
        prev_next = sched.next_run_at
        assert dispatch.fire_due_schedules() == 1
        db.session.refresh(sched)
        assert sched.last_run_at is not None
        assert sched.next_run_at > prev_next
        assert Execution.query.filter_by(
            schedule_id=sched.id).count() == 1
        assert AuditEvent.query.filter_by(
            event="schedule.fired",
            detail=sched.id).count() == 1


def test_targets_fire_not_duplicated_on_refire(app):
    """A second tick sees the advanced next_run_at — no duplicate
    dispatch across worker restarts or competing ticks."""
    with app.app_context():
        p = Project(name="P")
        db.session.add(p)
        db.session.flush()
        b = _mk(p, "b", "sha9")
        item = _item(p, b)
        sched = _sched(p, item, b)
        assert dispatch.fire_due_schedules() == 1
        assert dispatch.fire_due_schedules() == 0
        assert Execution.query.filter_by(
            schedule_id=sched.id).count() == 1


def test_targets_once_disables_after_single_fire(app):
    with app.app_context():
        p = Project(name="P")
        db.session.add(p)
        db.session.flush()
        b = _mk(p, "b", "sha9")
        item = _item(p, b)
        sched = _sched(p, item, b, kind="once")
        assert dispatch.fire_due_schedules() == 1
        db.session.refresh(sched)
        assert sched.enabled is False
        assert sched.next_run_at is None
        assert dispatch.fire_due_schedules() == 0
        assert Execution.query.filter_by(
            schedule_id=sched.id).count() == 1


def test_targets_missing_binding_falls_back_to_project(app):
    """A stale binding_id in targets_json degrades to the project
    binding rather than crashing the fire."""
    with app.app_context():
        p = Project(name="P")
        db.session.add(p)
        db.session.flush()
        b1 = _mk(p, "proj", "sha1")
        b2 = _mk(p, "tgt", "sha2")
        item = _item(p, b2)
        sched = Schedule(
            project_id=p.id, name="s", target_type="suite",
            kind="interval", interval_minutes=60,
            next_run_at=utcnow() - timedelta(minutes=1),
            created_by="manager",
            targets_json=json.dumps([
                {"kind": "suite", "path": item.path,
                 "source_item_id": item.id,
                 "binding_id": "nonexistent0000000000000000"}]))
        db.session.add(sched)
        db.session.commit()
        assert dispatch.fire_due_schedules() == 1
        ex = Execution.query.filter_by(schedule_id=sched.id).one()
        assert ex.binding_id == b1.id
        assert ex.source_sha == "sha1"
