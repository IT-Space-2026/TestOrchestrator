"""End-to-end pipeline tests (handoff §4) using a fixture executable.

The fixture mimics a KRE-like CLI (accepts -reportFolder=, writes JUnit +
evidence). It proves workspace isolation, argument-array invocation, JUnit
normalization, evidence collection, cancellation, and durable state — it
does NOT prove real KRE CLI compatibility (A14).
"""

import json
import os
import stat
import time
from pathlib import Path

import pytest

from testorchestrator import dispatch
from testorchestrator.execution import (Execution, ExecutionState, GitBinding,
                                        Outcome, Project, Runner,
                                        RuntimeConnection, Suite, SuiteEntry,
                                        SyncState, TestResult, Artifact)
from testorchestrator.extensions import db


FAKE_KRE = """#!/usr/bin/env bash
# Fixture mimicking a KRE-like console invocation.
report=""
project=""
for arg in "$@"; do
  case "$arg" in
    -reportFolder=*) report="${arg#-reportFolder=}";;
    -projectPath=*) project="${arg#-projectPath=}";;
    -version) echo "9.9.9"; exit 0;;
  esac
done
if [ -z "$report" ]; then echo "missing reportFolder" >&2; exit 3; fi
if [ ! -d "$project" ]; then echo "cannot find project" >&2; exit 4; fi
mkdir -p "$report/Reports/Suite1/20260101_000000"
cat > "$report/Reports/Suite1/20260101_000000/JUnit_Report.xml" <<'XML'
<?xml version="1.0"?>
<testsuite name="Suite1" tests="3" failures="1" errors="0" skipped="0">
  <testcase classname="Cases.Login" name="valid login" time="1.5"/>
  <testcase classname="Cases.Login" name="invalid login" time="0.8">
    <failure message="assert failed">expected dashboard</failure>
  </testcase>
  <testcase classname="Cases.Logout" name="logout" time="0.2" skipped="true">
    <skipped message="not ready"/>
  </testcase>
</testsuite>
XML
printf 'PNGDATA' > "$report/Reports/Suite1/20260101_000000/valid-login.png"
echo "execution finished normally"
exit 0
"""


@pytest.fixture()
def fake_kre(tmp_path):
    exe = tmp_path / "fake-kre"
    exe.write_text(FAKE_KRE)
    exe.chmod(0o755)
    return str(exe)


@pytest.fixture()
def pipeline_env(app, tmp_path, fake_kre):
    """Project + binding(source dir) + suite + runner + runtime wiring."""
    source = tmp_path / "source-repo"
    source.mkdir()
    (source / "Test Cases").mkdir()
    (source / "Test Cases" / "login.tc").write_text("<tc/>")

    with app.app_context():
        project = Project(name="Demo")
        db.session.add(project)
        db.session.flush()
        binding = GitBinding(
            project_id=project.id, name="main-src", url=str(source),
            ref="main", worktree_path=str(source),
            last_synced_sha="abc123def456", status="synced")
        db.session.add(binding)
        suite = Suite(project_id=project.id, name="Smoke", version=1)
        db.session.add(suite)
        db.session.flush()
        db.session.add(SuiteEntry(
            suite_id=suite.id, order=0, case_ref="Test Cases/login.tc"))
        db.session.add(Runner(
            name="local-1", connection_mode="local",
            workspace_root=str(tmp_path / "work"), max_slots=2))
        runner = Runner.query.filter_by(name="local-1").first()
        db.session.add(RuntimeConnection(
            name="kre-1", runner_id=runner.id,
            executable_path=fake_kre,
            workspace_root=str(tmp_path / "krework")))
        db.session.commit()
        yield {
            "project": project, "suite": suite, "binding": binding,
            "runner": runner,
        }


def _run_worker(app, times=3):
    with app.app_context():
        for _ in range(times):
            dispatch.worker_once()


def test_full_pipeline_pass_and_fail_collected(app, pipeline_env):
    """A failed assertion still reaches collection; outcome reflects
    evidence, not wrapper exit status (§4, §7)."""
    env = pipeline_env
    with app.app_context():
        ex = dispatch.create_execution(
            "admin", env["suite"], binding=env["binding"],
            source_sha="abc123def456")
        ex_id = ex.id
        assert ex.state == ExecutionState.QUEUED

    _run_worker(app)

    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert ex.state == ExecutionState.COMPLETED
        # 1 failed case of 3 → outcome failed, but collection ran.
        assert ex.outcome == Outcome.FAILED
        results = TestResult.query.filter_by(execution_id=ex_id).all()
        assert len(results) == 3
        statuses = sorted(r.status.value for r in results)
        assert statuses == ["failed", "passed", "skipped"]
        # Evidence: JUnit + PNG screenshot + invocation/stdout/stderr logs.
        artifacts = Artifact.query.filter_by(execution_id=ex_id).all()
        kinds = sorted(a.kind for a in artifacts)
        assert "junit" in kinds and "screenshot" in kinds
        assert kinds.count("log") == 3
        # stderr may legitimately be empty — integrity is the checksum.
        assert all(a.sha256 and a.size is not None for a in artifacts)
        junit = next(a for a in artifacts if a.kind == "junit")
        assert junit.size > 0
        # Independent state fields: no external endpoints configured.
        assert ex.git_state == SyncState.NOT_CONFIGURED
        # Immutable snapshot recorded the source SHA.
        assert ex.snapshot()["source_sha"] == "abc123def456"


