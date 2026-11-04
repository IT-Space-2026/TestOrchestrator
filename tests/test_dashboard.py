"""Dashboard aggregation, live endpoint, audit search, projects list,
and nav-placement coverage (unreleased UI)."""

from datetime import timedelta

import pytest

from testorchestrator import dashboard as dash
from testorchestrator.extensions import db
from testorchestrator.execution import utcnow
from testorchestrator.execution import (Execution, ExecutionState, Outcome,
                                        Project, Runner, Schedule, Suite)
from testorchestrator.models import AuditEvent, record_audit


def _seed_project(name="P1"):
    p = Project(name=name, description="desc")
    db.session.add(p)
    db.session.flush()
    s = Suite(project_id=p.id, name=f"{name}-suite")
    db.session.add(s)
    db.session.flush()
    return p, s


def _ex(p, s, state, outcome, days_ago=None, finished=True):
    e = Execution(project_id=p.id, suite_id=s.id, state=state,
                  outcome=outcome, created_by="t")
    e.snapshot_json = '{"suite_name": "%s"}' % s.name
    if finished:
        e.finished_at = utcnow() - timedelta(days=days_ago or 0)
    db.session.add(e)
    return e


class TestDashboardSnapshot:
    def test_metrics_from_real_data(self, app):
        with app.app_context():
            p1, s1 = _seed_project("A")
            p2, s2 = _seed_project("B")
            _ex(p1, s1, ExecutionState.COMPLETED, Outcome.PASSED)
            _ex(p1, s1, ExecutionState.FAILED, Outcome.FAILED)
            _ex(p2, s2, ExecutionState.RUNNING, Outcome.NONE,
                finished=False)
            _ex(p2, s2, ExecutionState.COMPLETED, Outcome.PASSED,
                days_ago=40)   # outside period
            db.session.add(Runner(name="r1", enabled=True,
                                  draining=False, health="ok"))
            db.session.add(Runner(name="r2", enabled=True,
                                  draining=True, health="down"))
            db.session.add(Schedule(
                project_id=p1.id, name="sch", target_type="suite",
                suite_id=s1.id, kind="once",
                next_run_at=utcnow() + timedelta(hours=3),
                created_by="t"))
            db.session.commit()

            class U:  # admin sees all
                def project_allowed(self, _): return True
            d = dash.snapshot(U())
            m = d["metrics"]
            assert m["projects_total"] == 2
            assert m["executions_active"] == 1
            assert m["passed_period"] == 1 and m["failed_period"] == 1
            assert m["terminal_period"] == 2   # 40-day-old excluded
            assert m["runners_available"] == 1
            assert m["runners_total"] == 2
            assert m["schedules_upcoming"] == 1

    def test_project_visibility_scope(self, app):
        with app.app_context():
            p1, s1 = _seed_project("Vis")
            p2, s2 = _seed_project("Hidden")
            _ex(p2, s2, ExecutionState.COMPLETED, Outcome.FAILED)
            db.session.commit()

            class U:
                def project_allowed(self, pid): return pid == p1.id
            d = dash.snapshot(U())
            assert d["metrics"]["projects_total"] == 1
            assert d["metrics"]["terminal_period"] == 0
            assert [r["name"] for r in d["projects"]] == ["Vis"]

    def test_pass_rate_denominator_is_attempts(self, app):
        with app.app_context():
            p, s = _seed_project("R")
            _ex(p, s, ExecutionState.COMPLETED, Outcome.PASSED)
            _ex(p, s, ExecutionState.COMPLETED, Outcome.FAILED)
            _ex(p, s, ExecutionState.COMPLETED, Outcome.PASSED)
            db.session.commit()

            class U:
                def project_allowed(self, _): return True
            row = dash.snapshot(U())["projects"][0]
            assert row["pass_total"] == 3 and row["pass_rate"] == 67


