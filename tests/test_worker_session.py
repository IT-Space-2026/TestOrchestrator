"""Focused regression coverage for the worker-session starvation defect.

`worker_loop` runs inside ONE app context for the process lifetime. If a
cycle leaves the session's transaction open, that connection holds the
SQLite write lock indefinitely and starves web request-time writes —
proven on the Windows task-run CI (web unresponsive until the Worker
task was stopped; healthy immediately after). The loop must release
db.session after EVERY cycle: success, idle, and exception paths.
"""
import sqlite3
from unittest.mock import patch

import pytest

from testorchestrator import dispatch
from testorchestrator.extensions import db
from testorchestrator.settings import Setting


class _StopLoop(Exception):
    pass


def _run_loop(cycles_before_stop=3):
    """Drive worker_loop until `time.sleep` raises _StopLoop."""
    import types
    import time as _t
    sleeps = [0]

    def _sleep(_interval):
        sleeps[0] += 1
        if sleeps[0] >= cycles_before_stop:
            raise _StopLoop

    fake_time = types.SimpleNamespace(sleep=_sleep, time=_t.time)
    with patch.object(dispatch, "time", fake_time):
        with pytest.raises(_StopLoop):
            dispatch.worker_loop(interval=0.0)
    return sleeps[0]


def test_session_released_after_success_and_idle_cycles(app):
    """Every iteration — work done or idle — ends with session.remove()."""
    with app.app_context():
        calls = {"once": 0, "remove": 0}
        real_remove = db.session.remove

        def fake_once():
            calls["once"] += 1
            db.session.query(Setting).count()  # touch the session like real work
            return 0

        def spy_remove():
            calls["remove"] += 1
            real_remove()

        with patch.object(dispatch, "worker_once", side_effect=fake_once), \
             patch.object(db.session, "remove", side_effect=spy_remove):
            _run_loop(cycles_before_stop=3)
        assert calls["once"] == 3
        assert calls["remove"] == 3


def test_session_released_when_cycle_raises(app):
    """The finally must fire even when worker_once raises."""
    with app.app_context():
        calls = {"remove": 0}
        real_remove = db.session.remove

        def spy_remove():
            calls["remove"] += 1
            real_remove()

        def boom():
            raise RuntimeError("cycle failure")

        with patch.object(dispatch, "worker_once", side_effect=boom), \
             patch.object(db.session, "remove", side_effect=spy_remove):
            with pytest.raises(RuntimeError):
                dispatch.worker_loop(interval=0.0)
        assert calls["remove"] == 1


def _db_path(app):
    return app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")


def test_uncommitted_worker_write_does_not_starve_other_writers(app):
    """A left-open write txn blocks other connections; releasing the
    session (as the loop's finally does) frees the lock immediately."""
    path = _db_path(app)
    with app.app_context():
        db.session.add(Setting(key="dirty", value="1"))
        db.session.flush()  # write txn open — the starvation condition
        blocked = sqlite3.connect(path, timeout=0.2)
        with pytest.raises(sqlite3.OperationalError):
            blocked.execute(
                "INSERT INTO settings (key, value) VALUES ('web', 'w')")
        blocked.close()

        db.session.remove()  # what worker_loop's finally now guarantees

        free = sqlite3.connect(path, timeout=2)
        free.execute(
            "INSERT INTO settings (key, value) VALUES ('web', 'w')")
        free.commit()
        # Rolled back, not silently committed by the release.
        assert free.execute(
            "SELECT value FROM settings WHERE key='dirty'").fetchone() is None
        assert free.execute(
            "SELECT value FROM settings WHERE key='web'").fetchone() == ("w",)
        free.close()


def test_committed_work_survives_session_release(app):
    """Cleanup must not undo intended commits: durable state written by
    a cycle stays written after remove()."""
    path = _db_path(app)
    with app.app_context():
        db.session.add(Setting(key="committed", value="yes"))
        db.session.commit()
        db.session.remove()
        conn = sqlite3.connect(path, timeout=2)
        assert conn.execute(
            "SELECT value FROM settings WHERE key='committed'"
        ).fetchone() == ("yes",)
        conn.close()


def test_real_worker_once_leaves_no_blocker_after_release(app):
    """Run the real worker_once (licence-bypassed, dagu off), then the
    loop's release — another connection must write without contention."""
    path = _db_path(app)
    with app.app_context():
        dispatch.worker_once()
        db.session.remove()
        conn = sqlite3.connect(path, timeout=2)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('after', 'ok')")
        conn.commit()
        assert conn.execute(
            "SELECT value FROM settings WHERE key='after'"
        ).fetchone() == ("ok",)
        conn.close()
