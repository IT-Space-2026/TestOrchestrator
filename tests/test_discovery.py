"""Katalon project discovery — typed inventory, root detection,
safety, and lifecycle coverage (synthetic fixtures only)."""

import json
from pathlib import Path

import pytest

from testorchestrator.extensions import db
from testorchestrator.execution import (DiscoveredItem, GitBinding,
                                        Project, utcnow)
from testorchestrator.gitsync import discover_project


def _fixture(tmp_path, files: dict):
    """Build a synthetic Katalon project tree."""
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp_path


def _binding(app, worktree, project=None, subdirectory=""):
    p = project or Project(name="P")
    db.session.add(p); db.session.flush()
    b = GitBinding(project_id=p.id, name="b", url="x",
                   worktree_path=str(worktree),
                   subdirectory=subdirectory,
                   last_synced_sha="sha1", status="synced")
    db.session.add(b); db.session.commit()
    return b


SUITE = """<TestSuiteEntity><name>S1</name>
<testCaseLink><guid>g1</guid>
<testCaseId>Test Cases/API/tc1</testCaseId></testCaseLink>
<testCaseLink><guid>g2</guid>
<testCaseId>Test Cases/API/tc2</testCaseId></testCaseLink>
</TestSuiteEntity>"""
COLL = """<TestSuiteCollectionEntity>
<testSuiteRunConfigurations><TestSuiteRunConfiguration>
<profileName>default</profileName>
<testSuiteEntity>Test Suites/S1</testSuiteEntity>
</TestSuiteRunConfiguration></testSuiteRunConfigurations>
</TestSuiteCollectionEntity>"""
CASE = "<TestCaseEntity><name>tc1</name></TestCaseEntity>"
PROF = "<GlobalVariableEntity><name>default</name></GlobalVariableEntity>"


def _base_files():
    return {
        "P.prj": "<ProjectEntity></ProjectEntity>",
        "Test Cases/API/tc1.tc": CASE,
        "Test Cases/API/nested/deep tc Ünicode.tc": CASE,
        "Test Suites/S1.ts": SUITE,
        "Test Suites/Coll 1.ts": COLL,
        "Profiles/default.glbl": PROF,
        "Scripts/groovy/script.groovy": "// not a case",
        ".cache/cache.ts": "noise",
        "Keywords/k.groovy": "// not a case",
    }


def test_first_discovery_counts_and_types(app, tmp_path):
    wt = _fixture(tmp_path / "wt", _base_files())
    with app.app_context():
        b = _binding(app, wt)
        r = discover_project(b)
        assert r["cases"] == 2 and r["suites"] == 1
        assert r["collections"] == 1 and r["profiles"] == 1
        assert b.discovery_status == "ok"
        assert b.project_root == "."
        # noise excluded
        assert not DiscoveredItem.query.filter(
            DiscoveredItem.path.like("%.groovy%")).count()
        assert not DiscoveredItem.query.filter(
            DiscoveredItem.path.like("%cache%")).count()


def test_membership_order_preserved(app, tmp_path):
    wt = _fixture(tmp_path / "wt", _base_files())
    with app.app_context():
        b = _binding(app, wt)
        discover_project(b)
        s = DiscoveredItem.query.filter_by(kind="suite").first()
        assert s.members() == ["Test Cases/API/tc1",
                               "Test Cases/API/tc2"]
        c = DiscoveredItem.query.filter_by(kind="collection").first()
        assert c.members() == ["Test Suites/S1"]


def test_case_sensitive_and_unicode_paths(app, tmp_path):
    wt = _fixture(tmp_path / "wt", _base_files())
    with app.app_context():
        b = _binding(app, wt)
        discover_project(b)
        paths = {i.path for i in DiscoveredItem.query.filter_by(
            kind="case")}
        assert any("Ünicode" in p or "unicode" in p.lower()
                   for p in paths)


