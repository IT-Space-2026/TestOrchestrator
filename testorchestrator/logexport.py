"""Log export engine — CSV export for the Audit Trail and Operational
Logs (same engine, distinct categories; no second log system).

Semantics (docs/external-logging.md, Admin manual §Logging):

- Exports apply the caller's current filters to ALL matching rows —
  never just the rendered page — against a fixed ``cutoff_utc`` captured
  at request time, so records arriving mid-export cannot bleed in.
- Ordering is stable and identical to the pages: occurred_at DESC,
  id DESC.
- UTF-8 CSV (BOM so spreadsheets decode Unicode), explicit UTC
  timestamps, headers. csv-module quoting handles commas/quotes/CR/LF;
  formula-injection protection additionally prefixes spreadsheet-active
  leading characters — quoting alone is not relied on.
- Values pass through the same redaction applied before persistence.
- ≤ ``logs_export_inline_max`` rows: streamed directly in the request
  (no temp file). Above that: a background job writes a protected file
  (mode 0600 under the instance dir), visible on a status page, and
  expires after ``logs_export_ttl_minutes`` — downloads re-check the
  caller is an Administrator and the job is unexpired.
- ``logs_export_max_rows`` is a hard cap: beyond it the request fails
  loudly ("narrow the filters") — exports are never silently truncated.
- Every request/completion/failure/download is audited with filters,
  count, actor and export id — never exported content.
"""

import csv
import io
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

from .extensions import db

COLUMNS = [
    "event_id", "timestamp_utc", "log_type", "severity", "category",
    "event_name", "actor_id", "actor_name", "project_id",
    "execution_id", "parent_execution_id", "unit_id", "attempt_id",
    "target_id", "request_id", "outcome", "message", "details_json",
]

# Spreadsheet-active leading characters — formula injection protection
# beyond CSV quoting (CSV injection via =, +, -, @, tab, CR).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

_jobs_lock = threading.Lock()
_jobs_running = 0
MAX_JOBS = 2


def _now():
    return datetime.now(timezone.utc)


def _utc_iso(dt) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class LogExportJob(db.Model):
    """Background export request — record survives the file so audit +
    status remain after expiry."""

    __tablename__ = "log_export_jobs"

    id = db.Column(db.String(32), primary_key=True,
                   default=lambda: uuid.uuid4().hex)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=_now)
    created_by = db.Column(db.String(64), nullable=False)
    kind = db.Column(db.String(16), nullable=False)  # audit | operational
    filters_json = db.Column(db.Text, nullable=False, default="{}")
    cutoff_utc = db.Column(db.DateTime(timezone=True), nullable=False)
    status = db.Column(db.String(12), nullable=False, default="queued")
    row_count = db.Column(db.Integer, nullable=False, default=0)
    error = db.Column(db.String(256), nullable=False, default="")
    file_name = db.Column(db.String(128), nullable=False, default="")
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------- rows


def audit_query(filters: dict):
    """Same filter contract as the Audit page — export scope == view."""
    from .models import AuditEvent
    q = AuditEvent.query
    if v := (filters.get("q") or "").strip():
        like = f"%{v}%"
        q = q.filter(db.or_(AuditEvent.event.like(like),
                            AuditEvent.detail.like(like),
                            AuditEvent.username.like(like)))
    if v := (filters.get("event") or "").strip():
        q = q.filter(AuditEvent.event == v)
    if v := (filters.get("actor") or "").strip():
        q = q.filter(AuditEvent.username == v)
    if v := (filters.get("related") or "").strip():
        q = q.filter(AuditEvent.detail.like(f"%{v}%"))
    for bound in ("from", "to"):
        raw = (filters.get(bound) or "").strip()
        if raw:
            try:
                dt = datetime.fromisoformat(raw)
                if bound == "to" and len(raw) == 10:
                    dt = dt + timedelta(days=1)
                q = q.filter(AuditEvent.occurred_at >= dt
                             if bound == "from"
                             else AuditEvent.occurred_at <= dt)
            except ValueError:
                pass
    return q.order_by(AuditEvent.occurred_at.desc(),
                      AuditEvent.id.desc())


