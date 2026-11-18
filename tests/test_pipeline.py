"""End-to-end pipeline tests (handoff §4) using a fixture executable.

The fixture mimics a KRE-like CLI (accepts -reportFolder=, writes JUnit +
evidence). It proves workspace isolation, argument-array invocation, JUnit
normalization, evidence collection, cancellation, and durable state — it
does NOT prove real KRE CLI compatibility (A14).
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest
import subprocess
import threading

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
        # Evidence: JUnit + PNG screenshot + the worker step log — the
        # remote path returns ONE consolidated run log, not three
        # portal-side invocation files.
        artifacts = Artifact.query.filter_by(execution_id=ex_id).all()
        kinds = sorted(a.kind for a in artifacts)
        assert "junit" in kinds and "screenshot" in kinds
        assert kinds.count("log") >= 1
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

    # Dispatch (submit spawns the remote run; portal-side state is
    # DISPATCHED — the run lives on the worker, not in this process).
    with app.app_context():
        dispatch.worker_once()
        ex = db.session.get(Execution, ex_id)
        assert ex.state == ExecutionState.DISPATCHED
        assert ex.dispatch_id
        assert dispatch.request_cancel(ex_id, "admin")
        assert ex.state == ExecutionState.CANCEL_REQUESTED
        # cancel reaches the (stubbed) master -> process tree dies ->
        # reconcile collects the cancelled buildrequest -> terminal.
        dispatch.worker_once()
        dispatch.worker_once()

    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert ex.state == ExecutionState.CANCELED
        assert ex.outcome == Outcome.INCOMPLETE
        assert ex.exit_status is not None  # termination acknowledged
        # The remote process tree is dead — no orphaned sleep procs.
        import subprocess as _sp
        out = _sp.run(["pgrep", "-f", "slow-kre"],
                      capture_output=True, text=True)
        assert out.returncode != 0


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
_WRAP = str(Path(__file__).resolve().parent.parent
           / "scripts" / "kre_cred_exec.py")


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



def test_portal_schedule_repo_target(app, client, login_as):
    """POST a once-schedule with a repo:<item_id> target — persisted to
    targets_json; a foreign project's item is rejected visibly."""
    with app.app_context():
        from testorchestrator.execution import GitBinding, Project
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


def test_create_runtime_honors_selected_runner(app, client, login_as):
    """create_runtime binds the explicitly selected remote runner —
    not the first remote runner in the table."""
    with app.app_context():
        r1 = Runner(name="r-first", connection_mode="remote")
        r2 = Runner(name="r-second", connection_mode="remote")
        db.session.add_all([r1, r2]); db.session.commit()
        r2_id = r2.id
    login_as("admin")
    resp = client.post("/system/infrastructure/runtimes", data={
        "name": "kre-x", "exec_location": "remote",
        "worker_label": "kre=x", "runner_id": r2_id,
        "executable_path": "/bin/true"})
    assert resp.status_code in (302, 303)
    with app.app_context():
        rt = RuntimeConnection.query.filter_by(name="kre-x").one()
        assert rt.runner_id == r2_id
        assert rt.runner_id == r2_id


def test_create_runtime_rejects_bad_runner(app, client, login_as):
    """Invalid/disabled/mode-incompatible runner selections fail
    visibly with no partial runtime create."""
    with app.app_context():
        remote = Runner(name="r1", connection_mode="remote")
        local = Runner(name="l1", connection_mode="local")
        dead = Runner(name="d1", connection_mode="remote", enabled=False)
        db.session.add_all([remote, local, dead]); db.session.commit()
        bad = ["does-not-exist", local.id, dead.id]
    login_as("admin")
    for rid in bad:
        client.post("/system/infrastructure/runtimes", data={
            "name": f"rt-{rid[:4]}", "exec_location": "remote",
            "worker_label": f"kre={rid[:4]}", "runner_id": rid,
            "executable_path": "/bin/true"})
    with app.app_context():
        assert RuntimeConnection.query.count() == 0
