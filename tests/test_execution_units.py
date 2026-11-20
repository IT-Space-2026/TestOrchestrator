"""Parallel unit executions: durable parent + identifiable children,
frozen per-unit assignments, idempotent aggregation, cancellation
propagation, and no dispatch of the parent itself."""
import json

from testorchestrator import dispatch
from testorchestrator.execution import (Execution, ExecutionState,
                                        Outcome, Project, Suite)
from testorchestrator.extensions import db


def _env(app):
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.commit()
        return s.id


def _mk_units(app, sid, n=2):
    with app.app_context():
        s = db.session.get(Suite, sid)
        parent, exs = dispatch.create_unit_run(
            "m", [{"index": i, "key": f"shard {i + 1}/{n}",
                   "shard": f"{i + 1}/{n}"} for i in range(n)],
            suite=s, source_sha="abc")
        return parent.id, [e.id for e in exs]


def test_unit_run_parent_and_children(app):
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 3)
    with app.app_context():
        p = db.session.get(Execution, pid)
        assert p.role == "parent" and p.unit_count == 3
        assert p.state == ExecutionState.QUEUED
        kids_db = sorted(
            Execution.query.filter_by(parent_id=pid).all(),
            key=lambda e: e.unit_index)
        assert [e.id for e in kids_db] == kids
        for i, e in enumerate(kids_db):
            assert e.role == "unit" and e.unit_index == i
            assert e.unit_key == f"shard {i + 1}/3"
            snap = e.snapshot()
            assert snap["parent_id"] == pid
            assert snap["unit"]["shard"] == f"{i + 1}/3"


def test_parent_never_dispatched(app):
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 2)
    with app.app_context():
        # The redrive sweep must not launch the aggregation parent —
        # it owns no process.
        dispatch._redrive_queued_remote()
        p = db.session.get(Execution, pid)
        assert p.state == ExecutionState.QUEUED and not p.dispatch_id


def _finish(app, eid, outcome, state=ExecutionState.COMPLETED):
    e = db.session.get(Execution, eid)
    e.state = state
    e.outcome = outcome
    db.session.commit()


def test_aggregate_all_passed(app):
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 2)
    with app.app_context():
        for k in kids:
            _finish(app, k, Outcome.PASSED)
        dispatch.aggregate_parent_runs()
        p = db.session.get(Execution, pid)
        assert p.state == ExecutionState.COMPLETED
        assert p.outcome == Outcome.PASSED
        assert p.finished_at is not None
        # Idempotent: a second sweep changes nothing.
        dispatch.aggregate_parent_runs()
        assert p.state == ExecutionState.COMPLETED


def test_aggregate_failed_child_blocks_success(app):
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 2)
    with app.app_context():
        _finish(app, kids[0], Outcome.PASSED)
        # Still running — parent must NOT report success early.
        dispatch.aggregate_parent_runs()
        p = db.session.get(Execution, pid)
        assert p.state == ExecutionState.RUNNING
        assert p.outcome == Outcome.NONE
        _finish(app, kids[1], Outcome.FAILED)
        dispatch.aggregate_parent_runs()
        db.session.refresh(p)
        assert p.state == ExecutionState.COMPLETED
        assert p.outcome == Outcome.FAILED


def test_aggregate_infrastructure_child_incomplete(app):
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 2)
    with app.app_context():
        _finish(app, kids[0], Outcome.PASSED)
        _finish(app, kids[1], Outcome.INCOMPLETE,
                ExecutionState.FAILED)
        dispatch.aggregate_parent_runs()
        p = db.session.get(Execution, pid)
        assert p.state == ExecutionState.COMPLETED
        assert p.outcome == Outcome.INCOMPLETE
        assert p.error_kind == "infrastructure"
        assert kids[1][:8] in p.error_text


def test_parent_cancel_propagates_and_blocks_queued(app):
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 3)
    with app.app_context():
        # One running, two still queued.
        c0 = db.session.get(Execution, kids[0])
        c0.state = ExecutionState.DISPATCHED
        c0.dispatch_id = "bb:1"
        db.session.commit()
        assert dispatch.request_cancel(pid, "op", "stop all")
        kids_db = Execution.query.filter_by(parent_id=pid).all()
        assert all(e.state == ExecutionState.CANCEL_REQUESTED
                   for e in kids_db)
        # Terminate children as the cancel path would.
        for e in kids_db:
            e.state = ExecutionState.CANCELED
            e.outcome = Outcome.INCOMPLETE
        db.session.commit()
        dispatch.aggregate_parent_runs()
        p = db.session.get(Execution, pid)
        assert p.state == ExecutionState.CANCELED
        assert p.outcome == Outcome.INCOMPLETE


