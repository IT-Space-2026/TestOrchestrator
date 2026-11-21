"""Operational Logs — structured component events, distinct from the
Audit trail. Admin-only; sources are allowlisted (never arbitrary fs
paths); remote worker logs are NOT collected and are shown as
unavailable honestly."""
import re
from datetime import datetime, timedelta, timezone
from .extensions import db
from .settings import DEFAULTS

SEVERITIES = ("debug", "info", "warning", "error", "critical")
CATEGORIES = ("system", "dispatch", "execution", "report",
              "worker", "auth", "config", "storage")


class OperationalLog(db.Model):
    __tablename__ = "operational_logs"
    id = db.Column(db.Integer, primary_key=True)
    occurred_at = db.Column(db.DateTime(timezone=True), nullable=False,
                            default=lambda: datetime.now(timezone.utc),
                            index=True)
    severity = db.Column(db.String(12), nullable=False, default="info",
                         index=True)
    component = db.Column(db.String(64), nullable=False, index=True)
    category = db.Column(db.String(32), nullable=False, default="system")
    project_id = db.Column(db.String(64), index=True)
    execution_id = db.Column(db.String(64), index=True)
    worker_id = db.Column(db.String(64), index=True)
    correlation_id = db.Column(db.String(64), index=True)
    source = db.Column(db.String(64), nullable=False, default="portal")
    message = db.Column(db.String(512), nullable=False)
    details = db.Column(db.Text, default="")


def _now():
    return datetime.now(timezone.utc)


_PRUNE_EVERY = 64
_emits_since_prune = 0


def emit(component: str, message: str, *, severity: str = "info",
         category: str = "system", project_id=None, execution_id=None,
         worker_id=None, correlation_id=None, details: str = "",
         source: str = "portal") -> OperationalLog:
    """Append one structured operational event; prune to retention."""
    row = OperationalLog(
        component=component[:64],
        message=redact(message)[:512],
        severity=severity if severity in SEVERITIES else "info",
        category=category if category in CATEGORIES else "system",
        project_id=project_id, execution_id=execution_id,
        worker_id=worker_id, correlation_id=correlation_id,
        source=source,
        details=redact(details) if details else "")
    db.session.add(row)
    db.session.commit()
    # Optional structured output — fail-open, never weakens the DB row.
    try:
        from . import log_output, logschema
        log_output.publish(logschema.operational_event(row))
    except Exception:
        pass
    # Prune amortized — a full count/delete scan on every emit turns
    # hot paths (worker tick, dispatch) into O(n) per log line.
    global _emits_since_prune
    _emits_since_prune += 1
    if _emits_since_prune >= _PRUNE_EVERY:
        _emits_since_prune = 0
        prune()
    return row


def prune() -> None:
    """Retention/rotation independent of audit: age + row cap."""
    days = int(DEFAULTS.get("logs_retention_days", 30))
    max_rows = int(DEFAULTS.get("logs_max_rows", 20000))
    cutoff = _now() - timedelta(days=days)
    OperationalLog.query.filter(OperationalLog.occurred_at
                                < cutoff).delete(synchronize_session=False)
    excess = OperationalLog.query.count() - max_rows
    if excess > 0:
        oldest = (OperationalLog.query
                  .order_by(OperationalLog.occurred_at)
                  .limit(excess).all())
        for r in oldest:
            db.session.delete(r)
    db.session.commit()


SECRET_RE = re.compile(
    r"(?i)[\"']?(password|passwd|passphrase|secret|token|apikey|api_key"
    r"|api-key|private_key|signing_key|licen[cs]e_key|client_secret"
    r"|credential|authorization|auth_header|cookie|set-cookie"
    r"|x-api-key|bearer)[\"']?\s*[:=]\s*[\"']?\S+")

_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")


def redact(text: str) -> str:
    """Redact secret-looking values in free text — applied BEFORE
    persistence (emit/record_audit) so local rows, CSV and JSON output
    share the same redaction."""
    if not text:
        return ""
    # Bearer first: a quote-tolerant SECRET_RE could otherwise consume
    # the "Bearer" keyword as the value of e.g. Authorization: "Bearer…"
    text = _BEARER_RE.sub("Bearer •••", str(text))
    return SECRET_RE.sub(
        lambda m: m.group(0).split(m.group(1))[0] + m.group(1) + "=•••",
        text)


def query(filters: dict):
    q = OperationalLog.query
    if v := filters.get("severity"):
        q = q.filter_by(severity=v)
    if v := filters.get("component"):
        q = q.filter_by(component=v)
    if v := filters.get("category"):
        q = q.filter_by(category=v)
    if v := filters.get("project_id"):
        q = q.filter_by(project_id=v)
    if v := filters.get("execution_id"):
        q = q.filter_by(execution_id=v)
    if v := filters.get("worker_id"):
        q = q.filter_by(worker_id=v)
    if v := filters.get("correlation_id"):
        q = q.filter_by(correlation_id=v)
    if v := filters.get("q"):
        q = q.filter(OperationalLog.message.contains(v))
    if v := filters.get("since"):
        q = q.filter(OperationalLog.occurred_at >= v)
    if v := filters.get("until"):
        q = q.filter(OperationalLog.occurred_at <= v)
    return q.order_by(OperationalLog.occurred_at.desc(),
                      OperationalLog.id.desc())
