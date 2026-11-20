"""Versioned structured event schema — one representation for audit and
operational events (docs/external-logging.md).

``schema_version`` is ``to.log.event/1.0``: additive optional fields may
appear in patch releases; consumers must ignore unknown keys. Removing or
retyping a field bumps the version. The shape maps cleanly onto
OpenTelemetry attributes — it is deliberately NOT labelled OTLP.

Field meanings:

- ``event_id``        unique per emitted event (uuid4 hex); preserved
                      end-to-end so downstream collectors can deduplicate.
- ``timestamp_utc``   event time, ISO-8601 with explicit ``Z``.
- ``log_type``        ``audit`` | ``operational`` — always distinguishable.
- ``severity``        debug|info|warning|error|critical (audit = info).
- ``category``        free-form category within the log_type vocabulary.
- ``event_name``      dotted action name (audit) or "" when not modelled.
- ``message``         human summary, redacted before it reaches here.
- ``outcome``         success|failure|denied|"" — blank when not modelled.
- ``component``       emitting component (portal, scheduler, dispatch…).
- ``host``/``instance``/``revision``  process/host identity for
                      multi-instance correlation; ``revision`` is the
                      deployed-revision string (git head or build stamp).
- actor/project/execution/parent/unit/attempt/target/buildbot/request
                      identifiers — present only when the source row
                      carries them; never invented.
- ``trace_id``/``span_id`` — optional correlation fields for callers that
                      already hold tracing context; absent otherwise.
- ``details``         structured object — never a dumped request body or
                      environment; secret-shaped values are redacted.
"""

import json
import re
import socket
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = "to.log.event/1.0"
LOG_TYPES = ("audit", "operational")

_OPTIONAL_IDS = (
    "actor_id", "actor_name", "project_id", "execution_id",
    "parent_execution_id", "unit_id", "attempt_id", "target_id",
    "buildbot_id", "request_id", "trace_id", "span_id",
)

# Key/identifier names whose VALUES are secrets — redacted wherever they
# appear (message text, details keys, details JSON). Broader than a UI
# hint: this is the last barrier before persistence AND every output.
SENSITIVE_KEY_RE = re.compile(
    r"(?i)(pass(word|wd|phrase)?|secret|token|api[_-]?key|private[_-]?key"
    r"|signing[_-]?key|licen[cs]e[_-]?key|client[_-]?secret|credential"
    r"|authorization|auth[_-]?header|cookie|bearer|session)")

_PLACEHOLDER = "•••"


def redact_text(text: str) -> str:
    """Redact ``key=value`` / ``key: value`` / Bearer secrets in text."""
    from .logs import SECRET_RE  # single source for the text pattern
    if not text:
        return ""
    return SECRET_RE.sub(
        lambda m: m.group(0).split(m.group(1))[0]
        + m.group(1) + "=" + _PLACEHOLDER, str(text))


def redact_value(key: str, value):
    """Redact one details value: secret-keyed values wholesale, strings
    by pattern, containers recursively."""
    if SENSITIVE_KEY_RE.search(str(key)):
        return _PLACEHOLDER
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value("", v) for v in value]
    return value


def redact_details(details):
    """Normalize details into a redacted JSON-safe object.

    Strings that happen to contain JSON are parsed first so nested
    secret keys are still caught; anything else becomes {"text": …}."""
    if details in (None, "", {}):
        return {}
    obj = details
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except (ValueError, TypeError):
            obj = {"text": obj}
    if isinstance(obj, dict):
        return {k: redact_value(k, v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_value("", v) for v in obj]
    return {"text": redact_text(str(obj))}


def _utc_iso(dt) -> str:
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _revision() -> str:
    """Deployed revision — git head or packaged build stamp, best-effort."""
    from .deploy_info import deployed_revision
    try:
        info = deployed_revision()
        return (info.get("git_head") or info.get("build_stamp")
                or info.get("version_file") or "")
    except Exception:
        return ""


def event(*, log_type: str, event_name: str = "", message: str = "",
          severity: str = "info", category: str = "", outcome: str = "",
          component: str = "portal", occurred_at=None, event_id: str = "",
          details=None, **identifiers) -> dict:
    """Build one schema-conforming event dict.

    Unknown ``identifiers`` keys raise — typos in identifier wiring are a
    schema bug, not data. Optional id fields are emitted only when set.
    """
    if log_type not in LOG_TYPES:
        raise ValueError(f"log_type must be one of {LOG_TYPES}")
    unknown = set(identifiers) - set(_OPTIONAL_IDS)
    if unknown:
        raise TypeError(f"unknown event identifier(s): {sorted(unknown)}")
    ev = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id or uuid.uuid4().hex,
        "timestamp_utc": _utc_iso(occurred_at),
        "log_type": log_type,
        "severity": severity or "info",
        "category": category or ("audit" if log_type == "audit"
                                 else "system"),
        "event_name": event_name or "",
        "message": redact_text(message),
        "outcome": outcome or "",
        "component": component or "portal",
        "host": socket.gethostname(),
        "instance": _instance_name(),
        "revision": _revision(),
        "details": redact_details(details),
    }
    for key in _OPTIONAL_IDS:
        value = identifiers.get(key)
        if value not in (None, ""):
            ev[key] = str(value)
    return ev


def _instance_name() -> str:
    from .config import INSTANCE_DIR
    return INSTANCE_DIR.name


def to_json_line(ev: dict) -> str:
    """One event as a single NDJSON line — ASCII-safe separators, no
    embedded newlines."""
    return json.dumps(ev, ensure_ascii=False, separators=(",", ":"),
                      default=str)


def audit_event(row) -> dict:
    """Map an ``audit_events`` row onto the schema."""
    return event(
        log_type="audit", event_id=f"audit-{row.id}",
        occurred_at=row.occurred_at, event_name=row.event,
        severity="info", category="audit", component="audit-trail",
        actor_name=row.username, message=row.detail,
        details={"detail": row.detail} if row.detail else {})


def operational_event(row) -> dict:
    """Map an ``operational_logs`` row onto the schema."""
    return event(
        log_type="operational", event_id=f"op-{row.id}",
        occurred_at=row.occurred_at, severity=row.severity,
        category=row.category, component=row.component or row.source,
        project_id=row.project_id, execution_id=row.execution_id,
        target_id=row.worker_id, request_id=row.correlation_id,
        message=row.message, details=row.details)
