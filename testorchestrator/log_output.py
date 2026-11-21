"""Optional structured event output — vendor-neutral NDJSON sink.

One structured representation (logschema) feeds every sink:

    structured events ── local DB + admin pages (authoritative)
                      ── CSV export
                      └─ optional NDJSON file / stdout
                            → customer-managed collector → log server

Design rules (docs/external-logging.md):

- Disabled until configured — ``logs_json_enabled`` / ``logs_stdout_enabled``
  (settings, or TO_LOGS_JSON_ENABLED / TO_LOGS_STDOUT env for installers).
- Local DB storage remains authoritative; a sink failure NEVER raises back
  into emit() and never weakens audit persistence. Failures are counted
  (``status()``) and go to the stdlib logger — never back through emit(),
  which is what prevents recursive logging-failure loops.
- Multi-process safe: events are written with a single O_APPEND write;
  rotated names carry timestamp+pid so two portal workers cannot clobber
  each other's rotation. A contended rotation may produce an extra file —
  bounded by max_files + purge-on-rotate.
- Bounded: file size cap, rotated-file count cap, retention deletes the
  oldest. Buffering is the OS page cache — one write() per line, no
  in-process queue to grow.
"""

import logging
import os
import sys
import threading
import time
from pathlib import Path

_std = logging.getLogger("testorchestrator.log_output")

_STATE = {"written": 0, "dropped": 0, "rotated": 0,
          "last_error": "", "last_write_at": None}
_rotate_seq = 0
_lock = threading.Lock()
_cfg_cache = {"at": 0.0, "cfg": None}
_CFG_TTL_S = 5.0

_ACTIVE = "events.jsonl"


def _truthy(v) -> bool:
    return v in (True, "1", "true", "yes", "on")


def _config() -> dict:
    """Sink configuration — settings DB with env override for installers;
    cached briefly so emit hot paths don't query per event."""
    now = time.monotonic()
    if _cfg_cache["cfg"] is not None and \
            now - _cfg_cache["at"] < _CFG_TTL_S:
        return _cfg_cache["cfg"]
    from . import settings as settings_mod
    from .config import INSTANCE_DIR

    def _get(key, default):
        try:
            return settings_mod.get(key, default)
        except Exception:
            return default

    cfg = {
        "json_enabled": _truthy(os.environ.get("TO_LOGS_JSON_ENABLED",
                                _get("logs_json_enabled", False))),
        "stdout_enabled": _truthy(os.environ.get("TO_LOGS_STDOUT_ENABLED",
                                  _get("logs_stdout_enabled", False))),
        "dir": Path(os.environ.get("TO_LOGS_JSON_DIR")
                    or _get("logs_json_dir", "")
                    or (INSTANCE_DIR / "logs")),
        "max_bytes": int(_get("logs_json_max_bytes", 10 * 1024 * 1024)),
        "max_files": int(_get("logs_json_max_files", 5)),
    }
    _cfg_cache.update(at=now, cfg=cfg)
    return cfg


def reset_config_cache() -> None:
    """Tests: force the next publish() to re-read settings."""
    _cfg_cache.update(at=0.0, cfg=None)


def status() -> dict:
    """Output health — expose write failures + drops honestly."""
    return dict(_STATE)


def _rotate(path: Path, cfg: dict) -> None:
    """Rename the active file aside when it reached the size cap, then
    delete the oldest rotated files beyond max_files. The suffix is
    timestamp-to-nanoseconds + pid + a per-process sequence — two
    rotations inside one second (burst writes) must NEVER collide and
    clobber a file full of events."""
    import time as _t
    global _rotate_seq
    _rotate_seq += 1
    rotated = path.with_name(
        f"{path.stem}-{_t.strftime('%Y%m%dT%H%M%S')}-{_t.time_ns() % 10**12}"
        f"-{os.getpid()}-{_rotate_seq}.jsonl")
    try:
        if rotated.exists():
            return  # defensive: never overwrite a rotated file
        path.rename(rotated)
        _STATE["rotated"] += 1
    except OSError:
        return  # another process may have rotated first — keep appending
    files = sorted(path.parent.glob(f"{path.stem}-*.jsonl"),
                   key=lambda p: p.name)
    for stale in files[:-cfg["max_files"]]:
        try:
            stale.unlink()
        except OSError:
            pass


def _write_file(line: str, cfg: dict) -> None:
    path = cfg["dir"] / _ACTIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and path.stat().st_size >= cfg["max_bytes"]:
            _rotate(path, cfg)
    except OSError:
        pass
    # O_APPEND + a single write() call keeps each line atomic across the
    # portal's multiple processes; a partial write is completed in-loop.
    data = line.encode("utf-8", "replace")
    if not data.endswith(b"\n"):
        data += b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


def publish(ev: dict) -> None:
    """Send one schema event to the configured sinks. Fail-open: errors
    are counted and stdlib-logged, never raised into the caller — the DB
    row already committed is authoritative."""
    from .logschema import to_json_line
    try:
        cfg = _config()
    except Exception as exc:  # settings table mid-migration etc.
        with _lock:
            _STATE["dropped"] += 1
            _STATE["last_error"] = f"config: {exc}"
        return
    if not cfg["json_enabled"] and not cfg["stdout_enabled"]:
        return
    line = to_json_line(ev)
    with _lock:
        try:
            if cfg["json_enabled"]:
                _write_file(line, cfg)
            if cfg["stdout_enabled"]:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            _STATE["written"] += 1
            _STATE["last_write_at"] = ev.get("timestamp_utc")
        except Exception as exc:
            _STATE["dropped"] += 1
            _STATE["last_error"] = str(exc)[:256]
            # stdlib only — emitting an OperationalLog here would recurse.
            _std.warning("structured log output failed: %s", exc)


def reset_stats() -> None:
    """Tests: reset health counters."""
    with _lock:
        _STATE.update(written=0, dropped=0, rotated=0,
                      last_error="", last_write_at=None)
