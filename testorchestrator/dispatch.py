"""Execution pipeline: dispatch, state machine, local runner (§4–§8).

Workflow: prepare -> execute -> collect -> normalize -> finalize.
Collection continues after failed tests; the final verdict comes from
recorded execution evidence, not from a wrapper that hides failures.

Durability: execution rows + outbox events are committed transactionally
before any side effect, so a worker/portal restart reconciles rather than
duplicating work (A06). An ambiguous state becomes UNKNOWN — never a blind
re-dispatch of a potentially destructive run.

Dispatch and collection run in `flask worker`, outside request handlers.
"""

import hashlib
import json
import os
import time
from datetime import timedelta
from pathlib import Path

from flask import current_app

from . import settings as settings_mod
from .adapters import kre as kre_adapter
from .adapters import nodefw, pytestfw, robotfw


def _tag_list(raw) -> list:
    """Comma-separated tag list -> sanitized tokens (no empty items,
    no shell metacharacters — argv only, never a shell)."""
    out = []
    for t in str(raw or "").split(","):
        t = t.strip()
        if t and all(c.isalnum() or c in "-_:. " for c in t):
            out.append(t[:64])
    return out
from .adapters.buildbot import BuildbotAdapter
from . import buildbot_cfg
from .execution import (Artifact, DiscoveredItem, Execution,
                        ExecutionState, GitBinding,
                        OutboxEvent, Outcome, Plan, Release,
                        ResourceReservation, Runner,
                        RuntimeConnection, Schedule, Suite, SyncState,
                        TestResult, utcnow)
from sqlalchemy.exc import OperationalError

from .extensions import db
from .models import record_audit
from .results import aggregate_outcome, entry_state, parse_junit
from . import results as results_mod

LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 15


class DispatchError(RuntimeError):
    pass


# ---------- creation / snapshot ----------

def _snapshot_runtime_kind(rc: dict) -> str:
    """Runtime kind for the run_config's selected runtime — decides
    whether the recorded browser default is KRE's or the framework's.
    Unknown/unresolved runtime records KRE (the pre-2.6 default)."""
    rid = str(rc.get("runtime_id") or "").strip()
    if not rid:
        return "kre"
    rt = db.session.get(RuntimeConnection, rid)
    return (getattr(rt, "runtime_kind", "") or "kre") if rt else "kre"


def _require_probe_opt(runtime, marker: str, desc: str) -> None:
    """Dispatch gate for optional parallel dependencies: the
    readiness probe must have REPORTED the marker ok on this target —
    informational absence is fine in sequential/default mode, but a
    parallel run that needs the dependency is rejected, never
    silently downgraded to sequential."""
    try:
        markers = json.loads(getattr(runtime, "probe_json", "") or "{}")
    except Exception:
        markers = {}
    if markers.get(marker) != "ok":
        raise DispatchError(
            f"{desc} is required but readiness reported "
            f"{marker}={markers.get(marker, 'never-probed')!r} on this "
            "target — install it and re-run the readiness probe")


def _gated_pabot(runtime, rc: dict) -> int:
    n = (int(rc.get("parallel") or 0)
         if (rc.get("parallel_mode") or "default") == "parallel"
         else 0)
    if n:
        _require_probe_opt(runtime, "opt_pabot",
                           f"pabot (parallel_mode=parallel, "
                           f"--processes {n})")
    return n


_PARALLEL_MODES = ("default", "sequential", "parallel")


def _validate_parallel(rc: dict, kind: str) -> None:
    """Server-side parallel/shard validation — every entry path
    (portal form, API, schedule, retry) flows through
    create_execution, so a bad combination is REJECTED here, never
    silently clamped or downgraded to sequential.

    Semantics: parallel_mode "default" lets the framework choose;
    "sequential" forces one process where the framework supports it;
    "parallel" requires parallel >= 1. A worker count never implies
    sharding; shard index/total validate as a pair and only apply to
    Playwright (other kinds use unit-level partition manifests)."""
    try:
        par = int(rc.get("parallel") or 0)
    except (TypeError, ValueError):
        raise DispatchError("parallel must be an integer")
    mode = str(rc.get("parallel_mode") or "").strip() or (
        "parallel" if par > 0 else "default")
    if mode not in _PARALLEL_MODES:
        raise DispatchError(
            f"unknown parallel_mode {mode!r} "
            f"(choose from {', '.join(_PARALLEL_MODES)})")
    if par > 64 or par < 0:
        raise DispatchError("parallel must be between 0 and 64")
    if mode == "sequential" and par > 0:
        raise DispatchError(
            "parallel_mode=sequential conflicts with parallel>0 — "
            "pick one")
    if mode == "parallel" and par < 1:
        raise DispatchError(
            "parallel_mode=parallel requires parallel >= 1")
    if kind == "cypress" and (mode == "parallel" or par > 0):
        raise DispatchError(
            "parallel is not supported for cypress — Cypress has no "
            "local in-process parallelism and Cypress Cloud is not "
            "required; use spec partitioning (unit run) instead")
    if kind == "kre" and (mode == "parallel" or par > 0):
        raise DispatchError("parallel is not supported for KRE runs")
    rc["parallel_mode"] = mode
    rc["parallel"] = par
    # Shard pair: both-or-neither, index within 1..total, playwright
    # only. A shard assignment is NOT created by a worker count.
    si, st = rc.get("shard_index"), rc.get("shard_total")
    if bool(si) != bool(st):
        raise DispatchError(
            "shard_index and shard_total must be given together")
    if si:
        try:
            si, st = int(si), int(st)
        except (TypeError, ValueError):
            raise DispatchError("shard index/total must be integers")
        if st < 1 or not (1 <= si <= st):
            raise DispatchError(
                f"invalid shard {si}/{st} — index must be within "
                "1..total")
        if kind != "playwright":
            raise DispatchError(
                f"--shard is playwright-only (runtime kind {kind!r}); "
                f"partition other frameworks via unit runs")
        rc["shard_index"], rc["shard_total"] = si, st


def create_execution(username: str, suite: Suite = None,
                     binding: GitBinding = None,
                     plan_id: str = "",
                     release_id: str = None,
                     source_sha: str = "", run_config: dict = None,
                     extra_snapshot: dict = None,
                     schedule_id: str = "",
                     source_item=None,
                     role: str = "", parent_id: str = "",
                     unit_index=None, unit_key: str = "",
                     commit: bool = True) -> Execution:
    """Create an immutable execution snapshot (§4, A07). Exactly one
    target: a TO suite OR a repository DiscoveredItem — never both,
    never neither. Repo-def runs snapshot their durable identity
    (binding+kind+path+sha) so history survives inventory replacement."""
    if bool(suite) == bool(source_item):
        raise DispatchError(
            "exactly one execution target required: suite or "
            "source item")

    project_id = (suite.project_id if suite else source_item.project_id)
    if release_id:
        rel = db.session.get(Release, release_id)
        if rel is None or rel.project_id != project_id:
            raise DispatchError(
                "selected release is unavailable or out of scope for "
                "this project")

    rc = dict(run_config or {})
    if source_item is not None:
        rc.setdefault("suite_path", source_item.path)
        rc.setdefault("collection", source_item.kind == "collection")
    _validate_parallel(rc, _snapshot_runtime_kind(rc))
    snapshot = {
        "plan_id": plan_id,
        "plan_run_id": (extra_snapshot or {}).get("plan_run_id", ""),
        "source_target": ({
            "binding_id": source_item.binding_id,
            "item_id": source_item.id,
            "kind": source_item.kind,
            "path": source_item.path,
            "name": source_item.name,
            "sha": source_item.sha,
        } if source_item is not None else None),
        "suite_id": suite.id if suite else "",
        "suite_name": (source_item.name if source_item is not None
                       else suite.name),
        "suite_version": suite.version if suite else 0,
        "suite_hash": suite.content_hash if suite else "",
        "entries": ([
            {
                "order": e.order,
                "case_ref": e.case_ref,
                "enabled": e.enabled,
                "config": json.loads(e.config_json or "{}"),
            }
            for e in suite.entries
        ] if suite else []),
        "run_config": {
            "profile": str(rc.get("profile") or "default")[:128],
            # KRE default applies to KRE runtimes only — node runtimes
            # record the framework browser ("" = framework default).
            "browser": (str(rc.get("browser")
                            or "Chrome (headless)")[:128]
                        if _snapshot_runtime_kind(rc) == "kre"
                        else str(rc.get("browser") or "")[:128]),
            # Remote WebDriver (opt-in): browser runs on the WebDriver
            # service, not the KRE host — overrides -browserType=.
            "remote_webdriver_url":
                str(rc.get("remote_webdriver_url") or "")[:512],
            "remote_webdriver_type":
                str(rc.get("remote_webdriver_type") or "")[:64],
            # Requested execution target ("" = auto pick at dispatch).
            "requested_runner": str(rc.get("runner_id") or "")[:32],
            "requested_runtime": str(rc.get("runtime_id") or "")[:32],
            "collection": bool(rc.get("collection")),
            "suite_path": str(rc.get("suite_path") or "")[:512],
            # Robot Framework selection knobs (kind=robot only —
            # ignored by other runtime kinds).
            "robot_suite": str(rc.get("robot_suite") or "")[:256],
            "robot_test": str(rc.get("robot_test") or "")[:256],
            "robot_include": str(rc.get("robot_include") or "")[:512],
            "robot_exclude": str(rc.get("robot_exclude") or "")[:512],
            # pytest selection knobs (kind=pytest only).
            "pytest_keyword": str(rc.get("pytest_keyword") or "")[:256],
            "pytest_markers": str(rc.get("pytest_markers") or "")[:256],
            # Running-time limit in seconds (process-launch clock,
            # not queue time); absent/0 = adapter default 3600.
            "timeout_seconds":
                int(rc.get("timeout_seconds") or 0),
            # Framework-local parallelism (2.6): validated in
            # _validate_parallel() — mode is explicit:
            #   default    = no flags; the framework decides
            #   sequential = forced single process (playwright
            #                --workers 1; others already sequential)
            #   parallel   = playwright --workers N, pytest -n N
            #                (xdist), robot pabot --processes N
            "parallel_mode": rc.get("parallel_mode") or "default",
            "parallel": int(rc.get("parallel") or 0),
            # Playwright shard i/N — assignment only, never implied
            # by a worker count. Validated pairwise.
            "shard_index": int(rc.get("shard_index") or 0),
            "shard_total": int(rc.get("shard_total") or 0),
            # Requested version-gated KRE options — applied only if the
            # detected runtime version supports them (adapters/kre.py).
            "kre_options": {
                str(k)[:32]: str(v)[:64]
                for k, v in dict(rc.get("kre_options") or {}).items()},
            # Operator acknowledgement for gated-option drops — opt-in,
            # audited in kre-gating.log when used.
            "kre_options_allow_drop":
                bool(rc.get("kre_options_allow_drop")),
        },
        "binding_id": binding.id if binding else None,
        "source_sha": source_sha,
        "created_by": username,
    }
    # disabled runtime connections must not launch — selectors
    # filter them, but a direct submission is rejected here too.
    rt_id = (run_config or {}).get("runtime_id")
    if rt_id:
        from .execution import RuntimeConnection as _RC
        _rt = db.session.get(_RC, rt_id)
        if _rt is None or not _rt.enabled:
            raise DispatchError(
                "runtime connection unavailable or disabled")
        # worker_label runtimes are bindings to a removed worker
        # transport. They fail VISIBLY here rather than silently
        # rerouting: the runtime must be re-registered against a
        # Buildbot target.
        if _rt.worker_label:
            raise DispatchError(
                "remote execution is not configured/available — "
                "this installation is bound to a removed worker "
                "transport; re-register it against a Buildbot target")
        # Buildbot targets: the runner's target_type picks the
        # orchestrated transport.
        elif _buildbot_configured() and _rt.runner is not None \
                and _rt.runner.target_type in (
                    "local", "buildbot-worker", "ssh"):
            snapshot["dispatch_transport"] = "buildbot"
    # Every evidence row carries the exact deployed code it ran on —
    # module path + git state catches stale-tree mismatches where the
    # running process is not the tree acceptance believes was tested.
    from . import deploy_info
    snapshot["deployed_rev"] = deploy_info.deployed_revision()
    snapshot.update(extra_snapshot or {})
    execution = Execution(
        project_id=project_id,
        suite_id=suite.id if suite else None,
        release_id=release_id or None,
        source_item_id=source_item.id if source_item else None,
        binding_id=(binding.id if binding else
                    (source_item.binding_id if source_item else None)),
        snapshot_json=json.dumps(snapshot),
        source_sha=source_sha,
        # source_ref = the SELECTED ref (selector), distinct from the
        # resolved source_sha. environment = effective run_config value.
        source_ref=((binding.ref or None) if binding else None),
        environment=(rc.get("environment") or None),
        created_by=username,
        schedule_id=schedule_id or None,
        role=role, parent_id=parent_id or None,
        unit_index=unit_index, unit_key=(unit_key or "")[:128],
    )
    db.session.add(execution)
    try:
        qd = int(rc.get("queue_timeout_seconds") or 0)
        if qd > 0:
            execution.queue_deadline_at = utcnow() + timedelta(
                seconds=qd)
    except (TypeError, ValueError):
        pass
    db.session.flush()  # id assigned before outbox references it

    # Dispatch route: Buildbot is the sole orchestrator — persisted in
    # the same transaction as the execution (§7). The transport is
    # stamped so capacity-blocked QUEUED executions are redrivable —
    # their one-shot event is consumed even when no runner was
    # eligible at that tick. A "parent" execution owns no process —
    # it aggregates its unit children, never dispatches itself.
    if role != "parent":
        # Dispatch route: the orchestrator claim is persisted in the
        # same transaction as the execution (§7) — unless the caller
        # is composing a larger atomic unit (create_unit_run).
        kind = "orchestrator-dispatch"
        if "dispatch_transport" not in snapshot:
            snapshot["dispatch_transport"] = "buildbot"
            execution.snapshot_json = json.dumps(snapshot)
        db.session.add(OutboxEvent(
            kind=kind,
            payload_json=json.dumps({"execution_id": execution.id})))
    if commit:
        db.session.commit()
    return execution


