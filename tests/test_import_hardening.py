"""Import hardening (2.0.2): concurrency, interruption/recovery,
evidence lifecycle, persisted fingerprints, missing legacy evidence."""
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from testorchestrator import evidence, importer
from testorchestrator.execution import (Artifact, Execution,
                                        ExecutionState, GitBinding,
                                        ImportLock, ImportSession,
                                        Outcome, Project, TestResult)
from testorchestrator.extensions import db

from tests.test_import_results import (CASE_PASS, JUNIT, UUID,
                                       cand, confirm, kre_run,
                                       make_binding, preview, sess)


# ---------- concurrent confirmation ----------

def test_concurrent_confirm_same_candidate_one_execution(
        tmp_path, app):
    """Two concurrent confirms of the same candidate must yield ONE
    execution — the second sees the project claim and stops."""
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run().items()}
    pid, bid = make_binding(tmp_path, app, files, "conc")
    sid = preview(app, pid, bid)

    results = {}

    def run(tag):
        with app.app_context():
            s = db.session.get(ImportSession, sid)
            ids = [c.id for c in s.candidates]
            try:
                results[tag] = importer.confirm_import(
                    s, ids, [], f"user-{tag}")
            except Exception as exc:
                results[tag] = {"error": str(exc)}

    t1 = threading.Thread(target=run, args=("a",))
    t2 = threading.Thread(target=run, args=("b",))
    t1.start(); t2.start(); t1.join(); t2.join()

    with app.app_context():
        n = Execution.query.filter_by(project_id=pid).count()
        # either one confirm ran and the other was refused, or both
        # ran serially with the second resolving to skip — never two
        # executions, never duplicate results.
        assert n == 1
        assert TestResult.query.count() == 2
        assert db.session.get(ImportLock, pid) is None  # released


def test_confirm_refuses_while_lock_held(tmp_path, app):
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run().items()}
    pid, bid = make_binding(tmp_path, app, files, "lock")
    sid = preview(app, pid, bid)
    with app.app_context():
        db.session.add(ImportLock(project_id=pid, session_id="other",
                                  claimed_by="someone"))
        db.session.commit()
        s = db.session.get(ImportSession, sid)
        with pytest.raises(importer.ImportError_, match="already running"):
            importer.confirm_import(s, ["x"], [], "me")


def test_stale_lock_reclaimed(tmp_path, app):
    """A crashed confirm's lock (older than the stale window) is
    reclaimed so retry is never permanently blocked."""
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run().items()}
    pid, bid = make_binding(tmp_path, app, files, "stale")
    sid = preview(app, pid, bid)
    from datetime import timedelta
    from testorchestrator.execution import utcnow
    with app.app_context():
        db.session.add(ImportLock(
            project_id=pid, session_id="crashed", claimed_by="old",
            claimed_at=utcnow() - timedelta(
                seconds=importer.CONFIRM_CLAIM_STALE_S + 60)))
        db.session.commit()
    out = confirm(app, sid)
    assert len(out["imported"]) == 1
    with app.app_context():
        assert db.session.get(ImportLock, pid) is None


def test_lock_released_on_confirm_failure(tmp_path, app, monkeypatch):
    """A confirm that raises mid-batch still releases the project
    claim — a crash cannot deadlock the project."""
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run().items()}
    pid, bid = make_binding(tmp_path, app, files, "rel")
    sid = preview(app, pid, bid)
    with app.app_context():
        s = db.session.get(ImportSession, sid)
        monkeypatch.setattr(importer, "_confirm_import_inner",
                            lambda *a, **k: 1 / 0)
        with pytest.raises(ZeroDivisionError):
            importer.confirm_import(s, ["x"], [], "me")
        assert db.session.get(ImportLock, pid) is None


# ---------- interruption / recovery ----------

def test_failed_candidate_retryable(tmp_path, app):
    """One failing candidate in a multi-candidate batch fails alone;
    a later confirm retries it successfully."""
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run().items()}
    files.update({f"Reports/20261110_143000/{k}": v
                  for k, v in kre_run(
                      ts="2026-11-10T14:30:00",
                      uuid="bbbb1111-2222-3333-4444-555566667777"
                      ).items()})
    pid, bid = make_binding(tmp_path, app, files, "retry")
    sid = preview(app, pid, bid)
    # force the first candidate to fail inside _create_execution
    with app.app_context():
        s = db.session.get(ImportSession, sid)
        first = s.candidates[0]
        orig = importer._create_execution
        calls = {"n": 0}
        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("simulated evidence disk failure")
            return orig(*a, **k)
        importer._create_execution = flaky
        try:
            out1 = importer.confirm_import(
                s, [c.id for c in s.candidates], [], "u")
        finally:
            importer._create_execution = orig
        assert len(out1["failed"]) == 1
        assert len(out1["imported"]) == 1
        failed_c = db.session.get(
            type(s.candidates[0]), first.id)
        assert failed_c.status == "failed"
        assert "disk failure" in failed_c.error
    # retry — failed candidate is reprocessed
    out2 = confirm(app, sid)
    assert len(out2["imported"]) == 1
    with app.app_context():
        assert Execution.query.filter_by(project_id=pid).count() == 2
        assert all(c.status in ("imported", "skipped") for c in
                   db.session.get(ImportSession, sid).candidates)


