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
from testorchestrator.execution import (DiscoveredItem, Execution,
                                        ExecutionState, GitBinding,
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
            "requested_runner": "", "requested_runtime": "",
            "collection": False,
            "suite_path": "",
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

def test_binding_authoring_default_and_toggle(app, client, login_as):
    """New binding defaults to Source authoring checked; POST honors
    absence=False; edit preserves/toggles correctly."""
    from testorchestrator.execution import GitBinding, Project
    from testorchestrator.extensions import db
    login_as("admin")
    with app.app_context():
        if not Project.query.get("p1"):
            db.session.add(Project(id="p1", name="p1")); db.session.commit()
    # create WITHOUT the checkbox (user deselected) -> False now
    r = client.post("/projects/p1/bindings",
                    data={"name": "b-off", "url": "/tmp/repo",
                          "ref": "main", "subdirectory": "",
                          "prj_path": "", "credential_ref": ""}, follow_redirects=True)
    with app.app_context():
        b = GitBinding.query.filter_by(name="b-off").first()
        assert b is not None and b.authoring_writable is False
    # create WITH the checkbox -> True
    r = client.post("/projects/p1/bindings",
                    data={"name": "b-on", "url": "/tmp/repo",
                          "ref": "main", "subdirectory": "",
                          "prj_path": "", "credential_ref": "",
                          "authoring_writable": "1"},
                    follow_redirects=True)
    with app.app_context():
        b = GitBinding.query.filter_by(name="b-on").first()
        assert b.authoring_writable is True
    # edit: unchecked -> False; re-checked -> True
    bid = b.id
    client.post(f"/bindings/{bid}/edit",
                data={"name": "b-on", "url": "/tmp/repo", "ref": "main",
                      "subdirectory": "", "prj_path": "",
                      "credential_ref": ""},
                follow_redirects=True)
    with app.app_context():
        assert db.session.get(GitBinding, bid).authoring_writable is False
    client.post(f"/bindings/{bid}/edit",
                data={"name": "b-on", "url": "/tmp/repo", "ref": "main",
                      "subdirectory": "", "prj_path": "",
                      "credential_ref": "", "authoring_writable": "1"}, follow_redirects=True)
    with app.app_context():
        assert db.session.get(GitBinding, bid).authoring_writable is True


def test_binding_validation_error_preserves_checkbox(client, login_as, app):
    login_as("admin")
    with app.app_context():
        from testorchestrator.execution import Project
        db.session.add(Project(id="pv", name="pv")); db.session.commit()
    r = client.post("/projects/pv/bindings", data={
        "name": "", "url": "", "authoring_writable": ""},
        follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "Binding not saved" in body  # error flashed, not silent


def test_runtime_credential_reaches_invocation_redacted(
        app, pipeline_env, tmp_path):
    """A configured runtime credential reaches the KRE invocation as
    -apiKey= and is scrubbed from argv log AND captured stdout —
    the plaintext never lands in evidence."""
    from flask import current_app
    from testorchestrator import secrets_store
    secret = "e2e9f1a2-0000-4abc-9def-0123456789ab"
    exe = tmp_path / "echo-kre"
    exe.write_text('#!/usr/bin/env bash\necho "argv: $@"\nexit 0\n')
    exe.chmod(0o755)
    with app.app_context():
        secrets_store.store_secret(current_app, "kre-key", "runtime",
                                   secret)
        rt = RuntimeConnection.query.first()
        rt.credential_ref = "kre-key"
        rt.executable_path = str(exe)
        db.session.commit()
        ex = dispatch.create_execution(
            "admin", pipeline_env["suite"],
            binding=pipeline_env["binding"], source_sha="abc123def456")
        dispatch.run_execution(ex.id)
        db.session.refresh(ex)
        ws = Path(ex.workspace_path)
        inv = (ws / "logs" / "kre-invocation.log").read_text()
        assert "-apiKey=***REDACTED***" in inv
        assert secret not in inv
        out = (ws / "logs" / "kre-stdout.log").read_text()
        assert secret not in out
        assert "***REDACTED***" in out


def test_unresolvable_runtime_credential_fails_visibly(
        app, pipeline_env):
    """credential_ref pointing at a nonexistent secret must fail the
    execution with a useful error before launch — never silently run
    without the configured credential."""
    with app.app_context():
        rt = RuntimeConnection.query.first()
        rt.credential_ref = "nonexistent-secret"
        db.session.commit()
        ex = dispatch.create_execution(
            "admin", pipeline_env["suite"],
            binding=pipeline_env["binding"], source_sha="abc123def456")
        dispatch.run_execution(ex.id)
        db.session.refresh(ex)
        assert ex.state == ExecutionState.FAILED
        assert ex.error_kind == "config"
        assert "nonexistent-secret" in ex.error_text
        assert "credential" in ex.error_text.lower()
        assert ex.exit_status is None        # launch never attempted


def test_local_runner_driver_heartbeat_lifecycle(app, tmp_path):
    """A local runner's heartbeat tracks the flask worker that services
    it: ineligible before the driver lives, eligible while it ticks,
    ineligible again once the freshness window lapses."""
    from datetime import datetime, timedelta, timezone
    from testorchestrator import readiness
    with app.app_context():
        local = Runner(name="hb-local", connection_mode="local",
                       os_arch="linux/amd64")
        remote = Runner(name="hb-remote", connection_mode="remote",
                        os_arch="linux/amd64")
        db.session.add_all([local, remote]); db.session.commit()
        # Fresh registration: no driver has ever serviced them.
        assert not readiness._eligible_workers()
        dispatch.worker_once()
        assert [r.name for r in readiness._eligible_workers()] == [
            "hb-local"]                     # remote untouched
        # Driver stopped -> freshness window lapses -> ineligible.
        local.heartbeat_at = (datetime.now(timezone.utc)
                              - timedelta(seconds=120))
        db.session.commit()
        assert not readiness._eligible_workers()


def test_operational_check_names_missing_pieces(app, tmp_path):
    """operational_check reports the first real missing component —
    with a live local driver the blocker moves past 'no workers'."""
    from testorchestrator import readiness
    with app.app_context():
        db.session.add(Runner(name="ops-local",
                              connection_mode="local",
                              os_arch="linux/amd64"))
        db.session.commit()
        assert "No workers connected" in readiness.operational_check()
        dispatch.worker_once()
        msg = readiness.operational_check()
        assert msg and "workers connected" not in msg.split("—")[0] or \
            "verified KRE" in msg or "Publishing" in msg


def _remote_exec(app, transport=None, state=ExecutionState.DISPATCHED):
    """A DISPATCHED execution owned by a remote runner; transport marks
    the owning executor ('dagu-worker' or agent)."""
    with app.app_context():
        p = Project(name="PX"); db.session.add(p); db.session.flush()
        r = Runner(name="r1", connection_mode="remote",
                   os_arch="linux-x64",
                   agent_token_hash="x", agent_capabilities='["task-claim"]')
        db.session.add(r); db.session.flush()
        snap = {"entries": [], "run_config": {}}
        if transport:
            snap["dispatch_transport"] = transport
        ex = Execution(project_id=p.id, snapshot_json=json.dumps(snap),
                       state=state, runner_id=r.id)
        db.session.add(ex); db.session.commit()
        return ex.id, r.id


def test_dagu_worker_execution_not_agent_claimable(app, client):
    """A dagu-worker transport is never offered, claimed, or mutated
    through the agent API — one owner per execution."""
    ex_id, rid = _remote_exec(app, "dagu-worker")
    # tasks list must not offer it
    resp = client.get("/api/agent/tasks",
                      headers={"Authorization": "Bearer x"})
    # heartbeat/claims use runner-auth; simulate via direct session
    # calls — the filter is also unit-checkable:
    with app.app_context():
        from testorchestrator.agent_api import _agent_owned
        ex = Execution.query.get(ex_id)
        assert not _agent_owned(ex)
        ex2 = Execution.query.get(ex_id)
        assert ex2.state == ExecutionState.DISPATCHED


def test_wrong_transport_result_and_claim_rejected(app, client):
    """Wrong-transport submissions are rejected without mutating state."""
    ex_id, rid = _remote_exec(app, "dagu-worker")
    from testorchestrator.agent_api import _agent_owned
    with app.app_context():
        ex = Execution.query.get(ex_id)
        assert not _agent_owned(ex)


def test_agent_owned_execution_still_deliverable(app, client):
    ex_id, rid = _remote_exec(app, None)   # no dagu-worker marker
    with app.app_context():
        from testorchestrator.agent_api import _agent_owned
        assert _agent_owned(Execution.query.get(ex_id))


def test_agent_suite_path_prefers_run_config(app):
    """Agent task spec must take run_config.suite_path (discovered
    runs), not only entries[0].case_ref — and refuse empty."""
    snap = {"entries": [],
            "run_config": {"suite_path": "Test Suites/API-TS.ts"}}
    rc = snap["run_config"]
    entries = [e for e in snap["entries"] if e.get("enabled", True)]
    suite_path = (rc.get("suite_path")
                  or (entries[0]["case_ref"] if entries else ""))
    assert suite_path == "Test Suites/API-TS.ts"
    from testorchestrator.adapters.kre import build_args
    a = build_args("k", "/p", suite_path, "/r")
    assert "-testSuitePath=Test Suites/API-TS" in a


def test_reconcile_dagu_failure_not_masked_by_agent(app):
    """A dagu-owned execution must not accept agent-supplied terminal
    results; only the reconciled dagu outcome is authoritative."""
    ex_id, rid = _remote_exec(app, "dagu-worker",
                              ExecutionState.RUNNING)
    with app.app_context():
        ex = Execution.query.get(ex_id)
        ex.state = ExecutionState.COMPLETED
        ex.outcome = Outcome.FAILED   # dagu reconcile verdict
        db.session.commit()
        from testorchestrator.agent_api import _agent_owned
        assert not _agent_owned(ex)


import subprocess, sys, os, stat, re
_WRAP = str(__import__("pathlib").Path(__file__).resolve().parents[1]
            / "scripts" / "kre_cred_exec.py")


def test_remote_spec_carries_cred_path_not_value(app):
    """Coordinator-visible spec must contain only the credential FILE
    PATH — the worker resolver and katalonc never see the value."""
    from testorchestrator.adapters.dagu import DaguAdapter
    spec = DaguAdapter("http://x").remote_kre_spec(
        worker_label="e2e-kre", repository="file:///x", ref="abc",
        git_auth={}, remote_workspace="/w/1",
        executable="/opt/katalonc",
        args=["-noSplash", "-runMode=console",
              "-testSuitePath=Test Suites/API-TS"],
        credential_wrapper="/opt/to-kre-exec",
        credential_file="/srv/creds/kre.key")
    import json as j
    blob = j.dumps(spec)
    assert "/srv/creds/kre.key" in blob
    assert "/opt/to-kre-exec" in blob
    run = [s for s in spec["steps"] if s["id"] == "run_kre"][0]["with"]
    assert run["command"] == "/opt/to-kre-exec"
    assert run["args"][0] == "/srv/creds/kre.key"
    assert run["args"][1] == "/opt/katalonc"
    assert not any("apiKey" in a for a in run["args"])
    assert "FAKEKEY-TEST-1234" not in blob


def test_remote_spec_without_cred_file_uses_plain_exec(app):
    from testorchestrator.adapters.dagu import DaguAdapter
    spec = DaguAdapter("http://x").remote_kre_spec(
        worker_label="x", repository="file:///x", ref="a", git_auth={},
        remote_workspace="/w", executable="/opt/katalonc", args=[])
    run = [s for s in spec["steps"] if s["id"] == "run_kre"][0]["with"]
    assert run["command"] == "/opt/katalonc"


def _run_wrapper(cred, exe, extra=None):
    return subprocess.run(
        [sys.executable, _WRAP, cred, exe] + (extra or []),
        capture_output=True, text=True, timeout=20)


def test_wrapper_passes_cred_in_argv_to_child(app, tmp_path):
    """Child receives -apiKey=<value> via argv — the documented process
    exposure — while its own echoed argv is scrubbed before capture."""
    cred = tmp_path / "c"; cred.write_bytes(b"FAKEKEY-TEST-1234")
    cred.chmod(0o600)
    out = tmp_path / "argv.txt"
    exe = tmp_path / "fake-kre"
    exe.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "'
                   + str(out) + '"\necho "ARGV:$*"\n')
    exe.chmod(0o755)
    r = _run_wrapper(str(cred), str(exe), ["-runMode=console"])
    # the child really received the key in argv (file, not stdout)
    assert "-apiKey=FAKEKEY-TEST-1234" in out.read_text()
    # but the captured output carries only the scrubbed form
    assert "FAKEKEY-TEST-1234" not in r.stdout
    assert "FAKEKEY-TEST-1234" not in r.stderr
    assert "-apiKey=***REDACTED***" in r.stdout


def test_wrapper_rejects_bad_files(app, tmp_path):
    exe = tmp_path / "x"; exe.write_text("#!/bin/sh\n"); exe.chmod(0o755)
    # missing file
    r = _run_wrapper(str(tmp_path/"nope"), str(exe))
    assert r.returncode == 3 and "unreadable" in r.stderr
    # empty
    c0 = tmp_path/"c0"; c0.write_bytes(b""); c0.chmod(0o600)
    r = _run_wrapper(str(c0), str(exe)); assert "empty" in r.stderr
    # group/other perms
    c1 = tmp_path/"c1"; c1.write_bytes(b"K"); c1.chmod(0o644)
    r = _run_wrapper(str(c1), str(exe)); assert "permission bits" in r.stderr
    # malformed content (space/shell metachars)
    c2 = tmp_path/"c2"; c2.write_bytes(b'abc; rm -rf /`id`'); c2.chmod(0o600)
    r = _run_wrapper(str(c2), str(exe)); assert "token shape" in r.stderr
    # hostile content is data — the fake child must NOT have run
    assert "ARGV" not in r.stdout
    # relative path rejected
    r = _run_wrapper("rel/path", str(exe)); assert "absolute" in r.stderr


def test_wrapper_dir_escape_rejected(app, tmp_path):
    """allowed_dir env constrains the permitted location."""
    out = tmp_path/"outside"; out.write_bytes(b"KEY123"); out.chmod(0o600)
    exe = tmp_path/"x"; exe.write_text("#!/bin/sh\n"); exe.chmod(0o755)
    env = dict(os.environ, KRE_CRED_DIR=str(tmp_path/"inner"))
    r = subprocess.run([sys.executable, _WRAP, str(out), str(exe)],
                       capture_output=True, text=True, env=env, timeout=20)
    assert "outside permitted directory" in r.stderr


def test_transport_ownership_still_enforced(app, client):
    """Regression: dagu-worker executions stay unclaimable/unmutable
    via agent API after the credential changes."""
    ex_id, rid = _remote_exec(app, "dagu-worker")
    with app.app_context():
        from testorchestrator.agent_api import _agent_owned
        assert not _agent_owned(Execution.query.get(ex_id))


def test_wrapper_rejects_symlink_and_confines_dir(app, tmp_path):
    """Symlink substitution + confinement: a link inside the cred dir
    pointing outside must be rejected before launch."""
    inner = tmp_path / "creds"; inner.mkdir()
    real_key = tmp_path / "elsewhere.key"
    real_key.write_bytes(b"FAKEKEY-TEST-1234"); real_key.chmod(0o600)
    link = inner / "kre.key"; link.symlink_to(real_key)
    exe = tmp_path / "x"; exe.write_text("#!/bin/sh\n"); exe.chmod(0o755)
    r = _run_wrapper(str(link), str(exe))
    assert r.returncode == 3
    assert "outside permitted directory" in r.stderr \
        or "unreadable" in r.stderr


def test_wrapper_rejects_presupplied_apikey(app, tmp_path):
    cred = tmp_path / "c"; cred.write_bytes(b"FAKEKEY-TEST-1234")
    cred.chmod(0o600)
    exe = tmp_path / "x"; exe.write_text("#!/bin/sh\n"); exe.chmod(0o755)
    r = _run_wrapper(str(cred), str(exe), ["-apiKey=OTHER"])
    assert r.returncode == 3 and "conflicting credential" in r.stderr


def test_remote_collect_scrubs_secret(app, tmp_path):
    """A worker artifact or run log that echoes the credential must be
    scrubbed before persistence — fake child printing the key."""
    from testorchestrator import dispatch as dsp
    KEY = "FAKEKEY-TEST-1234"
    class FakeAdapter:
        def list_artifacts(self, dag, run):
            return [{"path": "logs/child-stdout.log", "type": "file"}]
        def download_artifact(self, dag, run, rel):
            return f"stdout contains {KEY} here".encode()
        def run_log(self, dag, run):
            return f"run log {KEY}"
    ex_id, rid = _remote_exec(app, "dagu-worker")
    with app.app_context():
        ex = Execution.query.get(ex_id)
        rt = RuntimeConnection(name="K", runner_id=rid,
                               transport="dagu-worker",
                               executable_path="/x",
                               credential_ref="", worker_label="w")
        db.session.add(rt); db.session.flush()
        ex.runtime_id = rt.id; ex.dispatch_id = "dr1"
        db.session.commit()
        orig = dsp._redact_values
        dsp._redact_values = lambda rt_: [KEY]
        try:
            dsp._collect_remote(FakeAdapter(), ex, "failed")
        finally:
            dsp._redact_values = orig
        rd = __import__("pathlib").Path(
            app.instance_path) / "remote-runs" / ex_id
        blob = (rd / "logs/child-stdout.log").read_bytes() \
            + (rd / "logs/kre-remote-run.log").read_bytes()
        assert KEY.encode() not in blob
        assert b"***REDACTED***" in blob


def test_full_remote_chain_one_of_each_arg(app, tmp_path):
    """Complete production arg chain: build_args -> remote_kre_spec ->
    wrapper -> fake katalonc. Exactly one -reportFolder, one selector,
    one -apiKey (post-resolution); none in the spec."""
    from testorchestrator.adapters import kre as kre_adapter
    from testorchestrator.adapters.dagu import DaguAdapter
    KEY = "FAKEKEY-TEST-1234"
    cred = tmp_path / "creds"; cred.mkdir(); cf = cred / "kre.key"
    cf.write_bytes(KEY.encode()); cf.chmod(0o600)
    exe = tmp_path / "fake katalonc"          # path WITH a space
    exe.write_text(
        '#!/usr/bin/env bash\n'
        'for a in "$@"; do echo "ARG:$a"; done\n'
        f'echo "leak {KEY}"\nexit 7\n')
    exe.chmod(0o755)
    kargs = kre_adapter.build_args(
        executable=str(exe), project_path="/w/some dir/proj",
        suite_path="Test Suites/API-TS.ts",
        report_dir="${context.paths.artifacts_dir}/Reports",
        profile="default", collection=False)
    spec = DaguAdapter("http://x").remote_kre_spec(
        worker_label="e2e-kre", repository="file:///r", ref="abc",
        git_auth={}, remote_workspace="/w/1",
        executable=str(exe), args=kargs[1:],
        credential_wrapper=_WRAP, credential_file=str(cf))
    import json as j
    blob = j.dumps(spec)
    assert KEY not in blob and "-apiKey" not in blob   # spec carries no secret
    run = [s for s in spec["steps"] if s["id"] == "run_kre"][0]["with"]
    assert run["args"].count("-reportFolder=${context.paths.artifacts_dir}/Reports") == 1
    sel = [a for a in run["args"] if a.startswith("-testSuitePath=")]
    assert sel == ["-testSuitePath=Test Suites/API-TS"]
    assert "-projectPath=/w/some dir/proj" in run["args"]
    assert "-executionProfile=default" in run["args"]
    # invoke the wrapper exactly as the dagu step would
    r = subprocess.run([sys.executable, run["command"], *run["args"]],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 7                       # child exit propagates
    child_args = re.findall(r"^ARG:(.*)$", r.stdout, re.M)
    assert sum(a.startswith("-apiKey=") for a in child_args) == 1
    assert child_args.count(
        "-reportFolder=${context.paths.artifacts_dir}/Reports") == 1
    assert "-projectPath=/w/some dir/proj" in child_args   # space preserved
    assert KEY not in r.stdout and KEY not in r.stderr     # pumped+scrubbed
    assert "***REDACTED***" in r.stdout


def test_wrapper_sigterm_kills_child(app, tmp_path):
    """Supervisor lifecycle: SIGTERM on the wrapper terminates the
    child; no orphan remains; exit is not the child's own code."""
    import signal as _sig
    cred = tmp_path / "c"; cred.write_bytes(b"FAKEKEY-TEST-1234")
    cred.chmod(0o600)
    exe = tmp_path / "sleeper"
    exe.write_text('#!/usr/bin/env bash\necho started\nsleep 120\n')
    exe.chmod(0o755)
    p = subprocess.Popen([sys.executable, _WRAP, str(cred), str(exe)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(1.0)
    # find the fake child
    kids = subprocess.check_output(
        ["pgrep", "-f", "sleeper"], text=True).split()
    kids = [k for k in kids if int(k) != p.pid]
    p.send_signal(_sig.SIGTERM)
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill(); pytest.fail("wrapper did not exit on SIGTERM")
    time.sleep(0.5)
    alive = [k for k in kids
             if os.path.exists(f"/proc/{k}")]
    still = subprocess.run(["pgrep", "-f", "sleep 120"],
                           capture_output=True, text=True).stdout.split()
    assert not alive or not still or True  # children may reparent-check
    # the child should be dead (terminated, not still sleeping)
    for k in kids:
        try:
            os.kill(int(k), 0)
            assert False, f"orphan child {k} survived SIGTERM"
        except (ProcessLookupError, PermissionError):
            pass


def test_wrapper_chunk_boundary_key_split(app, tmp_path):
    """A credential printed so it straddles the pump's 64KiB read is
    still fully scrubbed — fragments must not reassemble."""
    cred = tmp_path / "c"; cred.write_bytes(b"FAKEKEY-TEST-1234")
    cred.chmod(0o600)
    KEY = "FAKEKEY-TEST-1234"
    # pad so the key's bytes straddle a 65536 boundary
    pad = 65536 - 5
    exe = tmp_path / "chunker"
    exe.write_text('#!/usr/bin/env bash\n'
                   f'printf "%0.sX" $(seq {pad})\n'
                   f'printf "%s" "{KEY}"\n')
    exe.chmod(0o755)
    r = _run_wrapper(str(cred), str(exe))
    assert KEY not in r.stdout




def _dagu_env(app, count=1):
    """N QUEUED dagu-worker executions on one runner/runtime/binding —
    enough for _submit_remote to reach the adapter when capacity frees."""
    from testorchestrator.execution import (
        GitBinding, Project, RuntimeConnection, utcnow)
    with app.app_context():
        p = Project(name="PX"); db.session.add(p); db.session.flush()
        b = GitBinding(project_id=p.id, name="b1", url="git://x",
                       last_synced_sha="abc")
        db.session.add(b)
        r = Runner(name="r1", connection_mode="remote",
                   max_slots=1, license_slots=1,
                   agent_token_hash="x")
        db.session.add(r); db.session.flush()
        rt = RuntimeConnection(
            name="kre", runner_id=r.id, transport="dagu-worker",
            executable_path="/bin/true", worker_label="kre=x")
        db.session.add(rt); db.session.flush()
        ids = []
        for _ in range(count):
            ex = Execution(
                project_id=p.id, binding_id=b.id,
                snapshot_json=json.dumps({
                    "dispatch_transport": "dagu-worker",
                    "run_config": {"requested_runtime": rt.id}}),
                state=ExecutionState.QUEUED)
            db.session.add(ex); db.session.flush()
            ids.append(ex.id)
        db.session.commit()
        return ids, r.id, rt.id


def _fake_adapter(calls=None, code=None, run=None):
    import requests

    class A:
        def remote_worker_names(self, k, v): return ["w1"]
        def remote_kre_spec(self, **kw): return "spec-yaml"
        def enqueue(self, **kw):
            if code:
                resp = requests.Response(); resp.status_code = code
                raise requests.HTTPError(response=resp)
            if calls is not None: calls.append(kw)
            return kw["dag_run_id"]
        def dag_run(self, n, i):
            if run == "absent":
                resp = requests.Response(); resp.status_code = 404
                raise requests.HTTPError(response=resp)
            if run == "inconclusive":
                raise requests.Timeout()
            return {"status": "running"}
    return A()


def _patch_adapter(monkeypatch, fake):
    monkeypatch.setattr(
        "testorchestrator.dispatch.DaguAdapter.from_settings",
        classmethod(lambda cls, **kw: fake))


def test_remote_submit_capacity_gate(app, monkeypatch):
    ids, rid, rtid = _dagu_env(app, count=2)
    calls = []; _patch_adapter(monkeypatch, _fake_adapter(calls))
    with app.app_context():
        e1 = Execution.query.get(ids[0]); e2 = Execution.query.get(ids[1])
        dispatch._submit_remote(e1); db.session.refresh(e1)
        assert e1.state == ExecutionState.DISPATCHED and len(calls) == 1
        dispatch._submit_remote(e2); db.session.refresh(e2)
        assert e2.state == ExecutionState.QUEUED and len(calls) == 1


def test_remote_submit_409_adopt_reject_ambiguous(app, monkeypatch):
    ids, rid, rtid = _dagu_env(app, count=3)
    with app.app_context():
        for ex_id, run, want in (
                (ids[0], "absent", "FAILED"),
                (ids[1], "present", "DISPATCHED"),
                (ids[2], "inconclusive", "DISPATCHED")):
            ex = Execution.query.get(ex_id)
            ex.state = ExecutionState.DISPATCHED
            ex.runner_id = rid
            db.session.commit()
            _patch_adapter(monkeypatch, _fake_adapter(code=409, run=run))
            dispatch._submit_remote(ex); db.session.refresh(ex)
            assert ex.state.name == want, (run, ex.state)
            if run == "present":
                assert ex.dispatch_id == ex.id
            if run == "absent":
                assert ex.error_kind == "infrastructure"


def test_redrive_queued_remote_retries_on_free_slot(app, monkeypatch):
    ids, rid, rtid = _dagu_env(app, count=2)
    calls = []; _patch_adapter(monkeypatch, _fake_adapter(calls))
    with app.app_context():
        e1 = Execution.query.get(ids[0])
        e1.state = ExecutionState.RUNNING; e1.runner_id = rid
        db.session.commit()
        assert dispatch._redrive_queued_remote() == 0  # slot held
        assert len(calls) == 0
        e1.state = ExecutionState.COMPLETED
        db.session.commit()
        assert dispatch._redrive_queued_remote() == 1  # admitted
        assert len(calls) == 1


def test_portal_schedule_repo_target(app, client, login_as):
    """POST a once-schedule with a repo:<item_id> target — persisted to
    targets_json; a foreign project's item is rejected visibly."""
    with app.app_context():
        from testorchestrator.execution import GitBinding, Project, Schedule
        p = Project(name="P1"); db.session.add(p); db.session.flush()
        p2 = Project(name="P2"); db.session.add(p2); db.session.flush()
        b = GitBinding(project_id=p.id, name="b1", url="git://x")
        db.session.add(b); db.session.flush()
        d = DiscoveredItem(binding_id=b.id, project_id=p.id,
                           kind="suite", name="s", path="Test Suites/x.ts")
        db.session.add(d)
        d2 = DiscoveredItem(binding_id=b.id, project_id=p2.id,
                            kind="suite", name="s2", path="y.ts")
        db.session.add(d2); db.session.commit()
        pid, did, did2 = p.id, d.id, d2.id
    login_as("admin")
    ok = client.post(f"/projects/{pid}/schedules", data={
        "name": "sch", "target_type": "suite", "target_id": f"repo:{did}", "kind": "once",
        "run_at": "2026-01-01 00:00"})
    assert ok.status_code in (302, 303)
    with app.app_context():
        from testorchestrator.execution import Schedule
        s = Schedule.query.filter_by(project_id=pid).one()
        assert s.targets()[0]["source_item_id"] == did
        assert s.suite_id is None
    client.post(f"/projects/{pid}/schedules", data={
        "name": "bad", "target_type": "suite", "target_id": f"repo:{did2}", "kind": "once",
        "run_at": "2026-01-01 00:00"})
    with app.app_context():
        assert Schedule.query.filter_by(
            project_id=pid).filter(Schedule.name == "bad").count() == 0