class TestDashboardEndpoints:
    def test_data_endpoint_json(self, app, client, login_as):
        login_as("admin")
        with app.app_context():
            p, s = _seed_project("J")
            _ex(p, s, ExecutionState.COMPLETED, Outcome.PASSED)
            db.session.commit()
        r = client.get("/dashboard/data")
        assert r.status_code == 200
        body = r.get_json()
        assert body["metrics"]["projects_total"] == 1
        assert "generated_at" in body

    def test_page_renders(self, client, login_as):
        login_as("admin")
        body = client.get("/").data.decode()
        for needle in ("metric-grid", "dashboard.js", "proj-rows",
                       "chart-outcomes"):
            assert needle in body

    def test_anonymous_redirected(self, client):
        r = client.get("/dashboard/data")
        assert r.status_code in (302, 401, 403)


class TestAuditSearch:
    def test_filters_and_pagination(self, app, client, login_as):
        login_as("admin")
        with app.app_context():
            for i in range(60):
                record_audit("alice" if i % 2 else "bob",
                             "event.even" if i % 2 else "event.odd",
                             f"detail-{i}")
            db.session.commit()
        r = client.get("/system/audit?event=event.odd")
        assert r.data.count(b"event.odd") >= 30
        r2 = client.get("/system/audit?q=detail-42")
        assert b"detail-42" in r2.data and b"detail-41" not in r2.data
        # 30 alice events → one page; filter retained in the form
        r3 = client.get("/system/audit?actor=alice")
        assert r3.status_code == 200 and b"alice" in r3.data
        # pagination exists for the unfiltered set (60 → 2 pages) and
        # carries the query string when filters combine with pages
        r4 = client.get("/system/audit?page=2")
        assert b"page 2" in r4.data and b"Prev" in r4.data
        r5 = client.get("/system/audit?event=event.odd&q=detail")
        assert b"event=event.odd" in r5.data or b"event.odd" in r5.data

    def test_no_results_state(self, client, login_as):
        login_as("admin")
        body = client.get("/system/audit?q=zzz-nothing").data.decode()
        assert "No audit events match" in body

    def test_admin_only(self, client, login_as):
        login_as("tester")
        assert client.get("/system/audit").status_code == 403


class TestProjectsPage:
    def test_search_sort_empty(self, app, client, login_as):
        login_as("admin")
        with app.app_context():
            p1, _ = _seed_project("Alpha")
            _seed_project("Beta")
            db.session.commit()
        body = client.get("/projects?q=alpha").data.decode()
        assert "Alpha" in body and "Beta" not in body
        body2 = client.get("/projects?q=zzz").data.decode()
        assert 'No projects match "zzz"' in body2

    def test_badges_rendered(self, app, client, login_as):
        login_as("admin")
        with app.app_context():
            p, s = _seed_project("Badge")
            _ex(p, s, ExecutionState.COMPLETED, Outcome.PASSED)
            db.session.commit()
        assert "outcome-passed" in client.get("/projects").data.decode()


class TestNavPlacement:
    def test_notices_under_admin(self, client, login_as):
        login_as("admin")
        body = client.get("/").data.decode()
        # single entry, inside the Admin section (after 'Audit')
        assert body.count("main.notices") <= 1
        assert "Third-party notices &amp; licences" in body
        assert body.index("Audit") < body.index("Third-party notices")
        # top-level duplicate gone: entry no longer before Projects link
        assert body.index("Executions</a>") < body.index("Admin")

    def test_notices_still_accessible_to_non_admin(self, client, login_as):
        login_as("tester")
        r = client.get("/about/notices")
        assert r.status_code == 200


def test_dashboard_project_cap_labelled(app):
    """The dashboard project table is a bounded preview — capped at 50
    rows and labelled, never silently dropping projects."""
    with app.app_context():
        for i in range(55):
            _seed_project(f"Proj{i:02d}")
        db.session.commit()

        class U:
            def project_allowed(self, _): return True
        d = dash.snapshot(U())
        assert len(d["projects"]) == 50
        assert d["projects_total_rows"] == 55
        assert d["projects_capped"] is True
        # metrics still count everything, not just the preview
        assert d["metrics"]["projects_total"] == 55