def test_unit_results_stay_distinct(app):
    """Identical unit rows must never merge: each child keeps its own
    execution identity, snapshot assignment and result rows."""
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 2)
    with app.app_context():
        from testorchestrator.execution import CaseStatus, TestResult
        for i, k in enumerate(kids):
            db.session.add(TestResult(
                execution_id=k, classname="c", case_name="t",
                status=CaseStatus.PASSED, attempt=1))
            _finish(app, k, Outcome.PASSED)
        db.session.commit()
        dispatch.aggregate_parent_runs()
        n = TestResult.query.filter(
            TestResult.execution_id.in_(kids)).count()
        assert n == 2   # one per unit — aggregation never collapses


def test_forming_parent_never_aggregates(app):
    """A parent row with unit_count unset is 'still forming' — even if
    all existing children are terminal, it must NOT roll up. This is
    the partial-creation guard."""
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 2)
    with app.app_context():
        p = db.session.get(Execution, pid)
        p.unit_count = None      # simulate interrupted creation
        db.session.commit()
        for k in kids:
            _finish(app, k, Outcome.PASSED)
        dispatch.aggregate_parent_runs()
        db.session.refresh(p)
        assert p.state == ExecutionState.QUEUED
        assert p.outcome == Outcome.NONE


def test_empty_units_rejected(app):
    sid = _env(app)
    with app.app_context():
        import pytest as _pt
        s = db.session.get(Suite, sid)
        with _pt.raises(dispatch.DispatchError):
            dispatch.create_unit_run("m", [], suite=s)


def test_children_share_frozen_revision(app):
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 2)
    with app.app_context():
        p = db.session.get(Execution, pid)
        for k in kids:
            e = db.session.get(Execution, k)
            assert e.source_sha == p.source_sha == "abc"
            assert e.binding_id == p.binding_id


def test_redrive_never_double_launches_child(app):
    """Competing sweep passes must not double-launch: the second
    redrive sees dispatch_id already set and skips."""
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 2)
    with app.app_context():
        c = db.session.get(Execution, kids[0])
        c.dispatch_id = "bb:99"   # claimed by a competing tick
        c.state = ExecutionState.DISPATCHED
        db.session.commit()
        dispatch._redrive_queued_remote()
        db.session.refresh(c)
        assert c.dispatch_id == "bb:99"   # not overwritten


def test_cancel_race_mixed_children(app):
    """Parent cancel with children in QUEUED + DISPATCHED + terminal:
    propagation covers all non-terminal, completed children are
    untouched, and the rollup waits for real terminal states."""
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 3)
    with app.app_context():
        db.session.get(Execution, kids[0]).state = \
            ExecutionState.DISPATCHED
        _finish(app, kids[1], Outcome.PASSED)
        db.session.commit()
        assert dispatch.request_cancel(pid, "op")
        kdb = [db.session.get(Execution, k) for k in kids]
        assert kdb[0].state == ExecutionState.CANCEL_REQUESTED
        assert kdb[1].state == ExecutionState.COMPLETED  # untouched
        assert kdb[2].state == ExecutionState.CANCEL_REQUESTED
        # roll up after queued/running children reach terminal
        kdb[0].state = ExecutionState.CANCELED
        kdb[0].outcome = Outcome.INCOMPLETE
        kdb[2].state = ExecutionState.CANCELED
        kdb[2].outcome = Outcome.INCOMPLETE
        db.session.commit()
        dispatch.aggregate_parent_runs()
        p = db.session.get(Execution, pid)
        # one child PASSED but two cancelled + cancel was requested ->
        # CANCELED/INCOMPLETE, never a partial pass
        assert p.state == ExecutionState.CANCELED
        assert p.outcome == Outcome.INCOMPLETE


