"""Runbook wiring tests: probe-only smoke semantics, explicit-exec CLI,
Dagu config command, Admin agent-token issuance, state-monitor endpoint,
and argv sanitization (A01 review)."""

import json
import shutil
import stat
from pathlib import Path

from testorchestrator import dispatch
from testorchestrator.agent_api import issue_agent_token
from testorchestrator.execution import (ExecutionState, Project, Runner,
                                        RuntimeConnection, Suite, SuiteEntry)
from testorchestrator.extensions import db
from testorchestrator import settings as settings_mod


FAKE_KRE = """#!/usr/bin/env bash
report=""
for arg in "$@"; do
  case "$arg" in
    -reportFolder=*) report="${arg#-reportFolder=}";;
    -version) echo "9.7.0"; exit 0;;
  esac
done
mkdir -p "$report/s" && echo '<testsuite tests="1"/>' > "$report/s/junit.xml"
"""


def _fake_kre(tmp_path):
    exe = tmp_path / "katalonc"
    exe.write_text(FAKE_KRE)
    exe.chmod(0o755)
    return exe


def test_kre_smoke_is_probe_only(app, tmp_path):
    """Probe must not start project tests: no invocation, no report dir."""
    exe = _fake_kre(tmp_path)
    # Clear any pre-existing smoke workspaces so creation is observable.
    for name in ("kre-smoke", "kre-exec"):
        shutil.rmtree(Path(app.instance_path) / name, ignore_errors=True)
    runner = app.test_cli_runner()
    res = runner.invoke(args=["kre-smoke", str(exe)])
    assert res.exit_code == 0, res.output
    assert "detected version 9.7.0" in res.output
    # No smoke workspace or reports may be produced by a probe.
    assert not (Path(app.instance_path) / "kre-smoke").exists()
    assert not (Path(app.instance_path) / "kre-exec").exists()


def test_kre_exec_requires_explicit_suite(app, tmp_path):
    exe = _fake_kre(tmp_path)
    runner = app.test_cli_runner()
    res = runner.invoke(args=["kre-exec", str(exe), "--project",
                              str(tmp_path)])
    assert res.exit_code != 0  # --suite is required

    res = runner.invoke(args=["kre-exec", str(exe), "--project",
                              str(tmp_path), "--suite",
                              "Test Suites/Smoke"])
    assert res.exit_code == 0, res.output
    assert "-testSuitePath=Test Suites/Smoke" in res.output
    assert "junit.xml" in res.output
    # The exact argv is recorded as evidence.
    inv = (Path(app.instance_path) / "kre-exec" / "logs"
           / "kre-invocation.log")
    assert "-runMode=console" in inv.read_text()


def test_dagu_config_saves_settings(app):
    runner = app.test_cli_runner()
    res = runner.invoke(args=["dagu-config", "--endpoint",
                              "http://127.0.0.1:29987/",
                              "--username", "orchestrator",
                              "--credential-ref", "missing"])
    assert res.exit_code != 0  # credential ref must resolve

    res = runner.invoke(args=["dagu-config", "--endpoint",
                              "http://127.0.0.1:29987/",
                              "--username", "orchestrator"])
    assert res.exit_code == 0, res.output
    with app.app_context():
        assert settings_mod.get("dagu_endpoint") == "http://127.0.0.1:29987"
        assert settings_mod.get("dagu_username") == "orchestrator"


