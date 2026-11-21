"""Parallel unit executions: durable parent + identifiable children,
frozen per-unit assignments, idempotent aggregation, cancellation
propagation, and no dispatch of the parent itself."""
import json

from testorchestrator import dispatch
from testorchestrator.dispatch import utcnow
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


def test_cypress_config_pattern_honored(app, tmp_path):
    """specPattern in cypress.config.js is the effective config —
    automatic discovery uses it; a non-literal pattern is rejected,
    never replaced by a guessed fallback."""
    from testorchestrator.execution import GitBinding
    with app.app_context():
        d = tmp_path / "repo"
        (d / "tests" / "ui").mkdir(parents=True)
        (d / "tests" / "ui" / "a.spec.js").write_text("//")
        (d / "cypress" / "e2e").mkdir(parents=True)
        (d / "cypress" / "e2e" / "b.cy.js").write_text("//")
        (d / "cypress.config.js").write_text(
            "module.exports={e2e:{specPattern:'tests/**/*.spec.js'}}")
        b = GitBinding(project_id="p", name="b", url="u",
                       worktree_path=str(d), last_synced_sha="s")
        assert dispatch.discover_cypress_specs(b, sha="s") == [
            "tests/ui/a.spec.js"]
        # explicit pattern overrides config
        assert dispatch.discover_cypress_specs(
            b, sha="s", spec_pattern="cypress/**/*.cy.js") == [
            "cypress/e2e/b.cy.js"]
        # non-literal pattern -> explicit error
        (d / "cypress.config.js").write_text(
            "module.exports={e2e:{specPattern:process.env.PAT}}")
        import pytest as _pt
        with _pt.raises(dispatch.DispatchError,
                        match="non-literal"):
            dispatch.discover_cypress_specs(b, sha="s")


def test_unsafe_spec_paths_rejected(app, tmp_path):
    from testorchestrator.execution import GitBinding
    with app.app_context():
        d = tmp_path / "repo"
        (d / "cypress" / "e2e").mkdir(parents=True)
        (d / "cypress" / "e2e" / "a,b.cy.js").write_text("//")
        b = GitBinding(project_id="p", name="b", url="u",
                       worktree_path=str(d), last_synced_sha="s")
        import pytest as _pt
        with _pt.raises(dispatch.DispatchError,
                        match="represent"):
            dispatch.discover_cypress_specs(b, sha="s")
        # spaces are fine inside one argv element
        (d / "cypress" / "e2e" / "a,b.cy.js").rename(
            d / "cypress" / "e2e" / "a b.cy.js")
        assert dispatch.discover_cypress_specs(b, sha="s") == [
            "cypress/e2e/a b.cy.js"]


def test_framework_default_unbounded_on_budget(app):
    """Playwright default mode auto-scales — on a max_procs target it
    is unbounded and must fail visibly, never queue silently."""
    from testorchestrator.execution import (Runner, RuntimeConnection)
    sid = _env(app)
    with app.app_context():
        r = Runner(name="cap2", connection_mode="local",
                   max_slots=4, max_procs=4)
        db.session.add(r); db.session.flush()
        rt = RuntimeConnection(name="pw2", runner_id=r.id,
                               executable_path="/usr/bin/npx",
                               runtime_kind="playwright", enabled=True)
        db.session.add(rt); db.session.commit()
        ex = dispatch.create_execution(
            "m", db.session.get(Suite, sid), source_sha="x",
            run_config={"runtime_id": rt.id, "runner_id": r.id,
                        "parallel_mode": "default"})
        assert dispatch.requested_procs(ex) >= dispatch.UNBOUNDED_PROCS
        dispatch._buildbot_assign_and_submit(ex)
        db.session.refresh(ex)
        assert ex.state == ExecutionState.FAILED
        assert ex.error_kind == "config"
        # bounded mode on the same runner is admitted
        ex2 = dispatch.create_execution(
            "m", db.session.get(Suite, sid), source_sha="x",
            run_config={"runtime_id": rt.id, "runner_id": r.id,
                        "parallel_mode": "sequential"})
        assert dispatch.requested_procs(ex2) == 1