def create_unit_run(username: str, units: list[dict], **kw) -> tuple:
    """Partitioned parallel run: a durable "parent" execution plus N
    identifiable "unit" children. Each unit dict is the frozen child
    assignment — {"index": i, "key": "shard 1/2", "shard": "1/2",
    "specs": [...]} — recorded in the child's immutable snapshot.
    Children dispatch independently through the normal capacity
    pipeline with isolated workspaces/report dirs; the parent
    aggregates their outcomes (aggregate_parent_runs) and propagates
    cancellation (request_cancel)."""
    if not units:
        raise DispatchError(
            "unit run requires at least one unit — an intentionally "
            "empty selection is a config error, not a zero-unit run")
    # ONE transaction: parent + every unit child + the expected count.
    # A crash mid-creation leaves no parent row at all — a parent with
    # unit_count unset can never exist, so a partially formed unit set
    # can never aggregate as if complete.
    parent = create_execution(username, role="parent",
                              commit=False, **kw)
    exs = []
    for u in units:
        i = int(u["index"])
        exs.append(create_execution(
            username, role="unit", parent_id=parent.id, unit_index=i,
            unit_key=str(u.get("key") or f"unit {i + 1}"),
            commit=False,
            extra_snapshot={"parent_id": parent.id,
                            "unit": u}, **kw))
    parent.unit_count = len(exs)
    db.session.commit()
    return parent, exs


_UNIT_TERMINAL = (ExecutionState.COMPLETED, ExecutionState.FAILED,
                  ExecutionState.CANCELED, ExecutionState.UNKNOWN)


# ---------- partitioned runs (Stage 4) ----------

def discover_cypress_specs(binding, sha: str = "",
                           spec_pattern: str = "",
                           exclude_pattern: str = "",
                           selection=()) -> list:
    """Resolve the Cypress spec manifest at the selected revision —
    the frozen list a partitioned run is built from. Discovery is
    read-only on the bound worktree; a mismatched/stale revision is
    an explicit error, never a silent re-scan of a different tree."""
    import fnmatch
    base = Path(binding.worktree_path or "")
    if binding.subdirectory:
        base = base / binding.subdirectory
    if not base.is_dir():
        raise DispatchError(
            "bound worktree is missing — sync the binding before "
            "partitioning")
    if sha and sha != (binding.last_synced_sha or ""):
        raise DispatchError(
            f"spec manifest cannot be resolved at {sha[:12]} — the "
            f"worktree is at {(binding.last_synced_sha or '?')[:12]}; "
            "sync the binding so discovery matches the frozen "
            "revision")

    pat = spec_pattern.strip()
    excl_from_cfg = ""
    if not pat:
        # Honor the project's OWN effective configuration: read
        # specPattern/excludeSpecPattern out of cypress.config.*.
        # Simple string literals only — arrays, functions, env-
        # dependent config are rejected explicitly, never replaced by
        # a guessed fallback manifest.
        cfg_file = next(
            (f for f in ("cypress.config.js", "cypress.config.ts",
                         "cypress.config.mjs", "cypress.config.cjs")
             if (base / f).is_file()), None)
        if cfg_file:
            import re as _re
            txt = (base / cfg_file).read_text(errors="replace")
            m = _re.search(r"specPattern\s*:\s*[\"\']([^\"\']+)[\"\']",
                           txt)
            if m:
                pat = m.group(1)
            elif _re.search(r"specPattern\s*:", txt):
                raise DispatchError(
                    f"{cfg_file} declares a non-literal specPattern "
                    "(array/expression) — unsupported for automatic "
                    "discovery; pass an explicit spec pattern")
            m = _re.search(
                r"excludeSpecPattern\s*:\s*[\"\']([^\"\']+)[\"\']",
                txt)
            if m and not exclude_pattern.strip():
                excl_from_cfg = m.group(1)
        pat = pat or "cypress/e2e/**/*.cy.*"

    def _match(rel: str, glob_pat: str) -> bool:
        # fnmatch has no recursive-glob semantics: `**/` must also
        # match ZERO directories (cypress glob convention).
        if fnmatch.fnmatch(rel, glob_pat):
            return True
        return "**/" in glob_pat and fnmatch.fnmatch(
            rel, glob_pat.replace("**/", ""))

    specs = sorted(str(p.relative_to(base)) for p in base.glob(
        "**/*") if p.is_file() and _match(
            str(p.relative_to(base)), pat)
        and not any(part.startswith(".") for part in
                    p.relative_to(base).parts))
    excl = [x.strip() for x in (
        exclude_pattern.strip() or excl_from_cfg).split(",")
        if x.strip()]
    for x in excl:
        specs = [s for s in specs if not _match(s, x)]
    if selection:
        sel = set(selection)
        missing = sel - set(specs)
        if missing:
            raise DispatchError(
                "selected specs are not in the resolved manifest: "
                + ", ".join(sorted(missing)[:5]))
        specs = [s for s in specs if s in sel]
    if not specs:
        raise DispatchError(
            "spec discovery resolved an empty manifest — check the "
            "spec pattern, exclusions and selection")
    # Invocation safety: Cypress --spec takes a comma-separated list
    # inside ONE argv element — a literal comma or newline in a path
    # would corrupt the invocation, so those paths are rejected
    # explicitly rather than mislaunched. Spaces and POSIX
    # separators are safe inside a single argv.
    unsafe = [sp for sp in specs
              if "," in sp or "\n" in sp or "\r" in sp
              or "\x00" in sp]
    if unsafe:
        raise DispatchError(
            "spec paths cannot be represented safely in a --spec "
            "list (comma/control characters): "
            + ", ".join(unsafe[:3]))
    return specs


def partition_units(items: list, n: int, key_prefix: str,
                    payload_key: str) -> list:
    """Contiguous deterministic split — every item assigned to
    exactly one unit, assignments frozen in each child's snapshot.
    Requesting more units than items is an explicit error, not a
    silent empty unit."""
    if n < 1:
        raise DispatchError("unit count must be >= 1")
    if n > len(items):
        raise DispatchError(
            f"cannot split {len(items)} item(s) into {n} units — "
            f"at most {len(items)} non-empty units")
    base, extra = divmod(len(items), n)
    units, off = [], 0
    for i in range(n):
        take = base + (1 if i < extra else 0)
        part = items[off:off + take]
        off += take
        units.append({"index": i, "key": f"{key_prefix} {i + 1}/{n}",
                      payload_key: part})
    return units


def playwright_shard_units(n: int) -> list:
    """N playwright shard units — explicit i/N assignments; the
    framework does its own internal split, we freeze WHICH shard each
    unit owns."""
    if n < 2:
        raise DispatchError("sharded run requires at least 2 shards")
    if n > 64:
        raise DispatchError("shard count must be <= 64")
    return [{"index": i, "key": f"shard {i + 1}/{n}",
             "shard": f"{i + 1}/{n}"} for i in range(n)]


def aggregate_parent_runs() -> int:
    """Recompute parent executions from their unit children — run
    every tick so the rollup is restart-safe and idempotent (a pure
    recompute, not a terminal-transition hook). Rules: no parent
    success while any child is unfinished or unsuccessful; a parent
    cancel propagates to every non-terminal child, including queued
    ones; uncertain children make the parent INCOMPLETE, never green."""
    n = 0
    for p in Execution.query.filter(
            Execution.role == "parent",
            Execution.state.in_([ExecutionState.QUEUED,
                                 ExecutionState.RUNNING,
                                 ExecutionState.CANCEL_REQUESTED])).all():
        if p.unit_count is None:
            continue   # still forming — count is committed with the
        # full child set; a parent without it is incomplete, never green
        children = Execution.query.filter_by(parent_id=p.id).all()
        if not children or len(children) < p.unit_count:
            continue   # children not all persisted yet
        cancel_was_requested = (
            p.state == ExecutionState.CANCEL_REQUESTED)
        if cancel_was_requested and not all(
                c.state in _UNIT_TERMINAL for c in children):
            for c in children:
                if c.state not in _UNIT_TERMINAL \
                        and c.state != ExecutionState.CANCEL_REQUESTED:
                    request_cancel(c.id, "system",
                                   "parent run cancelled")
            continue   # next tick rolls up once children are terminal
        if p.state == ExecutionState.QUEUED and any(
                c.state != ExecutionState.QUEUED for c in children):
            p.state = ExecutionState.RUNNING
            p.started_at = p.started_at or utcnow()
        if not all(c.state in _UNIT_TERMINAL for c in children):
            db.session.commit()
            n += 1
            continue
        # All children terminal — final rollup, exactly once.
        if any(c.outcome == Outcome.FAILED for c in children):
            p.outcome = Outcome.FAILED
        elif any(c.outcome in (Outcome.INCOMPLETE, Outcome.NONE)
                 or c.state in (ExecutionState.FAILED,
                                ExecutionState.UNKNOWN)
                 for c in children):
            p.outcome = Outcome.INCOMPLETE
        else:
            p.outcome = Outcome.PASSED
        p.state = ExecutionState.COMPLETED
        if cancel_was_requested or all(
                c.state == ExecutionState.CANCELED for c in children):
            p.state = ExecutionState.CANCELED
            p.outcome = Outcome.INCOMPLETE
        p.finished_at = p.finished_at or utcnow()
        infra = [c.id[:8] for c in children
                 if c.state in (ExecutionState.FAILED,
                                ExecutionState.UNKNOWN)]
        if infra:
            p.error_kind = "infrastructure"
            p.error_text = (
                "unit execution(s) ended unresolved: "
                + ", ".join(infra))
        db.session.commit()
        n += 1
    return n


def queue_plan_executions(username: str, plan: Plan,
                          binding: GitBinding = None,
                          source_sha: str = "",
                          entries: list = None,
                          plan_run_id: str = "",
                          schedule_id: str = "",
                          run_config: dict = None) -> tuple:
    """Queue one execution per plan entry under a shared plan_run_id —
    the durable handle that lets summaries, stop-on-failure and
    rerun-failed group attempts of the same run (§10)."""
    run_id = plan_run_id or new_plan_run_id()
    exs = []
    for entry in (entries if entries is not None else plan.entries):
        exs.append(create_execution(
            username, entry.suite, binding=binding, source_sha=source_sha,
            plan_id=plan.id, run_config=run_config,
            extra_snapshot={"plan_run_id": run_id},
            schedule_id=schedule_id))
    return run_id, exs


def new_plan_run_id() -> str:
    import uuid
    return uuid.uuid4().hex


def retry_execution(username: str, ex: Execution) -> Execution:
    """Queue the NEXT attempt as a new immutable execution (§10):
    retry_of links the chain, attempt increments, plan_run_id is kept so
    plan aggregation counts the latest attempt — never a rerun/mutation
    of the original row."""
    terminal = (ExecutionState.COMPLETED, ExecutionState.FAILED,
                ExecutionState.CANCELED, ExecutionState.UNKNOWN)
    if ex.state not in terminal:
        raise DispatchError(
            "only terminal executions can be retried — cancel first")
    snap = ex.snapshot()
    binding = (db.session.get(GitBinding, ex.binding_id)
               if ex.binding_id else None)
    source_item = None
    if ex.suite is None:
        source_item = (db.session.get(DiscoveredItem, ex.source_item_id)
                       if ex.source_item_id else None)
        if source_item is None:
            raise DispatchError(
                "the repository item this execution ran is no longer "
                "present in discovery — re-run it from the Discovered "
                "page instead of retrying")
    rc = dict(snap.get("run_config") or {})
    # The snapshot stores normalized keys; create_execution consumes
    # the raw request keys — translate back so the retried execution
    # keeps its requested runner/runtime (and thus its transport).
    if rc.get("requested_runtime") and not rc.get("runtime_id"):
        rc["runtime_id"] = rc["requested_runtime"]
    if rc.get("requested_runner") and not rc.get("runner_id"):
        rc["runner_id"] = rc["requested_runner"]
    new_ex = create_execution(
        username, ex.suite, binding=binding, source_sha=ex.source_sha,
        source_item=source_item,
        plan_id=snap.get("plan_id") or "",
        run_config=rc,
        extra_snapshot={
            "plan_run_id": snap.get("plan_run_id") or "",
            "retry_of": ex.id,
            "attempt": int(snap.get("attempt") or 1) + 1,
        })
    record_audit(username, "execution.retry_queued",
                 f"{new_ex.id} (retry of {ex.id}, attempt "
                 f"{int(snap.get('attempt') or 1) + 1})")
    return new_ex


def _buildbot_configured() -> bool:
    """Buildbot is the sole orchestrator — 'configured' just means the
    loopback REST endpoint is known (installer/provisioner sets it)."""
    return bool(settings_mod.get("buildbot_endpoint"))


def _ssh_remote_cancel(ex: Execution) -> "bool | None":
    """Kill the detached remote process GROUP of an agentless-SSH run.

    The spec (host/user/key materialized at dispatch) lives in
    run-specs/<exid>.json; the remote pgid is <workdir>/run.pid —
    written by to_ssh_exec's setsid launch. Returns True only when
    remote death is verified (or nothing was ever launched), False on
    kill-attempted-but-unconfirmed, None when no SSH spec exists."""
    spec_path = buildbot_cfg.run_specs_dir() / f"{ex.id}.json"
    if not spec_path.is_file():
        return None
    try:
        spec = json.loads(spec_path.read_text())
        import base64
        import hashlib
        import shlex
        import socket as _sock
        import paramiko
        pkey = None
        for cls in (paramiko.Ed25519Key, paramiko.RSAKey,
                    paramiko.ECDSAKey, paramiko.DSSKey):
            try:
                pkey = cls.from_private_key_file(spec["key_file"])
                break
            except paramiko.SSHException:
                continue
        if pkey is None:
            return False
        wd = spec["remote_workdir"]
        qwd = shlex.quote(wd)
        transport = paramiko.Transport(_sock.create_connection(
            (spec["host"], int(spec.get("port") or 22)), timeout=15))
        try:
            transport.start_client(timeout=15)
            approved = spec.get("host_key_fingerprint") or ""
            if approved:
                rk = transport.get_remote_server_key()
                fp = ("SHA256:" + base64.b64encode(
                    hashlib.sha256(rk.asbytes()).digest()
                ).decode().rstrip("="))
                if fp != approved:
                    return False  # never authenticate to the wrong host
            transport.auth_publickey(spec["username"], pkey)

            def _run(cmd, timeout=15):
                chan = transport.open_session(timeout=timeout)
                chan.settimeout(timeout)
                chan.exec_command(cmd)
                out = chan.makefile("rb").read()
                err = chan.makefile_stderr("rb").read()
                return chan.recv_exit_status(), (
                    out + err).decode(errors="replace").strip()

            _, pid = _run(f"cat {qwd}/run.pid 2>/dev/null || true")
            if not pid.isdigit():
                return True  # never launched — nothing to kill
            _run(f"kill -TERM -{pid} 2>/dev/null; sleep 1; "
                 f"kill -KILL -{pid} 2>/dev/null; true", timeout=20)
            time.sleep(1)
            _, out = _run(f"kill -0 {pid} 2>/dev/null "
                          "&& echo alive || echo dead")
            return out == "dead"
        finally:
            transport.close()
    except Exception:
        return False


