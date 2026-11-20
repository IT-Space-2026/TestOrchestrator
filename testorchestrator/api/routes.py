"""CI/CD API (v1) — bearer-token automation surface.

Trigger runs, poll status, fetch cases/artifacts, cancel, retry —
the same lifecycle the portal exposes, for pipelines. Auth is an
``Authorization: Bearer to_…`` ApiToken (SHA-256 stored); scope
'read' polls, 'run' additionally mutates. Project scoping mirrors
User.allowed_projects; audit records every mutating call. No session
cookies, no CSRF — this blueprint is JSON-only by contract.
"""
import functools
import hashlib
import json
import mimetypes
from pathlib import Path

from flask import abort, current_app, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

from .. import dispatch
from ..execution import (ApiToken, Artifact, DiscoveredItem, Execution,
                         ExecutionState, GitBinding, Outcome, Suite,
                         TestResult, utcnow)
from ..extensions import db
from ..models import record_audit
from . import api_bp

_RUN_SCOPES = {"run"}


def _error(status: int, kind: str, message: str):
    return jsonify(error={"kind": kind, "message": message}), status


def api_token_required(write: bool = False):
    """Bearer-token gate. Tokens never exceed their stored scope and
    project list — a leaked 'read' token cannot trigger runs."""
    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            auth = request.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else ""
            row = None
            if token.startswith("to_"):
                row = ApiToken.query.filter_by(
                    token_hash=hashlib.sha256(
                        token.encode()).hexdigest()).first()
            if row is None or row.revoked_at is not None:
                record_audit("api", "api.auth.denied",
                             request.path[:128])
                return _error(401, "auth", "invalid or revoked token")
            if write and row.scope not in _RUN_SCOPES:
                return _error(403, "scope",
                              "token scope 'read' cannot mutate")
            row.last_used_at = utcnow()
            db.session.commit()
            g.api_token = row
            return view(*args, **kwargs)
        return wrapped
    return decorator


def _scope_execution(ex: Execution) -> Execution:
    if ex is None:
        abort(404)
    if not g.api_token.project_allowed(ex.project_id or ""):
        abort(404)  # scoped-out resources are indistinguishable
    return ex


def _exec_json(ex: Execution, detail: bool = False) -> dict:
    d = {
        "id": ex.id,
        "state": ex.state.value,
        "outcome": ex.outcome.value if ex.outcome else None,
        "project_id": ex.project_id,
        "suite_id": ex.suite_id,
        "source_item_id": ex.source_item_id,
        "framework": ex.framework,
        "runner_id": ex.runner_id,
        "runtime_id": ex.runtime_id,
        "wait_reason": ex.wait_reason,
        "client_ref": ex.client_ref,
        "created_at": ex.created_at.isoformat() if ex.created_at else None,
        "started_at": ex.started_at.isoformat() if ex.started_at else None,
        "finished_at": (ex.finished_at.isoformat()
                        if ex.finished_at else None),
    }
    if detail:
        snap = ex.snapshot()
        d.update({
            "error_kind": ex.error_kind,
            "error_text": ex.error_text,
            "cancel_reason": ex.cancel_reason,
            "parent_id": ex.parent_id,
            "role": ex.role,
            "bb_slot": ex.bb_slot,
            "dispatch_id": ex.dispatch_id,
            "attempts": snap.get("bb_attempts", []),
            "deployed_rev": snap.get("deployed_rev"),
            "run_config": snap.get("run_config", {}),
            "report_pub_status": ex.report_pub_status,
        })
    return d


@api_bp.route("/ping")
def ping():
    from ..deploy_info import deployed_revision
    rev = deployed_revision()
    return jsonify(ok=True,
                   version=rev.get("version_file") or
                           rev.get("git_head", "")[:12],
                   module_path=rev.get("module_path", ""))