def test_cancel_kills_process_tree(app, pipeline_env, tmp_path):
    """A05: cancel targets the process tree; state reflects acknowledgment."""
    slow_kre = tmp_path / "slow-kre"
    slow_kre.write_text(
        "#!/usr/bin/env bash\n"
        "report=\"\"\n"
        "for arg in \"$@\"; do case \"$arg\" in -reportFolder=*) report=\"${arg#-reportFolder=}\";; esac; done\n"
        "sleep 60 &\n"
        "sleep 60 &\n"  # children — tree must die, not just the shell
        "sleep 60\n")
    slow_kre.chmod(0o755)

    with app.app_context():
        from testorchestrator.execution import RuntimeConnection
        rt = RuntimeConnection.query.first()
        rt.executable_path = str(slow_kre)
        db.session.commit()
        ex = dispatch.create_execution("admin", pipeline_env["suite"])
        ex_id = ex.id

    # Run the execution in a thread; cancel mid-flight.
    def worker():
        with app.app_context():
            dispatch.worker_once()

    t = threading.Thread(target=worker)
    t.start()
    deadline = time.time() + 10
    with app.app_context():
        while time.time() < deadline:
            state = db.session.execute(
                db.text("SELECT state FROM executions WHERE id=:i"),
                {"i": ex_id}).scalar()
            if state == "RUNNING":
                break
            time.sleep(0.1)
        assert dispatch.request_cancel(ex_id, "admin")
    t.join(timeout=30)

    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert ex.state == ExecutionState.CANCELED
        assert ex.outcome == Outcome.INCOMPLETE
        assert ex.exit_status is not None  # termination acknowledged


import subprocess
import threading


def test_run_config_profile_browser_reach_invocation(app, pipeline_env):
    """Operator-selected profile/browser land in the recorded argv
    (kre-invocation.log), not silently defaulted."""
    with app.app_context():
        ex = dispatch.create_execution(
            "admin", pipeline_env["suite"], binding=pipeline_env["binding"],
            source_sha="abc123def456",
            run_config={"profile": "smoke-profile", "browser": "Firefox"})
        ex_id = ex.id
        assert ex.snapshot()["run_config"] == {
            "profile": "smoke-profile", "browser": "Firefox",
            "remote_webdriver_url": "", "remote_webdriver_type": "",
            "timeout_seconds": 0,
            "kre_options": {}, "kre_options_allow_drop": False}
    _run_worker(app)
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert ex.state == ExecutionState.COMPLETED
        inv = Path(ex.workspace_path) / "logs" / "kre-invocation.log"
        argv = inv.read_text()
        assert "-executionProfile=smoke-profile" in argv
        assert "-browserType=Firefox" in argv
        # The invocation record itself is collected as checksummed evidence.
        kinds = [a.rel_path for a in ex.artifacts]
        assert "logs/kre-invocation.log" in kinds


def test_missing_runtime_is_actionable_error(app, pipeline_env):
    """Unregistered/missing runtime → actionable prerequisite error, never
    a fabricated success (§5)."""
    with app.app_context():
        from testorchestrator.execution import RuntimeConnection
        rt = RuntimeConnection.query.first()
        rt.executable_path = "/nonexistent/kre"
        db.session.commit()
        ex = dispatch.create_execution("admin", pipeline_env["suite"])
        ex_id = ex.id
    _run_worker(app)
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert ex.state == ExecutionState.FAILED
        assert ex.error_kind == "infrastructure"
        assert "nonexistent" in ex.error_text or "KRE" in ex.error_text


def test_lease_expiry_reconciles_unknown_not_rerun(app, pipeline_env):
    """A06/A05: a stale lease marks the execution UNKNOWN — the ambiguous
    run is reconciled, never blindly duplicated (§7)."""
    from datetime import timedelta
    from testorchestrator.execution import utcnow
    with app.app_context():
        ex = dispatch.create_execution("admin", pipeline_env["suite"])
        ex.state = ExecutionState.RUNNING
        ex.lease_expires_at = utcnow() - timedelta(seconds=10)
        db.session.commit()
        assert dispatch.reconcile_stale() == 1
        assert db.session.get(Execution, ex.id).state == ExecutionState.UNKNOWN


