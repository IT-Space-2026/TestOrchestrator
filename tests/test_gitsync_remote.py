"""Git remote/push/conflict/restore acceptance (item 7).

Fixtures use Dulwich porcelain directly — the same transport the
application backend uses — so the suite exercises the real workflow
without GitPython or a git binary."""

import io
import json
from pathlib import Path

import pytest
from dulwich import porcelain
from dulwich.object_store import iter_tree_contents
from dulwich.repo import Repo

from testorchestrator import gitsync
from testorchestrator.execution import Project, Suite
from testorchestrator.extensions import db
from testorchestrator import settings as settings_mod

_ID = b"Test <t@example>"


def _remote_repo(tmp_path):
    """A real 'remote' (file://) + a working clone registered as export."""
    porcelain.init(str(tmp_path / "remote.git"), bare=True)
    porcelain.clone(str(tmp_path / "remote.git"), str(tmp_path / "work"))
    return Repo(str(tmp_path / "remote.git")), Repo(str(tmp_path / "work"))


def _commit_all(path: Path, message: str) -> str:
    porcelain.add(str(path))
    return porcelain.commit(str(path), message=message.encode(),
                            author=_ID, committer=_ID).decode()


def _push(path: Path) -> None:
    sink = io.BytesIO()
    porcelain.push(str(path), "origin", b"HEAD:refs/heads/master",
                   outstream=sink, errstream=sink)


def _remote_head(remote: Repo) -> str:
    return remote.head().decode()


def _remote_files(remote: Repo) -> list:
    tree = remote[remote.head()].tree
    return [e.path.decode() for e in
            iter_tree_contents(remote.object_store, tree)]


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
        assert any("TestOrchestrator" in f for f in _remote_files(remote))

        # Delete the local suite, restore from a FRESH clone.
        db.session.delete(suite); db.session.commit()
        porcelain.clone(str(tmp_path / "remote.git"),
                        str(tmp_path / "work2"))
        settings_mod.update({"git_export_repo": str(tmp_path / "work2")})
        counts = gitsync.restore_definitions(pid)
        assert counts["suites"] == 1
        restored = db.session.get(Suite, sid)
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
        other = tmp_path / "other"
        porcelain.clone(str(tmp_path / "remote.git"), str(other))
        conflict_file = (other / "TestOrchestrator" / "projects"
                         / project.id / "suites" / "conflict.json")
        conflict_file.parent.mkdir(parents=True, exist_ok=True)
        conflict_file.write_text('{"remote": true}')
        _commit_all(other, "remote edit")
        _push(other)

        # Local export now writes the same path -> merge conflict must
        # surface, never force-pushed.
        local_conflict = (tmp_path / "work" / "TestOrchestrator"
                          / "projects" / project.id / "suites"
                          / "conflict.json")
        local_conflict.write_text('{"local":1}')
        _commit_all(tmp_path / "work", "local edit")
        head_before = _remote_head(remote)
        with pytest.raises(gitsync.SyncError, match="conflict"):
            gitsync.push_export()
        # Remote still has exactly the remote commit — nothing overwritten.
        assert _remote_head(remote) == head_before


def test_disjoint_divergence_merges_and_pushes(app, tmp_path):
    """Two writers adding DIFFERENT paths must reconcile — the export
    workflow's real merge case. A real merge commit is created and
    pushed; neither side's content is lost."""
    remote, clone = _remote_repo(tmp_path)
    with app.app_context():
        settings_mod.update({"git_export_repo": str(tmp_path / "work")})
        project = Project(name="D")
        db.session.add(project); db.session.commit()
        gitsync.export_definitions(project)
        gitsync.push_export()

        other = tmp_path / "other"
        porcelain.clone(str(tmp_path / "remote.git"), str(other))
        (other / "remote-file.txt").write_text("from remote")
        _commit_all(other, "remote adds file")
        _push(other)

        # Local clone is now behind + has its own new path.
        (tmp_path / "work" / "local-file.txt").write_text("from local")
        _commit_all(tmp_path / "work", "local adds file")
        assert gitsync.push_export() == "pushed"
        # Both files exist locally after merge, and remotely after push.
        assert (tmp_path / "work" / "remote-file.txt").exists()
        assert (tmp_path / "work" / "local-file.txt").exists()
        files = _remote_files(remote)
        assert "remote-file.txt" in files and "local-file.txt" in files


