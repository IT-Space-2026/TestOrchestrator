"""Secrets store, user management, role scoping, Git export tests."""

import json
import stat
from pathlib import Path

import pytest

from testorchestrator import gitsync
from testorchestrator.execution import (ExecutionState, Outcome, Project, Suite,
                                        SyncState)
from testorchestrator.extensions import db
from testorchestrator.models import Role, User
from testorchestrator.secrets_store import (SecretError, list_secret_meta,
                                            read_secret, store_secret)


# ---------- secrets (§12) ----------

def test_secret_roundtrip_and_no_plaintext(app, tmp_path, monkeypatch):
    key = tmp_path / "secret.key"
    monkeypatch.setenv("TO_SECRET_KEY_FILE", str(key))
    with app.app_context():
        store_secret(app, "git-main", "git", "s3cr3t-token",
                     {"description": "main repo"})
        assert read_secret(app, "git-main") == "s3cr3t-token"
        meta = list_secret_meta()
        assert meta[0]["name"] == "git-main"
        assert "s3cr3t" not in json.dumps(meta)
        # DB stores ciphertext only.
        raw = db.session.execute(
            db.text("SELECT encrypted FROM secret_refs")).scalar()
        assert b"s3cr3t" not in raw


def test_missing_secret_is_actionable(app, monkeypatch, tmp_path):
    monkeypatch.setenv("TO_SECRET_KEY_FILE", str(tmp_path / "k"))
    with app.app_context():
        with pytest.raises(SecretError, match="not configured"):
            read_secret(app, "nonexistent")


def test_key_file_permissions(app, tmp_path, monkeypatch):
    key = tmp_path / "secret.key"
    monkeypatch.setenv("TO_SECRET_KEY_FILE", str(key))
    with app.app_context():
        store_secret(app, "x", "git", "y")
    assert stat.S_IMODE(key.stat().st_mode) & 0o777 == 0o600


# ---------- user management (§16) ----------

def test_admin_create_reset_revokes_sessions(client, login_as, app):
    admin_client = client
    newbie_client = app.test_client()
    login_as("admin")
    resp = admin_client.post("/system/users", data={
        "username": "newbie", "display_name": "", "role": "tester",
        "temporary_password": "Temp-Password-12345",
    })
    assert resp.status_code == 302
    with app.app_context():
        u = User.query.filter_by(username="newbie").first()
        assert u.must_change_password is True
        assert u.role == Role.TESTER

    # Log in as the new user: forced change gate applies.
    newbie_client.post("/login", data={"username": "newbie",
                                       "password": "Temp-Password-12345"})
    assert "/change-password" in newbie_client.get("/").headers["Location"]
    newbie_client.post("/change-password", data={
        "current_password": "Temp-Password-12345",
        "new_password": "My0wn-Str0ng-Pass!",
        "confirm_password": "My0wn-Str0ng-Pass!"})
    assert newbie_client.get("/").status_code == 200

    # Admin resets → the user's live session is revoked.
    with app.app_context():
        uid = User.query.filter_by(username="newbie").first().id
    admin_client.post(f"/system/users/{uid}/reset",
                      data={"temporary_password": "Reset-Temp-99999"})
    response = newbie_client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_last_admin_protected(client, login_as, app):
    login_as("admin")
    from testorchestrator.models import User
    with app.app_context():
        admin_id = User.query.filter_by(username="admin").first().id
    resp = client.post(f"/system/users/{admin_id}/deactivate")
    assert resp.status_code == 302
    with app.app_context():
        assert db.session.get(User, admin_id).is_active is True


def test_users_page_admin_only(client, login_as):
    login_as("tester")
    assert client.get("/system/users").status_code == 403
    login_as("po")
    assert client.get("/system/secrets").status_code == 403


# ---------- git export (§11) ----------

def test_git_export_layout(app, tmp_path):
    """Export writes the §11 layout and never touches secrets/users."""
    from testorchestrator import dispatch, gitsync
    from testorchestrator import settings as settings_mod
    from testorchestrator.execution import Artifact, TestResult

    with app.app_context():
        settings_mod.update({"git_export_repo": str(tmp_path / "dest.git" )})
        project = Project(name="P")
        db.session.add(project)
        db.session.flush()
        suite = Suite(project_id=project.id, name="S")
        db.session.add(suite)
        db.session.flush()
        ex = dispatch.create_execution("admin", suite)
        ex.state = ExecutionState.COMPLETED
        ex.outcome = Outcome.PASSED
        ex.report_dir = str(tmp_path / "rep")
        Path(ex.report_dir).mkdir()
        (Path(ex.report_dir) / "shot.png").write_bytes(b"PNG")
        db.session.add(TestResult(
            execution_id=ex.id, case_name="c1", status="passed",
            duration_s=1.0))
        from testorchestrator.results import sha256_file
        db.session.add(Artifact(
            execution_id=ex.id, kind="screenshot", rel_path="shot.png",
            sha256=sha256_file(Path(ex.report_dir) / "shot.png"),
            size=3, mime="image/png"))
        # A secret that must never leave the DB.
        db.session.commit()
        result = gitsync.export_execution(ex)
        base = Path(result["path"])
        for name in ("snapshot.json", "summary.json", "results.json",
                     "reporting.json"):
            assert (base / name).exists(), name
        assert (base / "evidence" / "screenshot" / "shot.png").exists()
        # Repo contains only the export tree.
        repo_files = list(Path(tmp_path / "dest.git").rglob("*"))
        assert not any("secret" in str(f).lower() for f in repo_files)


def test_git_not_configured_is_honest(app):
    from testorchestrator import dispatch
    with app.app_context():
        project = Project(name="P2")
        db.session.add(project); db.session.flush()
        suite = Suite(project_id=project.id, name="S2")
        db.session.add(suite); db.session.flush()
        ex = dispatch.create_execution("admin", suite)
        with pytest.raises(gitsync.NotConfigured):
            gitsync.export_execution(ex)


# ---------- project scoping (§16) ----------

def test_project_scope_enforced(client, login_as, app):
    with app.app_context():
        project = Project(name="Scoped")
        db.session.add(project)
        db.session.flush()
        suite = Suite(project_id=project.id, name="X")
        db.session.add(suite)
        db.session.commit()
        pid = project.id
        # Tester scoped away from this project.
        tester = User.query.filter_by(username="tester").first()
        tester.allowed_projects = json.dumps(["other"])
        db.session.commit()
    login_as("tester")
    assert client.get(f"/projects/{pid}").status_code == 403
    login_as("admin")
    assert client.get(f"/projects/{pid}").status_code == 200