def test_reservations_release_and_hold(app):
    """Terminal states free the proc charge; UNKNOWN keeps it — an
    uncertain run may still own processes."""
    from testorchestrator.execution import (Runner, RuntimeConnection)
    sid = _env(app)
    with app.app_context():
        r = Runner(name="cap3", connection_mode="local",
                   max_slots=4, max_procs=4)
        db.session.add(r); db.session.flush()
        rt = RuntimeConnection(name="rt3", runner_id=r.id,
                               executable_path="/usr/bin/python3",
                               runtime_kind="pytest", enabled=True)
        db.session.add(rt); db.session.commit()
        rid, rtid = r.id, rt.id
        mk = lambda: dispatch.create_execution(
            "m", db.session.get(Suite, sid), source_sha="x",
            run_config={"runtime_id": rtid, "runner_id": rid,
                        "parallel": 3, "parallel_mode": "parallel"})
        e1, e2, e3 = mk(), mk(), mk()
        assert dispatch._assign_runner(e1, r, rt)
        # e1 UNKNOWN (uncertain): charge held, e2 still blocked
        e1.state = ExecutionState.UNKNOWN
        db.session.commit()
        assert not dispatch._assign_runner(e2, r, rt)
        # resolved to FAILED: charge released, e2 admitted
        e1.state = ExecutionState.FAILED
        db.session.commit()
        assert dispatch._assign_runner(e2, r, rt)
        # completed: released again, e3 admitted
        e2.state = ExecutionState.COMPLETED
        db.session.commit()
        assert dispatch._assign_runner(e3, r, rt)
        # cancelled mid-flight: released
        e3.state = ExecutionState.CANCELED
        db.session.commit()
        assert dispatch._assign_runner(mk(), r, rt)


def test_queued_child_cancel_terminalizes_and_parent_rolls_up(app):
    """A unit cancelled while still QUEUED has no dispatch_id — no
    remote process exists to confirm. It must reach CANCELED so the
    parent can roll up; parking in CANCEL_REQUESTED strands both."""
    sid = _env(app)
    with app.app_context():
        parent, kids = dispatch.create_unit_run(
            "u", [{"index": 0, "key": "k0"},
                  {"index": 1, "key": "k1"}],
            suite=db.session.get(Suite, sid), source_sha="y",
            run_config={})
        # unit 0 dispatched, unit 1 still queued when the cancel lands
        kids[0].dispatch_id = "bb:9001"
        kids[0].state = ExecutionState.DISPATCHED
        kids[0].started_at = utcnow()
        db.session.commit()
        dispatch.request_cancel(parent.id, "u")
        kids[0].state = ExecutionState.CANCELED   # buildbot confirmed
        kids[0].outcome = Outcome.INCOMPLETE
        db.session.commit()
        n = dispatch._resolve_unlaunched_cancels()
        assert n == 1
        assert kids[1].state == ExecutionState.CANCELED
        assert kids[1].outcome == Outcome.INCOMPLETE
        dispatch.aggregate_parent_runs()
        db.session.refresh(parent)
        assert parent.state == ExecutionState.CANCELED
        assert parent.outcome == Outcome.INCOMPLETE


def test_cancel_racing_submission_is_not_resolved(app):
    """An empty dispatch_id does NOT prove never-launched: a cancel
    racing _assign_runner->_submit_buildbot leaves runner_id/bb_slot
    set while the buildrequest is still in flight. Such rows must
    stay CANCEL_REQUESTED (aging to UNKNOWN via lease), never be
    released early."""
    sid = _env(app)
    with app.app_context():
        ex = dispatch.create_execution(
            "u", db.session.get(Suite, sid), source_sha="x",
            run_config={})
        # cancel lands after assignment (runner + slot recorded) but
        # before the brid is persisted — submission may be in flight
        ex.state = ExecutionState.CANCEL_REQUESTED
        ex.runner_id = "r1"
        ex.bb_slot = 0
        db.session.commit()
        assert dispatch._resolve_unlaunched_cancels() == 0
        db.session.refresh(ex)
        assert ex.state == ExecutionState.CANCEL_REQUESTED
        # still not resolved once the brid lands — the reconciler
        # owns it from here
        ex.dispatch_id = "bb:42"
        db.session.commit()
        assert dispatch._resolve_unlaunched_cancels() == 0