@api_bp.route("/executions")
@api_token_required()
def list_executions():
    q = Execution.query
    allowed = json.loads(g.api_token.allowed_projects or "[]")
    if allowed:
        q = q.filter(Execution.project_id.in_(allowed))
    args = request.args
    if args.get("project_id"):
        pid = args["project_id"]
        if not g.api_token.project_allowed(pid):
            return _error(403, "scope", "project outside token scope")
        q = q.filter(Execution.project_id == pid)
    if args.get("state"):
        try:
            q = q.filter(Execution.state == ExecutionState(args["state"]))
        except ValueError:
            return _error(400, "param", f"bad state {args['state']!r}")
    if args.get("outcome"):
        try:
            q = q.filter(Execution.outcome == Outcome(args["outcome"]))
        except ValueError:
            return _error(400, "param", f"bad outcome {args['outcome']!r}")
    if args.get("client_ref"):
        q = q.filter(Execution.client_ref == args["client_ref"][:128])
    if args.get("since"):
        try:
            from datetime import datetime, timezone
            q = q.filter(Execution.created_at >= datetime.fromisoformat(
                args["since"]).astimezone(timezone.utc))
        except ValueError:
            return _error(400, "param", "bad since timestamp")
    limit = min(max(int(args.get("limit", 50) or 50), 1), 200)
    rows = (q.order_by(Execution.created_at.desc())
            .limit(limit).all())
    return jsonify(executions=[_exec_json(e) for e in rows],
                   count=len(rows))


@api_bp.route("/executions", methods=["POST"])
@api_token_required(write=True)
def create():
    """Trigger a run. Idempotent on (token, client_ref): a retry of a
    timed-out POST returns the SAME execution, never a duplicate."""
    body = request.get_json(silent=True) or {}
    client_ref = str(body.get("client_ref") or "")[:128]
    if client_ref:
        existing = Execution.query.filter_by(
            client_ref=client_ref).order_by(
            Execution.created_at.desc()).first()
        if existing is not None and \
                g.api_token.project_allowed(existing.project_id or ""):
            return jsonify(execution=_exec_json(existing, detail=True),
                           idempotent_replay=True)
    run_config = dict(body.get("run_config") or {})
    if body.get("runner_id"):
        run_config["runner_id"] = str(body["runner_id"])
    if body.get("runtime_id"):
        run_config["runtime_id"] = str(body["runtime_id"])
    try:
        if body.get("item_id"):
            item = db.session.get(DiscoveredItem, str(body["item_id"]))
            if item is None or item.kind not in ("suite", "collection"):
                return _error(404, "target", "item not found")
            if not g.api_token.project_allowed(item.project_id):
                return _error(403, "scope",
                              "item outside token project scope")
            binding = db.session.get(GitBinding, item.binding_id)
            ex = dispatch.create_execution(
                f"api:{g.api_token.name}", binding=binding,
                source_sha=(body.get("source_sha")
                            or (binding.last_synced_sha if binding
                                else "")),
                run_config=run_config, source_item=item)
        elif body.get("suite_id"):
            suite = db.session.get(Suite, str(body["suite_id"]))
            if suite is None:
                return _error(404, "target", "suite not found")
            if not g.api_token.project_allowed(suite.project_id):
                return _error(403, "scope",
                              "suite outside token project scope")
            binding = GitBinding.query.filter_by(
                project_id=suite.project_id).first()
            ex = dispatch.create_execution(
                f"api:{g.api_token.name}", suite, binding=binding,
                source_sha=(body.get("source_sha")
                            or (binding.last_synced_sha if binding
                                else "")),
                run_config=run_config)
        else:
            return _error(400, "target",
                          "one of item_id|suite_id is required")
    except dispatch.DispatchError as exc:
        return _error(400, "dispatch", str(exc))
    if client_ref:
        ex.client_ref = client_ref
        db.session.commit()
    record_audit(f"api:{g.api_token.name}", "api.execution.queued",
                 ex.id)
    return jsonify(execution=_exec_json(ex, detail=True)), 201


@api_bp.route("/executions/<execution_id>")
@api_token_required()
def get_execution(execution_id):
    ex = _scope_execution(db.session.get(Execution, execution_id))
    return jsonify(execution=_exec_json(ex, detail=True))