def test_runner_capacity_and_drain(app, pipeline_env):
    """§6: capacity enforced; draining runners take no new work."""
    with app.app_context():
        runner = pipeline_env["runner"]
        ex = dispatch.create_execution("admin", pipeline_env["suite"])
        # Occupy all slots.
        running = dispatch.create_execution("admin", pipeline_env["suite"])
        running.state = ExecutionState.RUNNING
        running.runner_id = runner.id
        running2 = dispatch.create_execution("admin", pipeline_env["suite"])
        running2.state = ExecutionState.RUNNING
        running2.runner_id = runner.id
        db.session.commit()
        assert dispatch.pick_runner(ex) is None  # max_slots=2 occupied
        # Drain blocks new assignment even with free slots.
        runner.max_slots = 5
        runner.draining = True
        db.session.commit()
        assert dispatch.pick_runner(ex) is None


def test_capacity_limit_licence_aware(app):
    """Floating KRE licences cap dispatch independently of hardware:
    min(max_slots, license_slots); unset licence = unconstrained."""
    from testorchestrator.dispatch import capacity_limit
    from testorchestrator.execution import Runner
    with app.app_context():
        r = Runner(name="r-cap", max_slots=4, license_slots=None)
        assert capacity_limit(r) == 4
        r.license_slots = 2
        assert capacity_limit(r) == 2      # licence seats limit
        r.license_slots = 8
        assert capacity_limit(r) == 4      # hardware limits
        r.license_slots = 0
        assert capacity_limit(r) == 4      # 0 = declared-unconstrained


def test_plan_outcome_aggregation():
    """Every required entry must reach an acceptable terminal state —
    incomplete work can never hide behind 'passed'."""
    from testorchestrator.results import plan_outcome as po
    assert po(["passed", "passed"]) == "passed"
    assert po(["passed", "not-started"]) == "not-started"
    assert po(["passed", "unknown"]) == "unknown"
    assert po(["passed", "cancelled"]) == "cancelled"
    assert po(["passed", "skipped"]) == "skipped"
    assert po(["skipped", "skipped"]) == "skipped"   # all skipped
    assert po([]) == "empty"                          # empty plan
    assert po(["passed", "failed", "cancelled"]) == "failed"
    assert po(["unknown", "error"]) == "error"
    assert po(["bogus"]) == "unknown"                 # unmapped state


def test_plan_summary_blocks_incomplete_entries(app):
    """plan_summary wired into project detail: a plan can only read
    'passed' when every required entry reached an acceptable terminal
    outcome — pending/unknown/cancelled entries block success."""
    from testorchestrator.results import plan_summary
    from testorchestrator.execution import (Plan, PlanEntry, Project,
                                            Suite, Execution,
                                            ExecutionState, Outcome)
    import json
    with app.app_context():
        p = Project(name="P", description="")
        db.session.add(p); db.session.flush()
        s1 = Suite(project_id=p.id, name="S1", version=1,
                   content_hash="h1", imported=False)
        s2 = Suite(project_id=p.id, name="S2", version=1,
                   content_hash="h2", imported=False)
        plan = Plan(project_id=p.id, name="PL", parallelism=1)
        db.session.add_all([s1, s2, plan]); db.session.flush()
        plan.entries = [PlanEntry(plan_id=plan.id, order=0,
                                  suite_id=s1.id),
                        PlanEntry(plan_id=plan.id, order=1,
                                  suite_id=s2.id)]
        db.session.flush()
        # never dispatched -> entries skipped, plan not "passed"
        assert plan_summary(plan) == "skipped"
        # one passed, one still queued -> not-started wins
        def mkex(suite, state, outcome):
            return Execution(
                project_id=p.id, suite_id=suite.id,
                snapshot_json=json.dumps({"plan_id": plan.id,
                                          "suite_id": suite.id}),
                state=state, outcome=outcome)
        db.session.add(mkex(s1, ExecutionState.COMPLETED, Outcome.PASSED))
        db.session.add(mkex(s2, ExecutionState.QUEUED, Outcome.NONE))
        db.session.commit()
        assert plan_summary(plan) == "not-started"
        # finish s2 passed -> now genuinely passed
        s2ex = [e for e in Execution.query.all()
                if e.snapshot().get("suite_id") == s2.id][0]
        s2ex.state = ExecutionState.COMPLETED
        s2ex.outcome = Outcome.PASSED
        db.session.commit()
        assert plan_summary(plan) == "passed"
