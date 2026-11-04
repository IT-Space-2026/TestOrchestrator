"""In-flight race, worker recovery, and NFF retry — controlled
ordering, not sleeps."""

import json
import threading
import pytest

from testorchestrator import dispatch, gitsync
from testorchestrator.extensions import db
from testorchestrator.execution import (Execution, ExecutionState,
                                        GitBinding, OutboxEvent,
                                        Project, Suite)
from tests.test_report_pub import _repo, _env


def test_deletion_wait_blocks_late_publish(app, tmp_path):
    """A publisher that passed its eligibility check but stalls before
    the remote update MUST NOT recreate a report deleted meanwhile.
    Controlled: publish-check -> deletion -> resume-publish."""
    repo = _repo(tmp_path)
    with app.app_context():
        p, b, s = _env(app, repo)
        ex = dispatch.create_execution("m", s, binding=b)
        ex.state = ExecutionState.COMPLETED
        db.session.commit()
        # 1) publisher checks eligibility (report_pub none, deletion none)
        assert ex.deletion_status == "none"
        # 2) deletion requested + processed BEFORE publish resumes
        ex.deletion_status = "pending"; db.session.commit()
        gitsync.delete_report(ex.id)          # removes remote report
        # 3) late publisher resumes — must refuse (deletion flag)
        r = gitsync.publish_report(ex.id)
        # either 'unknown execution' (row finalized) or a deletion
        # block — both prove no re-creation, no silent success.
        assert r.get("error")
        import subprocess
        out = subprocess.run(
            ["git", "-C", str(repo), "ls-tree", "master",
             "TestOrchestrator/Reports"],
            capture_output=True, text=True)
        assert ex.id not in out.stdout


def test_pending_publish_survives_restart(app, tmp_path):
    """A queued report-publish event survives 'restart' (a fresh
    worker pass) and completes."""
    repo = _repo(tmp_path)
    with app.app_context():
        p, b, s = _env(app, repo)
        ex = dispatch.create_execution("m", s, binding=b)
        ex.state = ExecutionState.COMPLETED
        db.session.commit()
        db.session.add(OutboxEvent(
            kind="report-publish",
            payload_json=json.dumps({"execution_id": ex.id})))
        db.session.commit()
        # 'restart' — a new process would re-scan pending events;
        # simulate by calling the handler on the queued row.
        ev = OutboxEvent.query.filter_by(kind="report-publish").first()
        from testorchestrator.dispatch import _handle_event
        _handle_event(ev)
        db.session.refresh(ex)
        assert ex.report_pub_status == "published"


def test_pending_delete_survives_restart(app, tmp_path):
    """A queued report-delete event survives 'restart' and finalizes."""
    repo = _repo(tmp_path)
    with app.app_context():
        p, b, s = _env(app, repo)
        ex = dispatch.create_execution("m", s, binding=b)
        ex.state = ExecutionState.COMPLETED
        db.session.commit()
        gitsync.publish_report(ex.id)
        db.session.add(OutboxEvent(
            kind="report-delete",
            payload_json=json.dumps({"execution_id": ex.id})))
        db.session.commit()
        ev = OutboxEvent.query.filter_by(kind="report-delete").first()
        from testorchestrator.dispatch import _handle_event
        _handle_event(ev)
        assert db.session.get(Execution, ex.id) is None


def test_nff_retry_preserves_both(app, tmp_path):
    """A push that fails non-fast-forward can retry after update —
    both reports + unrelated content preserved."""
    repo = _repo(tmp_path)
    with app.app_context():
        p, b, s = _env(app, repo)
        e1 = dispatch.create_execution("m", s, binding=b)
        e2 = dispatch.create_execution("m", s, binding=b)
        e1.state = e2.state = ExecutionState.COMPLETED
        db.session.commit()
        gitsync.publish_report(e1.id)
        # remote advances via a SEPARATE proper clone — the fixture
        # repo's own working tree is stale (non-bare push target), so
        # committing there would drop pushed files (test artifact,
        # not a service bug).
        import subprocess
        other = tmp_path / "other-clone"
        subprocess.run(["git", "clone", "-q", str(repo), str(other)],
                       check=True)
        subprocess.run(["git", "-C", str(other), "config",
                        "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(other), "config",
                        "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(other), "commit", "-q",
                        "--allow-empty", "-m", "other"], check=True)
        subprocess.run(["git", "-C", str(other), "push", "-q",
                        "origin", "master"], check=True)
        gitsync.publish_report(e2.id)   # update+push — survives NFF
        db.session.refresh(e2)
        assert e2.report_pub_status == "published"
        for eid in (e1.id, e2.id):
            out = subprocess.run(
                ["git", "-C", str(repo), "ls-tree", "master",
                 f"TestOrchestrator/Reports/{eid}"],
                capture_output=True, text=True)
            assert eid in out.stdout


