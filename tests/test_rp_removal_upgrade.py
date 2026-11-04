"""Upgrade fixture for the ReportPortal removal (item 5).

Builds a populated pre-removal database at revision 7a8b9c0d1e2f, runs
the real migration to head (8b9c0d1e2f3a -> c4d5e6f7a8b9), and asserts:
- rp_* / reportportal_* settings + columns are gone;
- shared secrets, unrelated settings, executions, results and artifacts
  are preserved;
- the application starts after migration.
"""

import json
from pathlib import Path

import sqlalchemy as sa

MIGRATIONS = str(Path(__file__).resolve().parent.parent / "migrations")


def _upgrade(app, rev="head"):
    with app.app_context():
        from flask_migrate import upgrade
        upgrade(directory=MIGRATIONS, revision=rev)


def test_upgrade_from_populated_preremoval_db(tmp_path):
    # Fresh DB through the pre-removal revision — do NOT create_all, the
    # schema must come from the migrations themselves.
    from tests.conftest import make_app
    app = make_app(tmp_path, "upgrade.db")
    _upgrade(app, "7a8b9c0d1e2f")

    eng = sa.create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
    with eng.begin() as c:
        # Populate pre-removal state.
        c.execute(sa.text(
            "INSERT INTO projects (id, name, description) "
            "VALUES ('p1', 'Legacy', '')"))
        c.execute(sa.text(
            "INSERT INTO suites (id, project_id, name, description, "
            "version, content_hash, imported, readonly) "
            "VALUES ('s1', 'p1', 'S', '', 1, 'h', 0, 0)"))
        c.execute(sa.text(
            "INSERT INTO executions (id, project_id, suite_id, "
            "snapshot_json, source_sha, created_by, state, outcome, "
            "git_state, dispatch_id, workspace_path, report_dir, "
            "report_status, report_path, allure_status, allure_path, "
            "allure_version, error_kind, error_text, cancel_reason, "
            "rp_state, rp_launch_id, rp_endpoint_used, rp_project_used) "
            "VALUES ('e1', 'p1', 's1', '{}', 'abc', 'admin', 'completed', "
            "'passed', 'none', '', '', '/tmp/r', 'ready', 'r.html', "
            "'pending', '', '', '', '', '', 'done', 'L42', 'https://rp', 'proj')"))
        c.execute(sa.text(
            "INSERT INTO test_results (id, execution_id, "
            "suite_entry_order, case_ref, case_name, classname, "
            "iteration, attempt, status, duration_s, message, "
            "unresolved, rp_remote_id) VALUES ('r1', 'e1', 0, 'c', 'c1', "
            "'k', 0, 1, 'passed', 1.0, '', 0, 'rid9')"))
        c.execute(sa.text(
            "INSERT INTO artifacts (id, execution_id, result_id, kind, "
            "rel_path, sha256, size, mime, mapped, rp_state, "
            "rp_remote_id) VALUES ('a1', 'e1', NULL, 'junit', 'r.xml', "
            "'aa', 2, 'text/xml', 1, 'done', 'ra1')"))
        # RP settings that must go + shared/unrelated settings that must stay.
        for key, val in (("reportportal_endpoint", "https://rp"),
                         ("reportportal_project", "proj"),
                         ("reportportal_credential_ref", "shared-cred"),
                         ("reportportal_enabled", "1"),
                         ("rp_launch_name", "L"),
                         ("rp_verify_tls", "1"),
                         ("rp_ca_ref", "ca"),
                         ("shared_setting", "keep-me"),
                         ("git_export_repo", "/srv/export")):
            c.execute(sa.text(
                "INSERT INTO settings (key, value) VALUES (:k, :v)"),
                {"k": key, "v": json.dumps(val)})
        # A shared secret referenced by RP settings must NOT be deleted —
        # only the settings pointing at it are removed.
        c.execute(sa.text(
            "INSERT INTO secret_refs (id, name, kind, encrypted, "
            "meta_json) VALUES ('sec1', 'shared-cred', 'generic', "
            "x'00', '{}')"))

    _upgrade(app)  # migrate to head — exercises 8b9c0d1e2f3a + c4d5e6f7a8b9

    with eng.begin() as c:
        cols = {r[1] for r in c.execute(
            sa.text("PRAGMA table_info(executions)"))}
        assert not (cols & {"rp_state", "rp_launch_id",
                            "rp_endpoint_used", "rp_project_used"})
        rcols = {r[1] for r in c.execute(
            sa.text("PRAGMA table_info(test_results)"))}
        assert "rp_remote_id" not in rcols
        acols = {r[1] for r in c.execute(
            sa.text("PRAGMA table_info(artifacts)"))}
        assert not (acols & {"rp_state", "rp_remote_id"})

        keys = {r[0] for r in c.execute(sa.text("SELECT key FROM settings"))}
        assert not any(k.startswith(("reportportal_", "rp_"))
                       for k in keys)
        assert {"shared_setting", "git_export_repo"} <= keys

        assert c.execute(sa.text(
            "SELECT name FROM secret_refs WHERE id='sec1'")).scalar() \
            == "shared-cred"
        assert c.execute(sa.text(
            "SELECT outcome FROM executions WHERE id='e1'")).scalar() \
            == "passed"
        assert c.execute(sa.text(
            "SELECT case_name FROM test_results WHERE id='r1'")) \
            .scalar() == "c1"
        assert c.execute(sa.text(
            "SELECT rel_path FROM artifacts WHERE id='a1'")).scalar() \
            == "r.xml"
        # New schema exists too.
        assert c.execute(sa.text(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='schedules'")).scalar() == "schedules"

    # Application serves a request post-migration.
    resp = app.test_client().get("/login")
    assert resp.status_code == 200