def test_identical_reports_across_units_not_deduped(app):
    """Two units emitting byte-identical junit remain two result
    sets — dedup is provenance-scoped, never execution-global."""
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 2)
    JUNIT = (b'<?xml version="1.0"?><testsuite tests="1">'
             b'<testcase classname="c" name="t" time="0.1"/>'
             b'</testsuite>')
    with app.app_context():
        # Byte-identical junit arriving from two distinct unit dirs:
        # _same_report_source must NOT link them (provenance differs).
        from testorchestrator import dispatch as _d
        assert not _d._same_report_source(
            "unit-0/Reports/junit.xml", "unit-1/Reports/junit.xml")
        assert _d._same_report_source(
            "Reports/junit.xml", "proj/src/Reports/junit.xml")
        from testorchestrator.execution import CaseStatus, TestResult
        for k in kids:
            db.session.add(TestResult(
                execution_id=k, classname="c", case_name="t",
                status=CaseStatus.PASSED, attempt=1))
            _finish(app, k, Outcome.PASSED)
        db.session.commit()
        dispatch.aggregate_parent_runs()
        # each unit's own row survives — no cross-execution collapse
        assert TestResult.query.filter(
            TestResult.execution_id.in_(kids)).count() == 2


def test_unit_artifacts_outlive_parent(app):
    """Children keep their own report/workspace paths — a failed or
    cancelled parent never rewrites them."""
    sid = _env(app)
    pid, kids = _mk_units(app, sid, 2)
    with app.app_context():
        for k in kids:
            e = db.session.get(Execution, k)
            e.report_dir = f"/tmp/reports-{k[:8]}"
            _finish(app, k, Outcome.FAILED)
        db.session.commit()
        dispatch.aggregate_parent_runs()
        for k in kids:
            e = db.session.get(Execution, k)
            assert e.report_dir == f"/tmp/reports-{k[:8]}"
            assert e.outcome == Outcome.FAILED


# ---------- parallel config validation (server-side, all paths) ------

def _vpar(app, sid, rc):
    with app.app_context():
        s = db.session.get(Suite, sid)
        return dispatch.create_execution(
            "m", s, source_sha="x", run_config=rc).id


def test_parallel_validation(app):
    import pytest as _pt
    sid = _env(app)
    # default: framework chooses, no flags
    eid = _vpar(app, sid, {"parallel": 0})
    with app.app_context():
        e = db.session.get(Execution, eid)
        rc = e.snapshot()["run_config"]
        assert rc["parallel_mode"] == "default"
    # sequential mode is explicit — survives to snapshot
    eid = _vpar(app, sid, {"parallel_mode": "sequential"})
    with app.app_context():
        e = db.session.get(Execution, eid)
        assert e.snapshot()["run_config"]["parallel_mode"] == \
            "sequential"
    for bad, msg in [
            ({"parallel": 65}, "between"),
            ({"parallel": 2, "parallel_mode": "sequential"},
             "conflicts"),
            ({"parallel": 0, "parallel_mode": "parallel"},
             "requires parallel >= 1"),
            ({"parallel_mode": "bogus"}, "unknown parallel_mode"),
            ({"shard_index": 1}, "together"),
            ({"shard_index": 3, "shard_total": 2}, "invalid shard"),
            ({"shard_index": 1, "shard_total": 2},
             "playwright-only"),  # suite runtime resolves kre
            ]:
        with _pt.raises(dispatch.DispatchError, match=msg):
            _vpar(app, sid, bad)


def test_parallel_persisted_in_snapshot(app):
    sid = _env(app)
    with app.app_context():
        from testorchestrator.execution import (Runner,
                                                RuntimeConnection)
        r = Runner(name="pw", connection_mode="local", max_slots=2)
        db.session.add(r); db.session.flush()
        rt = RuntimeConnection(name="rt", runner_id=r.id,
                               executable_path="/usr/bin/python3",
                               runtime_kind="pytest", enabled=True)
        db.session.add(rt); db.session.commit()
        rtid = rt.id
    eid = _vpar(app, sid, {"parallel": 4, "runtime_id": rtid})
    with app.app_context():
        e = db.session.get(Execution, eid)
        rc = e.snapshot()["run_config"]
        assert rc["parallel_mode"] == "parallel" and rc["parallel"] == 4


# ---------- partitioning ----------