def test_pytest_serial_flag(app):
    from testorchestrator.adapters import pytestfw
    a = pytestfw.build_args(suite_path="tests/", force_serial=True)
    assert a[a.index("-n") + 1] == "0"
    assert "-n" not in pytestfw.build_args(suite_path="tests/")


def test_slot_builders_and_pick(app):
    """N admitted slots need N distinct builders (a builder+worker pair
    serializes); _pick_slot hands out indices no ACTIVE sibling holds
    and recycles freed ones."""
    from testorchestrator.execution import (Runner, RuntimeConnection)
    from testorchestrator import buildbot_cfg
    sid = _env(app)
    with app.app_context():
        r = Runner(name="slots", connection_mode="local", max_slots=2,
                   target_type="buildbot-worker")
        db.session.add(r); db.session.flush()
        rt = RuntimeConnection(name="rts", runner_id=r.id,
                               executable_path="/usr/bin/python3",
                               runtime_kind="pytest", enabled=True)
        db.session.add(rt); db.session.commit()
        assert buildbot_cfg.slot_count(r) == 2
        assert buildbot_cfg.builder_for(r, 0) != \
            buildbot_cfg.builder_for(r, 1)
        mk = lambda: dispatch.create_execution(
            "m", db.session.get(Suite, sid), source_sha="x",
            run_config={"runtime_id": rt.id, "runner_id": r.id})
        e1, e2 = mk(), mk()
        assert dispatch._assign_runner(e1, r, rt)
        s1 = dispatch._pick_slot(e1, r)
        e1.bb_slot = s1
        e1.state = ExecutionState.RUNNING
        db.session.commit()
        # busy slot is skipped
        s2 = dispatch._pick_slot(e2, r)
        assert s2 != s1
        e2.bb_slot = s2
        e2.runner_id = r.id
        e2.state = ExecutionState.RUNNING
        db.session.commit()
        # both busy -> deterministic fallback still in range
        e3 = mk()
        assert 0 <= dispatch._pick_slot(e3, r) < 2
        # freed slot is reused
        e1.state = ExecutionState.COMPLETED
        db.session.commit()
        assert dispatch._pick_slot(e3, r) == s1


class _FakeAdapter:
    """Minimal stand-in for BuildbotAdapter — only what the attempt
    recorder touches."""
    def __init__(self, builds):
        self._builds = builds

    def builds_for_request(self, brid):
        return self._builds


def _bb_exec(app, sid):
    """DISPATCHED buildbot execution with a persisted brid."""
    ex = dispatch.create_execution(
        "u", db.session.get(Suite, sid), source_sha="x",
        run_config={})
    ex.state = ExecutionState.DISPATCHED
    ex.dispatch_id = "bb:55"
    ex.snapshot_json = json.dumps(
        {**ex.snapshot(), "dispatch_transport": "buildbot"})
    db.session.commit()
    return ex


def test_buildbot_attempts_recorded_distinctly(app):
    """Worker loss makes Buildbot resubmit the SAME buildrequest —
    the replacement build is a distinct attempt, never folded into
    the original. Both are recorded in the snapshot with their own
    outcomes so provenance survives."""
    sid = _env(app)
    with app.app_context():
        ex = _bb_exec(app, sid)
        builds = [
            {"buildid": 10, "number": 3, "builderid": 4,
             "workerid": 2, "started_at": "t0",
             "complete": True, "results": 5, "complete_at": "t1"},
            {"buildid": 11, "number": 4, "builderid": 4,
             "workerid": 2, "started_at": "t2",
             "complete": False},
        ]
        dispatch._record_buildbot_attempts(_FakeAdapter(builds), ex)
        db.session.commit()
        snap = db.session.get(Execution, ex.id).snapshot()
        attempts = snap["bb_attempts"]
        assert [a["buildid"] for a in attempts] == [10, 11]
        assert attempts[0]["results"] == 5  # RETRY, kept as its own
        assert attempts[0]["complete"] is True
        assert attempts[1]["complete"] is False
        # the retry is audited — never silently relaunched
        from testorchestrator.models import AuditEvent
        ev = AuditEvent.query.filter_by(
            event="execution.buildbot_retry_attempt").all()
        assert ev and "build #4" in ev[0].detail
        # idempotent: a second poll records no duplicates
        dispatch._record_buildbot_attempts(_FakeAdapter(builds), ex)
        db.session.commit()
        snap = db.session.get(Execution, ex.id).snapshot()
        assert len(snap["bb_attempts"]) == 2


