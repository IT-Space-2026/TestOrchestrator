"""Plan removal — no active endpoints, schedules/releases preserved,
history intact."""

def test_no_active_plan_endpoints(app):
    """Removed Plan routes cannot execute — no /plans/ or plan
    mutation endpoints in the app map."""
    rules = [r.rule for r in app.url_map.iter_rules()]
    assert not any("/plans" in r for r in rules)


def test_schedules_and_releases_preserved(app):
    """Plan removal must not break schedule/release data — existing
    rows survive and non-plan targets still resolve."""
    from testorchestrator.extensions import db
    from testorchestrator.execution import (Release, Schedule,
                                            Project, Suite)
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S", imported=False,
                  content_hash="x"); db.session.add(s)
        r = Release(project_id=p.id, name="R"); db.session.add(r)
        sch = Schedule(project_id=p.id, name="sched", target_type="suite",
                       kind="once", enabled=True)
        db.session.add(sch); db.session.commit()
        assert Schedule.query.filter_by(name="sched").count() == 1
        assert Release.query.filter_by(name="R").count() == 1


def test_executions_history_intact(app):
    """Historical executions keep their records regardless of Plan
    removal — snapshots are immutable."""
    from testorchestrator.extensions import db
    from testorchestrator.execution import (Execution, Outcome,
                                            Project)
    with app.app_context():
        p = Project(name="P2"); db.session.add(p); db.session.flush()
        e = Execution(project_id=p.id, suite_id=None,
                      source_sha="abc123",
                      outcome=Outcome.PASSED,
                      snapshot_json='{"source_target":"Test Suites/S1"}',
                      created_by="alice")
        db.session.add(e); db.session.commit()
        got = Execution.query.filter_by(id=e.id).first()
        assert "S1" in got.snapshot_json
        assert got.source_sha == "abc123"


def test_restored_schedule_and_release_routes(app, client):
    """Schedule + release create/edit endpoints exist after Plan
    removal (regression — they were accidentally cut)."""
    rules = {r.endpoint: r.rule for r in app.url_map.iter_rules()}
    for ep in ("portal.create_schedule", "portal.create_release",
               "portal.edit_release"):
        assert ep in rules, ep