def test_partition_units_exact_assignment(app):
    sid = _env(app)
    with app.app_context():
        specs = [f"spec{i}.cy.js" for i in range(7)]
        units = dispatch.partition_units(specs, 3, "spec group", "specs")
        flat = [s for u in units for s in u["specs"]]
        assert sorted(flat) == sorted(specs)     # each spec exactly once
        assert len(units) == 3 and all(u["specs"] for u in units)
        import pytest as _pt
        with _pt.raises(dispatch.DispatchError, match="at most"):
            dispatch.partition_units(specs, 8, "x", "specs")
        with _pt.raises(dispatch.DispatchError):
            dispatch.partition_units([], 2, "x", "specs")


def test_playwright_shard_units(app):
    sid = _env(app)
    with app.app_context():
        units = dispatch.playwright_shard_units(4)
        assert [u["shard"] for u in units] == [
            "1/4", "2/4", "3/4", "4/4"]
        import pytest as _pt
        with _pt.raises(dispatch.DispatchError):
            dispatch.playwright_shard_units(1)


def test_discover_cypress_specs(app, tmp_path):
    from testorchestrator.execution import GitBinding
    with app.app_context():
        d = tmp_path / "repo"
        (d / "cypress" / "e2e").mkdir(parents=True)
        for n in ("a.cy.js", "b.cy.js", "skip.spec.js"):
            (d / "cypress" / "e2e" / n).write_text("// spec")
        (d / "cypress" / "e2e" / "deep").mkdir()
        (d / "cypress" / "e2e" / "deep" / "c.cy.ts").write_text("//")
        b = GitBinding(project_id="p", name="b", url="u",
                       worktree_path=str(d), last_synced_sha="s1")
        specs = dispatch.discover_cypress_specs(b, sha="s1")
        assert specs == ["cypress/e2e/a.cy.js", "cypress/e2e/b.cy.js",
                         "cypress/e2e/deep/c.cy.ts"]
        # exclusions + selection + stale-sha error
        assert dispatch.discover_cypress_specs(
            b, sha="s1", exclude_pattern="cypress/e2e/b*") == [
            "cypress/e2e/a.cy.js", "cypress/e2e/deep/c.cy.ts"]
        assert dispatch.discover_cypress_specs(
            b, sha="s1", selection=["cypress/e2e/a.cy.js"]) == [
            "cypress/e2e/a.cy.js"]
        import pytest as _pt
        with _pt.raises(dispatch.DispatchError, match="manifest"):
            dispatch.discover_cypress_specs(b, sha="other-sha")
        with _pt.raises(dispatch.DispatchError, match="empty"):
            dispatch.discover_cypress_specs(
                b, sha="s1", spec_pattern="nope/**/*.cy.*")


# ---------- combined capacity: job slots AND process budget ----------

def test_proc_budget_blocks_parallel_jobs(app):
    """max_slots admits the jobs, but max_procs must reject the second
    parallel run when its own -n would breach the process budget."""
    from testorchestrator.execution import (Runner, RuntimeConnection)
    sid = _env(app)
    with app.app_context():
        p2 = db.session.get(Suite, sid)
        r = Runner(name="cap", connection_mode="local", max_slots=4,
                   max_procs=4)
        db.session.add(r); db.session.flush()
        rt = RuntimeConnection(name="rt2", runner_id=r.id,
                               executable_path="/usr/bin/python3",
                               runtime_kind="pytest", enabled=True)
        db.session.add(rt); db.session.commit()
        rid, rtid = r.id, rt.id
        mk = lambda: dispatch.create_execution(
            "m", db.session.get(Suite, sid), source_sha="x",
            run_config={"runtime_id": rtid, "runner_id": rid,
                        "parallel": 3, "parallel_mode": "parallel"})
        e1, e2 = mk(), mk()
        assert dispatch.requested_procs(e1) == 3
        # First job admitted: 3 of 4 procs used, one job slot left but
        # the second -n 3 would total 6 > 4 -> stays QUEUED.
        assert dispatch._assign_runner(e1, r, rt)
        assert not dispatch._assign_runner(e2, r, rt)
        db.session.refresh(e2)
        assert e2.state == ExecutionState.QUEUED
        # Unconstrained runner (max_procs NULL) still admits both jobs.
        r.max_procs = None
        db.session.commit()
        assert dispatch._assign_runner(e2, r, rt)
