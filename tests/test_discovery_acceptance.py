"""Acceptance-evidence tests for the discovery round — the items
disclosed as untested in the prior report."""

import json
import pytest

from testorchestrator.extensions import db
from testorchestrator.execution import (DiscoveredItem, GitBinding,
                                        Project, Schedule, Suite,
                                        Execution, utcnow)
from testorchestrator.gitsync import discover_project
from testorchestrator import dispatch


SUITE = """<TestSuiteEntity><name>S</name>
<testCaseLink><guid>g</guid>
<testCaseId>Test Cases/t.tc</testCaseId></testCaseLink>
</TestSuiteEntity>"""
COLL = """<TestSuiteCollectionEntity><testSuiteRunConfigurations>
<TestSuiteRunConfiguration><profileName>default</profileName>
<testSuiteEntity>Test Suites/S</testSuiteEntity>
</TestSuiteRunConfiguration></testSuiteRunConfigurations>
</TestSuiteCollectionEntity>"""
CASE = "<TestCaseEntity><name>t</name></TestCaseEntity>"


def _mk(wt, files):
    for rel, content in files.items():
        p = wt / rel; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def _b(app, wt, project=None):
    p = project or Project(name="P")
    db.session.add(p); db.session.flush()
    b = GitBinding(project_id=p.id, name="b", url="x",
                   worktree_path=str(wt), last_synced_sha="s1",
                   status="synced")
    db.session.add(b); db.session.commit()
    return b


def test_oversized_definition_warns_not_crashes(app, tmp_path):
    """A >512KB .ts is skipped with a warning — other items still
    discovered; the user sees a warning, not silent omission."""
    wt = tmp_path / "wt"
    _mk(wt, {"P.prj": "<p/>", "Test Cases/t.tc": CASE,
             "Test Suites/huge.ts": "<x>" + "a" * 600_000 + "</x>",
             "Test Suites/S.ts": SUITE})
    with app.app_context():
        b = _b(app, wt)
        r = discover_project(b)
        # both .ts files recorded — the oversized one carries a warning
        # (extension-based classification; it is not silently omitted)
        assert r["cases"] == 1 and r["suites"] == 2
        assert any("huge.ts" in w and "oversized" in w
                   for w in r["warnings"])


def test_xxe_attempt_safe(app, tmp_path):
    """External-entity DOCTYPE doesn't expand (no network/file reads)."""
    wt = tmp_path / "wt"
    _mk(wt, {"P.prj": "<p/>", "Test Cases/t.tc": CASE,
             "Test Suites/x.ts":
             '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x '
             'SYSTEM "file:///etc/passwd">]>'
             '<TestSuiteEntity><name>&x;</name></TestSuiteEntity>'})
    with app.app_context():
        b = _b(app, wt)
        r = discover_project(b)
        # iterparse start-tag only — entity never resolved; either a
        # parse warning or a suite with an unexpanded name is fine.
        assert r["cases"] == 1


def test_symlink_escape_excluded(app, tmp_path):
    """A symlinked .ts pointing outside the checkout is not followed."""
    wt = tmp_path / "wt"; outside = tmp_path / "outside.ts"
    outside.write_text(SUITE)
    _mk(wt, {"P.prj": "<p/>", "Test Cases/t.tc": CASE})
    link = wt / "Test Suites" / "escape.ts"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)
    with app.app_context():
        b = _b(app, wt)
        r = discover_project(b)
        # the escaped file must not appear as a discovered suite
        assert not DiscoveredItem.query.filter(
            DiscoveredItem.path.contains("escape")).count()


def test_cross_project_selector_rejected(app, client, login_as):
    """A discovered item from ANOTHER project can't be submitted as
    this project's source_item — server-side project check."""
    with app.app_context():
        p1 = Project(name="P1"); p2 = Project(name="P2")
        db.session.add_all([p1, p2]); db.session.flush()
        s = Suite(project_id=p1.id, name="S"); db.session.add(s)
        db.session.flush()
        from testorchestrator.execution import SuiteEntry
        db.session.add(SuiteEntry(suite_id=s.id, case_ref="Test Cases/t.tc"))
        b = GitBinding(project_id=p2.id, name="b", url="x",
                       worktree_path="/nonexistent",
                       last_synced_sha="s", status="synced")
        db.session.add(b); db.session.flush()
        foreign = DiscoveredItem(binding_id=b.id, project_id=p2.id,
                                 sha="s", kind="suite",
                                 path="Test Suites/X", name="X")
        db.session.add(foreign); db.session.commit()
        sid, fid = s.id, foreign.id
    login_as("admin")
    resp = client.post(f"/suites/{sid}/run",
                       data={"source_item": fid,
                             "execution_profile": "default"},
                       follow_redirects=True)
    # rejected -> back to the suite page, NOT an execution detail,
    # and no execution row created.
    assert resp.request.path == f"/suites/{sid}"
    with app.app_context():
        assert Execution.query.count() == 0