def _audit_row(r) -> list:
    from .logschema import redact_text
    return [f"audit-{r.id}", _utc_iso(r.occurred_at), "audit", "info",
            "audit", r.event, "", r.username, "", "", "", "", "", "",
            "", "", redact_text(r.detail),
            json.dumps({"detail": redact_text(r.detail)},
                       ensure_ascii=False) if r.detail else ""]


def _operational_row(r) -> list:
    from .logschema import redact_text, redact_details
    details = redact_details(r.details)
    return [f"op-{r.id}", _utc_iso(r.occurred_at), "operational",
            r.severity, r.category, "", "", "", r.project_id or "",
            r.execution_id or "", "", "", "", r.worker_id or "",
            r.correlation_id or "", "", redact_text(r.message),
            json.dumps(details, ensure_ascii=False, default=str)
            if details else ""]


def query_for(kind: str, filters: dict):
    if kind == "audit":
        return audit_query(filters)
    from . import logs
    return logs.query(filters)


def _row_fn(kind: str):
    return _audit_row if kind == "audit" else _operational_row


# ----------------------------------------------------------------- CSV


def csv_field(value) -> str:
    """One CSV cell: stringified, formula-injection guarded. Quoting for
    commas/quotes/newlines is left to the csv writer."""
    s = "" if value is None else str(value)
    if s.startswith(_FORMULA_PREFIXES):
        s = "'" + s
    return s