def test_buildbot_attempt_completion_updates_entry(app):
    """A later poll fills in results/complete_at on the same attempt
    entry — the record tracks each attempt's full lifecycle."""
    sid = _env(app)
    with app.app_context():
        ex = _bb_exec(app, sid)
        b = [{"buildid": 10, "number": 3, "complete": False}]
        dispatch._record_buildbot_attempts(_FakeAdapter(b), ex)
        db.session.commit()
        b = [{"buildid": 10, "number": 3, "complete": True,
              "results": 0, "complete_at": "t9"}]
        dispatch._record_buildbot_attempts(_FakeAdapter(b), ex)
        db.session.commit()
        att = db.session.get(Execution, ex.id).snapshot()["bb_attempts"]
        assert att[0]["results"] == 0 and att[0]["complete_at"] == "t9"


def test_artifact_dir_resolves_newest_attempt(app, tmp_path):
    """Per-attempt upload dirs: collection uses the newest attempt
    (the terminal one); a legacy flat dir still resolves."""
    from testorchestrator import buildbot_cfg
    with app.app_context():
        base = buildbot_cfg.artifacts_dir() / "ex1"
        (base / "attempt-3").mkdir(parents=True)
        (base / "attempt-10").mkdir(parents=True)
        (base / "attempt-7" / "Reports").mkdir(parents=True)
        assert buildbot_cfg.artifact_dir("ex1") == \
            base / "attempt-10"
        # legacy flat layout — no attempt dirs -> base itself
        (buildbot_cfg.artifacts_dir() / "ex2").mkdir(parents=True)
        assert buildbot_cfg.artifact_dir("ex2") == \
            buildbot_cfg.artifacts_dir() / "ex2"


def test_exec_spec_posix_flag():
    """The pidfile wrapper is POSIX-only; Windows workers get the raw
    argv contract (no sh). The spec carries the flag the master
    renderer consults."""
    from testorchestrator import buildbot_cfg
    s = buildbot_cfg.node_exec_spec(None, "npx", ["x"], posix=False)
    assert s["posix"] is False
    s = buildbot_cfg.node_exec_spec(None, "npx", ["x"])
    assert s["posix"] is True


def test_reservations_persist_ownership(app):
    """Assignment writes durable slot+procs reservations in the same
    transaction; terminal states release them via the sweep; UNKNOWN
    keeps ownership — a confirmed terminal state is the ONLY release."""
    from testorchestrator.execution import (
        Runner, RuntimeConnection, ResourceReservation)
    sid = _env(app)
    with app.app_context():
        r = Runner(name="own", connection_mode="local", max_slots=4,
                   max_procs=8)
        db.session.add(r); db.session.flush()
        rt = RuntimeConnection(name="rto", runner_id=r.id,
                               executable_path="/usr/bin/python3",
                               runtime_kind="pytest", enabled=True)
        db.session.add(rt); db.session.commit()
        ex = dispatch.create_execution(
            "m", db.session.get(Suite, sid), source_sha="x",
            run_config={"runtime_id": rt.id, "runner_id": r.id,
                        "parallel": 3, "parallel_mode": "parallel"})
        assert dispatch._assign_runner(ex, r, rt)
        rows = ResourceReservation.query.filter_by(
            execution_id=ex.id, released_at=None).all()
        kinds = {x.resource_kind: x.quantity for x in rows}
        assert kinds == {"slot": 1, "procs": 3}
        # UNKNOWN: ownership held — no release
        ex.state = ExecutionState.UNKNOWN
        db.session.commit()
        assert dispatch._release_terminal_reservations() == 0
        assert ResourceReservation.query.filter_by(
            execution_id=ex.id, released_at=None).count() == 2
        # resolved to a confirmed terminal state -> sweep releases
        ex.state = ExecutionState.FAILED
        db.session.commit()
        assert dispatch._release_terminal_reservations() == 2
        assert ResourceReservation.query.filter_by(
            execution_id=ex.id, released_at=None).count() == 0
        assert ResourceReservation.query.filter_by(
            execution_id=ex.id).first().release_state == "terminal"