def request_cancel(execution_id: str, username: str, reason: str = "") -> bool:
    """Cancel targets the actual process tree (§6); stays CANCEL_REQUESTED
    until termination is acknowledged — uncertainty is surfaced."""
    ex = db.session.get(Execution, execution_id)
    if ex is None:
        return False
    if ex.state in (ExecutionState.COMPLETED, ExecutionState.FAILED,
                    ExecutionState.CANCELED):
        return False
    if ex.dispatch_id:
        try:
            transport = ex.snapshot().get("dispatch_transport")
            if transport == "buildbot":
                # One call covers queued AND running (verified 4.3.0):
                # a claimed buildrequest's cancel stops the build and
                # the worker kills the remote process tree.
                BuildbotAdapter.from_settings().cancel(_bb_brid(ex))
        except Exception:
            pass  # flag + lease expiry still bound the run
    # Agentless-SSH runs detach via setsid and the buildbot worker
    # SIGKILLs the dispatcher step (to_ssh_exec's SIGTERM handler never
    # runs) — the remote group survives unless the portal kills it
    # itself. CANCELED is only honest after remote death is confirmed.
    ssh_kill = _ssh_remote_cancel(ex)
    if ssh_kill is True:
        reason = (reason + " " if reason else "") \
            + "[ssh remote group terminated]"
    elif ssh_kill is False:
        reason = (reason + " " if reason else "") \
            + "[ssh remote kill UNCONFIRMED]"
    ex.state = ExecutionState.CANCEL_REQUESTED
    ex.cancel_reason = reason[:256]
    db.session.commit()
    record_audit(username, "execution.cancel_requested", execution_id)
    # Parent cancellation owns the whole unit tree: every non-terminal
    # child — running AND queued — is cancelled (queued children are
    # blocked from ever launching). aggregate_parent_runs repeats this
    # sweep each tick, so propagation is restart-safe.
    if ex.role == "parent":
        for c in Execution.query.filter_by(parent_id=ex.id).all():
            if c.state not in _UNIT_TERMINAL:
                request_cancel(c.id, username, "parent run cancelled")
    return True


# ---------- runner selection (§6) ----------

def capacity_limit(runner: Runner) -> int:
    """Dispatch ceiling = min(hardware slots, declared licence seats).
    Unset licence means unconstrained, never an assumed seat count."""
    if runner.license_slots:
        return min(runner.max_slots, runner.license_slots)
    return runner.max_slots


def effective_slots(runner: Runner) -> int:
    """Free capacity = capacity_limit - currently leased executions.
    A floating KRE licence is consumed per concurrent SESSION — the
    limit here prevents dispatching more KRE processes than either the
    hardware or the licence allowance permits (never multiplied)."""
    active = Execution.query.filter(
        Execution.runner_id == runner.id,
        Execution.state.in_(_ACTIVE_STATES),
    ).count()
    return max(0, capacity_limit(runner) - active)


def pick_runner(execution: Execution) -> Runner | None:
    """Enabled, non-draining runners with project scope and free slots —
    local and remote modes both eligible. PB workers known-unreachable
    are skipped: a healthy alternative with capacity should win (the
    health field is refreshed by the reconcile sweep each tick;
    explicit picks are unaffected — master queues honestly)."""
    runners = Runner.query.filter_by(enabled=True, draining=False).all()
    for runner in runners:
        allowed = json.loads(runner.allowed_projects or "[]")
        if allowed and execution.project_id not in allowed:
            continue
        if runner.target_type == "buildbot-worker" \
                and runner.health == "unreachable":
            continue
        if effective_slots(runner) <= 0:
            continue
        return runner
    return None


def pick_runtime(execution: Execution, runner: Runner):
    for rt in runner.runtimes:
        if not rt.enabled:
            continue
        allowed = json.loads(rt.allowed_projects or "[]")
        if allowed and execution.project_id not in allowed:
            continue
        return rt
    return None


# ---------- worker ----------

def _cancel_check(execution_id: str):
    """Polls the durable cancel flag — the DB is the only authority, so a
    request-handler cancel reaches a worker in another process."""
    def check() -> bool:
        state = db.session.execute(
            db.text("SELECT state FROM executions WHERE id = :i"),
            {"i": execution_id},
        ).scalar()
        # db.Enum persists member names, not values.
        return state == ExecutionState.CANCEL_REQUESTED.name
    return check


class _DbCancelEvent:
    def __init__(self, execution_id):
        self.execution_id = execution_id
        self._check = _cancel_check(execution_id)

    def is_set(self):
        try:
            return self._check()
        except Exception:
            return False


def _transition(ex: Execution, state: ExecutionState) -> None:
    ex.state = state
    ex.heartbeat_at = utcnow()
    db.session.commit()


# States that consume a runner's capacity. UNKNOWN is included
# deliberately — an uncertain execution may still have a live process;
# it must not silently free a slot until an operator resolves it.
_ACTIVE_STATES = [
    ExecutionState.DISPATCHED, ExecutionState.PREPARING,
    ExecutionState.RUNNING, ExecutionState.COLLECTING,
    ExecutionState.NORMALIZING, ExecutionState.CANCEL_REQUESTED,
    ExecutionState.UNKNOWN,
]


def _active_count_subquery(runner: Runner, exclude_id):
    return (db.session.query(db.func.count(Execution.id))
            .filter(Execution.runner_id == runner.id,
                    Execution.id != exclude_id,
                    Execution.state.in_(_ACTIVE_STATES))
            .scalar_subquery())


def _pick_slot(ex: Execution, runner: Runner) -> int:
    """Free slot-builder index for this runner. A (builder, worker)
    pair serializes builds, so a runner's max_slots concurrency needs
    one builder per slot — we pick the lowest slot no other ACTIVE
    execution on this runner holds (hash offset spreads collisions;
    a collision only queues inside the slot's builder — never lost)."""
    slots = buildbot_cfg.slot_count(runner)
    if slots <= 1:
        return 0
    used = {s for (s,) in db.session.query(Execution.bb_slot).filter(
        Execution.runner_id == runner.id,
        Execution.id != ex.id,
        Execution.state.in_(_ACTIVE_STATES),
        Execution.bb_slot.isnot(None))}
    start = int(ex.id[:8], 16) % slots
    for k in range(slots):
        cand = (start + k) % slots
        if cand not in used:
            return cand
    return start


def requested_procs(ex: Execution) -> int:
    """Framework processes this run requests — the charge against
    runners.max_procs. It counts REQUESTED concurrency only:

      sequential        -> 1
      parallel N        -> N   (playwright --workers / pytest -n /
                                pabot --processes)
      framework-default -> kind-dependent: playwright auto-scales to
                           ~cpu/2 which is UNBOUNDED — charged
                           UNBOUNDED so a configured budget can never
                           be bypassed through unknown concurrency;
                           pytest/robot/cypress default is one process.
      parent            -> 0   (an aggregation parent owns no process;
                           only its unit children are charged)
      unit shard        -> its own requested count (a playwright shard
                           still launches --workers internally)

    This is NOT a cap on operating-system processes — node/npx/pytest
    helpers and browser subprocesses are outside the count."""
    if getattr(ex, "role", "") == "parent":
        return 0
    try:
        snap = ex.snapshot()
        rc = snap.get("run_config") or {}
    except Exception:
        return 1
    mode = rc.get("parallel_mode") or "default"
    if mode == "parallel":
        return max(1, int(rc.get("parallel") or 1))
    if mode == "default":
        # Kind resolves from the requested runtime (pre-assignment the
        # execution's runtime_id is still empty).
        rt = db.session.get(
            RuntimeConnection, rc.get("requested_runtime") or "")
        kind = getattr(rt, "runtime_kind", "") or ""
        if not kind and ex.runtime_id:
            rt = db.session.get(RuntimeConnection, ex.runtime_id)
            kind = getattr(rt, "runtime_kind", "") or ""
        if kind == "playwright":
            return UNBOUNDED_PROCS
    return 1


UNBOUNDED_PROCS = 1 << 20





def _active_procs_subquery(runner: Runner, exclude_id):
    """Sum of requested procs across active executions on the runner.
    Stored per execution in snapshot_json — SQLite json_extract reads
    it directly inside the CAS."""
    from sqlalchemy import func
    procs = func.coalesce(func.json_extract(
        Execution.snapshot_json,
        "$.run_config.parallel"), 0)
    mode = func.coalesce(func.json_extract(
        Execution.snapshot_json,
        "$.run_config.parallel_mode"), "default")
    eff = db.case((mode == "parallel", procs), else_=1)
    return (db.session.query(func.coalesce(func.sum(eff), 0))
            .filter(Execution.runner_id == runner.id,
                    Execution.id != exclude_id,
                    Execution.state.in_(_ACTIVE_STATES))
            .scalar_subquery())


def procs_within_budget(runner: Runner, ex: Execution) -> bool:
    """Combined-capacity rule: job slots AND the process budget are
    independent ceilings — an admitted job must not bypass the
    intended process budget via its own parallel setting."""
    if not runner.max_procs:
        return True
    active = db.session.query(db.func.coalesce(
        db.func.sum(db.case((
            db.func.coalesce(db.func.json_extract(
                Execution.snapshot_json,
                "$.run_config.parallel_mode"), "default")
            == "parallel",
            db.func.coalesce(db.func.json_extract(
                Execution.snapshot_json,
                "$.run_config.parallel"), 0)), else_=1)), 0)
    ).filter(
        Execution.runner_id == runner.id,
        Execution.id != ex.id,
        Execution.state.in_(_ACTIVE_STATES)).scalar()
    return int(active or 0) + requested_procs(ex) <= runner.max_procs


def _runner_cap(runner: Runner) -> int:
    return capacity_limit(runner)


def _assign_runner(ex: Execution, runner: Runner,
                   runtime: RuntimeConnection) -> bool:
    """Atomic QUEUED -> DISPATCHED assignment: runner/runtime/lease
    fields and the capacity check land in ONE conditional UPDATE, so
    two dispatchers can neither double-assign nor over-subscribe a
    runner's slots. Both the local path (then claims PREPARING) and
    the remote/agent path (agent picks up DISPATCHED) consume the slot
    here — capacity is reserved from assignment, not launch."""
    conds = [
        Execution.id == ex.id,
        Execution.state == ExecutionState.QUEUED,
        _active_count_subquery(runner, ex.id) < _runner_cap(runner)]
    # Combined capacity: the new job's requested procs plus the
    # already-active proc sum must fit the process budget — an
    # admitted job can never bypass the intended budget through its
    # own parallel setting. NULL max_procs = job slots only.
    if runner.max_procs:
        conds.append(
            _active_procs_subquery(runner, ex.id)
            + requested_procs(ex) <= runner.max_procs)
    n = db.session.query(Execution).filter(*conds).update(
        {"state": ExecutionState.DISPATCHED,
              "runner_id": runner.id,
              "runtime_id": runtime.id,
              "lease_expires_at": utcnow() + timedelta(
                  seconds=LEASE_SECONDS),
              "heartbeat_at": utcnow(),
              "wait_reason": ""},
             synchronize_session=False)
    if n:
        # Durable ownership ledger — written in the SAME transaction
        # as the assignment: the reservation exists before the run
        # launches, keyed to this execution, released only on a
        # confirmed terminal state (UNKNOWN keeps it).
        db.session.add(ResourceReservation(
            execution_id=ex.id, runner_id=runner.id,
            resource_kind="slot", quantity=1,
            acquired_at=utcnow()))
        procs = requested_procs(ex)
        if procs:
            db.session.add(ResourceReservation(
                execution_id=ex.id, runner_id=runner.id,
                resource_kind="procs", quantity=procs,
                acquired_at=utcnow()))
    db.session.commit()
    if n:
        db.session.refresh(ex)
    else:
        db.session.rollback()
        ex.wait_reason = _capacity_block_reason(runner, ex)
        db.session.commit()
    return bool(n)


def _capacity_block_reason(runner: Runner, ex: Execution) -> str:
    """Why this runner could not admit the execution — the actionable
    wait reason surfaced instead of a silent QUEUED stall."""
    active = Execution.query.filter(
        Execution.runner_id == runner.id,
        Execution.id != ex.id,
        Execution.state.in_(_ACTIVE_STATES)).count()
    cap = capacity_limit(runner)
    if active >= cap:
        lic = (f" (licence seats {runner.license_slots})"
               if runner.license_slots else "")
        return (f"capacity: runner '{runner.name}' job slots full "
                f"({active}/{cap} in use{lic})")
    if runner.max_procs:
        used = int(db.session.execute(
            db.select(_active_procs_subquery(runner, ex.id))
        ).scalar() or 0)
        need = requested_procs(ex)
        if used + need > runner.max_procs:
            free = max(0, runner.max_procs - used)
            return (f"capacity: '{runner.name}' process budget "
                    f"{used}/{runner.max_procs} in use; this run "
                    f"requests {need} (only {free} free)")
    return f"capacity: assignment race on '{runner.name}' — retrying"


def _claim_for_start(ex: Execution,
                     runner: Runner | None = None) -> bool:
    """Atomic QUEUED|DISPATCHED -> PREPARING claim — the precise launch
    boundary AND the capacity check, in one UPDATE. Below it the
    execution is cancel-scope; at/above it, started work runs (or takes
    CANCEL_REQUESTED). When a runner is given, the same conditional
    UPDATE also requires the runner's active count < its effective
    capacity — a slot is consumed atomically with the launch claim, so
    two dispatchers racing one remaining slot cannot both win."""
    q = db.session.query(Execution).filter(
        Execution.id == ex.id,
        Execution.state.in_([ExecutionState.QUEUED,
                             ExecutionState.DISPATCHED]))
    if runner is not None:
        q = q.filter(_active_count_subquery(runner, ex.id)
                     < _runner_cap(runner))
    n = q.update({"state": ExecutionState.PREPARING,
                  "heartbeat_at": utcnow()},
                 synchronize_session=False)
    db.session.commit()
    if n:
        db.session.refresh(ex)
    else:
        db.session.rollback()
    return bool(n)


