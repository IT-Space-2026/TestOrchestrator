"""Git remote/push/conflict/restore acceptance (item 7)."""

import json
from pathlib import Path

import git
import pytest

from testorchestrator import gitsync
from testorchestrator.execution import Project, Suite
from testorchestrator.extensions import db
from testorchestrator import settings as settings_mod


def _remote_repo(tmp_path):
    """A real 'remote' (file://) + a working clone registered as export."""
    remote = git.Repo.init(tmp_path / "remote.git", bare=True)
    clone = git.Repo.clone_from(str(tmp_path / "remote.git"),
                                tmp_path / "work")
    return remote, clone


def test_push_and_restore_roundtrip(app, tmp_path):
    remote, clone = _remote_repo(tmp_path)
    with app.app_context():
        settings_mod.update({"git_export_repo": str(tmp_path / "work")})
        project = Project(name="RestoreMe")
        db.session.add(project); db.session.flush()
        suite = Suite(project_id=project.id, name="SuiteA", description="d",
                      version=3)
        db.session.add(suite); db.session.commit()
        pid, sid = project.id, suite.id

        gitsync.export_definitions(project)
        assert gitsync.push_export() == "pushed"
        # Remote now contains the export tree.
        head = remote.git.rev_parse("HEAD")
        commit = remote.commit(head)
        remote_files = [e.path for e in commit.tree.traverse()]
        assert any("TestOrchestrator" in f for f in remote_files)

        # Delete the local suite, restore from a FRESH clone.
        db.session.delete(suite); db.session.commit()
        fresh = git.Repo.clone_from(str(tmp_path / "remote.git"),
                                    tmp_path / "work2")
        settings_mod.update({"git_export_repo": str(tmp_path / "work2")})
        counts = gitsync.restore_definitions(pid)
        assert counts["suites"] == 1
        restored = Suite.query.get(sid)
        assert restored.name == "SuiteA" and restored.version == 3


def test_conflict_surfaces_no_force_push(app, tmp_path):
    remote, clone = _remote_repo(tmp_path)
    with app.app_context():
        settings_mod.update({"git_export_repo": str(tmp_path / "work")})
        project = Project(name="C")
        db.session.add(project); db.session.flush()
        gitsync.export_definitions(project)
        gitsync.push_export()

        # Divergent commit on another clone of the same remote.
        other = git.Repo.clone_from(str(tmp_path / "remote.git"),
                                    tmp_path / "other")
        conflict_file = Path(other.working_dir) / "TestOrchestrator" / \
            "projects" / project.id / "suites" / "conflict.json"
        conflict_file.parent.mkdir(parents=True, exist_ok=True)
        conflict_file.write_text('{"remote": true}')
        other.git.add(A=True)
        other.index.commit("remote edit")
        other.remotes.origin.push()

        # Local export now writes the same path -> merge conflict must
        # surface, never force-pushed.
        (Path(clone.working_dir) / "TestOrchestrator" / "projects"
         / project.id / "suites" / "conflict.json").write_text('{"local":1}')
        clone.git.add(A=True)
        clone.index.commit("local edit")
        head_before = remote.git.rev_parse("HEAD")
        with pytest.raises(gitsync.SyncError, match="conflict"):
            gitsync.push_export()
        # Remote still has exactly the remote commit — nothing overwritten.
        assert remote.git.rev_parse("HEAD") == head_before


def test_secret_exclusion_in_export(app, tmp_path):
    """Nothing user/secret/session ever lands in the export repo."""
    remote, clone = _remote_repo(tmp_path)
    with app.app_context():
        settings_mod.update({"git_export_repo": str(tmp_path / "work")})
        project = Project(name="S3")
        db.session.add(project); db.session.flush()
        from testorchestrator import dispatch
        suite = Suite(project_id=project.id, name="x")
        db.session.add(suite); db.session.flush()
        ex = dispatch.create_execution("admin", suite)
        ex.report_dir = str(tmp_path / "rep")
        Path(ex.report_dir).mkdir()
        (Path(ex.report_dir) / "secret.txt").write_text("TOPSECRET")
        from testorchestrator.execution import Artifact
        from testorchestrator.results import sha256_file
        db.session.add(Artifact(
            execution_id=ex.id, kind="other", rel_path="secret.txt",
            sha256=sha256_file(Path(ex.report_dir) / "secret.txt"),
            size=9, mime="text/plain"))
        db.session.commit()
        gitsync.export_execution(ex)
        tree = list(Path(tmp_path / "work").rglob("*"))
        text = "".join(p.read_text(errors="ignore")
                       for p in tree if p.is_file())
        assert "TOPSECRET" not in text
