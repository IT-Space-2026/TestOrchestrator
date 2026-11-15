"""reporting-labs 2.0 migration POC — offline regeneration from stored
canonical results. Same fixtures/honesty as the Allure path; no live
KRE. The reporting-labs pytest plugin is disabled for this suite to keep
its own output out of the repo test logs."""
from pathlib import Path

import pytest

from testorchestrator import dispatch
from testorchestrator.adapters import reporting_labs as rl
from testorchestrator.execution import (Artifact, Execution, ExecutionState,
                                        Outcome, Project, Suite, SuiteEntry,
                                        TestResult)
from testorchestrator.extensions import db
from testorchestrator.results import sha256_file


@pytest.fixture(autouse=True)
def _no_rl_plugin(pytestconfig):
    pytestconfig.pluginmanager.set_blocked("reporting_labs")


def _mk(app, tmp_path, shots=(), results=((("C.tc", "pass"), "passed"),)):
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        db.session.add(SuiteEntry(suite_id=s.id, order=0, case_ref="C"))
        ex = dispatch.create_execution("admin", s)
        ex.state = ExecutionState.COMPLETED
        ex.outcome = Outcome.FAILED
        rd = tmp_path / "ev" / ex.id
        rd.mkdir(parents=True)
        ex.report_dir = str(rd)
        for (cls, name), status in results:
            db.session.add(TestResult(
                execution_id=ex.id, case_ref=cls, case_name=name,
                classname=cls, status=status, duration_s=1.5,
                message="boom" if status != "passed" else ""))
        for shot in shots:
            f = rd / shot
            f.write_bytes(b"\x89PNG")
            db.session.add(Artifact(
                execution_id=ex.id, kind="screenshot", rel_path=shot,
                sha256=sha256_file(f), size=f.stat().st_size,
                mime="image/png"))
        db.session.commit()
        return ex.id


def test_generate_single_file_report(app, tmp_path):
    ex_id = _mk(app, tmp_path, shots=("bad.png", "stray-evidence.png"),
                results=[(("C.tc", "ok"), "passed"),
                         (("C.tc", "bad"), "failed"),
                         (("C.tc", "err"), "error"),
                         (("C.tc", "skip"), "skipped")])
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        out = rl.generate_for(ex)
        html = Path(ex.report_dir) / "reporting-labs" / "index.html"
        assert out["status"] == "ready", out
        assert out["cases"] == 4 and out["attached"] == 1
        assert out["unmapped"] == 1
        assert html.exists() and html.stat().st_size > 100_000
        body = html.read_text(encoding="utf-8", errors="replace")
        for needle in ("ok", "bad", "unmatched"):
            assert needle in body, needle


def test_regenerate_idempotent(app, tmp_path):
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert rl.generate_for(ex)["status"] == "ready"
        first = (Path(ex.report_dir) / "reporting-labs" /
                 "index.html").read_bytes()
        assert rl.generate_for(ex)["status"] == "ready"
        again = (Path(ex.report_dir) / "reporting-labs" /
                 "index.html").read_bytes()
        assert len(again) > 100_000 and len(first) > 100_000


def test_missing_when_no_results(app, tmp_path):
    ex_id = _mk(app, tmp_path, results=[])
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert rl.generate_for(ex)["status"] == "missing"


def test_no_pdf_no_node(app, tmp_path):
    """POC contract: single index.html only — no pdf, no node, no
    allure-results intermediate dir."""
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        out = rl.generate_for(ex)
        root = Path(ex.report_dir)
        assert not (root / "reporting-labs" / "report.pdf").exists()
        assert not (root / "reporting-labs.tmp").exists()