def test_issue_agent_token_admin_only(client, login_as, app):
    with app.app_context():
        db.session.add(Runner(name="r1", connection_mode="local"))
        db.session.commit()
        rid = Runner.query.filter_by(name="r1").first().id

    login_as("tester")
    assert client.post(
        f"/system/infrastructure/runners/{rid}/issue-token"
    ).status_code == 403

    login_as("admin")
    resp = client.post(f"/system/infrastructure/runners/{rid}/issue-token",
                       follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        runner = db.session.get(Runner, rid)
        assert runner.agent_token_hash  # digest persisted, plaintext flashed
    assert b"toa_" in resp.data


# ---------- state monitor endpoint + reconcile wiring ----------

def _mk_execution(app, runner_id, state=ExecutionState.DISPATCHED):
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        db.session.add(SuiteEntry(suite_id=s.id, order=0, case_ref="C"))
        ex = dispatch.create_execution("admin", s)
        ex.state = state
        ex.runner_id = runner_id
        # Drain the dispatch outbox event — these tests drive the worker
        # manually and want the execution to stay in `state`.
        from testorchestrator.execution import OutboxEvent
        for ev in OutboxEvent.query.filter_by(status="pending"):
            ev.status = "done"
        db.session.commit()
        return ex.id


def _token(app, runner_id):
    with app.app_context():
        return issue_agent_token(db.session.get(Runner, runner_id))


def test_state_endpoint_scoping(client, app):
    """State labels: own runner OR any local-mode runner; foreign remote
    runners are refused (404), unauthenticated 401."""
    with app.app_context():
        remote = Runner(name="rem", connection_mode="remote")
        other_remote = Runner(name="rem2", connection_mode="remote")
        local = Runner(name="loc", connection_mode="local")
        db.session.add_all([remote, other_remote, local])
        db.session.commit()
        ids = (remote.id, other_remote.id, local.id)
    ex_id = _mk_execution(app, ids[0])
    t_remote = _token(app, ids[0])
    t_other = _token(app, ids[1])
    t_local = _token(app, ids[2])
    auth = lambda t: {"Authorization": f"Bearer {t}"}
    assert client.get(f"/api/agent/state/{ex_id}").status_code == 401
    # Owning remote runner reads its own execution.
    r = client.get(f"/api/agent/state/{ex_id}", headers=auth(t_remote))
    assert r.status_code == 200 and r.json["state"] == "dispatched"
    # A foreign remote runner does not.
    assert client.get(f"/api/agent/state/{ex_id}",
                      headers=auth(t_other)).status_code == 404
    # The local (orchestration-host) runner observes it for the Dagu step.
    assert client.get(f"/api/agent/state/{ex_id}",
                      headers=auth(t_local)).status_code == 200


def test_reconcile_dagu_runs_wired_and_running_not_masked(app, monkeypatch):
    """worker_once drives dagu reconciliation when configured; a 'running'
    dagu run must NOT extend the execution lease (agent disconnect still
    reconciles via expiry)."""
    from datetime import timedelta
    from testorchestrator.execution import Execution, utcnow

    class FakeDagu:
        label = "running"
        @classmethod
        def from_settings(cls):
            return cls()
        def status(self, _id):
            return {"statusLabel": self.label}

    monkeypatch.setattr(dispatch, "DaguAdapter", FakeDagu)
    with app.app_context():
        settings_mod.update({"dagu_endpoint": "http://dagu.local"})
        r = Runner(name="r", connection_mode="remote")
        db.session.add(r); db.session.commit()
        rid = r.id
        ex = _mk_execution(app, rid)
        from testorchestrator.execution import Execution as E
        e = db.session.get(E, ex)
        e.dispatch_id = "run1"
        e.lease_expires_at = utcnow() - timedelta(seconds=5)
        db.session.commit()

    with app.app_context():
        dispatch.worker_once()
    with app.app_context():
        from testorchestrator.execution import Execution as E
        e = db.session.get(E, ex)
        # Expired lease -> UNKNOWN despite dagu run still 'running'.
        assert e.state == ExecutionState.UNKNOWN

    # Finished dagu run without portal result -> UNKNOWN as well.
    FakeDagu.label = "succeeded"
    with app.app_context():
        ex2 = _mk_execution(app, rid)
        e2 = db.session.get(E, ex2)
        e2.dispatch_id = "run2"
        db.session.commit()
    with app.app_context():
        dispatch.worker_once()
    with app.app_context():
        assert db.session.get(E, ex2).state == ExecutionState.UNKNOWN


# ---------- argv sanitization ----------

def test_secret_bearing_arg_never_reaches_evidence(app, tmp_path, monkeypatch):
    """A runtime credential VALUE must be scrubbed from invocation log,
    captured stdout/stderr, artifacts, audit rows, and the Git export."""
    monkeypatch.setenv("TO_SECRET_KEY_FILE", str(tmp_path / "k"))
    fake = tmp_path / "kre"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "report=\"\"\n"
        "for arg in \"$@\"; do case \"$arg\" in -reportFolder=*) report=\"${arg#-reportFolder=}\";; esac; done\n"
        "echo \"running with args: $@\"\n"   # echoes argv incl. the secret
        "mkdir -p \"$report\" && echo '<testsuite tests=\"1\"/>' > \"$report/j.xml\"\n")
    fake.chmod(0o755)
    secret_value = "s3cr3t-invocation-key-99"
    with app.app_context():
        from testorchestrator.secrets_store import store_secret
        from flask import current_app
        store_secret(current_app, "rt-cred", "runtime", secret_value)
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        db.session.add(SuiteEntry(suite_id=s.id, order=0, case_ref="C"))
        r = Runner(name="r", connection_mode="local",
                   workspace_root=str(tmp_path / "w"))
        db.session.add(r); db.session.flush()
        rt = RuntimeConnection(
            name="kre", runner_id=r.id, executable_path=str(fake),
            credential_ref="rt-cred")
        db.session.add(rt); db.session.commit()
        ex = dispatch.create_execution("admin", s)
        ex_id = ex.id
    with app.app_context():
        dispatch.run_execution(ex_id)
    with app.app_context():
        from testorchestrator.execution import Execution, Artifact
        from testorchestrator.models import AuditEvent
        ex = db.session.get(Execution, ex_id)
        assert ex.state == ExecutionState.COMPLETED
        files = list(Path(ex.workspace_path).rglob("*"))
        blob = b"".join(f.read_bytes() for f in files if f.is_file())
        assert secret_value.encode() not in blob          # local logs
        assert b"***REDACTED***" in blob                  # scrub proof
        # Every artifact file is clean; rows carry no secret either.
        for a in Artifact.query.filter_by(execution_id=ex_id):
            f = Path(ex.report_dir) / a.rel_path
            probe = f if f.exists() else \
                Path(ex.workspace_path) / a.rel_path
            if probe.exists():
                assert secret_value.encode() not in probe.read_bytes()
        for row in AuditEvent.query.all():
            assert secret_value not in (row.detail or "")
        # UI log view serves the scrubbed file only.
        resp = app.test_client().get(
            f"/executions/{ex_id}/logs/kre-stdout.log")
        # (redirects to login when unauthenticated — never leaks content)
        assert secret_value.encode() not in resp.data
        # Git export tree carries no secret.
        settings_mod.update({"git_export_repo": str(tmp_path / "dest")})
        from testorchestrator import gitsync
        gitsync.export_execution(ex)
        for f in Path(tmp_path / "dest").rglob("*"):
            if f.is_file():
                assert secret_value.encode() not in f.read_bytes()


