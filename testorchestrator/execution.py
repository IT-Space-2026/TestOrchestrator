"""Execution domain model (handoff §4–§11).

Durable schema for the source-to-execution-to-evidence pipeline. Execution
state, test outcome, and Git synchronization are
independent state fields — a reporting outage never rewrites the recorded
execution evidence.
"""

import enum
import json
import uuid
from datetime import datetime, timezone

from .extensions import db


def utcnow():
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Project(db.Model):
    __tablename__ = "projects"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)


class GitBinding(db.Model):
    """Per-project Katalon repository binding (§10–§11). Separate from
    connection credentials — references a SecretRef by name only."""

    __tablename__ = "git_bindings"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    project_id = db.Column(db.String(32), db.ForeignKey("projects.id"),
                           nullable=False)
    name = db.Column(db.String(128), nullable=False)
    url = db.Column(db.String(512), nullable=False)          # https/ssh/local
    ref = db.Column(db.String(128), nullable=False, default="main")
    subdirectory = db.Column(db.String(256), nullable=False, default="")
    prj_path = db.Column(db.String(256), nullable=False, default="")  # .prj
    credential_ref = db.Column(db.String(64), nullable=False, default="")
    # Explicit admin opt-in for report write-back — read access is NOT
    # publish permission. Report destination stays on this branch.
    report_writable = db.Column(db.Boolean, nullable=False,
                                default=False)
    # Native source authoring — separate permission; one does not
    # imply the other.
    authoring_writable = db.Column(db.Boolean, nullable=False,
                                   default=False)
    authoring_branch = db.Column(db.String(64), nullable=True)
    worktree_path = db.Column(db.String(512), nullable=False, default="")
    # Discovery state (separate from fetch status): the last commit whose
    # inventory was successfully parsed — stale if newer SHA unfetched.
    discovered_sha = db.Column(db.String(64), nullable=False, default="")
    discovery_status = db.Column(db.String(16), nullable=False,
                                 default="pending")  # pending|ok|failed|none
    discovery_at = db.Column(db.DateTime(timezone=True), nullable=True)
    discovery_error = db.Column(db.String(512), nullable=False,
                                default="")
    project_root = db.Column(db.String(256), nullable=False, default="")
    last_synced_sha = db.Column(db.String(64), nullable=False, default="")
    status = db.Column(db.String(32), nullable=False, default="never-synced")
    last_error = db.Column(db.Text, nullable=False, default="")
    project = db.relationship("Project", backref="bindings")


class Suite(db.Model):
    __tablename__ = "suites"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    project_id = db.Column(db.String(32), db.ForeignKey("projects.id"),
                           nullable=False)
    name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    # Immutable versioning: edits create a new version row; imported suites
    # are read-only and can only be duplicated.
    version = db.Column(db.Integer, nullable=False, default=1)
    content_hash = db.Column(db.String(64), nullable=False, default="")
    imported = db.Column(db.Boolean, nullable=False, default=False)
    readonly = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    project = db.relationship("Project", backref="suites")


class SuiteEntry(db.Model):
    """Ordered case membership; repeated entries are allowed."""

    __tablename__ = "suite_entries"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    suite_id = db.Column(db.String(32), db.ForeignKey("suites.id"),
                         nullable=False)
    order = db.Column(db.Integer, nullable=False, default=0)
    case_ref = db.Column(db.String(512), nullable=False)   # repo-relative path
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    config_json = db.Column(db.Text, nullable=False, default="{}")
    suite = db.relationship("Suite", backref=db.backref(
        "entries", order_by="SuiteEntry.order", cascade="all, delete-orphan"))


class Plan(db.Model):
    __tablename__ = "plans"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    project_id = db.Column(db.String(32), db.ForeignKey("projects.id"),
                           nullable=False)
    name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    parallelism = db.Column(db.Integer, nullable=False, default=1)  # sequential
    project = db.relationship("Project", backref="plans")


class PlanEntry(db.Model):
    __tablename__ = "plan_entries"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    plan_id = db.Column(db.String(32), db.ForeignKey("plans.id"),
                        nullable=False)
    order = db.Column(db.Integer, nullable=False, default=0)
    suite_id = db.Column(db.String(32), db.ForeignKey("suites.id"),
                         nullable=False)
    config_json = db.Column(db.Text, nullable=False, default="{}")
    plan = db.relationship("Plan", backref=db.backref(
        "entries", order_by="PlanEntry.order", cascade="all, delete-orphan"))
    suite = db.relationship("Suite")