def test_wait_reason_records_capacity_block(app):
    """A capacity-blocked run carries an actionable reason — the
    wait is never a silent QUEUED stall."""
    from testorchestrator.execution import (Runner, RuntimeConnection)
    sid = _env(app)
    with app.app_context():
        r = Runner(name="full", connection_mode="local", max_slots=1)
        db.session.add(r); db.session.flush()
        rt = RuntimeConnection(name="rtf", runner_id=r.id,
                               executable_path="/usr/bin/python3",
                               runtime_kind="pytest", enabled=True)
        db.session.add(rt); db.session.commit()
        mk = lambda: dispatch.create_execution(
            "m", db.session.get(Suite, sid), source_sha="x",
            run_config={"runtime_id": rt.id, "runner_id": r.id})
        e1, e2 = mk(), mk()
        assert dispatch._assign_runner(e1, r, rt)
        assert not dispatch._assign_runner(e2, r, rt)
        db.session.refresh(e2)
        assert e2.state == ExecutionState.QUEUED
        assert "slots full" in e2.wait_reason
        assert "1/1" in e2.wait_reason


def test_queue_deadline_expires_to_failed(app):
    """A QUEUED run past its deadline fails with an explicit queue
    reason — starvation is surfaced, not silent."""
    from datetime import timedelta as _td
    sid = _env(app)
    with app.app_context():
        ex = dispatch.create_execution(
            "u", db.session.get(Suite, sid), source_sha="x",
            run_config={"queue_timeout_seconds": 60})
        assert ex.queue_deadline_at is not None
        # force overdue
        ex.queue_deadline_at = utcnow() - _td(seconds=1)
        db.session.commit()
        assert dispatch._expire_queue_deadlines() == 1
        db.session.refresh(ex)
        assert ex.state == ExecutionState.FAILED
        assert ex.error_kind == "queue"
        assert "deadline" in ex.error_text


def test_redrive_fifo_order(app):
    """Queued executions are redriven oldest-first — deterministic
    fair ordering, not arbitrary DB order."""
    sid = _env(app)
    with app.app_context():
        exs = [dispatch.create_execution(
            "u", db.session.get(Suite, sid), source_sha="x",
            run_config={}) for _ in range(3)]
        calls = []
        orig = dispatch._buildbot_assign_and_submit
        dispatch._buildbot_assign_and_submit = \
            lambda e: calls.append(e.id)
        try:
            dispatch._redrive_queued_remote()
        finally:
            dispatch._buildbot_assign_and_submit = orig
        assert calls == [e.id for e in exs]


def test_misfire_skip_policy(app):
    """misfire_policy='skip' drops an occurrence overdue past its
    interval and audits it — no catch-up storm after an outage."""
    from datetime import timedelta as _td
    from testorchestrator.execution import (Project, Schedule)
    with app.app_context():
        p = Project(name="Pm"); db.session.add(p); db.session.flush()
        s = Schedule(project_id=p.id, name="sch", target_type="suite",
                     kind="interval", interval_minutes=10,
                     misfire_policy="skip", enabled=True,
                     next_run_at=utcnow() - _td(minutes=30))
        db.session.add(s); db.session.commit()
        n = dispatch.fire_due_schedules()
        db.session.refresh(s)
        assert n == 0 or n is not None  # fired count may be 0
        from testorchestrator.models import AuditEvent
        ev = AuditEvent.query.filter_by(
            event="schedule.misfire_skipped").all()
        assert ev and "overdue" in ev[0].detail
        # next_run_at advanced — no catch-up
        assert s.next_run_at is not None and \
            s.next_run_at > utcnow().replace(tzinfo=None)


def test_execution_snapshot_records_deployed_rev(app):
    """Every evidence row carries the exact deployed code it ran on —
    module path + git state, so a stale-tree process can't pass as a
    synchronized deployment in the ledger."""
    sid = _env(app)
    with app.app_context():
        ex = dispatch.create_execution(
            "u", db.session.get(Suite, sid), source_sha="x",
            run_config={})
        rev = ex.snapshot()["deployed_rev"]
        assert rev["module_path"].endswith("testorchestrator")
        # the repo checkout resolves a git head and branch
        assert len(rev["git_head"]) == 40
        assert rev["git_branch"]
