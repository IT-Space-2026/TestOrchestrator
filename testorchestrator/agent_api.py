"""Remote runner agent API (§6) — the portal side of remote transport.

A secured Python agent polls the portal with a per-runner bearer token.
Tasks carry durable IDs, leases, and scoped payloads — the agent never
sends commands to execute; it receives an immutable execution snapshot and
invokes the operator-installed KRE itself via the local adapter path.
Artifact/result uploads are authenticated per runner.

Endpoints (all JSON unless noted):
  POST /api/agent/register   -> runner identity + capabilities ack
  GET  /api/agent/tasks      -> assigned executions for this runner
  POST /api/agent/heartbeat  -> runner+execution lease/heartbeat
  POST /api/agent/result     -> canonical execution result payload
  POST /api/agent/artifact/<execution_id> -> authenticated file upload
  GET  /api/agent/cancel/<execution_id>   -> cancel flag poll
"""

import functools
import hashlib
import json
from pathlib import Path

from flask import Blueprint, abort, current_app, g, jsonify, request

from .execution import (Artifact, Execution, ExecutionState, Runner,
                        RuntimeConnection, utcnow)
from .extensions import db
from .models import record_audit
from .results import safe_relpath, sha256_file

agent_bp = Blueprint("agent", __name__, url_prefix="/api/agent")

LEASE_SECONDS = 90


def issue_agent_token(runner: Runner) -> str:
    """Issue a new agent token; only the SHA-256 digest is persisted."""
    import secrets
    token = "toa_" + secrets.token_urlsafe(32)
    runner.agent_token_hash = hashlib.sha256(token.encode()).hexdigest()
    db.session.commit()
    return token