def test_orphan_evidence_after_commit_failure(tmp_path, app,
                                              monkeypatch):
    """Crash between evidence copy and execution commit leaves an
    orphan dir — prune reports it (dry-run) and removes it (apply)."""
    app.instance_path = str(tmp_path / "inst")
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run().items()}
    pid, bid = make_binding(tmp_path, app, files, "orph")
    sid = preview(app, pid, bid)
    with app.app_context():
        s = db.session.get(ImportSession, sid)
        orig = importer._copy_evidence
        def boom(pi, exec_id):
            dest = orig(pi, exec_id)        # evidence copied…
            raise OSError("crash before commit")
        monkeypatch.setattr(importer, "_copy_evidence", boom)
        out = importer.confirm_import(
            s, [c.id for c in s.candidates], [], "u")
        assert len(out["failed"]) == 1
        # orphan exists, owned by no execution
        orphans = evidence.find_orphans()
        assert len(orphans) == 1
        dry = evidence.prune_orphans(dry_run=True)
        assert dry["removed"] == [] and len(dry["orphans"]) == 1
        assert orphans[0].is_dir()          # dry-run deletes nothing
        applied = evidence.prune_orphans(dry_run=False)
        assert len(applied["removed"]) == 1
        assert not orphans[0].exists()


def test_live_evidence_not_pruned(tmp_path, app):
    """An execution's managed evidence dir is never an orphan."""
    app.instance_path = str(tmp_path / "inst")
    with app.app_context():
        project = Project(name="keep", description="")
        db.session.add(project)
        db.session.flush()
        ex = Execution(project_id=project.id, origin="live",
                       state=ExecutionState.COMPLETED,
                       outcome=Outcome.PASSED, created_by="t")
        db.session.add(ex)
        db.session.flush()
        rd = Path(app.instance_path) / "workspaces" / ex.id
        (rd / "Reports").mkdir(parents=True)
        ex.report_dir = str(rd / "Reports")
        ex.workspace_path = str(rd)
        db.session.commit()
    with app.app_context():
        assert evidence.find_orphans() == []


def test_execution_delete_removes_managed_evidence(tmp_path, app):
    """Deleting an execution removes its managed evidence dir (after
    the row commits) — no orphan left behind."""
    app.instance_path = str(tmp_path / "inst")
    from testorchestrator import gitsync
    with app.app_context():
        project = Project(name="del", description="")
        db.session.add(project)
        db.session.flush()
        ex = Execution(project_id=project.id, origin="imported",
                       state=ExecutionState.COMPLETED,
                       outcome=Outcome.PASSED, created_by="t")
        db.session.add(ex)
        db.session.flush()
        rd = Path(app.instance_path) / "imported-evidence" / ex.id
        (rd / "sub").mkdir(parents=True)
        (rd / "sub" / "f.txt").write_text("evidence")
        ex.report_dir = str(rd)
        db.session.commit()
        ex_id = ex.id
        out = gitsync.delete_report(ex_id)
        assert out["removed"] is True
        assert not rd.exists()
        assert Execution.query.get(ex_id) is None


def test_delete_never_removes_shared_evidence(tmp_path, app):
    """A dir still referenced by another execution must survive."""
    app.instance_path = str(tmp_path / "inst")
    with app.app_context():
        project = Project(name="shared", description="")
        db.session.add(project)
        db.session.flush()
        e1 = Execution(project_id=project.id, origin="live",
                       state=ExecutionState.COMPLETED,
                       outcome=Outcome.PASSED, created_by="t")
        db.session.add(e1)
        db.session.flush()
        shared = Path(app.instance_path) / "workspaces" / e1.id
        (shared / "Reports").mkdir(parents=True)
        e1.report_dir = str(shared / "Reports")
        e1.workspace_path = str(shared)
        db.session.commit()
        # second execution points inside the same dir
        e2 = Execution(project_id=project.id, origin="imported",
                       state=ExecutionState.COMPLETED,
                       outcome=Outcome.PASSED, created_by="t",
                       report_dir=str(shared / "Reports" / "imported"))
        db.session.add(e2)
        db.session.commit()
        e1_id = e1.id
    with app.app_context():
        res = evidence.remove_owned(e1_id, str(shared / "Reports"),
                                    str(shared))
        assert res["removed"] is False and res["reason"] == (
            "still referenced")
        assert shared.is_dir()