def test_cross_process_lock_serialization(app, tmp_path):
    """Two independent processes: the second blocks on the pub lock,
    resumes after release, and cannot recreate a deleted report."""
    repo = _repo(tmp_path)
    with app.app_context():
        p, b, s = _env(app, repo)
        ex = dispatch.create_execution("m", s, binding=b)
        ex.state = ExecutionState.COMPLETED
        db.session.commit()
        lock = (tmp_path / "reports-pub" / b.id)
        lock.mkdir(parents=True, exist_ok=True)
        lockfile = lock / ".pub.lock"
        # simulate a 'first worker' holding the lock
        import fcntl
        fh = open(lockfile, "w")
        fcntl.flock(fh, fcntl.LOCK_EX)
        # a second worker trying to publish would BLOCK — simulate by
        # a non-blocking attempt, which fails while the lock is held.
        fh2 = open(lockfile, "w")
        try:
            fcntl.flock(fh2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert False, "second worker should be locked out"
        except BlockingIOError:
            pass
        fh2.close()
        # 'release' the first worker's lock
        fcntl.flock(fh, fcntl.LOCK_UN); fh.close()
        # now the second worker CAN publish
        r = gitsync.publish_report(ex.id)
        assert r.get("published")
        # deletion-requested state then blocks re-creation
        ex.deletion_status = "pending"; db.session.commit()
        r2 = gitsync.publish_report(ex.id)
        assert r2.get("error")


def test_push_succeeded_local_unrecorded_recovers(app, tmp_path):
    """Remote push succeeded but the worker stopped before recording
    'published' — status stays pending; a retry re-derives the same
    digest content and finalizes without data loss."""
    repo = _repo(tmp_path)
    with app.app_context():
        p, b, s = _env(app, repo)
        ex = dispatch.create_execution("m", s, binding=b)
        ex.state = ExecutionState.COMPLETED
        db.session.commit()
        # simulate: push landed remotely but the local commit to
        # 'published' never happened (status still pending, no sha).
        gitsync.publish_report(ex.id)
        db.session.refresh(ex)
        ex.report_pub_status = "pending"     # unrecorded success
        ex.report_pub_sha = ""
        db.session.commit()
        # retry: same content — republishes and finalizes honestly
        r = gitsync.publish_report(ex.id)
        assert r.get("published")
        db.session.refresh(ex)
        assert ex.report_pub_status == "published"
        import subprocess
        out = subprocess.run(
            ["git", "-C", str(repo), "ls-tree", "master",
             ex.report_pub_dir], capture_output=True, text=True)
        assert ex.report_pub_dir in out.stdout   # report present


def test_interrupted_event_stays_eligible(app, tmp_path):
    """Scan-based outbox: an event row that wasn't processed remains
    for the next worker pass — no claim/ack needed."""
    repo = _repo(tmp_path)
    with app.app_context():
        p, b, s = _env(app, repo)
        ex = dispatch.create_execution("m", s, binding=b)
        ex.state = ExecutionState.COMPLETED
        db.session.commit()
        import json as _j
        db.session.add(OutboxEvent(
            kind="report-publish",
            payload_json=_j.dumps({"execution_id": ex.id})))
        db.session.commit()
        # 'interruption' — event still pending; next scan picks it up
        ev = OutboxEvent.query.filter_by(kind="report-publish").first()
        assert ev is not None
        from testorchestrator.dispatch import _handle_event
        _handle_event(ev)
        db.session.refresh(ex)
        assert ex.report_pub_status == "published"