def require_agent(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            abort(401)
        digest = hashlib.sha256(header[7:].encode()).hexdigest()
        runner = Runner.query.filter_by(agent_token_hash=digest).first()
        if runner is None or not runner.enabled:
            abort(401)
        g.runner = runner
        return view(*args, **kwargs)
    return wrapped


def _task_payload(ex: Execution) -> dict:
    runtime = ex.runtime
    # Invocation credentials resolve to VALUES only inside this scoped,
    # authenticated task — used solely to redact recorded argv/logs so
    # secret material never lands in evidence (§5, §9, §12).
    redact = []
    if runtime and runtime.credential_ref:
        from .secrets_store import read_secret, SecretError
        try:
            redact = [read_secret(current_app._get_current_object(),
                                  runtime.credential_ref)]
        except SecretError:
            pass  # unresolved ref — invocation still proceeds
    return {
        "execution_id": ex.id,
        "snapshot": ex.snapshot(),
        "source_sha": ex.source_sha,
        "runtime": {
            "id": runtime.id if runtime else "",
            "executable_path": runtime.executable_path if runtime else "",
            "workspace_root": runtime.workspace_root if runtime else "",
        },
        "redact": redact,
        "cancel_url": f"/api/agent/cancel/{ex.id}",
    }


# Protocol capability required for task pickup — agents must advertise
# "task-claim" at register to receive executable work. Pre-claim agents
# (or any agent lacking the capability) get an empty task list plus an
# explicit incompatibility note instead of work.
REQUIRED_CAPABILITY = "task-claim"


@agent_bp.route("/register", methods=["POST"])
@require_agent
def register():
    runner = g.runner
    runner.agent_version = request.json.get("agent_version", "")[:32]
    caps = request.json.get("capabilities") or []
    runner.agent_capabilities = json.dumps(caps[:16])
    runner.heartbeat_at = utcnow()
    runner.health = "ok"
    db.session.commit()
    return jsonify({
        "runner_id": runner.id, "name": runner.name,
        "max_slots": runner.max_slots, "draining": runner.draining,
        "allowed_projects": json.loads(runner.allowed_projects or "[]"),
        "claim_required": True,
        "compatible": REQUIRED_CAPABILITY in caps,
    })


def _agent_compatible() -> bool:
    try:
        return REQUIRED_CAPABILITY in json.loads(
            g.runner.agent_capabilities or "[]")
    except (TypeError, ValueError):
        return False


def _agent_owned(ex: Execution) -> bool:
    """The agent API may deliver/mutate only agent-owned executions.
    A dagu-worker transport owns its execution end-to-end — an agent
    touching it would create a second executor and conflicting
    authoritative results (§9 transport isolation)."""
    return ex.snapshot().get("dispatch_transport") != "dagu-worker"


@agent_bp.route("/tasks")
@require_agent
def tasks():
    """Assigned remote executions: DISPATCHED to this runner, lease held by
    the portal. The payload is an immutable snapshot — no arbitrary commands."""
    if g.runner.draining:
        return jsonify({"tasks": [], "draining": True})
    if not _agent_compatible():
        return jsonify({
            "tasks": [], "incompatible": True,
            "error": "agent does not support task-claim pickup — "
                     "upgrade the agent before it can receive work"})
    assigned = Execution.query.filter(
        Execution.runner_id == g.runner.id,
        Execution.state == ExecutionState.DISPATCHED,
    ).all()
    return jsonify({"tasks": [_task_payload(e) for e in assigned
                              if _agent_owned(e)]})


@agent_bp.route("/tasks/<execution_id>/claim", methods=["POST"])
@require_agent
def claim_task(execution_id):
    """Atomic DISPATCHED -> RUNNING pickup claim scoped to this runner —
    exactly one agent may take a task. A second claim (duplicate agent,
    reconnect racing an in-flight launch) gets 409 and must skip it."""
    ex = db.session.get(Execution, execution_id)
    if ex is not None and not _agent_owned(ex):
        record_audit(f"agent:{g.runner.name}",
                     "execution.claim_rejected_transport", execution_id)
        return jsonify({"claimed": False, "transport": "dagu-worker"}), 403
    n = db.session.query(Execution).filter(
        Execution.id == execution_id,
        Execution.runner_id == g.runner.id,
        Execution.state == ExecutionState.DISPATCHED,
    ).update({"state": ExecutionState.RUNNING,
              "started_at": utcnow(),
              "heartbeat_at": utcnow()},
             synchronize_session=False)
    db.session.commit()
    if not n:
        db.session.rollback()
        record_audit(f"agent:{g.runner.name}",
                     "execution.claim_rejected", execution_id)
        return jsonify({"claimed": False}), 409
    return jsonify({"claimed": True})


@agent_bp.route("/heartbeat", methods=["POST"])
@require_agent
def heartbeat():
    """Lease renewal for claimed executions + runner liveness."""
    runner = g.runner
    runner.heartbeat_at = utcnow()
    runner.health = "ok"
    data = request.get_json(silent=True) or {}
    renewed = []
    from datetime import timedelta
    for ex_id in data.get("executions", []):
        ex = db.session.get(Execution, ex_id)
        if ex and ex.runner_id == runner.id and _agent_owned(ex):
            ex.heartbeat_at = utcnow()
            ex.lease_expires_at = utcnow() + timedelta(seconds=LEASE_SECONDS)
            renewed.append(ex_id)
    db.session.commit()
    return jsonify({"renewed": renewed})


@agent_bp.route("/state/<execution_id>")
@require_agent
def state(execution_id):
    """Execution state label for the Dagu monitor step (Option B remote
    targeting). Returns ONLY the state label — a remote runner may read
    its own executions; a LOCAL runner token (the orchestration-host
    credential the Dagu step authenticates with) may observe any
    dispatched execution so the dagu run tracks it to terminal state or
    explicit uncertainty."""
    ex = db.session.get(Execution, execution_id)
    if ex is None:
        abort(404)
    if ex.runner_id != g.runner.id and g.runner.connection_mode != "local":
        abort(404)
    return jsonify({"state": ex.state.value, "outcome": ex.outcome.value})


@agent_bp.route("/cancel/<execution_id>")
@require_agent
def cancel(execution_id):
    ex = db.session.get(Execution, execution_id)
    if ex is None or ex.runner_id != g.runner.id:
        abort(404)
    return jsonify({
        "cancel": ex.state == ExecutionState.CANCEL_REQUESTED,
        "state": ex.state.value,
    })


@agent_bp.route("/result", methods=["POST"])
@require_agent
def result():
    """Canonical result payload from the agent. The portal reconciles it
    against the durable record — reconnects never create a second run."""
    data = request.get_json(silent=True) or {}
    ex = db.session.get(Execution, data.get("execution_id", ""))
    if ex is None or ex.runner_id != g.runner.id:
        abort(404)
    if not _agent_owned(ex):
        record_audit(f"agent:{g.runner.name}",
                     "execution.result_rejected_transport", ex.id)
        return jsonify({"status": "rejected",
                        "transport": "dagu-worker"}), 403
    if ex.state in (ExecutionState.COMPLETED, ExecutionState.FAILED,
                    ExecutionState.CANCELED):
        return jsonify({"status": "already-terminal"})
    if ex.state == ExecutionState.UNKNOWN:
        # Late-arriving evidence after ambiguous termination is REAL data
        # — preserve it for reconciliation/Admin review, but never reopen
        # the execution, overwrite a finalized outcome, or rerun tests.
        for case in data.get("results", []):
            from .execution import TestResult, CaseStatus
            db.session.add(TestResult(
                execution_id=ex.id,
                case_ref=case.get("case_ref", ""),
                case_name=case.get("case_name", "<unnamed>"),
                classname=case.get("classname", ""),
                iteration=case.get("iteration", 0),
                attempt=case.get("attempt", 1),
                status=CaseStatus(case.get("status", "failed")),
                duration_s=case.get("duration_s", 0.0),
                message=case.get("message", "")[:4000],
                unresolved=True,
            ))
        ex.error_text = (ex.error_text +
            " | late result preserved for review")[:2000]
        db.session.commit()
        record_audit(f"agent:{g.runner.name}",
                     "execution.late_result_received", ex.id)
        return jsonify({"status": "preserved-for-review"})

    from .execution import Outcome, TestResult, CaseStatus
    from . import dispatch
    ex.exit_status = data.get("exit_status")
    ex.error_kind = data.get("error_kind", "")
    ex.error_text = data.get("error_text", "")[:2000]
    for case in data.get("results", []):
        db.session.add(TestResult(
            execution_id=ex.id,
            case_ref=case.get("case_ref", ""),
            case_name=case.get("case_name", "<unnamed>"),
            classname=case.get("classname", ""),
            iteration=case.get("iteration", 0),
            attempt=case.get("attempt", 1),
            status=CaseStatus(case.get("status", "failed")),
            duration_s=case.get("duration_s", 0.0),
            message=case.get("message", "")[:4000],
            unresolved=bool(case.get("unresolved")),
        ))
    ex.outcome = Outcome(data.get("outcome", "incomplete"))
    canceled = data.get("canceled", False)
    ex.state = (ExecutionState.CANCELED if canceled
                else ExecutionState.COMPLETED
                if not ex.error_kind else ExecutionState.FAILED)
    ex.finished_at = utcnow()
    db.session.commit()
    record_audit(f"agent:{g.runner.name}", "execution.result_received", ex.id)
    return jsonify({"status": "recorded", "state": ex.state.value})


@agent_bp.route("/artifact/<execution_id>", methods=["POST"])
@require_agent
def artifact(execution_id):
    """Authenticated evidence upload: validated path, checksum, MIME; stored
    under the execution report dir with an Artifact row (§9)."""
    ex = db.session.get(Execution, execution_id)
    if ex is None or ex.runner_id != g.runner.id:
        abort(404)
    if not _agent_owned(ex):
        record_audit(f"agent:{g.runner.name}",
                     "execution.artifact_rejected_transport", ex.id)
        abort(403)
    upload = request.files.get("file")
    if upload is None:
        abort(400)
    rel = request.form.get("rel_path", upload.filename or "unnamed")
    kind = request.form.get("kind", "other")
    root = Path(current_app.instance_path) / "agent-evidence" / ex.id
    root.mkdir(parents=True, exist_ok=True)
    target = (root / rel)
    try:
        safe_relpath(root, target)
    except ValueError:
        abort(400)
    target.parent.mkdir(parents=True, exist_ok=True)
    upload.save(target)
    digest = sha256_file(target)
    db.session.add(Artifact(
        execution_id=ex.id, kind=kind, rel_path=rel, sha256=digest,
        size=target.stat().st_size,
        mime=request.form.get("mime", ""), mapped=False))
    ex.report_dir = ex.report_dir or str(root)
    db.session.commit()
    return jsonify({"sha256": digest})
