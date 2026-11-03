"""Runbook wiring tests: probe-only smoke semantics, explicit-exec CLI,
Dagu config command, and Admin agent-token issuance (A01 review)."""

import shutil
import stat
from pathlib import Path

from testorchestrator.execution import Runner
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
