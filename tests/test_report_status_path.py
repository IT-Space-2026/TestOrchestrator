"""Report-status code path: refresh_report_status must flip pending ->
ready when the collection step writes an entry file into the report
root — the same transition the real collector uses."""
import os
import pytest
from testorchestrator.extensions import db
from testorchestrator import reporting
from testorchestrator.execution import (Execution, Project,
                                        ExecutionState, Outcome)


@pytest.fixture()
def ex(app, tmp_path):
    rd = str(tmp_path / "rdir")
    with app.app_context():
        p = Project(name="rp-fixture")
        db.session.add(p)
        db.session.flush()
        e = Execution(project_id=p.id, state=ExecutionState.COMPLETED,
                      outcome=Outcome.PASSED, report_dir=rd)
        db.session.add(e)
        db.session.commit()
        yield e


def test_refresh_flips_pending_to_ready(app, ex):
    assert ex.report_status == "pending"
    root = reporting.report_root(ex)
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<h1>r</h1>")
    status = reporting.refresh_report_status(ex)
    assert status == "ready"
    assert ex.report_status == "ready"
    assert ex.report_path == "index.html"


def test_refresh_stays_missing_without_entry(app, ex):
    reporting.report_root(ex).mkdir(parents=True, exist_ok=True)
    assert reporting.refresh_report_status(ex) == "missing"


def test_refresh_partial_with_evidence_no_entry(app, ex):
    root = reporting.report_root(ex)
    root.mkdir(parents=True, exist_ok=True)
    (root / "junit.xml").write_text("<testsuite/>")
    assert reporting.refresh_report_status(ex) == "partial"


def test_report_route_serves_ready_report(app, client, login_as, ex):
    root = reporting.report_root(ex)
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<h1>Fixture report</h1>")
    reporting.refresh_report_status(ex)
    login_as("admin")
    r = client.get(f"/executions/{ex.id}/report")
    assert r.status_code == 200
    assert b"Fixture report" in r.data


def test_report_route_redirects_with_flash_when_missing(
        app, client, login_as, ex):
    """Missing report -> redirect to detail with an explanatory
    flash, NOT a silent 404 or a false-positive view."""
    login_as("admin")
    r = client.get(f"/executions/{ex.id}/report",
                   follow_redirects=False)
    assert r.status_code in (301, 302)
    r2 = client.get(f"/executions/{ex.id}/report",
                    follow_redirects=True)
    assert b"No viewable report" in r2.data