def test_nested_subdirectory_root(app, tmp_path):
    wt = _fixture(tmp_path / "wt", {
        "sub/P.prj": "<ProjectEntity></ProjectEntity>",
        "sub/Test Cases/t.tc": CASE,
        "sub/Test Suites/s.ts": SUITE,
        "sub/Profiles/p.glbl": PROF})
    with app.app_context():
        b = _binding(app, wt, subdirectory="sub")
        r = discover_project(b)
        assert r["cases"] == 1 and b.project_root == "."


def test_multiple_prj_requires_selection(app, tmp_path):
    wt = _fixture(tmp_path / "wt", {
        "a/A.prj": "<p/>", "b/B.prj": "<p/>"})
    with app.app_context():
        b = _binding(app, wt)
        r = discover_project(b)
        assert r.get("error") and "multiple" in r["error"]
        assert b.discovery_status == "failed"


def test_no_prj_is_error_not_empty(app, tmp_path):
    wt = _fixture(tmp_path / "wt", {"readme.md": "x"})
    with app.app_context():
        b = _binding(app, wt)
        r = discover_project(b)
        assert r.get("error")
        # a failed discovery is NOT a successful empty inventory
        assert b.discovery_status == "failed"
        assert DiscoveredItem.query.count() == 0


def test_malformed_xml_warns_not_fails(app, tmp_path):
    f = _base_files()
    f["Test Suites/broken.ts"] = "<unclosed"
    wt = _fixture(tmp_path / "wt", f)
    with app.app_context():
        b = _binding(app, wt)
        r = discover_project(b)
        assert r["suites"] >= 1                     # good ones kept
        assert any("malformed" in w or "members" in w
                   for w in r["warnings"])


def test_repeat_discovery_idempotent(app, tmp_path):
    wt = _fixture(tmp_path / "wt", _base_files())
    with app.app_context():
        b = _binding(app, wt)
        discover_project(b)
        n1 = DiscoveredItem.query.count()
        discover_project(b)
        assert DiscoveredItem.query.count() == n1   # no duplicates


def test_failed_discovery_preserves_inventory(app, tmp_path):
    wt = _fixture(tmp_path / "wt", _base_files())
    with app.app_context():
        b = _binding(app, wt)
        discover_project(b)
        n1 = DiscoveredItem.query.count()
        # break the worktree
        b.worktree_path = str(tmp_path / "gone")
        db.session.commit()
        r = discover_project(b)
        assert r.get("error")
        assert b.discovery_status == "failed"
        # previous inventory preserved (stale)
        assert DiscoveredItem.query.count() == n1
        assert b.discovered_sha == "sha1"


def test_collection_vs_suite_arg(app, tmp_path):
    from testorchestrator.adapters.kre import build_args
    s = build_args("k", "/p", "Test Suites/S", "/r", "default")
    c = build_args("k", "/p", "Test Suites/C", "/r", "default",
                   collection=True)
    assert "-testSuitePath=Test Suites/S" in s
    assert "-testSuiteCollectionPath=Test Suites/C" in c
    assert not any("testSuitePath" in a and "Collection" not in a
                   for a in c)


def test_upgrade_forward_path(tmp_path):
    """A DB at the OLD f8a9b0c1d2e3 schema (no members_json) gains the
    column via g9a0b1c2d3e4 — forward, not rewritten history."""
    import sqlite3
    from tests.conftest import make_app
    from pathlib import Path as P
    app = make_app(tmp_path, "fwd.db")
    MIGR = str(P(__file__).resolve().parent.parent / "migrations")
    with app.app_context():
        import sqlalchemy as sa
        from flask_migrate import upgrade as _up
        # Build the TRUE old schema with the real migrations instead of
        # a hand-faked subset — the forward chain also touches
        # schedules, runtime_connections, authoring_drafts, releases.
        _up(directory=MIGR, revision='f8a9b0c1d2e3')
        eng = sa.create_engine(f"sqlite:///{tmp_path/'fwd.db'}")
        from flask_migrate import upgrade
        upgrade(directory=MIGR)   # g9a0b1c2d3e4 -> adds members_json
        with eng.begin() as conn:
            cols = {r[1] for r in conn.execute(
                sa.text(
                "PRAGMA table_info(discovered_items)"))}
        assert "members_json" in cols
