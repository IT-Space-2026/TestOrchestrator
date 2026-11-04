"""Remote runner agent transport tests (§6): auth, task poll, heartbeats,
artifact upload, cancel flag, reconnect reconciliation."""

import json
import threading
import time
from pathlib import Path

import pytest

from testorchestrator import dispatch
from testorchestrator.agent_api import issue_agent_token
from testorchestrator.execution import (Execution, ExecutionState, Outcome,
                                        Project, Runner, RuntimeConnection,
                                        Suite, SuiteEntry)
from testorchestrator.extensions import db


@pytest.fixture()
def remote_env(app, tmp_path):
    """Remote runner + runtime + project/suite + issued agent token."""
    with app.app_context():
        project = Project(name="P")
        db.session.add(project)
        db.session.flush()
        suite = Suite(project_id=project.id, name="S")
        db.session.add(suite)
        db.session.flush()
        db.session.add(SuiteEntry(suite_id=suite.id, order=0,
                                  case_ref="Test Cases/x.tc"))
        runner = Runner(name="remote-1", connection_mode="remote",
                        workspace_root=str(tmp_path / "w"))
        db.session.add(runner)
        db.session.flush()
        db.session.add(RuntimeConnection(
            name="kre-remote", runner_id=runner.id,
            executable_path="/opt/katalon/katalonc"))
        db.session.commit()
        token = issue_agent_token(runner)
        yield {"runner": runner, "suite": suite, "token": token}


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_agent_register_and_task_poll(client, remote_env):
    env = remote_env
    # Unauthenticated/bad token refused.
    assert client.post("/api/agent/register", json={}).status_code == 401
    assert client.post("/api/agent/register", json={},
                       headers=_auth("toa_wrong")).status_code == 401

    resp = client.post("/api/agent/register",
                       json={"agent_version": "0.2",
                                "capabilities": ["task-claim"]},
                       headers=_auth(env["token"]))
    assert resp.status_code == 200
    assert resp.json["runner_id"] == env["runner"].id

    # Dispatch an execution — remote runner gets it via the task poll.
    with client.application.app_context():
        ex = dispatch.create_execution("admin", env["suite"])
        ex_id = ex.id
        dispatch.run_execution(ex.id)  # assigns remote runner, stays DISPATCHED
        assert db.session.get(Execution, ex.id).runner_id == env["runner"].id
    resp = client.get("/api/agent/tasks", headers=_auth(env["token"]))
    assert resp.status_code == 200
    tasks = resp.json["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["execution_id"] == ex_id
    assert tasks[0]["runtime"]["executable_path"] == "/opt/katalon/katalonc"


def test_agent_heartbeat_and_reconcile(client, remote_env):
    env = remote_env
    with client.application.app_context():
        ex = dispatch.create_execution("admin", env["suite"])
        ex_id = ex.id
        dispatch.run_execution(ex_id)
    resp = client.post("/api/agent/heartbeat",
                       json={"executions": [ex_id]},
                       headers=_auth(env["token"]))
    assert resp.json["renewed"] == [ex_id]
    with client.application.app_context():
        assert db.session.get(Execution, ex_id).heartbeat_at is not None


def test_agent_result_and_artifact(client, remote_env, tmp_path):
    env = remote_env
    with client.application.app_context():
        ex = dispatch.create_execution("admin", env["suite"])
        ex_id = ex.id
        dispatch.run_execution(ex_id)
    import io
    # Artifact upload first (agent collects before result post).
    resp = client.post(
        f"/api/agent/artifact/{ex_id}",
        data={"file": (io.BytesIO(b"PNGDATA"), "shot.png"),
              "rel_path": "shots/shot.png", "kind": "screenshot",
              "mime": "image/png"},
        content_type="multipart/form-data", headers=_auth(env["token"]))
    assert resp.status_code == 200
    assert len(resp.json["sha256"]) == 64

    resp = client.post("/api/agent/result", json={
        "execution_id": ex_id, "exit_status": 0, "canceled": False,
        "outcome": "failed",
        "results": [{"case_name": "c1", "status": "failed",
                     "message": "assert"}, {"case_name": "c2",
                     "status": "passed"}],
    }, headers=_auth(env["token"]))
    assert resp.status_code == 200
    with client.application.app_context():
        ex = db.session.get(Execution, ex_id)
        assert ex.state == ExecutionState.COMPLETED
        assert ex.outcome == Outcome.FAILED
        assert len(ex.results) == 2
        # Terminal state is durable: a reconnect/duplicate post can't
        # create a second run or reopen it (§6 reconciliation).
        resp2 = client.post("/api/agent/result", json={
            "execution_id": ex_id, "exit_status": 0},
            headers=_auth(env["token"]))
        assert resp2.json["status"] == "already-terminal"
        assert len(db.session.get(Execution, ex_id).results) == 2


def test_agent_cancel_flag(client, remote_env):
    env = remote_env
    with client.application.app_context():
        ex = dispatch.create_execution("admin", env["suite"])
        ex_id = ex.id
        dispatch.run_execution(ex_id)
    resp = client.get(f"/api/agent/cancel/{ex_id}",
                      headers=_auth(env["token"]))
    assert resp.json["cancel"] is False
    with client.application.app_context():
        dispatch.request_cancel(ex_id, "admin")
    resp = client.get(f"/api/agent/cancel/{ex_id}",
                      headers=_auth(env["token"]))
    assert resp.json["cancel"] is True


def test_draining_runner_gets_no_tasks(client, remote_env):
    env = remote_env
    with client.application.app_context():
        env["runner"].draining = True
        db.session.commit()
    resp = client.get("/api/agent/tasks", headers=_auth(env["token"]))
    assert resp.json["draining"] is True
    assert resp.json["tasks"] == []


def test_late_result_on_unknown_preserved_not_reopened(client, remote_env):
    """UNKNOWN + late valid result -> preserved for review, never
    reopened, never rerun, terminal states still refused."""
    env = remote_env
    with client.application.app_context():
        ex = dispatch.create_execution("admin", env["suite"])
        ex.state = ExecutionState.UNKNOWN
        ex.runner_id = env["runner"].id
        db.session.commit()
        ex_id = ex.id
    resp = client.post("/api/agent/result", json={
        "execution_id": ex_id, "exit_status": 0,
        "outcome": "passed", "canceled": False,
        "results": [{"case_name": "late1", "status": "passed"}],
    }, headers=_auth(env["token"]))
    assert resp.json["status"] == "preserved-for-review"
    with client.application.app_context():
        ex = db.session.get(Execution, ex_id)
        assert ex.state == ExecutionState.UNKNOWN   # not reopened
        assert ex.outcome is None or ex.outcome.value != "passed"
        assert len(ex.results) == 1                 # evidence kept
        assert ex.results[0].unresolved is True