# ---------- runbook fixture path (previously broken entry points) ----------

def test_runbook_fixture_path_end_to_end(client, login_as, app, tmp_path):
    """The runbook's UI/CLI steps stay covered: dagu-config -> runner +
    token -> runtime -> project + binding -> suite create -> run form ->
    worker -> completed execution with recorded argv + results."""
    fake = tmp_path / "katalonc"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "report=\"\"\n"
        "for arg in \"$@\"; do case \"$arg\" in -reportFolder=*) report=\"${arg#-reportFolder=}\";; esac; done\n"
        "mkdir -p \"$report/s\" && cat > \"$report/s/j.xml\" <<'XML'\n"
        "<testsuite tests=\"1\"><testcase name=\"tc1\" time=\"0.1\"/></testsuite>\nXML\n")
    fake.chmod(0o755)
    src = tmp_path / "src"; (src / "Test Cases").mkdir(parents=True)
    (src / "Test Cases" / "tc.tc").write_text("<tc/>")

    login_as("admin")
    with app.app_context():
        p = Project(name="Demo"); db.session.add(p); db.session.flush()
        from testorchestrator.execution import GitBinding
        b = GitBinding(project_id=p.id, name="src", url=str(src),
                       worktree_path=str(src), last_synced_sha="a" * 40,
                       status="synced")
        db.session.add(b); db.session.commit()
        pid, bid = p.id, b.id

    # token + runner + runtime via the UI routes
    resp = client.post("/system/infrastructure/runners", data={
        "name": "local-1", "description": "", "os_arch": "linux/amd64",
        "connection_mode": "local", "workspace_root": str(tmp_path / "w"),
        "max_slots": 1, "allowed_projects": []})
    assert resp.status_code == 302
    with app.app_context():
        rid = Runner.query.filter_by(name="local-1").first().id
    resp = client.post(f"/system/infrastructure/runners/{rid}/issue-token",
                       follow_redirects=True)
    assert b"toa_" in resp.data
    resp = client.post("/system/infrastructure/runtimes", data={
        "name": "kre-1", "description": "", "runner_id": rid,
        "executable_path": str(fake),
        "workspace_root": str(tmp_path / "kw"),
        "credential_ref": "", "concurrency": 1})
    assert resp.status_code == 302

    # suite creation through the project page form (was unroutable/broken)
    resp = client.post(f"/projects/{pid}/suites", data={
        "name": "Smoke", "description": "", "binding_id": bid,
        "case_refs": ["Test Cases/tc.tc"]}, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        suite = Suite.query.filter_by(name="Smoke").first()
        assert [e.case_ref for e in suite.entries] == ["Test Cases/tc.tc"]
        sid = suite.id

    # run form on the suite page with selected profile/browser
    resp = client.post(f"/suites/{sid}/run", data={
        "execution_profile": "smoke-profile",
        "browser_type": "Firefox"}, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        from testorchestrator.execution import Execution
        ex = Execution.query.order_by(Execution.created_at.desc()).first()
        ex_id = ex.id
        assert ex.snapshot()["run_config"] == {
            "profile": "smoke-profile", "browser": "Firefox",
            "remote_webdriver_url": "", "remote_webdriver_type": "",
            "requested_runner": "", "requested_runtime": "",
            "collection": False,
            "timeout_seconds": 0,
            "kre_options": {}, "kre_options_allow_drop": False}
    with app.app_context():
        dispatch.run_execution(ex_id)
    with app.app_context():
        from testorchestrator.execution import Execution
        ex = db.session.get(Execution, ex_id)
        assert ex.state == ExecutionState.COMPLETED
        argv = (Path(ex.workspace_path) / "logs"
                / "kre-invocation.log").read_text()
        assert "-executionProfile=smoke-profile" in argv
        assert "-browserType=Firefox" in argv
        assert len(ex.results) == 1