def csv_lines(kind: str, q, cutoff):
    """Yield CSV text — header first, then one chunk per row batch.
    Caller passes a query already ordered + cutoff-bounded."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    buf.write("\ufeff")
    w.writerow(COLUMNS)
    yield buf.getvalue()
    buf.seek(0)
    buf.truncate()
    row_fn = _row_fn(kind)
    for r in q.yield_per(500):
        w.writerow([csv_field(v) for v in row_fn(r)])
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate()


def export_filename(kind: str, job_id: str, when) -> str:
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    return f"testorchestrator-{kind}-{stamp}-{job_id[:8]}.csv"


def stream_csv(app, kind: str, filters: dict, cutoff):
    """Streaming generator that keeps an app context alive — Response
    consumes it after the view returns and the request context is gone.
    Query + iteration both happen inside."""
    def gen():
        with app.app_context():
            q = query_for(kind, filters).filter(
                _cutoff_col(kind) <= cutoff)
            yield from csv_lines(kind, q, cutoff)
    return gen()


def stream_jsonl(app, kind: str, filters: dict, cutoff):
    def gen():
        with app.app_context():
            from . import logschema
            q = query_for(kind, filters).filter(
                _cutoff_col(kind) <= cutoff)
            for r in q.yield_per(500):
                ev = (logschema.audit_event(r) if kind == "audit"
                      else logschema.operational_event(r))
                yield logschema.to_json_line(ev) + "\n"
    return gen()


# --------------------------------------------------------------- jobs


def _export_dir():
    from .config import INSTANCE_DIR
    d = INSTANCE_DIR / "log-exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _limits() -> dict:
    from . import settings as settings_mod

    def _g(key, default):
        try:
            return int(settings_mod.get(key, default))
        except Exception:
            return default

    return {"inline_max": _g("logs_export_inline_max", 10000),
            "max_rows": _g("logs_export_max_rows", 250000),
            "ttl_minutes": _g("logs_export_ttl_minutes", 60)}


def purge_expired() -> None:
    """Expire stale jobs + delete their files; interrupted queued/running
    jobs past TTL are marked failed (file fragment removed)."""
    now = _now()
    stale = LogExportJob.query.filter(
        LogExportJob.expires_at < now,
        LogExportJob.status.in_(("queued", "running", "done"))).all()
    for job in stale:
        if job.file_name:
            try:
                (_export_dir() / job.file_name).unlink(missing_ok=True)
                (_export_dir() / (job.file_name + ".part")).unlink(
                    missing_ok=True)
            except OSError:
                pass
        job.status = ("failed" if job.status in ("queued", "running")
                      else "expired")
        if job.status == "failed" and not job.error:
            job.error = "interrupted — job never completed before expiry"
    if stale:
        db.session.commit()


def request_export(kind: str, filters: dict, username: str):
    """Decide inline-stream vs background job. Returns
    (mode, job_or_None, count) — raises TooManyRowsError over the hard
    cap so the caller fails loudly instead of truncating."""
    purge_expired()
    cutoff = _now()
    q = query_for(kind, filters)
    count = q.count()
    limits = _limits()
    if count > limits["max_rows"]:
        raise TooManyRowsError(count, limits["max_rows"])
    if count <= limits["inline_max"]:
        return "inline", None, count, cutoff
    global _jobs_running
    with _jobs_lock:
        if _jobs_running >= MAX_JOBS:
            raise BusyError()
        _jobs_running += 1
    job = LogExportJob(
        created_by=username, kind=kind,
        filters_json=json.dumps(filters, ensure_ascii=False, default=str),
        cutoff_utc=cutoff, status="queued", row_count=count,
        file_name=export_filename(kind, uuid.uuid4().hex[:8], cutoff),
        expires_at=cutoff + timedelta(minutes=limits["ttl_minutes"]))
    db.session.add(job)
    db.session.commit()
    from flask import current_app
    app = current_app._get_current_object()
    threading.Thread(target=_run_job, args=(app, job.id),
                     name=f"log-export-{job.id[:8]}", daemon=True).start()
    return "job", job, count, cutoff


class TooManyRowsError(Exception):
    def __init__(self, count, cap):
        super().__init__(f"{count} matching rows exceed the "
                         f"{cap}-row export cap")
        self.count, self.cap = count, cap


class BusyError(Exception):
    pass


def _run_job(app, job_id: str) -> None:
    global _jobs_running
    try:
        with app.app_context():
            job = db.session.get(LogExportJob, job_id)
            if job is None:
                return
            try:
                job.status = "running"
                db.session.commit()
                filters = json.loads(job.filters_json)
                # Date bounds round-trip through JSON as strings —
                # restore datetimes so DB comparisons stay typed.
                for bound in ("since", "until"):
                    if isinstance(filters.get(bound), str):
                        try:
                            filters[bound] = datetime.fromisoformat(
                                filters[bound])
                        except ValueError:
                            filters.pop(bound)
                q = query_for(job.kind, filters).filter(
                    _cutoff_col(job.kind) <= job.cutoff_utc)
                tmp = _export_dir() / (job.file_name + ".part")
                written = 0
                with open(tmp, "w", encoding="utf-8", newline="") as fh:
                    for chunk in csv_lines(job.kind, q, job.cutoff_utc):
                        fh.write(chunk)
                        written += 1
                tmp.chmod(0o600)
                tmp.rename(_export_dir() / job.file_name)
                job.status = "done"
                job.row_count = written - 1  # minus header chunk
                db.session.commit()
                from .models import record_audit
                record_audit(
                    job.created_by, "logs.export.done",
                    f"{job.kind} export {job.id[:8]}: "
                    f"{job.row_count} rows")
            except Exception as exc:
                db.session.rollback()
                job = db.session.get(LogExportJob, job_id)
                if job is not None:
                    job.status = "failed"
                    job.error = str(exc)[:256]
                    db.session.commit()
                    from .models import record_audit
                    record_audit(job.created_by, "logs.export.failed",
                                 f"{job.kind} export {job.id[:8]}: "
                                 f"{str(exc)[:160]}")
    finally:
        with _jobs_lock:
            _jobs_running -= 1


def _cutoff_col(kind: str):
    if kind == "audit":
        from .models import AuditEvent
        return AuditEvent.occurred_at
    from .logs import OperationalLog
    return OperationalLog.occurred_at


def get_job(job_id: str):
    return db.session.get(LogExportJob, job_id)


def job_file(job: LogExportJob):
    """Resolved path for a completed job — defends against traversal via
    the stored name (only ever our own basename, but verify anyway)."""
    path = (_export_dir() / job.file_name).resolve()
    if path.parent != _export_dir().resolve():
        return None
    return path if path.exists() else None
