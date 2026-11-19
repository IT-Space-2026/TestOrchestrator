"""Regression: JUnit dedup in _normalize must remove the same report
re-collected through a second layout (SSH Reports/ + proj/src/Reports/)
WITHOUT removing identical reports produced by distinct execution
units (shards/children/parallel partitions).

Dedup is scoped to execution identity + report provenance: identical
bytes only collapse when one collected path is a path-suffix of the
other — i.e. the same source file reached via two roots.
"""
import hashlib
from pathlib import Path

from testorchestrator import dispatch as d
from testorchestrator.execution import (Artifact, Execution,
                                        ExecutionState, Outcome, Project)
from testorchestrator.extensions import db
from testorchestrator.results import sha256_file

JUNIT = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="s" tests="2" failures="0" skipped="0">
  <testcase classname="s" name="case-a" time="0.1"/>
  <testcase classname="s" name="case-b" time="0.2"/>
</testsuite>
"""

JUNIT_OTHER = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="s" tests="1" failures="1" skipped="0">
  <testcase classname="s" name="case-c" time="0.3">
    <failure message="no">x</failure>
  </testcase>
</testsuite>
"""


def _mk_execution(tmp_path):
    project = Project(name="p", description="")
    db.session.add(project)
    db.session.flush()
    ex = Execution(project_id=project.id, origin="live",
                   state=ExecutionState.COMPLETED,
                   outcome=Outcome.PASSED, created_by="t")
    db.session.add(ex)
    db.session.flush()
    rd = tmp_path / "rep" / ex.id
    rd.mkdir(parents=True)
    ex.report_dir = str(rd)
    return ex, rd


def _junit_artifact(ex, rel_path, file_path):
    db.session.add(Artifact(
        execution_id=ex.id, kind="junit", rel_path=rel_path,
        sha256=sha256_file(Path(file_path)),
        size=Path(file_path).stat().st_size))


def _write(root, rel, content):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def test_same_report_source():
    assert d._same_report_source(
        "Reports/junit.xml", "proj/src/Reports/junit.xml")
    assert d._same_report_source(
        "proj/src/Reports/junit.xml", "Reports/junit.xml")
    assert d._same_report_source("a/junit.xml", "a/junit.xml")
    # sibling unit dirs are different sources even at equal depth
    assert not d._same_report_source(
        "shards/1/Reports/junit.xml", "shards/2/Reports/junit.xml")
    assert not d._same_report_source(
        "unit-a/junit.xml", "unit-b/junit.xml")
    assert not d._same_report_source(
        "Reports/junit.xml", "Other/junit.xml")


def test_identical_report_via_two_layouts_counts_once(tmp_path, app):
    """SSH collection can fetch the same junit.xml as Reports/junit.xml
    and proj/src/Reports/junit.xml — cases must not double-count."""
    with app.app_context():
        ex, rd = _mk_execution(tmp_path)
        _junit_artifact(ex, "Reports/junit.xml",
                        _write(rd, "Reports/junit.xml", JUNIT))
        _junit_artifact(ex, "proj/src/Reports/junit.xml",
                        _write(rd, "proj/src/Reports/junit.xml", JUNIT))
        db.session.commit()
        d._normalize(ex)
        db.session.commit()
        from testorchestrator.execution import TestResult
        assert TestResult.query.filter_by(execution_id=ex.id).count() == 2


def test_identical_reports_from_distinct_units_stay_distinct(
        tmp_path, app):
    """Two shard/child units producing byte-identical junit are both
    counted — provenance differs, so content equality alone must not
    collapse them."""
    with app.app_context():
        ex, rd = _mk_execution(tmp_path)
        _junit_artifact(ex, "units/1/Reports/junit.xml",
                        _write(rd, "units/1/Reports/junit.xml", JUNIT))
        _junit_artifact(ex, "units/2/Reports/junit.xml",
                        _write(rd, "units/2/Reports/junit.xml", JUNIT))
        db.session.commit()
        d._normalize(ex)
        db.session.commit()
        from testorchestrator.execution import TestResult
        assert TestResult.query.filter_by(execution_id=ex.id).count() == 4


def test_distinct_reports_always_kept(tmp_path, app):
    with app.app_context():
        ex, rd = _mk_execution(tmp_path)
        _junit_artifact(ex, "Reports/junit.xml",
                        _write(rd, "Reports/junit.xml", JUNIT))
        _junit_artifact(ex, "Reports/other.xml",
                        _write(rd, "Reports/other.xml", JUNIT_OTHER))
        db.session.commit()
        d._normalize(ex)
        db.session.commit()
        from testorchestrator.execution import TestResult
        assert TestResult.query.filter_by(execution_id=ex.id).count() == 3


def test_dedup_never_spans_executions(tmp_path, app):
    """Identical reports in separate executions are independent —
    the dedup set is per-normalize-call."""
    with app.app_context():
        ex1, rd1 = _mk_execution(tmp_path)
        _junit_artifact(ex1, "Reports/junit.xml",
                        _write(rd1, "Reports/junit.xml", JUNIT))
        ex2, rd2 = _mk_execution(tmp_path / "b")
        _junit_artifact(ex2, "Reports/junit.xml",
                        _write(rd2, "Reports/junit.xml", JUNIT))
        db.session.commit()
        d._normalize(ex1)
        d._normalize(ex2)
        db.session.commit()
        from testorchestrator.execution import TestResult
        assert TestResult.query.filter_by(execution_id=ex1.id).count() == 2
        assert TestResult.query.filter_by(execution_id=ex2.id).count() == 2