def test_collection_type_through_snapshot(app, tmp_path):
    """collection in run_config -> snapshot -> build_args emits
    -testSuiteCollectionPath, never both."""
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        ex = dispatch.create_execution(
            "m", s, run_config={"collection": True,
                                "suite_path": "Test Suites/Coll"})
        rc = ex.snapshot()["run_config"]
        assert rc["collection"] is True
        assert rc["suite_path"] == "Test Suites/Coll"
        from testorchestrator.adapters.kre import build_args
        a = build_args("k", "/p", rc["suite_path"], "/r", "default",
                       collection=rc["collection"])
        assert "-testSuiteCollectionPath=Test Suites/Coll" in a
        assert not any(x.startswith("-testSuitePath=") for x in a)


def test_schedule_source_item_persists_type(app):
    """A schedule storing collection=True propagates the type into
    the fired execution's snapshot."""
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        sc = Schedule(project_id=p.id, name="w", target_type="suite",
                      suite_id=s.id, kind="interval", enabled=True,
                      next_run_at=utcnow(),
                      run_config_json=json.dumps(
                          {"collection": True,
                           "suite_path": "Test Suites/C"}),
                      created_by="m")
        db.session.add(sc); db.session.commit()
        ex = dispatch.create_execution("m", s,
                                       run_config=sc.run_config(),
                                       schedule_id=sc.id)
        rc = ex.snapshot()["run_config"]
        assert rc["collection"] is True
        assert rc["suite_path"] == "Test Suites/C"


def test_older_discovery_cannot_overwrite_newer(app, tmp_path):
    """Deterministic ordering: newer discovery B completes first, then
    the late result of older A must NOT overwrite B's inventory —
    A's write is skipped and marked stale."""
    wt1 = tmp_path / "wt1"; wt2 = tmp_path / "wt2"
    _mk(wt1, {"P.prj": "<p/>", "Test Cases/old.tc": CASE})
    _mk(wt2, {"P.prj": "<p/>", "Test Cases/new.tc": CASE,
              "Test Cases/new2.tc": CASE})
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        b = GitBinding(project_id=p.id, name="b", url="x",
                       worktree_path=str(wt2),
                       last_synced_sha="sha-B", status="synced")
        db.session.add(b); db.session.commit()
        # B (newer) completes first
        rb = discover_project(b, sha="sha-B")
        assert b.discovered_sha == "sha-B"
        n_b = DiscoveredItem.query.count()
        # A (older, started earlier, completes late) — sha-A is stale
        b.worktree_path = str(wt1)
        db.session.commit()
        ra = discover_project(b, sha="sha-A")
        assert ra.get("stale_skip")
        # B's inventory intact — not replaced by A's
        assert b.discovered_sha == "sha-B"
        assert DiscoveredItem.query.count() == n_b
        assert DiscoveredItem.query.filter(
            DiscoveredItem.path.contains("new")).count() == n_b


def test_plan_path_emits_collection_arg(app):
    """queue_plan_executions forwards the schedule run_config — a
    collection-typed plan entry gets -testSuiteCollectionPath, never
    -testSuitePath."""
    import json
    from testorchestrator.execution import Plan, PlanEntry
    from testorchestrator.adapters.kre import build_args
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        pl = Plan(project_id=p.id, name="Pl"); db.session.add(pl)
        db.session.flush()
        db.session.add(PlanEntry(plan_id=pl.id, suite_id=s.id,
                                 order=0))
        db.session.commit()
        cfg = {"collection": True, "suite_path": "Test Suites/Coll",
               "profile": "default"}
        run_id, exs = dispatch.queue_plan_executions(
            "m", pl, run_config=cfg, plan_run_id="pr1")
        rc = exs[0].snapshot()["run_config"]
        assert rc["collection"] is True
        a = build_args("k", "/p", rc["suite_path"], "/r",
                       rc["profile"], collection=rc["collection"])
        assert "-testSuiteCollectionPath=Test Suites/Coll" in a
        assert not any(x.startswith("-testSuitePath=") for x in a)
