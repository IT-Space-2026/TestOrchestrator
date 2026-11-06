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
    # remote: the unique Dagu worker.labels tag the installation is
    # bound to — empty => local (portal-host) execution.
    worker_label = db.Column(db.String(128), nullable=False, default="")
    runner = db.relationship("Runner", backref="runtimes")


class ExecutionState(str, enum.Enum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"      # handed to Dagu/outbox, awaiting runner
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
    # Allure reporting is a third independent status (besides execution
    # and local-native report) — ready|partial|missing|failed.
    allure_status = db.Column(db.String(16), nullable=False,
                              default="pending")
    allure_path = db.Column(db.String(512), nullable=False, default="")
    allure_version = db.Column(db.String(32), nullable=False, default="")
    exit_status = db.Column(db.Integer, nullable=True)
    error_kind = db.Column(db.String(32), nullable=False, default="")
    error_text = db.Column(db.Text, nullable=False, default="")
    cancel_reason = db.Column(db.String(256), nullable=False, default="")
    # Runner lease (§6): heartbeats keep the assignment alive; offline
    # heartbeat means UNKNOWN, never silent duplicate dispatch.
    lease_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    heartbeat_at = db.Column(db.DateTime(timezone=True), nullable=True)
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
    project = db.relationship("Project")
    suite = db.relationship("Suite")
    runner = db.relationship("Runner")
    runtime = db.relationship("RuntimeConnection")
    schedule = db.relationship("Schedule")
    source_item = db.relationship("DiscoveredItem")

    def snapshot(self) -> dict:
        return json.loads(self.snapshot_json or "{}")


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
    kind = db.Column(db.String(32), nullable=False)      # dagu-dispatch|local-dispatch|git-sync|rp-publish|binding-sync|runner-heartbeat
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
    kind = db.Column(db.String(32), nullable=False)      # git|dagu|runner|runtime
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