@api_bp.route("/executions/<execution_id>/cases")
@api_token_required()
def get_cases(execution_id):
    ex = _scope_execution(db.session.get(Execution, execution_id))
    rows = TestResult.query.filter_by(execution_id=ex.id).all()
    return jsonify(cases=[{
        "id": r.id, "case_ref": r.case_ref, "case_name": r.case_name,
        "classname": r.classname, "status": r.status.value,
        "duration_s": r.duration_s, "message": r.message[:1000],
        "attempt": r.attempt, "unresolved": r.unresolved,
    } for r in rows], count=len(rows))


@api_bp.route("/executions/<execution_id>/artifacts")
@api_token_required()
def get_artifacts(execution_id):
    ex = _scope_execution(db.session.get(Execution, execution_id))
    rows = Artifact.query.filter_by(execution_id=ex.id).all()
    return jsonify(artifacts=[{
        "id": a.id, "kind": a.kind, "rel_path": a.rel_path,
        "sha256": a.sha256, "size": a.size, "mime": a.mime,
        "mapped": a.mapped,
    } for a in rows], count=len(rows))


@api_bp.route("/executions/<execution_id>/artifacts/<artifact_id>")
@api_token_required()
def download_artifact(execution_id, artifact_id):
    """Evidence download — same integrity contract as the portal:
    checksum re-verified on every download, attachment-only."""
    ex = _scope_execution(db.session.get(Execution, execution_id))
    artifact = db.session.get(Artifact, artifact_id)
    if artifact is None or artifact.execution_id != ex.id:
        abort(404)
    root = Path(ex.report_dir or "")
    target = (root / artifact.rel_path).resolve()
    if not str(target).startswith(str(root.resolve()) + "/") \
            or not target.is_file():
        abort(404)
    from ..results import sha256_file
    if sha256_file(target) != artifact.sha256:
        abort(409)
    record_audit(f"api:{g.api_token.name}", "api.artifact.download",
                 artifact.id)
    return send_file(target, mimetype=artifact.mime or
                     mimetypes.guess_type(target.name)[0] or
                     "application/octet-stream",
                     download_name=target.name, as_attachment=True)


@api_bp.route("/executions/<execution_id>/cancel", methods=["POST"])
@api_token_required(write=True)
def cancel(execution_id):
    ex = _scope_execution(db.session.get(Execution, execution_id))
    ok = dispatch.request_cancel(
        ex.id, f"api:{g.api_token.name}",
        reason=str((request.get_json(silent=True) or {})
                   .get("reason") or ""))
    if not ok:
        return _error(409, "state", "execution is not cancellable")
    record_audit(f"api:{g.api_token.name}", "api.execution.cancelled",
                 ex.id)
    return jsonify(execution=_exec_json(
        db.session.get(Execution, ex.id), detail=True))


@api_bp.route("/executions/<execution_id>/retry", methods=["POST"])
@api_token_required(write=True)
def retry(execution_id):
    """Queue the next attempt — a NEW execution in the evidence chain,
    never a mutation of the original (same contract as the UI)."""
    ex = _scope_execution(db.session.get(Execution, execution_id))
    try:
        new_ex = dispatch.retry_execution(f"api:{g.api_token.name}", ex)
    except dispatch.DispatchError as exc:
        return _error(400, "dispatch", str(exc))
    record_audit(f"api:{g.api_token.name}", "api.execution.retried",
                 f"{ex.id} -> {new_ex.id}")
    return jsonify(execution=_exec_json(new_ex, detail=True),
                   previous_attempt=ex.id), 201


@api_bp.errorhandler(HTTPException)
def _http_json(exc):
    return _error(exc.code or 500, "http",
                  exc.description or exc.name)


@api_bp.errorhandler(Exception)
def _unhandled(exc):  # pragma: no cover - safety net
    current_app.logger.exception("api error")
    return _error(500, "internal", "internal error")