def _select_runner_runtime(ex: Execution):
    """Resolve the (runner, runtime) pair honoring an explicit
    run_config pick: an unavailable explicit pick is an error the
    caller must surface — never a silent retarget. Returns
    (runner, runtime, error): error non-empty -> fail the execution;
    runner None -> stay queued and retry next tick."""
    rc = ex.snapshot().get("run_config") or {}
    req_runner = rc.get("requested_runner") or ""
    req_runtime = rc.get("requested_runtime") or ""
    if req_runner:
        runner = db.session.get(Runner, req_runner)
        rn_allowed = (json.loads(runner.allowed_projects or "[]")
                      if runner else [])
        if (not runner or not runner.enabled or runner.draining
                or (rn_allowed and ex.project_id not in rn_allowed)):
            return None, None, (
                f"explicitly selected runner {req_runner[:12]}… is "
                "unavailable or out of scope for this project — no "
                "fallback to another target")
    elif req_runtime:
        # An explicitly selected runtime pins the runner — the
        # runtime's target IS the requested target (§2.6.0). Only fall
        # back to picking when no explicit selection exists.
        _rt = db.session.get(RuntimeConnection, req_runtime)
        runner = (_rt.runner if _rt and _rt.runner
                  and _rt.runner.enabled and not _rt.runner.draining
                  else None)
        if runner is None and _rt:
            return None, None, (
                f"runtime {req_runtime[:12]}… target is disabled or "
                "draining — no fallback")
        runner = runner or ex.runner or pick_runner(ex)
        if runner is None:
            return None, None, ""
    else:
        runner = ex.runner or pick_runner(ex)
        if runner is None:
            return None, None, ""
    if req_runtime:
        runtime = db.session.get(RuntimeConnection, req_runtime)
        rt_allowed = (json.loads(runtime.allowed_projects or "[]")
                      if runtime else [])
        if (not runtime or not runtime.enabled
                or runtime.runner_id != runner.id
                or (rt_allowed and ex.project_id not in rt_allowed)):
            return None, None, (
                f"explicitly selected runtime {req_runtime[:12]}… is "
                "unavailable or incompatible with the selected "
                "runner — no fallback")
    else:
        runtime = ex.runtime or pick_runtime(ex, runner)
        if runtime is None:
            return None, None, (
                "no enabled compatible runtime connection on runner "
                f"{runner.name!r} — register an existing KRE "
                "installation")
    return runner, runtime, ""


def run_execution(execution_id: str) -> None:
    """Run one execution through the full state machine. Any infra error is
    recorded; test failures do not stop collection (§7)."""
    ex = db.session.get(Execution, execution_id)
    if ex is None:
        return
    # Dispatch boundary: only still-pending work may enter the machine —
    # an execution cancelled while its outbox event waited (user cancel
    # or plan stop-on-failure enforcement) is skipped, never launched.
    if ex.state not in (ExecutionState.QUEUED, ExecutionState.DISPATCHED):
        record_audit("system", "execution.dispatch_skipped",
                     f"{ex.id}: state={ex.state.value}")
        return
    # Explicit execution target (run_config.requested_runner/runtime):
    # re-validate eligibility HERE — drain/disable/mismatch may have
    # happened since the form was filled. An unavailable explicit pick
    # REJECTS visibly — never silently falls back to another target.
    rc = ex.snapshot().get("run_config") or {}
    st = ex.snapshot().get("source_target")
    if st:
        # The inventory row is replaceable per-SHA — re-validate the
        # durable triple, not the row id. A removed/renamed/type-changed
        # definition fails VISIBLY, never silently retargets.
        cur = DiscoveredItem.query.filter_by(
            binding_id=st["binding_id"], kind=st["kind"],
            path=st["path"]).first()
        if cur is None:
            _fail(ex, "config",
                  f"source definition {st['kind']} {st['path']!r} is "
                  "no longer in the binding's inventory — run rejected "
                  "(no retargeting)")
            return
    runner, runtime, sel_err = _select_runner_runtime(ex)
    if sel_err:
        _fail(ex, "infrastructure", sel_err)
        return
    if runner is None:
        # Stay QUEUED — retry the next worker tick; do NOT mark
        # DISPATCHED (the assignment CAS requires QUEUED).
        return
    # Local path: consume the slot atomically, then claim PREPARING.
    if not _assign_runner(ex, runner, runtime):
        return   # lost race — another dispatcher owns it, or cancelled

    workspace_root = Path(
        runtime.workspace_root or runner.workspace_root
        or Path(current_app.instance_path) / "workspaces"
    )
    attempt_dir = workspace_root / execution_id
    report_dir = attempt_dir / "Reports"
    log_dir = attempt_dir / "logs"

    try:
        if not _claim_for_start(ex, runner):
            db.session.refresh(ex)
            if ex.state in (ExecutionState.CANCELED,
                            ExecutionState.CANCEL_REQUESTED):
                record_audit("system", "execution.dispatch_skipped",
                             f"{ex.id}: cancelled before launch boundary")
            # else: capacity won the race — stays queued for the next
            # tick; runner_id assignment is harmless until it starts.
            return
        attempt_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)
        source_dir = _resolve_source(ex)
        project_dir = kre_adapter.prepare_workspace(
            source_dir, attempt_dir, ex.source_sha
        )
        ex.workspace_path = str(attempt_dir)
        ex.report_dir = str(report_dir)
        ex.started_at = utcnow()

        rc = ex.snapshot().get("run_config") or {}
        # Version-gated options (§5): requested flags the detected KRE
        # build does not support are dropped with an evidence note —
        # never emitted as guessed flags.
        flags, dropped = kre_adapter.gated_flags(
            runtime.detected_version, rc.get("kre_options") or {})
        if dropped:
            # Explicit options the runtime cannot accept are REJECTED
            # before the process launches — the requested execution
            # semantics would change silently otherwise. Acknowledgement
            # (run_config.kre_options_allow_drop) is opt-in and audited.
            allow = bool(rc.get("kre_options_allow_drop"))
            log_dir.mkdir(parents=True, exist_ok=True)
            decision = ("drop (operator-acknowledged)" if allow
                        else "reject")
            note = (
                f"version-gated options for KRE "
                f"{runtime.detected_version or 'unknown'} — "
                f"dropped: {', '.join(dropped)}; requested: "
                f"{json.dumps(rc.get('kre_options') or {})}; "
                f"decision: {decision}")
            (log_dir / "kre-gating.log").write_text(note + "\n",
                                                  encoding="utf-8")
            record_audit(
                "system",
                "kre.options_gated" if allow else "kre.options_rejected",
                f"{ex.id}: {note}")
            if not allow:
                _fail(ex, "infrastructure",
                      f"KRE option gating rejected {len(dropped)} "
                      f"requested option(s) for detected version "
                      f"{runtime.detected_version or 'unknown'}: "
                      + ", ".join(dropped)
                      + " — launch aborted before dispatch; acknowledge "
                        "in run config to proceed without them")
                # The rejection record is registered evidence — _fail
                # skips _collect, so the gating log is filed here.
                gating_log = log_dir / "kre-gating.log"
                db.session.add(Artifact(
                    execution_id=ex.id, kind="log",
                    rel_path="logs/kre-gating.log",
                    sha256=results_mod.sha256_file(gating_log),
                    size=gating_log.stat().st_size,
                    mime="text/plain", mapped=False))
                db.session.commit()
                return
        ok_b, browser_val = kre_adapter.validate_browser(
            rc.get("browser", ""), runner.os_arch if runner else "")
        if not ok_b:
            _fail(ex, "config", browser_val)
            return
        args = kre_adapter.build_args(
            executable=runtime.executable_path,
            project_path=str(project_dir),
            suite_path=_snapshot_suite_path(ex),
            report_dir=str(report_dir),
            profile=rc.get("profile", "default"),
            browser=browser_val or "Chrome (headless)",
            remote_webdriver_url=rc.get("remote_webdriver_url", ""),
            remote_webdriver_type=rc.get("remote_webdriver_type", ""),
            collection=bool(rc.get("collection")),
            extra=flags,
        )

        _transition(ex, ExecutionState.RUNNING)
        # Invocation credential (§5): the VALUE reaches the subprocess via
        # -apiKey= argv as KRE expects, but is scrubbed from every log/
        # artifact by run_invocation's redact pass.
        creds = _redact_values(runtime)
        # Remote WebDriver URL may embed user:pass@ — scrub the
        # userinfo portion from logs/audit/artifacts like any secret.
        rw_url = rc.get("remote_webdriver_url") or ""
        if "://" in rw_url and "@" in rw_url:
            creds.append(rw_url.split("@", 1)[0].split("://", 1)[1])
        if creds:
            args = args + [f"-apiKey={creds[0]}"]
        # Timeout: run_invocation's process-group kill is the same safe
        # path as cancel — a timed-out tree is never left running while
        # its slot reports free (wait/force-kill before release).
        result = kre_adapter.run_invocation(
            args, log_dir, cancel_event=_DbCancelEvent(ex.id),
            timeout_s=int(rc.get("timeout_seconds") or 3600),
            redact=creds,
        )
        ex.exit_status = result.exit_status
        ex.finished_at = utcnow()

        # Collection ALWAYS runs — including after failures (§5, §7).
        _transition(ex, ExecutionState.COLLECTING)
        _collect(ex, report_dir, log_dir)

        _transition(ex, ExecutionState.NORMALIZING)
        _normalize(ex)

        canceled = (result.canceled
                    or ex.state == ExecutionState.CANCEL_REQUESTED)
        if canceled:
            ex.outcome = Outcome.INCOMPLETE
            db.session.commit()
        else:
            error_kind = kre_adapter.classify_error(result)
            if error_kind:
                ex.error_kind = error_kind
                ex.error_text = f"KRE reported a {error_kind} error"
                db.session.commit()

        # Evidence export still runs for canceled executions —
        # partial evidence is real. Only the terminal state differs.
        _transition(ex, ExecutionState.FINALIZING)
        _sync_git(ex)

        if canceled:
            ex.state = ExecutionState.CANCELED
        else:
            ex.state = (ExecutionState.FAILED if ex.error_kind
                        else ExecutionState.COMPLETED)
        db.session.commit()
    except DispatchError as exc:  # configuration problem — name it
        _fail(ex, "config", str(exc))
    except Exception as exc:  # infrastructure failure — record honestly
        _fail(ex, "infrastructure", f"{type(exc).__name__}: {exc}")
    finally:
        runner.heartbeat_at = utcnow()
        runner.health = "ok"
        db.session.commit()


def _redact_values(runtime: RuntimeConnection) -> list:
    """Resolve a runtime credential ref to VALUES for log scrubbing only —
    the secret itself never enters argv, artifacts, audit, or exports."""
    if not runtime or not runtime.credential_ref:
        return []
    from .secrets_store import SecretError, read_secret
    try:
        return [read_secret(current_app._get_current_object(),
                            runtime.credential_ref)]
    except SecretError as exc:
        raise DispatchError(
            f"runtime credential {runtime.credential_ref!r} is "
            f"configured but cannot be resolved: {exc}") from exc


def _fail(ex: Execution, kind: str, text: str) -> None:
    ex.state = ExecutionState.FAILED
    ex.error_kind = kind
    ex.error_text = text[:2000]
    ex.finished_at = utcnow()
    ex.outcome = Outcome.INCOMPLETE
    db.session.commit()


def _resolve_source(ex: Execution) -> Path:
    """Source contents for the isolated workspace: the binding's synced
    worktree (recorded SHA), never project code executed at index time."""
    if ex.binding_id:
        binding = db.session.get(GitBinding, ex.binding_id)
        if binding and binding.worktree_path:
            base = Path(binding.worktree_path)
            if binding.subdirectory:
                base = base / binding.subdirectory
            return base
    return Path(ex.workspace_path or "/nonexistent")


def _snapshot_suite_path(ex: Execution) -> str:
    # An explicitly selected discovered suite/collection path takes
    # precedence; otherwise the first entry's case_ref (existing
    # suite-list semantics).
    rc = ex.snapshot().get("run_config") or {}
    if rc.get("suite_path"):
        return rc["suite_path"]
    entries = [e for e in ex.snapshot().get("entries", []) if e.get("enabled")]
    first = entries[0]["case_ref"] if entries else ""
    return first or "Test Suites/suite"


def _collect(ex: Execution, report_dir: Path, log_dir: Path) -> None:
    """Evidence collection: JUnit discovery + manifest with checksums;
    every file is registered, unmapped files stay at run level (§9).
    Runner logs are mirrored INTO the evidence root so every rel_path
    resolves under report_dir — a logs/ entry is never registered for
    a file the download path cannot reach."""
    logs_in = report_dir / "logs"
    try:
        for log_path in sorted(log_dir.glob("kre-*.log")):
            logs_in.mkdir(parents=True, exist_ok=True)
            (logs_in / log_path.name).write_bytes(log_path.read_bytes())
    except OSError:
        pass  # log mirroring is best-effort; producers keep log_dir
    junit_files = set(kre_adapter.discover_reports(report_dir))
    manifest = results_mod.build_evidence_manifest(report_dir, junit_files)
    for item in manifest:
        if item["rel_path"] == "manifest.json":
            continue  # sealed index — rewritten+registered separately
        if item["rel_path"].startswith("logs/"):
            item["kind"] = "log"
        db.session.add(Artifact(
            execution_id=ex.id,
            kind=item["kind"], rel_path=item["rel_path"],
            sha256=item["sha256"], size=item["size"], mime=item["mime"],
            mapped=item["kind"] == "junit",
        ))
    db.session.commit()
    _write_evidence_manifest(ex, report_dir)
    # Local-report status is tracked separately from state/outcome.
    try:
        from . import reporting
        reporting.refresh_report_status(ex)
    except Exception as exc:
        try:
            reporting.refresh_failed(ex, exc)
        except Exception:
            pass  # reporting must never break the execution pipeline