def test_prune_rejects_symlink_escape(tmp_path, app):
    """A symlink inside a managed root is never followed or deleted."""
    app.instance_path = str(tmp_path / "inst")
    root = Path(app.instance_path) / "imported-evidence"
    root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("precious")
    link = root / "escape"
    link.symlink_to(outside)
    with app.app_context():
        report = evidence.prune_orphans(dry_run=False)
        assert str(link) not in report["removed"]
        assert outside.joinpath("keep.txt").is_file()
        assert link.is_symlink() or not link.exists()


# ---------- persisted fingerprints ----------

def test_lazy_fingerprint_persisted_once(tmp_path, app,
                                        monkeypatch):
    """Lazy fingerprinting stamps the row — the SECOND import over the
    same unstamped history performs no evidence reads."""
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run().items()}
    pid, bid = make_binding(tmp_path, app, files, "lazy")
    # unstamped legacy execution whose evidence exists in the worktree
    from testorchestrator.results import parse_junit
    with app.app_context():
        wc = Path(db.session.get(GitBinding, bid).worktree_path)
        rd = wc / "Reports" / "20261110_093000"
        ex = Execution(project_id=pid, origin="live",
                       framework="katalon",
                       state=ExecutionState.COMPLETED,
                       outcome=Outcome.FAILED, created_by="t",
                       report_dir=str(rd))
        db.session.add(ex)
        db.session.commit()
        db.session.add(Artifact(
            execution_id=ex.id, kind="junit",
            rel_path="JUnit_Report.xml",
            sha256="x" * 64, size=10, mime="", mapped=True))
        db.session.commit()
        ex_id = ex.id
        assert ex.suite_keys_json is None
    # first preview fingerprints the row (and persists)
    c1 = cand(app, preview(app, pid, bid))
    assert c1.decision in ("duplicate", "supplement")
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert ex.suite_keys_json is not None
        assert ex.run_uuid == UUID
        assert ex.content_hash
    # second preview must NOT re-read the STORED execution's evidence
    # (the candidate's own parse reads its own dir — that stays)
    cand_dir = tmp_path / "cand" / "r2"
    import shutil as _sh
    _sh.copytree(rd, cand_dir)
    stored_reads = {"n": 0}
    orig = importer._looks_junit
    def counting(path):
        if str(path).startswith(str(rd)):
            stored_reads["n"] += 1
        return orig(path)
    with app.app_context():
        importer._looks_junit = counting
        try:
            importer.classify(
                pid, importer.parse_run_dir(cand_dir, "r2", cand_dir))
        finally:
            importer._looks_junit = orig
    assert stored_reads["n"] == 0


def test_missing_legacy_evidence_warns_not_blocks(tmp_path, app):
    """A legacy row whose evidence is gone cannot be fingerprinted —
    the import proceeds as new WITH an explicit warning, never a
    silent blind import."""
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run().items()}
    pid, bid = make_binding(tmp_path, app, files, "gone")
    with app.app_context():
        ex = Execution(project_id=pid, origin="live",
                       state=ExecutionState.COMPLETED,
                       outcome=Outcome.PASSED, created_by="t",
                       report_dir="/nonexistent/evidence")
        db.session.add(ex)
        db.session.commit()
        ex_id = ex.id
    c = cand(app, preview(app, pid, bid))
    with app.app_context():
        from testorchestrator.execution import ImportCandidate
        ic = db.session.get(ImportCandidate, c.id)
        assert ic.decision == "new"
        assert any("could not be fingerprinted" in w
                   for w in ic.warnings())
        # the unfingerprintable row is stamped so it isn't re-scanned
        assert db.session.get(Execution, ex_id).suite_keys_json == "[]"


# ---------- classification precedence preserved ----------

def test_no_uuid_import_still_finds_content_duplicate(tmp_path, app):
    """Stamped-fast-path correctness: a no-uuid import with identical
    content+timestamps is a duplicate of the stamped row."""
    shared = dict(suite="TS_Login", ts="2026-11-10T09:30:00")
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run(uuid=UUID, **shared).items()}
    pid, bid = make_binding(tmp_path, app, files, "fp")
    confirm(app, preview(app, pid, bid))
    # now import a no-uuid copy with identical content
    files2 = {"R2/JUnit_Report.xml":
              files["Reports/20261110_093000/JUnit_Report.xml"]}
    pid2, bid2 = make_binding(tmp_path, app, files2, "fp2")
    # same project — reuse pid's binding? a second binding in same proj
    with app.app_context():
        b1 = db.session.get(GitBinding, bid)
        b2 = db.session.get(GitBinding, bid2)
        b2.project_id = pid
        db.session.commit()
    c = cand(app, preview(app, pid, bid2, result_dir="R2"))
    assert c.decision == "duplicate"
