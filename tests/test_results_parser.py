"""Canonical JUnit parsing + evidence manifest tests (§8–§9)."""

from pathlib import Path

import pytest

from testorchestrator.results import (JunitParseError, aggregate_outcome,
                                      build_evidence_manifest, classify_artifact,
                                      parse_junit, safe_relpath)


def _write(tmp_path, text):
    p = tmp_path / "report.xml"
    p.write_text(text)
    return p


def test_parse_basic(tmp_path):
    run = parse_junit(_write(tmp_path, """<?xml version="1.0"?>
<testsuite tests="2" failures="1">
  <testcase classname="C1" name="ok" time="1.0"/>
  <testcase classname="C1" name="bad" time="0.5">
    <failure>assert x</failure>
  </testcase>
</testsuite>"""))
    assert len(run.cases) == 2
    assert run.cases[1].status == "failed"
    assert run.cases[1].message == "assert x"
    assert not run.discrepant


def test_skipped_and_error(tmp_path):
    run = parse_junit(_write(tmp_path, """<testsuite tests="2">
  <testcase name="s"><skipped message="n/a"/></testcase>
  <testcase name="e"><error message="boom"/></testcase>
</testsuite>"""))
    assert [c.status for c in run.cases] == ["skipped", "error"]


def test_discrepant_producer_totals(tmp_path):
    """Producer claims 5 tests, file contains 2 — surfaced, not hidden."""
    run = parse_junit(_write(tmp_path, """<testsuite tests="5">
  <testcase name="a"/><testcase name="b"/>
</testsuite>"""))
    assert run.discrepant is True


def test_duplicate_names_counted_as_attempts(tmp_path):
    run = parse_junit(_write(tmp_path, """<testsuite tests="2">
  <testcase name="row"/><testcase name="row"/>
</testsuite>"""))
    assert len(run.cases) == 2  # data rows, not deduplicated


def test_malformed_and_non_junit(tmp_path):
    with pytest.raises(JunitParseError):
        parse_junit(_write(tmp_path, "<notxml"))
    with pytest.raises(JunitParseError):
        parse_junit(_write(tmp_path, "<html><body/></html>"))


def test_outcome_semantics():
    class C:
        def __init__(self, s): self.status = s
    assert aggregate_outcome([C("passed")], True) == "passed"
    assert aggregate_outcome([C("skipped"), C("skipped")], True) == "incomplete"
    assert aggregate_outcome([C("passed")], False) == "incomplete"
    assert aggregate_outcome([], True) == "incomplete"  # never zero-test pass
    assert aggregate_outcome([C("passed"), C("failed")], True) == "failed"


def test_safe_relpath_blocks_traversal(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "ok.txt"; inside.write_text("x")
    assert safe_relpath(root, inside) == "ok.txt"
    outside = tmp_path / "evil.txt"; outside.write_text("x")
    with pytest.raises(ValueError):
        safe_relpath(root, outside)


def test_manifest_checksums_and_kinds(tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    (root / "out.xml").write_text("<testsuite/>")
    (root / "shot.png").write_bytes(b"\x89PNG")
    (root / "page.html").write_text("<html/>")
    manifest = build_evidence_manifest(root, junit_files={root / "out.xml"})
    by_name = {m["rel_path"]: m for m in manifest}
    assert by_name["out.xml"]["kind"] == "junit"
    assert by_name["shot.png"]["kind"] == "screenshot"
    assert by_name["page.html"]["active_content"] is True
    assert all(len(m["sha256"]) == 64 for m in manifest)