class Release(db.Model):
    __tablename__ = "releases"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    project_id = db.Column(db.String(32), db.ForeignKey("projects.id"),
                           nullable=False)
    name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    owner = db.Column(db.String(64), nullable=False, default="")
    status = db.Column(db.String(32), nullable=False, default="draft")
    plan_id = db.Column(db.String(32), db.ForeignKey("plans.id"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    released_at = db.Column(db.DateTime(timezone=True), nullable=True)
    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.Date, nullable=True)
    # sync-to-repo state: destination binding + path + published sha;
    # sync_dirty marks edits not yet exported. A failed publish never
    # stamps synced_sha.
    sync_binding_id = db.Column(db.String(32), nullable=True)
    sync_path = db.Column(db.String(512), nullable=False, default="")
    synced_sha = db.Column(db.String(64), nullable=False, default="")
    sync_dirty = db.Column(db.Boolean, nullable=False, default=False)
    project = db.relationship("Project", backref="releases")


class ReleaseItem(db.Model):
    """A release's testing scope — a reference to a repository
    definition (suite or collection), never a copy."""
    __tablename__ = "release_items"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    release_id = db.Column(db.String(32), db.ForeignKey("releases.id"),
                           nullable=False)
    kind = db.Column(db.String(16), nullable=False)
    path = db.Column(db.String(512), nullable=False)
    source_item_id = db.Column(
        db.String(32), db.ForeignKey("discovered_items.id",
                                     ondelete="SET NULL"),
        nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)


class Schedule(db.Model):
    """Scheduled suite/plan execution (§10, §16 Test Manager).

    Fired by the durable worker tick — never inside request handlers.
    A missed fire time runs late on the next tick (recorded), it is never
    silently dropped; a 'once' schedule disables itself after firing."""

    __tablename__ = "schedules"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    project_id = db.Column(db.String(32), db.ForeignKey("projects.id"),
                           nullable=False)
    name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    target_type = db.Column(db.String(8), nullable=False)   # suite|plan
    suite_id = db.Column(db.String(32), db.ForeignKey("suites.id"),
                         nullable=True)
    plan_id = db.Column(db.String(32), db.ForeignKey("plans.id"),
                        nullable=True)
    kind = db.Column(db.String(16), nullable=False,
                     default="interval")                   # once|interval
    # allow = previous behaviour; skip = drop the new occurrence while
    # an active execution from this schedule exists.
    overlap_policy = db.Column(db.String(16), nullable=False,
                               default="allow")
    # late = a missed fire runs once late on the next tick (default,
    # previous behaviour); skip = an occurrence overdue past its
    # interval is dropped and audited — the schedule never catch-up
    # storms after a long outage.
    misfire_policy = db.Column(db.String(16), nullable=False,
                               default="late")
    interval_minutes = db.Column(db.Integer, nullable=True)
    next_run_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_run_at = db.Column(db.DateTime(timezone=True), nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    run_config_json = db.Column(db.Text, nullable=False, default="{}")
    # Repository-discovered targets selected in the schedule UI —
    # [{"kind","path","source_item_id","binding_id"}, ...]; empty list
    # falls back to the legacy suite/plan fields.
    targets_json = db.Column(db.Text, nullable=False, default="[]")
    created_by = db.Column(db.String(64), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    project = db.relationship("Project", backref=db.backref(
        "schedules", cascade="all, delete-orphan"))
    suite = db.relationship("Suite")
    plan = db.relationship("Plan")

    def run_config(self) -> dict:
        return json.loads(self.run_config_json or "{}")

    def targets(self) -> list:
        return json.loads(self.targets_json or "[]")


class Runner(db.Model):
    """Registered execution host (§6). Local mode first."""

    __tablename__ = "runners"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    name = db.Column(db.String(128), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=False, default="")
    os_arch = db.Column(db.String(64), nullable=False, default="")
    connection_mode = db.Column(db.String(16), nullable=False, default="local")
    allowed_projects = db.Column(db.Text, nullable=False, default="[]")
    tags = db.Column(db.Text, nullable=False, default="[]")
    capabilities = db.Column(db.Text, nullable=False, default="[]")
    workspace_root = db.Column(db.String(512), nullable=False, default="")
    max_slots = db.Column(db.Integer, nullable=False, default=1)
    # Operator-declared KRE licence allowance for this worker (§3):
    # floating licences are per concurrent SESSION — max_slots (hardware
    # capacity) and licence seats are independent limits. NULL/0 = the
    # operator declared no licence constraint (never assumed seats).
    license_slots = db.Column(db.Integer, nullable=True)
    # Process budget (2.6): total framework worker/processes across ALL
    # concurrent runs on this target. A job admitted under max_slots
    # must not bypass the intended process budget through its own
    # parallel setting — the CAS admission counts requested parallel
    # procs (default 1 per job). NULL = unconstrained (job slots only).
    max_procs = db.Column(db.Integer, nullable=True)
    health = db.Column(db.String(16), nullable=False, default="unknown")
    heartbeat_at = db.Column(db.DateTime(timezone=True), nullable=True)
    draining = db.Column(db.Boolean, nullable=False, default=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    credential_ref = db.Column(db.String(64), nullable=False, default="")
    # Remote transport (§6): hashed agent bearer token + last seen version.
    # The plaintext token is shown to the Admin once at issuance — never
    # stored or logged.
    agent_token_hash = db.Column(db.String(64), nullable=False, default="")
    agent_version = db.Column(db.String(32), nullable=False, default="")
    agent_capabilities = db.Column(db.String(256), nullable=False,
                                   default="[]")
    # Execution-target kind (docs/orchestration-replacement.md):
    #   "local"            portal-host runner (drives KRE on this host)
    #   "buildbot-worker"  Windows/Linux Buildbot worker over PB
    #   "ssh"              agentless host — the controller's ssh
    #                      dispatcher connects, runs, collects; nothing
    #                      TO-side executes on the remote host.
    target_type = db.Column(db.String(20), nullable=False,
                            default="local")
    # Buildbot worker identity: unique workername + per-worker password
    # (secret ref). Empty for local/ssh targets.
    bb_worker_name = db.Column(db.String(64), nullable=False, default="")
    bb_credential_ref = db.Column(db.String(64), nullable=False,
                                  default="")
    # Agentless SSH target configuration — connectivity details only;
    # key material lives in the secrets store, never on the runner row.
    ssh_host = db.Column(db.String(256), nullable=False, default="")
    ssh_port = db.Column(db.Integer, nullable=True)     # None -> 22
    ssh_username = db.Column(db.String(64), nullable=False, default="")
    ssh_credential_ref = db.Column(db.String(64), nullable=False,
                                   default="")
    ssh_host_key_alg = db.Column(db.String(32), nullable=False,
                                 default="")
    ssh_host_key_fingerprint = db.Column(db.String(128), nullable=False,
                                         default="")
    ssh_workdir = db.Column(db.String(512), nullable=False, default="")
    # Last Admin-UI connection check outcome (unregistered worker /
    # unreachable host / host missing runner are distinct states).
    check_status = db.Column(db.String(16), nullable=False, default="")
    check_error = db.Column(db.String(512), nullable=False, default="")
    check_at = db.Column(db.DateTime(timezone=True), nullable=True)


class RuntimeConnection(db.Model):
    """Registered *existing* KRE installation on a runner (§5). Connect-only:
    never installs, licenses, upgrades, or removes KRE."""

    __tablename__ = "runtime_connections"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    runner_id = db.Column(db.String(32), db.ForeignKey("runners.id"),
                          nullable=False)
    transport = db.Column(db.String(16), nullable=False, default="local")
    executable_path = db.Column(db.String(512), nullable=False)
    workspace_root = db.Column(db.String(512), nullable=False, default="")
    credential_ref = db.Column(db.String(64), nullable=False, default="")
    detected_version = db.Column(db.String(64), nullable=False, default="")
    verified_capabilities = db.Column(db.Text, nullable=False, default="[]")
    allowed_projects = db.Column(db.Text, nullable=False, default="[]")
    concurrency = db.Column(db.Integer, nullable=False, default=1)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    last_diagnostic = db.Column(db.Text, nullable=False, default="")
    last_check_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # Machine-readable probe markers from the last completed readiness
    # run — optional parallel deps (opt_xdist/opt_pabot) are gated on
    # these at dispatch; the human summary lives in last_diagnostic.
    probe_json = db.Column(db.Text, nullable=False, default="")
    # remote: the unique worker label the legacy installation was
    # bound to — empty => local (portal-host) execution.
    worker_label = db.Column(db.String(128), nullable=False, default="")
    # Worker-side SSH deploy-key path for ssh:// bindings — a path on
    # the worker host; key material never enters the portal.
    git_ssh_key_path = db.Column(db.String(512), nullable=False,
                                 default="")
    # Worker-local credential FILE path for orchestrated KRE — the
    # coordinator spec carries this path only; the worker-side
    # resolver (scripts/kre_cred_exec.py) reads the value on the
    # worker immediately before exec. Admin-configured; an execution
    # request can never select an arbitrary worker path.
    credential_file = db.Column(db.String(512), nullable=False,
                                default="")
    # Test framework this runtime drives: "kre" (existing rows),
    # "playwright" or "cypress" (Node.js, run inside the staged
    # project via npx — project-pinned deps, never bundled). The
    # executable_path of a node runtime is the npx/npx.cmd binary
    # resolved on the TARGET host.
    runtime_kind = db.Column(db.String(16), nullable=False,
                             default="kre")
    runner = db.relationship("Runner", backref="runtimes")


class ExecutionState(str, enum.Enum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"      # handed to the orchestrator, awaiting runner
    PREPARING = "preparing"
    RUNNING = "running"
    COLLECTING = "collecting"      # continues after failed tests (§7)
    NORMALIZING = "normalizing"
    PUBLISHING = "publishing"      # external publish; local results already safe
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"              # infrastructure/execution failure
    CANCEL_REQUESTED = "cancel_requested"
    CANCELED = "canceled"
    UNKNOWN = "unknown"            # ambiguous after timeout/restart


class Outcome(str, enum.Enum):
    NONE = "none"
    PASSED = "passed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"      # partial/canceled — never silently green


class SyncState(str, enum.Enum):
    NONE = "none"
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"      # remote may have accepted; reconcile first
    NOT_CONFIGURED = "not-configured"


class Execution(db.Model):
    """Immutable execution snapshot + live state (§4, §8)."""

    __tablename__ = "executions"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    project_id = db.Column(db.String(32), db.ForeignKey("projects.id"),
                           nullable=False)
    suite_id = db.Column(db.String(32), db.ForeignKey("suites.id"),
                         nullable=True)
    release_id = db.Column(db.String(32), db.ForeignKey("releases.id"),
                           nullable=True)
    release = db.relationship("Release", foreign_keys=[release_id])
    # Direct repository-definition runs: convenience link to the
    # DiscoveredItem index row (replaceable per-SHA — the durable
    # identity lives in the snapshot's source_target triple).
    source_item_id = db.Column(db.String(32),
                               db.ForeignKey("discovered_items.id",
                                             ondelete="SET NULL"),
                               nullable=True)
    # Immutable snapshot: suite content hash + resolved source SHA per
    # binding — never mutated by later definition edits (A07).
    snapshot_json = db.Column(db.Text, nullable=False, default="{}")
    source_sha = db.Column(db.String(64), nullable=False, default="")
    # Selected source ref (branch/tag selector — NOT the resolved SHA)
    # + the environment captured from the effective run_config.
    source_ref = db.Column(db.String(128), nullable=True)
    environment = db.Column(db.String(128), nullable=True)
    binding_id = db.Column(db.String(32), db.ForeignKey("git_bindings.id"),
                           nullable=True)
    runner_id = db.Column(db.String(32), db.ForeignKey("runners.id"),
                          nullable=True)
    runtime_id = db.Column(db.String(32),
                           db.ForeignKey("runtime_connections.id"),
                           nullable=True)
    created_by = db.Column(db.String(64), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # Independent state fields (§8): execution, outcome and Git sync —
    # tracked separately.
    state = db.Column(db.Enum(ExecutionState), nullable=False,
                      default=ExecutionState.QUEUED)
    outcome = db.Column(db.Enum(Outcome), nullable=False,
                        default=Outcome.NONE)
    git_state = db.Column(db.Enum(SyncState), nullable=False,
                          default=SyncState.NONE)
    # Which schedule fired this execution — None for manual runs.
    # Records origin for overlap checks + history filtering; Run now
    # links back to the same schedule row.
    schedule_id = db.Column(db.String(32),
                            db.ForeignKey("schedules.id"), nullable=True)
    dispatch_id = db.Column(db.String(64), nullable=False, default="")
    workspace_path = db.Column(db.String(512), nullable=False, default="")
    report_dir = db.Column(db.String(512), nullable=False, default="")
    # Local-report status is INDEPENDENT of execution state/outcome (§9):
    # pending|ready|partial|missing|failed. A collection failure never
    # rewrites the actual test outcome.
    report_status = db.Column(db.String(16), nullable=False,
                              default="pending")
    report_path = db.Column(db.String(512), nullable=False, default="")
    # reporting-labs reporting — the sole enhanced-report backend (2.0;
    # Allure columns removed by migration p6q7r8s9t0u1).
    rl_status = db.Column(db.String(16), nullable=False,
                          default="pending")
    rl_path = db.Column(db.String(512), nullable=False, default="")
    rl_version = db.Column(db.String(32), nullable=False, default="")
    exit_status = db.Column(db.Integer, nullable=True)
    error_kind = db.Column(db.String(32), nullable=False, default="")
    error_text = db.Column(db.Text, nullable=False, default="")
    cancel_reason = db.Column(db.String(256), nullable=False, default="")
    # Parallel execution units (2.6): a partitioned run is a "parent"
    # execution that owns no process itself plus N "unit" children, each
    # a real dispatchable execution with its own frozen assignment
    # (shard/spec list) in its snapshot. role: "" ordinary | "parent" |
    # "unit". unit_count on the parent is the expected child count.
    role = db.Column(db.String(16), nullable=False, default="")
    parent_id = db.Column(db.String(32), db.ForeignKey("executions.id"),
                          nullable=True)
    unit_index = db.Column(db.Integer, nullable=True)
    unit_key = db.Column(db.String(128), nullable=False, default="")
    unit_count = db.Column(db.Integer, nullable=True)
    children = db.relationship(
        "Execution",
        backref=db.backref("parent", remote_side=[id]),
        foreign_keys=[parent_id])
    # Buildbot slot-builder index this execution was submitted to
    # (slot-builders: one builder per admitted concurrency slot — a
    # (builder, worker) pair serializes, so N slots need N builders).
    bb_slot = db.Column(db.Integer, nullable=True)
    # Runner lease (§6): heartbeats keep the assignment alive; offline
    # heartbeat means UNKNOWN, never silent duplicate dispatch.
    lease_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    heartbeat_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # Actionable cause while QUEUED (which capacity/policy gate blocks
    # dispatch) — a wait is never a silent stall. Cleared on launch.
    wait_reason = db.Column(db.String(256), nullable=False, default="")
    # Optional queue deadline (queue_timeout_seconds in run_config):
    # a QUEUED execution past it fails with an explicit queue reason.
    queue_deadline_at = db.Column(db.DateTime(timezone=True),
                                  nullable=True)
    # CI API idempotency: a client-supplied reference; a repeated POST
    # with the same (token, client_ref) returns this execution instead
    # of double-launching — same contract as SubmitToken for the UI.
    client_ref = db.Column(db.String(128), nullable=False, default="",
                           index=True)
    # Webhook fan-out marker: set once every enabled endpoint has been
    # enqueued for this execution's terminal/UNKNOWN state — durable so
    # a crash between transition and enqueue is retried, never lost.
    notified_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # Report publication to the bound source repo
    # (TestOrchestrator/Reports/<execution-id>/ at repo ROOT) —
    # distinct from evidence export and from the test outcome.
    report_pub_status = db.Column(db.String(16), nullable=False,
                                  default="none")   # none|pending|published|failed
    report_pub_repo = db.Column(db.String(128), nullable=False, default="")
    report_pub_branch = db.Column(db.String(64), nullable=False, default="")
    report_pub_dir = db.Column(db.String(512), nullable=False, default="")
    report_pub_sha = db.Column(db.String(64), nullable=False, default="")
    report_pub_digest = db.Column(db.String(64), nullable=False, default="")
    report_pub_error = db.Column(db.String(512), nullable=False, default="")
    # Synchronized deletion: commit-removal of the recorded dir on the
    # recorded branch — never history rewrite, never force-push.
    deletion_status = db.Column(db.String(16), nullable=False,
                                default="none")     # none|pending|failed
    deletion_error = db.Column(db.String(512), nullable=False, default="")
    # ---- result import (origin + provenance + dedup) ----
    # live = dispatched by this app; imported = repository-result import.
    # The common history is the same table either way.
    origin = db.Column(db.String(16), nullable=False, default="live")
    # Producer framework — 'katalon' today; the model is
    # framework-independent so future parsers (robot/cypress/playwright)
    # reuse these columns instead of inventing parallel history.
    framework = db.Column(db.String(32), nullable=False,
                          default="katalon")
    # evidence format ('junit' today) — kept separate from framework:
    # a bare JUnit file proves the format, not the producer
    result_format = db.Column(db.String(32), nullable=False,
                              default="junit")
    imported_at = db.Column(db.DateTime(timezone=True), nullable=True)
    imported_by = db.Column(db.String(64), nullable=False, default="")
    # Dedup identities — populated at normalize for live runs and at
    # parse for imported ones; empty means "not fingerprintable".
    run_uuid = db.Column(db.String(64), nullable=False, default="")
    content_hash = db.Column(db.String(64), nullable=False, default="")
    # Persisted suite keys [[id|name, timestamp], ...] — lets dedup's
    # suite/timestamp overlap check run in memory instead of re-reading
    # junit evidence per row. NULL = never fingerprinted; "[]" =
    # fingerprinted, nothing found (evidence missing/unreadable).
    suite_keys_json = db.Column(db.Text, nullable=True)
    # Import provenance — empty for live executions.
    source_repo = db.Column(db.String(512), nullable=False, default="")
    source_commit = db.Column(db.String(64), nullable=False, default="")
    source_path = db.Column(db.String(512), nullable=False, default="")
    project = db.relationship("Project")
    suite = db.relationship("Suite")
    runner = db.relationship("Runner")
    runtime = db.relationship("RuntimeConnection")
    schedule = db.relationship("Schedule")
    source_item = db.relationship("DiscoveredItem")

    def snapshot(self) -> dict:
        return json.loads(self.snapshot_json or "{}")

    def suite_keys(self) -> list | None:
        """Persisted [[id|name, timestamp], ...] suite keys.
        None = never fingerprinted; [] = fingerprinted, no evidence."""
        if self.suite_keys_json is None:
            return None
        try:
            return [tuple(k) for k in json.loads(self.suite_keys_json)]
        except (TypeError, ValueError):
            return []


class ResourceReservation(db.Model):
    """Durable resource-ownership ledger (§6). Written atomically in
    the assignment transaction — a held slot/proc charge has a row
    keyed to its owning execution before the run ever launches.
    ``released_at`` is set only when the execution reaches a
    CONFIRMED terminal state: UNKNOWN keeps its reservation open
    because the process may still own the resource. Capacity
    admission still derives from execution states (proven CAS);
    this table is the ownership record — whose resource, how much,
    held from when until which terminal outcome."""

    __tablename__ = "resource_reservations"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    execution_id = db.Column(db.String(32), db.ForeignKey("executions.id"),
                             nullable=False, index=True)
    runner_id = db.Column(db.String(32), nullable=False, index=True)
    resource_kind = db.Column(db.String(16), nullable=False)  # slot|procs
    slot_index = db.Column(db.Integer, nullable=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    acquired_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    released_at = db.Column(db.DateTime(timezone=True), nullable=True)
    release_state = db.Column(db.String(32), nullable=False, default="")
    execution = db.relationship("Execution", backref=db.backref(
        "reservations", cascade="all, delete-orphan"))


class CaseStatus(str, enum.Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class TestResult(db.Model):
    """Canonical parsed result row (§8). Source case identity is kept
    verbatim — case names alone are not unique."""

    __tablename__ = "test_results"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    execution_id = db.Column(db.String(32), db.ForeignKey("executions.id"),
                             nullable=False, index=True)
    suite_entry_order = db.Column(db.Integer, nullable=True)
    case_ref = db.Column(db.String(512), nullable=False, default="")
    case_name = db.Column(db.String(512), nullable=False)
    classname = db.Column(db.String(512), nullable=False, default="")
    iteration = db.Column(db.Integer, nullable=False, default=0)
    attempt = db.Column(db.Integer, nullable=False, default=1)
    status = db.Column(db.Enum(CaseStatus), nullable=False)
    duration_s = db.Column(db.Float, nullable=False, default=0.0)
    message = db.Column(db.Text, nullable=False, default="")
    # Unresolved when producer output cannot be mapped to a source case
    # deterministically — exposed honestly, never guessed (§8).
    unresolved = db.Column(db.Boolean, nullable=False, default=False)
    execution = db.relationship("Execution", backref=db.backref(
        "results", cascade="all, delete-orphan"))


class Artifact(db.Model):
    """Evidence file with integrity metadata and per-artifact publication
    state (§9). Paths are validated; content checksummed."""

    __tablename__ = "artifacts"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    execution_id = db.Column(db.String(32), db.ForeignKey("executions.id"),
                             nullable=False, index=True)
    result_id = db.Column(db.String(32), db.ForeignKey("test_results.id"),
                          nullable=True)
    kind = db.Column(db.String(32), nullable=False)  # junit|log|screenshot|video|har|html|other
    rel_path = db.Column(db.String(512), nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    size = db.Column(db.Integer, nullable=False)
    mime = db.Column(db.String(128), nullable=False, default="")
    # Filename association alone is insufficient; unmapped files stay at
    # run/suite level and are labelled unmapped.
    mapped = db.Column(db.Boolean, nullable=False, default=True)
    collected_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    execution = db.relationship("Execution", backref=db.backref(
        "artifacts", cascade="all, delete-orphan"))


class OutboxEvent(db.Model):
    """Durable dispatch/sync/publish outbox (§7, §11). Processed by
    `flask worker` outside request handlers; survives restarts."""

    __tablename__ = "outbox_events"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    kind = db.Column(db.String(32), nullable=False)      # orchestrator-dispatch|git-sync|rp-publish|binding-sync|runner-heartbeat
    payload_json = db.Column(db.Text, nullable=False, default="{}")
    status = db.Column(db.String(16), nullable=False, default="pending")
    attempts = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.Text, nullable=False, default="")
    available_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    processed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def payload(self) -> dict:
        return json.loads(self.payload_json or "{}")


class SecretRef(db.Model):
    """Protected secret store (§12). Values encrypted with a key held OUTSIDE
    the database; only references appear in settings/snapshots. Rotation is
    audited by metadata only — secret bytes never enter logs."""

    __tablename__ = "secret_refs"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    name = db.Column(db.String(64), nullable=False, unique=True)
    kind = db.Column(db.String(32), nullable=False)      # git|buildbot|ssh|runner|runtime
    encrypted = db.Column(db.LargeBinary, nullable=False)
    meta_json = db.Column(db.Text, nullable=False, default="{}")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    rotated_at = db.Column(db.DateTime(timezone=True), nullable=True)


class SubmitToken(db.Model):
    """Submission-idempotency record for Run now (and future manual
    triggers). Unique (schedule_id, username, token) — a repeated
    submission with the same token maps back to the SAME created
    execution instead of double-launching. The unique constraint is
    the only thing that survives concurrent requests/processes."""
    __tablename__ = "submit_tokens"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    schedule_id = db.Column(db.String(32), db.ForeignKey("schedules.id"),
                            nullable=False)
    username = db.Column(db.String(64), nullable=False, default="")
    token = db.Column(db.String(64), nullable=False, default="")
    execution_id = db.Column(db.String(32), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    __table_args__ = (db.UniqueConstraint(
        "schedule_id", "username", "token",
        name="uq_submit_tokens"),)


class ApiToken(db.Model):
    """CI/CD API credential (§12). Only a SHA-256 of the token is
    persisted — the bearer value is shown once at creation and is
    unrecoverable afterwards. Revocation is a tombstone: the row
    survives for audit, the credential stops working immediately."""
    __tablename__ = "api_tokens"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    name = db.Column(db.String(128), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    # Portal-equivalent capability tier: 'read' (poll status/results)
    # or 'run' (additionally trigger/cancel/retry). Tokens are never
    # admin — infrastructure/secrets/users stay UI-only.
    scope = db.Column(db.String(16), nullable=False, default="read")
    # Same project-scoping contract as User.allowed_projects.
    allowed_projects = db.Column(db.Text, nullable=False, default="[]")
    created_by = db.Column(db.String(64), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    last_used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def project_allowed(self, project_id: str) -> bool:
        allowed = json.loads(self.allowed_projects or "[]")
        return not allowed or project_id in allowed


class WebhookEndpoint(db.Model):
    """Outbound notification subscription (§integrations). Matching
    executions are delivered via the durable outbox — a subscriber
    outage retries with backoff, never drops the event silently.
    `secret_ref` names a SecretRef (kind 'webhook') holding the HMAC
    key; the key bytes never appear in this row, logs, or audit."""
    __tablename__ = "webhook_endpoints"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    name = db.Column(db.String(128), nullable=False)
    url = db.Column(db.String(1024), nullable=False)
    # Comma-separated execution states to notify on — e.g.
    # "completed,failed,canceled"; 'unknown' is opt-in.
    events = db.Column(db.String(256), nullable=False,
                      default="completed,failed,canceled")
    secret_ref = db.Column(db.String(64), nullable=False, default="")
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    # Empty = all projects; otherwise JSON list of project ids.
    project_scope = db.Column(db.Text, nullable=False, default="[]")
    created_by = db.Column(db.String(64), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    def event_set(self) -> set:
        return {e.strip().lower() for e in self.events.split(",")
                if e.strip()}

    def project_allowed(self, project_id: str) -> bool:
        allowed = json.loads(self.project_scope or "[]")
        return not allowed or project_id in allowed


class DiscoveredItem(db.Model):
    """Typed discovered inventory from a source binding (§onboarding).
    Rebuilt atomically per discovered SHA — a failed fetch/discovery
    never erases the last good set."""
    __tablename__ = "discovered_items"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    binding_id = db.Column(db.String(32), db.ForeignKey("git_bindings.id"),
                           nullable=False)
    project_id = db.Column(db.String(32), db.ForeignKey("projects.id"),
                           nullable=False)
    sha = db.Column(db.String(64), nullable=False, default="")
    kind = db.Column(db.String(16), nullable=False)   # case|suite|collection|profile
    path = db.Column(db.String(512), nullable=False)  # repo-relative
    name = db.Column(db.String(256), nullable=False, default="")
    parent = db.Column(db.String(512), nullable=False, default="")
    order_idx = db.Column(db.Integer, nullable=False, default=0)
    dynamic = db.Column(db.Boolean, nullable=False, default=False)
    warnings = db.Column(db.Text, nullable=False, default="")
    members_json = db.Column(db.Text, nullable=False, default="[]")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    def members(self) -> list:
        import json as _j
        try:
            return _j.loads(self.members_json or "[]")
        except Exception:
            return []


class ImportSession(db.Model):
    """Durable preview bookkeeping for repository-result import.
    Workflow state only — never an execution-history record. A preview
    can be re-confirmed after restart or safely retried after failure."""
    __tablename__ = "import_sessions"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    project_id = db.Column(db.String(32), db.ForeignKey("projects.id"),
                           nullable=False)
    binding_id = db.Column(db.String(32), db.ForeignKey("git_bindings.id"),
                           nullable=False)
    revision_input = db.Column(db.String(256), nullable=False, default="")
    resolved_sha = db.Column(db.String(64), nullable=False, default="")
    result_dir = db.Column(db.String(512), nullable=False, default="")
    staging_path = db.Column(db.String(512), nullable=False, default="")
    created_by = db.Column(db.String(64), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    # previewed|committed|failed — 'committed' may still carry failed
    # per-candidate outcomes (retryable).
    status = db.Column(db.String(16), nullable=False, default="previewed")
    summary_json = db.Column(db.Text, nullable=False, default="{}")
    error = db.Column(db.Text, nullable=False, default="")
    project = db.relationship("Project")

    def summary(self) -> dict:
        return json.loads(self.summary_json or "{}")


class ImportCandidate(db.Model):
    """One detected run inside an ImportSession, with its dedup
    decision and import outcome."""
    __tablename__ = "import_candidates"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    session_id = db.Column(db.String(32),
                           db.ForeignKey("import_sessions.id"),
                           nullable=False, index=True)
    seq = db.Column(db.Integer, nullable=False, default=0)
    run_dir = db.Column(db.String(512), nullable=False, default="")
    # member report dirs (rel to scan root) — collection runs span
    # several dirs sharing one execution.uuid
    run_dirs_json = db.Column(db.Text, nullable=False, default="[]")
    # new|duplicate|supplement|ambiguous|unsupported|malformed
    decision = db.Column(db.String(16), nullable=False, default="new")
    match_execution_id = db.Column(db.String(32), nullable=False,
                                   default="")
    run_uuid = db.Column(db.String(64), nullable=False, default="")
    content_hash = db.Column(db.String(64), nullable=False, default="")
    suite_name = db.Column(db.String(512), nullable=False, default="")
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    case_count = db.Column(db.Integer, nullable=False, default=0)
    warnings_json = db.Column(db.Text, nullable=False, default="[]")
    # pending|imported|skipped|failed
    status = db.Column(db.String(16), nullable=False, default="pending")
    imported_execution_id = db.Column(db.String(32), nullable=False,
                                      default="")
    error = db.Column(db.Text, nullable=False, default="")
    session = db.relationship("ImportSession",
                              backref=db.backref(
                                  "candidates",
                                  order_by="ImportCandidate.seq"))

    def warnings(self) -> list:
        return json.loads(self.warnings_json or "[]")

    def run_dirs(self) -> list:
        return json.loads(self.run_dirs_json or "[]")


class ImportLock(db.Model):
    """Single-writer claim on a project's import-confirm path.

    Concurrent confirmations of the same candidate (same or different
    sessions) must not both insert executions — for uuid-less content
    no unique index can arbitrate. The lock row serializes confirmers;
    a crashed claim is reclaimable after CONFIRM_CLAIM_STALE_S."""
    __tablename__ = "import_locks"
    project_id = db.Column(db.String(32), primary_key=True)
    session_id = db.Column(db.String(32), nullable=False, default="")
    claimed_by = db.Column(db.String(64), nullable=False, default="")
    claimed_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=utcnow)


class AuthoringDraft(db.Model):
    """A user-owned editable draft of a native suite/collection —
    isolated from execution checkouts; published only after diff
    review against the recorded base revision."""
    __tablename__ = "authoring_drafts"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    binding_id = db.Column(db.String(32),
                           db.ForeignKey("git_bindings.id"),
                           nullable=False)
    project_id = db.Column(db.String(32), nullable=False)
    owner = db.Column(db.String(128), nullable=False)
    kind = db.Column(db.String(16), nullable=False)
    path = db.Column(db.String(512), nullable=False)
    base_sha = db.Column(db.String(64), nullable=False, default="")
    members_json = db.Column(db.Text, nullable=False, default="[]")
    # suite/collection-level editable settings (verified .ts fields)
    settings_json = db.Column(db.Text, nullable=False, default="{}")
    # per-member config: ref -> {isRun, isReuseDriver, ...}
    member_cfg_json = db.Column(db.Text, nullable=False,
                                default="{}")
    name = db.Column(db.String(256), nullable=False, default="")
    status = db.Column(db.String(16), nullable=False, default="draft")
    published_sha = db.Column(db.String(64), nullable=False,
                              default="")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow,
                           onupdate=utcnow)

    def members(self) -> list:
        return json.loads(self.members_json or "[]")

    def settings(self) -> dict:
        return json.loads(self.settings_json or "{}")

    def member_cfg(self) -> dict:
        return json.loads(self.member_cfg_json or "{}")