def _write_evidence_manifest(ex: Execution, report_dir: Path) -> None:
    """Sealed evidence index: manifest.json lands INSIDE the report
    dir listing every registered artifact (path/kind/sha256/size) plus
    execution identity + deployed source state — then it is registered
    as an artifact itself, so the standard checksum contract covers
    the manifest too. Tampering with any artifact OR the manifest
    breaks a recorded hash; deletion removes both honestly."""
    try:
        rows = [a for a in Artifact.query.filter_by(
            execution_id=ex.id).all() if a.kind != "manifest"]
        from .deploy_info import deployed_revision
        snap = ex.snapshot()
        doc = {
            "schema": "to-evidence-manifest/1",
            "execution_id": ex.id,
            "generated_at": utcnow().isoformat(),
            "deployed_rev": deployed_revision(),
            "dispatch_id": ex.dispatch_id,
            "bb_slot": ex.bb_slot,
            "attempts": snap.get("bb_attempts", []),
            "artifacts": [{
                "rel_path": a.rel_path, "kind": a.kind,
                "sha256": a.sha256, "size": a.size, "mime": a.mime,
                "mapped": a.mapped,
            } for a in sorted(rows, key=lambda r: r.rel_path)],
        }
        path = report_dir / "manifest.json"
        path.write_text(json.dumps(doc, indent=2, sort_keys=True),
                        encoding="utf-8")
        Artifact.query.filter_by(execution_id=ex.id,
                                 kind="manifest").delete()
        db.session.add(Artifact(
            execution_id=ex.id, kind="manifest",
            rel_path="manifest.json",
            sha256=results_mod.sha256_file(path),
            size=path.stat().st_size, mime="application/json",
            mapped=False))
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "evidence manifest write failed for %s", ex.id)


def _same_report_source(a: str, b: str) -> bool:
    """True when two collected rel_paths resolve to the same source
    file reached through different collection layouts — one path's
    trailing components equal the other's whole path (Reports/junit.xml
    inside proj/src/Reports/junit.xml). Sibling unit/shard directories
    never match, so identical reports from distinct execution units
    stay distinct."""
    pa = a.replace("\\", "/").split("/")
    pb = b.replace("\\", "/").split("/")
    if len(pa) == len(pb):
        return pa == pb
    longer, shorter = (pa, pb) if len(pa) > len(pb) else (pb, pa)
    return longer[-len(shorter):] == shorter


def _normalize(ex: Execution) -> None:
    """Canonical result rows from actual JUnit output. Discrepant producer
    totals and unmapped cases are surfaced, not guessed away (§8)."""
    junit_artifacts = Artifact.query.filter_by(
        execution_id=ex.id, kind="junit").all()
    cases = []
    seen_names = {}
    seen_hash = {}  # digest -> first rel_path. Identical bytes only
                    # dedup when the paths resolve to the same source
                    # file collected via two layouts (SSH Reports/ +
                    # proj/src/Reports/) — identical reports from
                    # distinct units stay distinct.
    parsed_docs = []  # each artifact parsed once — reused for dedup
    for artifact in junit_artifacts:
        path = Path(ex.report_dir) / artifact.rel_path
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        prior = seen_hash.get(digest)
        if prior is not None and _same_report_source(
                prior, artifact.rel_path):
            continue
        seen_hash.setdefault(digest, artifact.rel_path)
        try:
            parsed = parse_junit(path)
        except Exception:
            continue
        parsed_docs.append(parsed)
        for case in parsed.cases:
            key = (case.classname, case.name)
            seen_names[key] = seen_names.get(key, 0) + 1
            cases.append((case, seen_names[key], parsed))
    completed = ex.exit_status is not None
    for case, occurrence, parsed in cases:
        db.session.add(TestResult(
            execution_id=ex.id,
            case_ref=case.classname or case.name,
            case_name=case.name,
            classname=case.classname,
            iteration=case.iteration,
            attempt=occurrence,
            status=case.status,
            duration_s=case.duration_s,
            message=case.message[:4000],
            unresolved=parsed.discrepant,
        ))
    ex.outcome = Outcome(
        aggregate_outcome([c[0] for c in cases], completed=completed)
    )
    # Stamp dedup fingerprints so repository imports match this
    # live run — same canonicalisation the importer applies.
    try:
        from . import importer as _imp
        _uuid_f = Path(ex.report_dir or "") / "execution.uuid"
        if _uuid_f.is_file() and not ex.run_uuid:
            ex.run_uuid = _uuid_f.read_text().strip()[:64]
        if not ex.content_hash and cases:
            suite_keys, timestamps, totals = [], [], 0
            for parsed in parsed_docs:
                for s in parsed.suites:
                    suite_keys.append((s["id"] or s["name"],
                                       s["timestamp"]))
                    if s["timestamp"]:
                        timestamps.append(s["timestamp"])
                totals += parsed.producer_tests
            ex.content_hash = _imp.content_fingerprint(
                suite_keys, timestamps, [c[0] for c in cases], totals)
            if ex.suite_keys_json is None:
                ex.suite_keys_json = json.dumps(
                    sorted({tuple(k) for k in suite_keys}))
    except Exception:
        pass                        # fingerprinting never breaks normalize
    db.session.commit()


def _sync_git(ex: Execution) -> None:
    """Git export outbox (§11). Sync state is independent of outcome."""
    from . import gitsync
    try:
        gitsync.export_execution(ex)
        ex.git_state = SyncState.DONE
    except gitsync.NotConfigured:
        ex.git_state = SyncState.NOT_CONFIGURED
    except Exception as exc:
        ex.git_state = SyncState.FAILED
        ex.error_text = (ex.error_text + f" | git: {exc}")[:2000]
    db.session.commit()


# ---------- outbox processing ----------

def process_outbox(limit: int = 10) -> int:
    """Claim and process pending durable events. Status transitions are
    committed before side effects; failures leave a retryable record with
    the error — an ambiguous timeout is reconciled, never blind-duplicated."""
    now = utcnow()
    events = OutboxEvent.query.filter(
        OutboxEvent.status == "pending",
        OutboxEvent.available_at <= now,
    ).order_by(OutboxEvent.created_at).limit(limit).all()
    processed = 0
    for event in events:
        event.status = "processing"
        event.attempts += 1
        db.session.commit()
        try:
            _handle_event(event)
            event.status = "done"
            event.processed_at = utcnow()
        except Exception as exc:
            event.status = "pending" if event.attempts < 5 else "failed"
            event.last_error = f"{type(exc).__name__}: {exc}"[:1000]
            event.available_at = utcnow() + timedelta(
                seconds=min(300, 2 ** event.attempts))
        db.session.commit()
        processed += 1
    return processed


def _bb_brid(ex: Execution) -> str:
    return ex.dispatch_id.split(":", 1)[1] \
        if ex.dispatch_id.startswith("bb:") else ex.dispatch_id


def _submit_buildbot(ex: Execution, runner: Runner,
                     runtime: RuntimeConnection) -> None:
    """Submit one execution to its per-target builder. The brid is
    persisted as ``bb:<brid>`` dispatch identity; a lost/ambiguous
    submission is reconciled by the execution-id PROPERTY before any
    resubmit — never blind-duplicated. Assignment is already durable
    (caller) before the master sees the buildrequest."""
    adapter = BuildbotAdapter.from_settings()
    if not ex.dispatch_id:
        # Reconcile-before-submit: a previous attempt may have created
        # the buildrequest without us reading the response — adopt an
        # existing live brid instead of creating a second run.
        live = [br for br in adapter.find_buildrequests(ex.id)
                if not br.get("complete")]
        if live:
            ex.dispatch_id = "bb:" + str(
                live[0]["buildrequestid"])
            db.session.commit()
            record_audit("system", "execution.buildbot_adopted",
                         f"{ex.id}: brid {ex.dispatch_id}")
            return
    source = _resolve_source(ex)
    buildbot_cfg.stage_source(ex, source)
    _snap = ex.snapshot()
    rc = _snap.get("run_config") or {}
    _unit = _snap.get("unit") or {}
    # Slot-builders: a (builder, worker) pair runs one build at a time,
    # so the runner's admitted slots map to distinct builders. Pick a
    # free slot; bb_slot is persisted with the dispatch identity.
    slot = _pick_slot(ex, runner)
    timeout_s = int(rc.get("timeout_seconds") or 3600)
    node_kind = getattr(runtime, "runtime_kind", "") or "kre"
    if node_kind in nodefw.KINDS:
        ok_b, browser_val = nodefw.validate_browser(
            node_kind, rc.get("browser", ""))
        if not ok_b:
            _fail(ex, "config", browser_val)
            return
        _pmode = rc.get("parallel_mode") or "default"
        _pw_workers = (1 if _pmode == "sequential"
                       else int(rc.get("parallel") or 0)
                       if _pmode == "parallel" else 0)
        if node_kind == "cypress" and _pw_workers:
            raise DispatchError(
                "parallel is not supported for cypress — use spec "
                "partitioning (unit run) instead")
        _shard = (str(_unit.get("shard") or "")
                  or (f"{int(rc['shard_index'])}/{int(rc['shard_total'])}"
                      if rc.get("shard_index") else ""))
        _specs = _unit.get("specs") or []
        node_args = nodefw.build_args(
            node_kind, browser_val,
            spec=(",".join(_specs) if _specs else ""),
            workers=_pw_workers,
            shard=_shard)
        node_env = nodefw.junit_env(node_kind)
        win = (runner.os_arch or "").lower().startswith("win")
        # npm sits next to npx — an explicit executable_path resolves
        # the sibling, otherwise PATH does.
        npx_path = runtime.executable_path or (
            "npx.cmd" if win else "npx")
        npm = str(Path(npx_path).with_name("npm.cmd" if win
                                           else "npm")) \
            if Path(npx_path).parent != Path(".") else (
                "npm.cmd" if win else "npm")
        deps_argv = [npm, "ci", "--no-audit", "--no-fund"]
        if runner.target_type == "ssh":
            buildbot_cfg.materialize_ssh_key(runner)
            extra = {"exec_subdir": "src", "env": node_env,
                     "deps_argv": deps_argv,
                     "report_rel": "proj/src/Reports"}
            npx_dir = str(Path(npx_path).parent)
            if Path(npx_path).is_absolute() and npx_dir != ".":
                extra["path_prefix"] = npx_dir
            spec_file = buildbot_cfg.write_ssh_run_spec(
                ex, runner, runtime, node_args, extra=extra)
            import sys as _sys
            spec = buildbot_cfg.ssh_exec_spec(
                ex, python=_sys.executable,
                dispatcher_script=str(
                    Path(__file__).resolve().parent.parent / "scripts"
                    / "to_ssh_exec.py"),
                spec_file=str(spec_file))
        else:
            spec = buildbot_cfg.node_exec_spec(
                ex, npx_path, node_args, env=node_env,
                deps_argv=deps_argv, posix=not win)
            buildbot_cfg.run_specs_dir().mkdir(parents=True,
                                               exist_ok=True)
            (buildbot_cfg.run_specs_dir() / f"{ex.id}.spec.json"
             ).write_text(json.dumps(spec))
        builder = buildbot_cfg.builder_for(runner, slot)
        brid = adapter.submit(
            builder, ex.id,
            {"exec_spec": BuildbotAdapter.encode_props(spec),
             "exec_timeout": str(timeout_s)})
        ex.dispatch_id = "bb:" + str(brid)
        ex.bb_slot = slot
        db.session.commit()
        record_audit("system", "execution.buildbot_dispatched",
                     f"{ex.id}: {builder} ({node_kind}) brid {brid}")
        return
    if node_kind == robotfw.KIND:
        # python -m robot under the runtime's configured interpreter —
        # no browser concept (browsers belong to the project and its
        # libraries), no deps step (deps live in the configured env).
        robot_args = robotfw.build_args(
            suite_path=(rc.get("suite_path") or "").strip(),
            test=(rc.get("robot_test") or "").strip(),
            suite=(rc.get("robot_suite") or "").strip(),
            include=_tag_list(rc.get("robot_include")),
            exclude=_tag_list(rc.get("robot_exclude")),
            pabot_processes=(
                _gated_pabot(runtime, rc)))
        win = (runner.os_arch or "").lower().startswith("win")
        py = (runtime.executable_path or "").strip() \
            or ("python.exe" if win else "python3")
        if runner.target_type == "ssh":
            buildbot_cfg.materialize_ssh_key(runner)
            extra = {"exec_subdir": "src",
                     "report_rel": "proj/src/Reports"}
            py_dir = str(Path(py).parent)
            if Path(py).is_absolute() and py_dir != ".":
                extra["path_prefix"] = py_dir
            spec_file = buildbot_cfg.write_ssh_run_spec(
                ex, runner, runtime, robot_args, executable=py,
                extra=extra)
            import sys as _sys
            spec = buildbot_cfg.ssh_exec_spec(
                ex, python=_sys.executable,
                dispatcher_script=str(
                    Path(__file__).resolve().parent.parent / "scripts"
                    / "to_ssh_exec.py"),
                spec_file=str(spec_file))
        else:
            spec = buildbot_cfg.node_exec_spec(
                ex, py, robot_args, posix=not win)
            buildbot_cfg.run_specs_dir().mkdir(parents=True,
                                               exist_ok=True)
            (buildbot_cfg.run_specs_dir() / f"{ex.id}.spec.json"
             ).write_text(json.dumps(spec))
        builder = buildbot_cfg.builder_for(runner, slot)
        brid = adapter.submit(
            builder, ex.id,
            {"exec_spec": BuildbotAdapter.encode_props(spec),
             "exec_timeout": str(timeout_s)})
        ex.dispatch_id = "bb:" + str(brid)
        ex.bb_slot = slot
        db.session.commit()
        record_audit("system", "execution.buildbot_dispatched",
                     f"{ex.id}: {builder} (robot) brid {brid}")
        return
    if node_kind == pytestfw.KIND:
        # python -m pytest under the runtime's configured interpreter —
        # junit under Reports/, same spec/collect pipeline as robot.
        _pmode = rc.get("parallel_mode") or "default"
        _pprocs = (int(rc.get("parallel") or 0)
                   if _pmode == "parallel" else 0)
        if _pprocs:
            _require_probe_opt(
                runtime, "opt_xdist",
                "pytest-xdist (parallel_mode=parallel, -n "
                f"{_pprocs})")
        _serial = False
        if _pmode == "sequential":
            # Enforce sequential even when the project's own config
            # enables xdist: -n 0 wins — but only where xdist exists.
            try:
                _m = json.loads(getattr(runtime, "probe_json", "")
                                or "{}")
            except Exception:
                _m = {}
            _serial = _m.get("opt_xdist") == "ok"
        pytest_args = pytestfw.build_args(
            suite_path=(rc.get("suite_path") or "").strip(),
            keyword=(rc.get("pytest_keyword") or "").strip(),
            markers=(rc.get("pytest_markers") or "").strip(),
            numprocesses=_pprocs, force_serial=_serial)
        win = (runner.os_arch or "").lower().startswith("win")
        py = (runtime.executable_path or "").strip() \
            or ("python.exe" if win else "python3")
        if runner.target_type == "ssh":
            buildbot_cfg.materialize_ssh_key(runner)
            extra = {"exec_subdir": "src",
                     "report_rel": "proj/src/Reports"}
            py_dir = str(Path(py).parent)
            if Path(py).is_absolute() and py_dir != ".":
                extra["path_prefix"] = py_dir
            spec_file = buildbot_cfg.write_ssh_run_spec(
                ex, runner, runtime, pytest_args, executable=py,
                extra=extra)
            import sys as _sys
            spec = buildbot_cfg.ssh_exec_spec(
                ex, python=_sys.executable,
                dispatcher_script=str(
                    Path(__file__).resolve().parent.parent / "scripts"
                    / "to_ssh_exec.py"),
                spec_file=str(spec_file))
        else:
            spec = buildbot_cfg.node_exec_spec(
                ex, py, pytest_args, posix=not win)
            buildbot_cfg.run_specs_dir().mkdir(parents=True,
                                               exist_ok=True)
            (buildbot_cfg.run_specs_dir() / f"{ex.id}.spec.json"
             ).write_text(json.dumps(spec))
        builder = buildbot_cfg.builder_for(runner, slot)
        brid = adapter.submit(
            builder, ex.id,
            {"exec_spec": BuildbotAdapter.encode_props(spec),
             "exec_timeout": str(timeout_s)})
        ex.dispatch_id = "bb:" + str(brid)
        ex.bb_slot = slot
        db.session.commit()
        record_audit("system", "execution.buildbot_dispatched",
                     f"{ex.id}: {builder} (pytest) brid {brid}")
        return
    flags, dropped = kre_adapter.gated_flags(
        runtime.detected_version, rc.get("kre_options") or {})
    if dropped and not rc.get("kre_options_allow_drop"):
        _fail(ex, "infrastructure",
              f"version-gated options rejected for detected version "
              f"{runtime.detected_version or 'unknown'}: "
              + ", ".join(dropped))
        return
    ok_b, browser_val = kre_adapter.validate_browser(
        rc.get("browser", ""), "linux")
    if not ok_b:
        _fail(ex, "config", browser_val)
        return

    if runner.target_type == "ssh":
        cred_file = runtime.credential_file or ""
        if runtime.credential_ref and not cred_file:
            _fail(ex, "config",
                  "ssh target: runtime credential_ref is portal-side "
                  "only — configure a credential_file path on the "
                  "remote host instead")
            return
        buildbot_cfg.materialize_ssh_key(runner)
        remote = runner.ssh_workdir.rstrip("/") + "/" + ex.id
        args = kre_adapter.build_args(
            executable=runtime.executable_path,
            # source.tar holds a top-level src/ — the remote project
            # root after untar is <workdir>/proj/src.
            project_path=remote + "/proj/src",
            suite_path=_snapshot_suite_path(ex),
            report_dir=remote + "/Reports",
            profile=rc.get("profile", "default"),
            browser=browser_val or "Chrome (headless)",
            remote_webdriver_url=rc.get("remote_webdriver_url", ""),
            remote_webdriver_type=rc.get("remote_webdriver_type", ""),
            collection=bool(rc.get("collection")),
            extra=flags)
        spec_file = buildbot_cfg.write_ssh_run_spec(
            ex, runner, runtime, args[1:], cred_file)
        import sys as _sys
        spec = buildbot_cfg.ssh_exec_spec(
            ex,
            # The dispatcher runs on the controller host — this same
            # interpreter (payload venv in packaged installs) has
            # paramiko and the dispatcher script's deps.
            python=_sys.executable,
            dispatcher_script=str(
                Path(__file__).resolve().parent.parent / "scripts"
                / "to_ssh_exec.py"),
            spec_file=str(spec_file))
        builder = buildbot_cfg.builder_for(runner, slot)
    else:
        cred_file = runtime.credential_file or ""
        if not cred_file and runtime.credential_ref:
            if runner.target_type == "local":
                # Controller-host worker: portal resolves the secret to
                # a per-run 0600 file the wrapper reads — never argv.
                from .secrets_store import read_secret
                cred_file = buildbot_cfg.write_run_credential(
                    ex.id, read_secret(
                        current_app._get_current_object(),
                        runtime.credential_ref))
            else:
                _fail(ex, "config",
                      "remote worker: runtime credential_ref is "
                      "portal-side only — configure a credential_file "
                      "on the worker host instead")
                return
        args = kre_adapter.build_args(
            executable=runtime.executable_path,
            project_path=".",
            suite_path=_snapshot_suite_path(ex),
            report_dir="Reports",
            profile=rc.get("profile", "default"),
            browser=browser_val or "Chrome (headless)",
            remote_webdriver_url=rc.get("remote_webdriver_url", ""),
            remote_webdriver_type=rc.get("remote_webdriver_type", ""),
            collection=bool(rc.get("collection")),
            extra=flags)
        spec = buildbot_cfg.kre_exec_spec(
            ex, runtime, args[1:], credential_file=cred_file,
            posix=not (runner.os_arch or "").lower().startswith("win"))
        # Persist the rendered spec — collection re-renders it as the
        # kre-invocation.log evidence record (same contract the local
        # path had: the exact argv, never secret values).
        buildbot_cfg.run_specs_dir().mkdir(parents=True, exist_ok=True)
        (buildbot_cfg.run_specs_dir() / f"{ex.id}.spec.json"
         ).write_text(json.dumps(spec))
        builder = buildbot_cfg.builder_for(runner, slot)

    brid = adapter.submit(
        builder, ex.id,
        {"exec_spec": BuildbotAdapter.encode_props(spec),
         "exec_timeout": str(timeout_s)})
    ex.dispatch_id = "bb:" + str(brid)
    ex.bb_slot = slot
    db.session.commit()
    record_audit("system", "execution.buildbot_dispatched",
                 f"{ex.id}: {builder} brid {brid}")