def test_binding_clone_update_and_index(app, tmp_path):
    """End-to-end binding sync: dulwich clone -> recorded SHA -> worktree
    keeps .git -> second sync takes the update path -> new commits land."""
    remote, clone = _remote_repo(tmp_path)
    # Seed the remote with a Katalon-ish tree.
    seed = tmp_path / "work"
    (seed / "Test Cases" / "smoke").mkdir(parents=True)
    (seed / "Test Cases" / "smoke" / "case1.tc").write_text("<tc/>")
    (seed / "Test Suites").mkdir()
    (seed / "Test Suites" / "main.ts").write_text("<ts/>")
    _commit_all(seed, "seed katalon project")
    _push(seed)
    with app.app_context():
        project = Project(name="B")
        db.session.add(project); db.session.flush()
        from testorchestrator.execution import GitBinding
        binding = GitBinding(
            project_id=project.id, name="src",
            url=str(tmp_path / "remote.git"), ref="master")
        db.session.add(binding); db.session.commit()
        result = gitsync.sync_binding(binding.id)
        assert result["sha"]
        assert (Path(result["worktree"]) / ".git").exists()  # kept
        assert gitsync.list_test_cases(binding) == [
            "Test Cases/smoke/case1.tc"]
        assert gitsync.list_test_suites(binding) == [
            "Test Suites/main.ts"]

        # Advance the remote, sync again — exercises the update path.
        (seed / "Test Cases" / "smoke" / "case2.tc").write_text("<tc/>")
        sha2 = _commit_all(seed, "add case2")
        _push(seed)
        result2 = gitsync.sync_binding(binding.id)
        assert result2["sha"] == sha2
        assert result2["worktree"] == result["worktree"]  # same .git
        assert "Test Cases/smoke/case2.tc" in \
            gitsync.list_test_cases(binding)


def test_exact_revision_resolution(app, tmp_path):
    """Branch, tag and exact-sha selection all resolve byte-exact."""
    remote, clone = _remote_repo(tmp_path)
    seed = tmp_path / "work"
    (seed / "f").write_text("v1")
    _commit_all(seed, "one")
    (seed / "f").write_text("v2")
    sha2 = _commit_all(seed, "two")
    _push(seed)
    from testorchestrator import git_backend
    assert git_backend.run("resolve", seed, "HEAD") == sha2
    assert git_backend.run("resolve", seed, "master") == sha2
    assert git_backend.run("resolve", seed, sha2) == sha2
    # tag -> exact commit
    porcelain.tag_create(str(seed), "v1.0", objectish=sha2.encode())
    assert git_backend.run("resolve", seed, "v1.0") == sha2
    with pytest.raises(git_backend.GitBackendError):
        git_backend.run("resolve", seed, "0" * 40)


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


def test_broad_secret_exclusion(app, tmp_path):
    """Item 4: export allowlist — no credential value, password hash,
    session token, private key, or unlisted artifact kind leaks."""
    remote, clone = _remote_repo(tmp_path)
    with app.app_context():
        settings_mod.update({"git_export_repo": str(tmp_path / "work")})
        project = Project(name="S4")
        db.session.add(project); db.session.flush()
        from testorchestrator import dispatch
        from testorchestrator.execution import Artifact
        from testorchestrator.results import sha256_file
        from testorchestrator.secrets_store import store_secret
        store_secret(app, "git-cred", "git", "ghp_LIVE_TOKEN_123")
        suite = Suite(project_id=project.id, name="x")
        db.session.add(suite); db.session.flush()
        ex = dispatch.create_execution("admin", suite)
        ex.report_dir = str(tmp_path / "rep2")
        Path(ex.report_dir).mkdir()
        for name, content in (
            ("hash.txt", "$argon2id$v=19$m=65536"),
            ("key.pem", "-----BEGIN PRIVATE KEY-----"),
            ("tok.txt", "ghp_LIVE_TOKEN_123"),
            ("session.bin", "session-cookie-value"),
        ):
            (Path(ex.report_dir) / name).write_text(content)
            db.session.add(Artifact(
                execution_id=ex.id,
                kind="har" if name == "tok.txt" else "other",
                rel_path=name, sha256=sha256_file(Path(ex.report_dir) / name),
                size=len(content), mime="text/plain"))
        db.session.commit()
        gitsync.export_execution(ex)
        blob = "".join(p.read_text(errors="ignore")
                       for p in Path(tmp_path / "work").rglob("*")
                       if p.is_file())
        for marker in ("ghp_LIVE_TOKEN_123", "BEGIN PRIVATE KEY",
                       "$argon2id$", "session-cookie-value"):
            assert marker not in blob, marker
        # Only whitelisted kinds may exist under evidence/.
        ev = Path(tmp_path / "work") / "TestOrchestrator"
        assert not any(f.name in ("tok.txt", "hash.txt", "key.pem",
                                  "session.bin")
                       for f in ev.rglob("*") if f.is_file())
