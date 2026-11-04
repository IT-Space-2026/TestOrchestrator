"""Report publication + synchronized deletion — local fixture repos."""

import json
from pathlib import Path

import pytest

from testorchestrator import dispatch, gitsync
from testorchestrator.extensions import db
from testorchestrator.execution import (Execution, ExecutionState,
                                        GitBinding, Project, Suite,
                                        utcnow)


def _repo(tmp_path):
    src = tmp_path / "repo"
    src.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "config",
                    "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(src), "config",
                    "user.name", "t"], check=True)
    (src / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(src), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "init"],
                   check=True)
    return src


def _env(app, repo):
    p = Project(name="P"); db.session.add(p); db.session.flush()
    b = GitBinding(project_id=p.id, name="b", url=str(repo),
                   ref="master", worktree_path=str(repo),
                   last_synced_sha="sha1", status="synced")
    db.session.add(b); db.session.flush()
    s = Suite(project_id=p.id, name="S"); db.session.add(s)
    db.session.flush()
    return p, b, s


def test_publish_to_repo_root(app, tmp_path):
    """Finalized report publishes under TestOrchestrator/Reports/<id>/
    at the repository root with a versioned manifest."""
    repo = _repo(tmp_path)
    with app.app_context():
        p, b, s = _env(app, repo)
        ex = dispatch.create_execution("m", s, binding=b,
                                       source_sha="sha1")
        ex.state = ExecutionState.COMPLETED
        ex.report_dir = str(tmp_path / "rep")
        Path(ex.report_dir).mkdir()
        (Path(ex.report_dir) / "j.xml").write_text("<testsuite/>")
        db.session.commit()
        r = gitsync.publish_report(ex.id)
        assert r.get("published")
        db.session.refresh(ex)
        assert ex.report_pub_status == "published"
        assert ex.report_pub_dir == f"TestOrchestrator/Reports/{ex.id}"
        # pushed content lives on the REMOTE ref — check via git show,
        # not the working tree (a non-bare repo isn't checkout-updated).
        import subprocess
        out = subprocess.run(
            ["git", "-C", str(repo), "show",
             f"master:{ex.report_pub_dir}/manifest.json"],
            capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        d = json.loads(out.stdout)
        assert d["execution_id"] == ex.id and d["version"] == 1
        out2 = subprocess.run(
            ["git", "-C", str(repo), "show",
             f"master:{ex.report_pub_dir}/report/j.xml"],
            capture_output=True, text=True)
        assert out2.returncode == 0


def test_publication_failure_not_execution_failure(app, tmp_path):
    """A failed push marks publication failed — the execution stays
    COMPLETED; retry is available."""
    repo = _repo(tmp_path)
    with app.app_context():
        p, b, s = _env(app, repo)
        ex = dispatch.create_execution("m", s, binding=b)
        ex.state = ExecutionState.COMPLETED
        db.session.commit()
        # break the repo so push fails
        b.url = str(tmp_path / "nonexistent")
        db.session.commit()
        r = gitsync.publish_report(ex.id)
        assert r.get("error")
        db.session.refresh(ex)
        assert ex.report_pub_status == "failed"
        assert ex.state == ExecutionState.COMPLETED   # unchanged


def test_idempotent_retry_skips_unchanged(app, tmp_path):
    """Retrying a published report with unchanged content skips."""
    repo = _repo(tmp_path)
    with app.app_context():
        p, b, s = _env(app, repo)
        ex = dispatch.create_execution("m", s, binding=b)
        ex.state = ExecutionState.COMPLETED
        db.session.commit()
        gitsync.publish_report(ex.id)
        db.session.refresh(ex)
        sha1 = ex.report_pub_sha
        r = gitsync.publish_report(ex.id)
        assert r.get("skipped")


def test_delete_removes_only_its_report(app, tmp_path):
    """Deletion commits removal of ONLY this execution's dir; other
    reports and files survive."""
    repo = _repo(tmp_path)
    with app.app_context():
        p, b, s = _env(app, repo)
        ex = dispatch.create_execution("m", s, binding=b)
        ex.state = ExecutionState.COMPLETED
        db.session.commit()
        gitsync.publish_report(ex.id)
        import subprocess
        out = subprocess.run(["git", "-C", str(repo), "ls-tree",
                              "master", ex.report_pub_dir],
                             capture_output=True, text=True)
        assert ex.report_pub_dir in out.stdout
        r = gitsync.delete_report(ex.id)
        assert r.get("removed")
        import subprocess
        out = subprocess.run(["git", "-C", str(repo), "ls-tree",
                              "master", ex.report_pub_dir],
                             capture_output=True, text=True)
        assert out.stdout.strip() == ""   # dir removed
        out2 = subprocess.run(["git", "-C", str(repo), "ls-tree",
                               "master", "f.txt"],
                              capture_output=True, text=True)
        assert "f.txt" in out2.stdout   # unrelated preserved
        db.session.refresh(ex)
        assert ex.report_pub_dir == ""


def test_delete_never_published_is_noop(app, tmp_path):
    """A never-published execution deletes cleanly — no repo write."""
    repo = _repo(tmp_path)
    with app.app_context():
        p, b, s = _env(app, repo)
        ex = dispatch.create_execution("m", s, binding=b)
        db.session.commit()
        r = gitsync.delete_report(ex.id)
        assert r.get("absent")


def test_deletion_blocks_republication(app, tmp_path):
    """Once deletion is requested, publication retries are refused —
    no recreating a doomed report."""
    repo = _repo(tmp_path)
    with app.app_context():
        p, b, s = _env(app, repo)
        ex = dispatch.create_execution("m", s, binding=b)
        ex.deletion_status = "pending"
        db.session.commit()
        r = gitsync.publish_report(ex.id)
        assert "deletion" in r["error"]