def _buildbot_assign_and_submit(ex: Execution) -> None:
    """Buildbot dispatch (docs/orchestration-replacement.md): same
    ordering contract — assignment commits BEFORE the
    buildrequest exists so a claimed build's first poll sees a durable
    DISPATCHED row; explicit picks are honored or visibly rejected; a
    lost capacity CAS or unreachable master leaves the row QUEUED for
    the redrive (which reconciles by property before resubmitting)."""
    # Dispatch boundary: only still-pending work may enter — an
    # execution cancelled while its outbox event waited is skipped and
    # audited, never launched (same contract as run_execution's gate).
    if ex.state not in (ExecutionState.QUEUED,):
        record_audit("system", "execution.dispatch_skipped",
                     f"{ex.id}: state={ex.state.value}")
        return
    runner, runtime, sel_err = _select_runner_runtime(ex)
    if sel_err:
        _fail(ex, "infrastructure", sel_err)
        return
    if runner is None:
        scoped = [r for r in Runner.query.filter_by(
            enabled=True, draining=False).all()
            if not json.loads(r.allowed_projects or "[]")
            or ex.project_id in json.loads(
                r.allowed_projects or "[]")]
        unreachable = [r for r in scoped
                       if r.target_type == "buildbot-worker"
                       and r.health == "unreachable"]
        if unreachable and len(unreachable) == len(scoped):
            ex.wait_reason = (
                "all eligible runners unreachable — workers "
                "offline; run waits for reconnect")
        else:
            ex.wait_reason = (
                "no eligible runner with free capacity (enabled, "
                "non-draining, project scope) — retried each tick")
        db.session.commit()
        return
    if runner.max_procs and requested_procs(ex) >= UNBOUNDED_PROCS:
        # A framework-default playwright run auto-scales to ~cpu/2 —
        # unbounded on a budgeted target. QUEUED-forever would be a
        # silent stall; reject with an actionable config error.
        _fail(ex, "config",
              f"runner {runner.name!r} enforces a process budget "
              f"(max_procs={runner.max_procs}) and this run's "
              "framework-default concurrency is unbounded — set "
              "parallel_mode to sequential or an explicit parallel "
              "count")
        return
    if not _assign_runner(ex, runner, runtime):
        return
    try:
        _submit_buildbot(ex, runner, runtime)
    except Exception as exc:
        import requests as _req
        if isinstance(exc, (_req.ConnectionError, _req.Timeout)):
            # Connect-level failure — the request provably never
            # reached the master (or the reconcile query will adopt
            # whatever did): release the slot, stay QUEUED, retry.
            ex.state = ExecutionState.QUEUED
            ex.runner_id = None
            ex.runtime_id = None
            ex.lease_expires_at = None
            ex.bb_slot = None
            ex.error_text = (
                ex.error_text + " | buildbot master unreachable; "
                "held QUEUED for redrive")[:2000]
            _release_reservations(ex, "requeued")
            db.session.commit()
            return
        _fail(ex, "infrastructure",
              f"Buildbot submission failed — reconcile before retry: "
              f"{str(exc)[:300]}")
        return
    db.session.refresh(ex)
    if ex.state == ExecutionState.CANCEL_REQUESTED:
        # Cancel landed between assignment and submit — issue the
        # buildrequest cancel now (brid exists, request_cancel saw none).
        try:
            BuildbotAdapter.from_settings().cancel(_bb_brid(ex))
        except Exception:
            pass


def _handle_event(event: OutboxEvent) -> None:
    payload = event.payload()
    if event.kind == "report-publish":
        from .gitsync import publish_report
        publish_report(payload["execution_id"])
        return
    if event.kind == "report-delete":
        from .gitsync import delete_report
        delete_report(payload["execution_id"])
        return
    if event.kind in ("local-dispatch",
                      "orchestrator-dispatch"):
        # Buildbot is the sole orchestrator; legacy in-flight event
        # kinds route to the same assign->submit path.
        ex = db.session.get(Execution, payload["execution_id"])
        if ex is not None:
            _buildbot_assign_and_submit(ex)
    elif event.kind == "git-sync":
        from . import gitsync
        gitsync.sync_binding(payload["binding_id"])
    elif event.kind == "rp-publish":
        pass  # removed integration — absorb stale queued events harmlessly
    elif event.kind == "runner-heartbeat":
        runner = db.session.get(Runner, payload["runner_id"])
        if runner:
            runner.heartbeat_at = utcnow()
            runner.health = "ok"
            db.session.commit()
    elif event.kind == "webhook":
        _deliver_webhook(payload["endpoint_id"], payload["execution_id"])
    else:
        raise DispatchError(f"unknown outbox kind {event.kind!r}")


def _deliver_webhook(endpoint_id: str, execution_id: str) -> None:
    """POST one execution's terminal notification to one subscriber.
    Any failure raises — the outbox retry/backoff owns the record;
    a non-2xx is an error, never a silent drop. The HMAC signature
    lets the subscriber verify authenticity without a shared
    channel: X-TO-Signature = sha256=<hmac(body, key)>."""
    import hmac as _hmac
    import urllib.request as _req
    from .execution import WebhookEndpoint
    ep = db.session.get(WebhookEndpoint, endpoint_id)
    ex = db.session.get(Execution, execution_id)
    if ep is None or not ep.enabled or ex is None:
        return  # endpoint deleted/disabled or exec gone — nothing owed
    body = json.dumps({
        "event": f"execution.{ex.state.value.lower()}",
        "execution_id": ex.id,
        "state": ex.state.value,
        "outcome": ex.outcome.value if ex.outcome else None,
        "project_id": ex.project_id,
        "suite_id": ex.suite_id,
        "framework": ex.framework,
        "runner_id": ex.runner_id,
        "created_at": ex.created_at.isoformat() if ex.created_at else None,
        "finished_at": ex.finished_at.isoformat() if ex.finished_at else None,
        "error_kind": ex.error_kind,
        "error_text": ex.error_text[:500],
        "client_ref": ex.client_ref,
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-TO-Event": f"execution.{ex.state.value.lower()}",
        "X-TO-Execution": ex.id,
    }
    if ep.secret_ref:
        from . import secrets_store
        from flask import current_app
        try:
            key = secrets_store.read_secret(current_app, ep.secret_ref)
        except Exception:
            key = ""
        if key:
            headers["X-TO-Signature"] = "sha256=" + _hmac.new(
                key.encode("utf-8"), body, hashlib.sha256).hexdigest()
    req = _req.Request(ep.url, data=body, headers=headers,
                     method="POST")
    with _req.urlopen(req, timeout=15) as resp:
        if resp.status >= 400:
            raise DispatchError(
                f"webhook {ep.name!r} returned HTTP {resp.status}")
    record_audit("system", "webhook.delivered",
                 f"{ep.name}: {ex.id} {ex.state.value}")


def _enqueue_terminal_notifications() -> int:
    """Fan-out sweep: terminal or UNKNOWN executions whose webhook
    enqueue marker is unset get one outbox event per matching enabled
    endpoint, then notified_at — durable, so a crash between the state
    transition and this sweep retries instead of dropping the
    notification. Runs in the worker loop; covers transitions made by
    any process (reconciler, UI resolve, deadline sweep)."""
    due = Execution.query.filter(
        Execution.state.in_(_TERMINAL_STATES + [ExecutionState.UNKNOWN]),
        Execution.notified_at.is_(None),
    ).limit(50).all()
    if not due:
        return 0
    from .execution import WebhookEndpoint
    endpoints = WebhookEndpoint.query.filter_by(enabled=True).all()
    n = 0
    for ex in due:
        state_name = ex.state.value.lower()
        for ep in endpoints:
            if state_name not in ep.event_set():
                continue
            if not ep.project_allowed(ex.project_id):
                continue
            db.session.add(OutboxEvent(
                kind="webhook",
                payload_json=json.dumps({
                    "endpoint_id": ep.id,
                    "execution_id": ex.id})))
            n += 1
        ex.notified_at = utcnow()
    db.session.commit()
    return n


_TERMINAL_STATES = [
    ExecutionState.COMPLETED, ExecutionState.FAILED,
    ExecutionState.CANCELED,
]


def _release_reservations(ex: Execution, release_state: str) -> int:
    """Mark this execution's open reservations released — durable
    ownership ledger. Called on confirmed terminal transitions and on
    the connect-failure requeue (where the slot is honestly freed)."""
    rows = ResourceReservation.query.filter_by(
        execution_id=ex.id, released_at=None).all()
    for r in rows:
        r.released_at = utcnow()
        r.release_state = release_state
    return len(rows)


def _release_terminal_reservations() -> int:
    """Sweep: any reservation still open on a confirmed-terminal
    execution is released — capacity may only be freed by a terminal
    state, and this pass also repairs rows whose terminal transition
    predates the ledger. UNKNOWN is deliberately NOT terminal."""
    rows = (ResourceReservation.query
            .join(Execution,
                  ResourceReservation.execution_id == Execution.id)
            .filter(ResourceReservation.released_at.is_(None),
                    Execution.state.in_(_TERMINAL_STATES)).all())
    for r in rows:
        r.released_at = utcnow()
        r.release_state = "terminal"
    if rows:
        db.session.commit()
    return len(rows)


def _expire_queue_deadlines() -> int:
    """Queue deadline: a QUEUED execution past queue_deadline_at fails
    with an explicit reason — capacity starvation is surfaced, never
    an indefinite silent wait."""
    now = utcnow()
    due = Execution.query.filter(
        Execution.state == ExecutionState.QUEUED,
        Execution.queue_deadline_at.isnot(None),
        Execution.queue_deadline_at < now).all()
    for ex in due:
        _fail(ex, "queue",
              f"queue deadline exceeded — still waiting "
              f"({ex.wait_reason or 'no eligible runner'}); "
              "increase runner capacity or queue_timeout_seconds")
        record_audit("system", "execution.queue_deadline_expired",
                     ex.id)
    return len(due)


def reconcile_stale() -> int:
    """Restart reconciliation (A05/A06): executions whose lease/heartbeat
    expired while non-terminal become UNKNOWN — never re-dispatched."""
    now = utcnow()
    stale = Execution.query.filter(
        Execution.state.in_([
            ExecutionState.DISPATCHED, ExecutionState.PREPARING,
            ExecutionState.RUNNING, ExecutionState.COLLECTING,
            ExecutionState.NORMALIZING, ExecutionState.CANCEL_REQUESTED,
        ]),
        Execution.lease_expires_at.isnot(None),
        Execution.lease_expires_at < now,
    ).all()
    for ex in stale:
        ex.state = ExecutionState.UNKNOWN
        ex.error_kind = "infrastructure"
        # Durable audit trail for ambiguous resolution (§18): every
        # UNKNOWN is attributable, never silent or auto-rerun.
        record_audit("system", "execution.reconciled_unknown", ex.id)
        ex.error_text = (
            ex.error_text + " | lease expired; state uncertain "
            "(reconciled, not re-run)").strip(" |")[:2000]
        db.session.commit()
    return len(stale)


# ---------- schedules & plan policies (§10) ----------

def fire_due_schedules(now=None) -> int:
    """Fire enabled schedules whose next_run_at has passed — the worker
    tick owns firing; request handlers only manage definitions. A missed
    fire time runs late on the next tick and is audited, never dropped."""
    now = now or utcnow()
    due = Schedule.query.filter(
        Schedule.enabled.is_(True),
        Schedule.next_run_at.isnot(None),
        Schedule.next_run_at <= now).all()
    fired = 0
    for sched in due:
        # Atomic claim (multi-worker safe): exactly one worker advances
        # next_run_at/disables for THIS due time — the loser sees
        # rowcount 0 and skips. A fire failure rolls the claim back so
        # the schedule stays due; crash-after-claim is audited, and a
        # 'once' schedule can never double-dispatch.
        try:
            if sched.kind == "once":
                claimed = db.session.query(Schedule).filter(
                    Schedule.id == sched.id,
                    Schedule.enabled.is_(True),
                    Schedule.next_run_at == sched.next_run_at,
                ).update({"enabled": False, "next_run_at": None},
                         synchronize_session=False)
            else:
                claimed = db.session.query(Schedule).filter(
                    Schedule.id == sched.id,
                    Schedule.enabled.is_(True),
                    Schedule.next_run_at == sched.next_run_at,
                ).update({"next_run_at": now + timedelta(
                             minutes=sched.interval_minutes or 60)},
                         synchronize_session=False)
        except OperationalError as exc:
            # Only expected writer-lock/busy conditions are retried —
            # unrelated DB failures stay visible (raised, not skipped).
            if "locked" not in str(exc).lower() \
                    and "busy" not in str(exc).lower():
                raise
            db.session.rollback()   # session stays usable next tick
            continue
        if not claimed:
            db.session.rollback()
            continue  # another worker claimed this fire
        # Misfire policy ('late' default = previous behaviour): a
        # missed interval fire runs once late. 'skip' drops an
        # occurrence overdue past its interval — the claim above
        # already advanced next_run_at, so a skipped fire can never
        # catch-up storm after an outage. Evaluated on the pre-claim
        # due time (overdue captured before the update).
        overdue_s = 0.0
        if sched.next_run_at:
            nra = sched.next_run_at
            if nra.tzinfo is None:
                overdue_s = max(
                    0.0, (now.replace(tzinfo=None) - nra)
                    .total_seconds())
            else:
                overdue_s = max(
                    0.0, (now - nra).total_seconds())
        try:
            # Post-claim refresh: fire the row's CURRENT config/target —
            # a concurrent edit/disable/delete between the due scan and
            # the claim must not dispatch stale values. A deleted row
            # raises here and stays unfired.
            db.session.refresh(sched)
            cfg = sched.run_config()
            if sched.kind == "interval" \
                    and sched.misfire_policy == "skip" \
                    and sched.interval_minutes \
                    and overdue_s > sched.interval_minutes * 60:
                record_audit(
                    "system", "schedule.misfire_skipped",
                    f"{sched.id}: overdue {int(overdue_s)}s > "
                    f"interval {sched.interval_minutes}m — "
                    "occurrence dropped (misfire_policy=skip)")
                db.session.commit()
                continue
            # Overlap policy (interval schedules only — 'once' fires a
            # single time): skip = drop this occurrence while a previous
            # execution from THIS schedule is still active. Evaluated
            # inside the already-claimed row's transaction — the claim
            # already advanced next_run_at, so the skipped slot moves
            # forward (no catch-up storm). UNKNOWN counts as active —
            # uncertain work blocks overlap until resolved.
            if sched.kind == "interval" and                     sched.overlap_policy == "skip":
                active = db.session.query(Execution).filter(
                    Execution.schedule_id == sched.id,
                    Execution.state.in_(_ACTIVE_STATES)).count()
                if active:
                    record_audit("system", "schedule.overlap_skipped",
                                 f"{sched.id}: {active} active")
                    db.session.commit()
                    continue
            binding = GitBinding.query.filter_by(
                project_id=sched.project_id).first()
            sha = binding.last_synced_sha if binding else ""
            targets = sched.targets()
            if targets:
                for tgt in targets:
                    d_item = db.session.get(
                        DiscoveredItem, tgt.get("source_item_id"))
                    if (d_item is None or
                            d_item.project_id
                            != sched.project_id):
                        record_audit("worker", "schedule.target_unavailable",
                                     sched.id)
                        continue
                    tgt_binding = (
                        db.session.get(GitBinding,
                                       tgt.get("binding_id"))
                        or binding)
                    tgt_sha = (tgt_binding.last_synced_sha
                               if tgt_binding else sha)
                    create_execution(sched.created_by, None,
                                     binding=tgt_binding,
                                     source_sha=tgt_sha,
                                     run_config=cfg,
                                     schedule_id=sched.id,
                                     source_item=d_item)
            elif sched.target_type == "plan" and sched.plan_id:
                plan = db.session.get(Plan, sched.plan_id)
                if plan is None:
                    raise DispatchError("scheduled plan no longer exists")
                queue_plan_executions(sched.created_by, plan,
                                      binding=binding, source_sha=sha,
                                      schedule_id=sched.id,
                                      run_config=cfg)
            elif sched.target_type == "suite" and sched.suite_id:
                suite = db.session.get(Suite, sched.suite_id)
                if suite is None:
                    raise DispatchError("scheduled suite no longer exists")
                create_execution(sched.created_by, suite, binding=binding,
                                 source_sha=sha, run_config=cfg,
                                 schedule_id=sched.id)
            else:
                raise DispatchError(
                    f"invalid schedule target {sched.target_type!r}")
            record_audit(sched.created_by, "schedule.fired", sched.id)
            fired += 1
        except Exception as exc:
            db.session.rollback()   # un-claim — stays due, fire retried
            record_audit("system", "schedule.fire_failed",
                         f"{sched.id}: {type(exc).__name__}: {exc}")
            db.session.commit()
        else:
            sched.last_run_at = now
            db.session.commit()     # claim + execution commit together
    return fired


def enforce_plan_policies() -> int:
    """Per-entry failure policy (§10): an entry configured
    on_failure=stop whose LATEST execution reached a terminal non-passed
    state cancels its plan-run siblings still queued/dispatched —
    entries already running finish naturally (cancel-queued scope only).

    Runs every tick, restart-safe: a missed enforcement simply applies
    late. A queued retry ('not-started' latest state) holds enforcement."""
    cancelled = 0
    runs = {}
    # Every execution of a run matters — including pending retries:
    # collecting only terminal rows would let a queued retry be ignored
    # and siblings wrongly cancelled.
    for ex in Execution.query.all():
        rid = ex.snapshot().get("plan_run_id")
        if rid:
            runs.setdefault(rid, []).append(ex)
    if not runs:
        return 0
    pending = {e.id: e for e in Execution.query.filter(
        Execution.state.in_([ExecutionState.QUEUED,
                             ExecutionState.DISPATCHED])).all()}
    for rid, exs in runs.items():
        plan = db.session.get(
            Plan, exs[0].snapshot().get("plan_id") or "")
        if plan is None:
            continue
        entries = {e.suite_id: e for e in plan.entries}
        # Latest execution per entry decides — a retry in flight blocks.
        latest = {}
        for ex in exs:
            sid = ex.snapshot().get("suite_id")
            if sid not in latest or (
                    ex.created_at and latest[sid].created_at
                    and ex.created_at > latest[sid].created_at):
                latest[sid] = ex
        stop_orders = []
        for sid, ex in latest.items():
            entry = entries.get(sid)
            if entry is None:
                continue
            cfg = json.loads(entry.config_json or "{}")
            if cfg.get("on_failure") == "stop" \
                    and entry_state(ex) not in ("passed", "not-started"):
                stop_orders.append(entry.order)
        if not stop_orders:
            continue
        threshold = min(stop_orders)
        for sib in pending.values():
            snap = sib.snapshot()
            if snap.get("plan_run_id") != rid:
                continue
            entry = entries.get(snap.get("suite_id"))
            if entry is None or entry.order <= threshold:
                continue
            # Atomic cancel-scope transition: only if the sibling is
            # STILL queued/dispatched. If its launch claim landed first
            # (PREPARING+), this UPDATE matches 0 rows — started work
            # continues; cancel scope is defined at the DB row, not on
            # a stale ORM snapshot.
            n = db.session.query(Execution).filter(
                Execution.id == sib.id,
                Execution.state.in_([ExecutionState.QUEUED,
                                     ExecutionState.DISPATCHED]),
            ).update({
                "state": ExecutionState.CANCELED,
                "cancel_reason": (
                    f"plan stop-on-failure: entry order {threshold} "
                    "ended non-passed"),
                "outcome": Outcome.INCOMPLETE,
                "finished_at": utcnow(),
            }, synchronize_session=False)
            if not n:
                continue   # already started — out of cancel scope
            if sib.dispatch_id:
                try:
                    BuildbotAdapter.from_settings().cancel(
                        _bb_brid(sib))
                except Exception:
                    pass
            record_audit("system", "plan.stop_on_failure_cancel", sib.id)
            cancelled += 1
    if cancelled:
        db.session.commit()
    return cancelled


def _heartbeat_local_runners() -> int:
    """The flask worker IS the execution driver for every
    connection_mode=local runner on this instance — its own liveness
    is their availability signal (§6). Stamped while the loop lives;
    when it stops, the existing freshness window marks them ineligible.
    Heartbeat certifies driver liveness only — never KRE/credential
    availability, which separate checks own."""
    runners = Runner.query.filter_by(enabled=True,
                                     connection_mode="local").all()
    for r in runners:
        r.heartbeat_at = utcnow()
        r.health = "ok"
    if runners:
        db.session.commit()
    return len(runners)


def _resolve_unlaunched_cancels() -> int:
    """A cancel that lands while an execution is still QUEUED has no
    remote process to confirm — nothing ever launched, so the
    cancellation is already fact. Without this the row parks in
    CANCEL_REQUESTED forever: the dispatch boundary only
    audits-and-skips it, the buildbot reconciler only follows
    executions holding a brid, and a parent can never roll up while a
    child stays non-terminal.
    Boundary: an empty dispatch_id alone does NOT prove "never
    launched" — a cancel racing _assign_runner->_submit_buildbot
    leaves runner_id/bb_slot set while the buildrequest is still
    in flight. Only a row that provably never entered assignment
    (no runner, no slot, no brid, never started) may be resolved
    here; anything else is ambiguous and ages out to UNKNOWN via
    lease expiry — uncertainty is surfaced, never silently released."""
    n = 0
    for ex in Execution.query.filter(
            Execution.state == ExecutionState.CANCEL_REQUESTED,
            Execution.role != "parent",
            Execution.runner_id.is_(None),
            Execution.bb_slot.is_(None),
            db.or_(Execution.dispatch_id.is_(None),
                   Execution.dispatch_id == ""),
            Execution.started_at.is_(None)).all():
        ex.state = ExecutionState.CANCELED
        ex.outcome = Outcome.INCOMPLETE
        ex.finished_at = utcnow()
        record_audit("system", "execution.cancel_confirmed_unlaunched",
                     ex.id)
        db.session.commit()
        n += 1
    return n


def _redrive_queued_remote(limit: int = 10) -> int:
    """Capacity-blocked executions: their dispatch outbox event is
    consumed once — a lost capacity CAS or a no-runner tick must not
    strand them QUEUED forever. Each tick revisits QUEUED executions
    with no dispatch_id: assign->submit runs again only while
    dispatch_id is empty — assignment precedes submit, so QUEUED + no
    dispatch_id proves no run was ever started; _submit_buildbot
    additionally reconciles by execution-id property before forcing a
    new buildrequest.
    Fair ordering: oldest-first (created_at). A job that cannot fit
    now does not starve younger ones — the scan continues past it
    (bounded), and its wait_reason records exactly which gate blocks
    it."""
    n = 0
    for ex in Execution.query.filter(
            Execution.state == ExecutionState.QUEUED,
            Execution.role != "parent").order_by(
            Execution.created_at.asc()).limit(100):
        if n >= limit:
            break
        transport = ex.snapshot().get("dispatch_transport")
        if transport == "buildbot" and not ex.dispatch_id:
            _buildbot_assign_and_submit(ex)
            if ex.state != ExecutionState.QUEUED:
                n += 1
    return n


def worker_once() -> int:
    # Orchestrator reconciliation (§7) runs BEFORE the lease sweep: an
    # aborted run with a pending cancel request must resolve to
    # CANCELED from the coordinator's authoritative status — not to
    # UNKNOWN merely because the lease elapsed first in the same tick.
    # A finished run with no portal result is ambiguous -> UNKNOWN,
    # never a blind re-dispatch.
    n = 0
    if _buildbot_configured():
        try:
            n += reconcile_buildbot_runs()
        except Exception:
            pass  # orchestrator unreachable — retry next cycle
    n += (reconcile_stale() + fire_due_schedules() + process_outbox()
          + enforce_plan_policies() + _resolve_unlaunched_cancels()
          + aggregate_parent_runs()
          + _release_terminal_reservations() + _expire_queue_deadlines()
          + _enqueue_terminal_notifications()
          + _heartbeat_local_runners() + _redrive_queued_remote())
    return n


def worker_loop(interval: float = 2.0) -> None:
    while True:
        try:
            worker_once()
        finally:
            # Release the session/transaction every cycle — a left-open
            # transaction holds the SQLite write lock and starves the
            # web app's request-time provisioning writes.
            db.session.remove()
        time.sleep(interval)


# ---------- Buildbot orchestration (docs/orchestration-replacement.md) -

def _collect_buildbot(adapter: BuildbotAdapter, ex: Execution,
                      results_code: int) -> None:
    """Terminal buildrequest -> materialize master-side uploaded
    artifacts into the portal collection dir and run the SAME
    collect/normalize/finalize pipeline — parallel to _collect_remote.
    Artifacts land at <buildbot_home>/artifacts/<exec_id> via the
    builder's DirectoryUpload step; the step log comes over the
    localhost API."""
    attempt_dir = (Path(current_app.instance_path)
                   / "remote-runs" / ex.id)
    report_dir = attempt_dir / "Reports"
    log_dir = attempt_dir / "logs"
    # Capture the cancel intent BEFORE the collection pipeline
    # transitions the row through COLLECTING/NORMALIZING/FINALIZING —
    # checking ex.state at the end would never see CANCEL_REQUESTED.
    cancel_requested = (ex.state == ExecutionState.CANCEL_REQUESTED
                        or bool(ex.cancel_reason))
    rt0 = db.session.get(RuntimeConnection, ex.runtime_id)
    try:
        creds = _redact_values(rt0) if rt0 else []
    except DispatchError:
        creds = []

    def _scrub(data: bytes) -> bytes:
        for c in creds:
            data = data.replace(c.encode("utf-8", "replace"),
                                b"***REDACTED***")
        return data

    import shutil as _sh
    # Attempt-stamped uploads: a buildrequest retried after worker
    # loss uploads per-build — the newest attempt dir is the terminal
    # one; earlier attempts stay preserved beside it.
    src_dir = buildbot_cfg.artifact_dir(ex.id)
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        if src_dir.is_dir():
            for item in src_dir.rglob("*"):
                if item.is_file():
                    rel = item.relative_to(src_dir)
                    dest = report_dir.parent / rel if str(
                        rel).startswith("Reports") else report_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(_scrub(item.read_bytes()))
    except Exception as exc:
        _fail(ex, "infrastructure",
              f"buildbot run {ex.dispatch_id} ended "
              f"(results={results_code}) but artifact collection "
              f"failed: {exc}")
        return
    try:
        spec_path = buildbot_cfg.run_specs_dir() / f"{ex.id}.spec.json"
        if spec_path.is_file():
            spec = json.loads(spec_path.read_text())
            if spec.get("executable"):
                argv = [spec["executable"], *spec.get("args", [])]
                if spec.get("credential_wrapper") \
                        and spec.get("credential_file"):
                    argv = [spec["credential_wrapper"],
                            spec["credential_file"], *argv]
                import shlex as _shlex
                (log_dir / "kre-invocation.log").write_text(
                    " ".join(_shlex.quote(str(a)) for a in argv) + "\n",
                    encoding="utf-8")
    except Exception:
        pass  # invocation record is best-effort evidence
    try:
        builds = adapter.builds_for_request(_bb_brid(ex))
        if builds:
            # The terminal attempt's log — retries (worker loss)
            # produce multiple builds under one buildrequest.
            (log_dir / "kre-remote-run.log").write_bytes(_scrub(
                adapter.run_log(builds[-1]["buildid"])
                .encode("utf-8", "replace")))
    except Exception:
        pass  # run log is best-effort evidence
    ex.workspace_path = str(attempt_dir)
    ex.report_dir = str(report_dir)
    ex.exit_status = 0 if results_code == 0 else 1
    ex.finished_at = utcnow()
    _transition(ex, ExecutionState.COLLECTING)
    _collect(ex, report_dir, log_dir)
    _transition(ex, ExecutionState.NORMALIZING)
    _normalize(ex)
    _transition(ex, ExecutionState.FINALIZING)
    _sync_git(ex)
    buildbot_cfg.clear_staging(ex.id)
    has_results = bool(TestResult.query.filter_by(
        execution_id=ex.id).first())
    if results_code == 6 or (cancel_requested
                             and not (results_code == 0 or has_results)):
        # CANCELLED — or any non-success result after a cancel request:
        # buildrequest cancel may surface as RETRY/FAILURE depending on
        # where the worker was in the step, but the user intent was
        # recorded and must govern the terminal state.
        if cancel_requested:
            if "UNCONFIRMED" in (ex.cancel_reason or ""):
                ex.state = ExecutionState.FAILED
                ex.error_kind = "infrastructure"
                ex.error_text = (
                    f"cancellation requested but remote process-tree "
                    f"death was not confirmed (buildbot run "
                    f"{ex.dispatch_id} ended results={results_code}) — "
                    "manual verification required")
            else:
                ex.state = ExecutionState.CANCELED
            ex.outcome = Outcome.INCOMPLETE
        else:
            # Unrequested abort — worker loss/external stop are
            # indistinguishable to the portal; honest uncertainty,
            # never a mislabeled user cancellation.
            ex.state = ExecutionState.FAILED
            ex.error_kind = "infrastructure"
            ex.error_text = (
                f"buildbot run {ex.dispatch_id} ended CANCELLED "
                "without a recorded cancellation request — cause "
                "undetermined; any late evidence was collected")
            ex.outcome = Outcome.INCOMPLETE
            record_audit("system",
                         "execution.buildbot_aborted_unrequested", ex.id)
    elif results_code == 0 or has_results:
        # A failed build with real junit is failed TESTS — the
        # normalized outcome carries the verdict, not the step status.
        ex.state = ExecutionState.COMPLETED
    elif (blocked := _retry_blocked_line(adapter, ex)) is not None:
        # The retry prep step REFUSED to launch: the prior attempt's
        # liveness could not be proven stopped, or the configured
        # retry policy rejected the attempt. The original run may
        # still be executing — UNKNOWN is the honest state: open
        # reservations stay held until an operator reconciles, never
        # a silent terminal label and never a freed slot.
        ex.state = ExecutionState.UNKNOWN
        ex.error_kind = "infrastructure"
        ex.error_text = (blocked + " — automatic relaunch blocked; "
                         "resource claims retained pending operator "
                         "reconciliation")
        ex.outcome = Outcome.INCOMPLETE
        record_audit("system", "execution.retry_blocked",
                     f"{ex.id}: {blocked}")
    else:
        ex.state = ExecutionState.FAILED
        # EXCEPTION (4) = the step never ran the test binary — an
        # infrastructure/config failure (missing executable, worker
        # error), not a test verdict. The step log carries the cause.
        if results_code == 4:
            ex.error_kind = "infrastructure"
            tail = ""
            try:
                builds = adapter.builds_for_request(_bb_brid(ex))
                if builds:
                    tail = adapter.run_log(
                        builds[0]["buildid"]).strip()[-300:]
            except Exception:
                pass
            ex.error_text = (
                f"buildbot run {ex.dispatch_id} ended EXCEPTION — the "
                f"test executable did not run"
                + (f": {tail}" if tail else ""))
        else:
            ex.error_kind = "execution"
            detail = ""
            if rt0 is not None and (rt0.runtime_kind or "") \
                    in (robotfw.KIND, pytestfw.KIND):
                # Robot/pytest write xunit only on a valid execution —
                # classify the console log so invalid data / missing
                # libraries / interruptions are named, not lumped
                # into "no result data".
                _fw = (robotfw if (rt0.runtime_kind or "")
                       == robotfw.KIND else pytestfw)
                label = ""
                for cand in (report_dir / "run.out",
                             log_dir / "kre-remote-run.log",
                             report_dir / "Reports" / "output.xml"):
                    try:
                        label = _fw.classify_log(
                            cand.read_text(errors="replace"))
                    except OSError:
                        continue
                    if label:
                        break
                # SSH runs drop an exit_code artifact — the recorded
                # rc sharpens the classification (e.g. 251
                # informational, 253 user stop, 252 invalid data).
                if not label:
                    for cand in (report_dir / "exit_code",
                                 report_dir / "Reports" / "exit_code"):
                        try:
                            rc = int(cand.read_text().strip())
                        except (OSError, ValueError):
                            continue
                        sev, label = _fw.classify_exit(rc)
                        label = f"exit {rc}: {label}"
                        if sev in ("config", "interrupted", "internal"):
                            ex.error_kind = (
                                "execution" if sev == "interrupted"
                                else "infrastructure")
                        break
                detail = f" ({label})" if label else ""
            ex.error_text = (f"buildbot run {ex.dispatch_id} ended "
                             f"results={results_code} with no result "
                             f"data{detail}")
    db.session.commit()


def _retry_blocked_line(adapter: BuildbotAdapter,
                        ex: Execution) -> "str | None":
    """TO_RETRY_BLOCKED marker from the LATEST attempt's prep step, or
    None when absent/unfetchable. A resubmitted buildrequest whose
    prep refuses (unresolvable prior attempt, retry policy exceeded)
    fails the build before the exec step — the marker is the only
    signal distinguishing a deliberate safety block from an ordinary
    run failure."""
    try:
        builds = adapter.builds_for_request(_bb_brid(ex))
    except Exception:
        return None
    if not builds:
        return None
    try:
        log = adapter.run_log(builds[-1]["buildid"],
                              step_names=("prep",))
    except Exception:
        return None
    for line in (log or "").splitlines():
        if "TO_RETRY_BLOCKED" in line:
            return line.strip()[:400]
    return None


def _record_buildbot_attempts(adapter: BuildbotAdapter,
                              ex: Execution) -> None:
    """Track every Buildbot build spawned under this execution's
    buildrequest in the snapshot. Buildbot resubmits a buildrequest
    automatically after worker loss (RETRY): each new build is a
    DISTINCT attempt with its own pidfile/workspace/artifact dir —
    recorded here so provenance survives and a retry can never be
    mistaken for the original run."""
    builds = adapter.builds_for_request(_bb_brid(ex))
    if not builds:
        return
    snap = ex.snapshot()
    attempts = snap.setdefault("bb_attempts", [])
    known = {a.get("buildid") for a in attempts}
    for b in sorted(builds, key=lambda x: x.get("number", 0)):
        entry = next(
            (a for a in attempts
             if a.get("buildid") == b.get("buildid")), None)
        if entry is None:
            entry = {"buildid": b.get("buildid"),
                     "number": b.get("number"),
                     "started_at": b.get("started_at"),
                     "builderid": b.get("builderid"),
                     "workerid": b.get("workerid")}
            attempts.append(entry)
            if b.get("number") and len(attempts) > 1:
                record_audit(
                    "system", "execution.buildbot_retry_attempt",
                    f"{ex.id}: attempt build #{b.get('number')} "
                    f"(buildid {b.get('buildid')}) started under "
                    "the same buildrequest — prior attempt "
                    "interrupted (worker loss)")
        entry["complete"] = bool(b.get("complete"))
        if b.get("complete"):
            entry["results"] = b.get("results")
            entry["complete_at"] = b.get("complete_at")
    ex.snapshot_json = json.dumps(snap)


def reconcile_buildbot_runs() -> int:
    """Poll DISPATCHED buildbot executions (parallel to
    the orchestrator reconcile): finished-without-result -> UNKNOWN is NOT
    reachable here — a completed buildrequest always maps to a real
    terminal transition; unrequested CANCELLED stays honest. Live
    buildrequests renew the lease."""
    adapter = None
    fixed = 0
    for ex in Execution.query.filter(
            Execution.state.in_([ExecutionState.DISPATCHED,
                                 ExecutionState.CANCEL_REQUESTED])).all():
        if ex.snapshot().get("dispatch_transport") != "buildbot" \
                or not ex.dispatch_id:
            continue
        adapter = adapter or BuildbotAdapter.from_settings()
        try:
            br = adapter.buildrequest(_bb_brid(ex))
        except Exception:
            continue    # transient — lease expiry bounds the ambiguity
        if not br:
            continue
        try:
            _record_buildbot_attempts(adapter, ex)
        except Exception:
            pass    # attempt audit is best-effort, never blocks reconcile
        if not br.get("complete"):
            ex.lease_expires_at = utcnow() + timedelta(
                seconds=LEASE_SECONDS)
            db.session.commit()
            continue
        _collect_buildbot(adapter, ex, int(br.get("results") or 0))
        fixed += 1
    # Runner liveness from the worker registry: a registered-but-
    # unconnected worker is distinguishable from an unreachable SSH
    # host (different mechanism, different UI state).
    if adapter is not None:
        try:
            online = {w.get("name") for w in adapter.workers()
                      if w.get("connected_to")}
            for r in Runner.query.filter(
                    Runner.target_type == "buildbot-worker",
                    Runner.enabled.is_(True)).all():
                live = r.bb_worker_name in online
                r.health = "ok" if live else "unreachable"
                if live:
                    r.heartbeat_at = utcnow()
            db.session.commit()
        except Exception:
            pass
    return fixed
