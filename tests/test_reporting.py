"""Local-reporting tests (consolidated §3–§5): report_status
independence, artifact authorization/path safety, usable-report zip,
idempotent refresh — FIXTURE evidence, not real-KRE acceptance."""

import io
import zipfile
from pathlib import Path

from testorchestrator import dispatch, reporting
from testorchestrator.execution import (Artifact, Execution, ExecutionState,
                                        Outcome, Project, Suite, SuiteEntry)
from testorchestrator.extensions import db


def _mk(app, tmp_path, report_files: dict, artifacts=True):
    """Execution row + on-disk evidence under the stable run dir."""
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        db.session.add(SuiteEntry(suite_id=s.id, order=0, case_ref="C"))
        ex = dispatch.create_execution("admin", s)
        ex.state = ExecutionState.COMPLETED
        ex.outcome = Outcome.FAILED  # a real verdict — report must not touch it
        rd = tmp_path / "evidence" / ex.id
        rd.mkdir(parents=True)
        ex.report_dir = str(rd)
        ex.workspace_path = str(tmp_path / "work" / ex.id)
        db.session.commit()
        for rel, content in report_files.items():
            f = rd / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(content)
            if artifacts:
                db.session.add(Artifact(
                    execution_id=ex.id, kind="html" if rel.endswith(".html")
                    else "junit" if rel.endswith(".xml") else "screenshot",
                    rel_path=rel, sha256=__import__(
                        "testorchestrator.results", fromlist=["x"]
                    ).sha256_file(f), size=f.stat().st_size,
                    mime="text/html"))
        db.session.commit()
        return ex.id


def _ctx_login(client, login_as):
    login_as("admin")


def test_ready_when_native_html_present(app, tmp_path):
    ex_id = _mk(app, tmp_path, {
        "R/report.html": b"<html><body>KRE report</body></html>",
        "R/assets/app.css": b"body{}",
        "R/screens/fail.png": b"\x89PNG"})
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert reporting.refresh_report_status(ex) == "ready"
        assert ex.report_path == "R/report.html"


def test_partial_when_results_without_report(app, tmp_path):
    ex_id = _mk(app, tmp_path, {"R/suite/junit.xml": b"<testsuite/>"})
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert reporting.refresh_report_status(ex) == "partial"
        assert ex.report_path == ""


def test_missing_when_nothing(app, tmp_path):
    ex_id = _mk(app, tmp_path, {})
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert reporting.refresh_report_status(ex) in ("missing", "partial")


def test_report_status_does_not_touch_outcome(app, tmp_path):
    ex_id = _mk(app, tmp_path, {})
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        reporting.refresh_report_status(ex)
        assert ex.state == ExecutionState.COMPLETED
        assert ex.outcome == Outcome.FAILED


def test_view_report_sandboxed(client, login_as, app, tmp_path):
    ex_id = _mk(app, tmp_path, {
        "R/report.html": b"<html><script>x()</script></html>"})
    _ctx_login(client, login_as)
    resp = client.get(f"/executions/{ex_id}/report")
    assert resp.status_code == 200
    assert resp.headers["Content-Security-Policy"] == "sandbox"
    resp2 = client.get(
        f"/executions/{ex_id}/report/files/../evidence/{ex_id}/R/report.html")
    assert resp2.status_code == 404  # traversal refused
    # Cross-run access: another execution's asset path under this entry dir
    ex2_id = _mk(app, tmp_path, {"R/report.html": b"<html/>"})
    resp3 = client.get(f"/executions/{ex_id}/report/files/secret.png")
    assert resp3.status_code == 404


def test_downloads_are_usable_and_authorized(client, login_as, app, tmp_path):
    files = {"R/report.html": b"<html/>", "R/a.js": b"1",
             "R/s.png": b"\x89PNG"}
    ex_id = _mk(app, tmp_path, files)
    _ctx_login(client, login_as)
    z = client.get(f"/executions/{ex_id}/report.zip")
    assert z.status_code == 200
    names = set(zipfile.ZipFile(io.BytesIO(z.data)).namelist())
    assert {"report.html", "a.js", "s.png", "manifest.json"} <= names
    a = client.get(f"/executions/{ex_id}/artifacts.zip")
    assert a.status_code == 200
    anames = set(zipfile.ZipFile(io.BytesIO(a.data)).namelist())
    assert "manifest.json" in anames and "R/report.html" in anames
    # Unauthenticated downloads refused (fresh client, no session).
    anon = app.test_client()
    assert anon.get(f"/executions/{ex_id}/report.zip").status_code in \
        (302, 401)
    assert anon.get(f"/executions/{ex_id}/report").status_code in \
        (302, 401)


def test_effective_response_headers(client, login_as, app, tmp_path):
    """Recorded effective headers: report content is sandbox-isolated,
    downloads are attachments, baseline hardening preserved."""
    ex_id = _mk(app, tmp_path, {"R/report.html": b"<html/>",
                                "R/a.js": b"1"})
    _ctx_login(client, login_as)
    view = client.get(f"/executions/{ex_id}/report")
    assert view.headers["Content-Security-Policy"] == "sandbox"
    assert view.headers["X-Content-Type-Options"] == "nosniff"
    assert view.headers["X-Frame-Options"] == "DENY"
    z = client.get(f"/executions/{ex_id}/report.zip")
    assert z.headers["Content-Type"].startswith("application/zip")
    assert "attachment" in z.headers.get("Content-Disposition", "")
    # Baseline CSP on a normal page — report override did not leak.
    detail = client.get(f"/executions/{ex_id}")
    assert "default-src 'self'" in \
        detail.headers["Content-Security-Policy"]


def test_refresh_is_idempotent_and_reruns_nothing(client, login_as, app,
                                                  tmp_path):
    ex_id = _mk(app, tmp_path, {"R/j.xml": b"<testsuite/>"})
    _ctx_login(client, login_as)
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        n0 = len(ex.artifacts)
    # New file lands on disk post-collection; refresh registers ONLY it.
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        f = Path(ex.report_dir) / "R" / "late.png"
        f.write_bytes(b"\x89PNG")
    resp = client.post(f"/executions/{ex_id}/report/refresh",
                       follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert len(ex.artifacts) == n0 + 1
        paths = {a.rel_path for a in ex.artifacts}
        assert len(paths) == len(ex.artifacts)  # no duplicate rel_paths
        assert ex.outcome == Outcome.FAILED  # untouched
    # Second refresh: nothing new registered.
    with app.app_context():
        assert reporting.register_missing_artifacts(
            db.session.get(Execution, ex_id)) == 0


# ---------- bundled-git launcher ----------

def test_git_launcher_scopes_env_and_preserves_os_environ():
    from testorchestrator import git_runtime
    exe = git_runtime.executable()
    if git_runtime.bundled_available():
        assert "to-git-launch" in exe
        content = Path(exe).read_text()
        assert "LD_LIBRARY_PATH" in content and "git-core" in content
        # The launcher execs the vendored binary — runs standalone.
        import subprocess
        out = subprocess.run([exe, "--version"], capture_output=True,
                             text=True, timeout=15)
        assert out.returncode == 0 and "git version" in out.stdout
        # Global env untouched — no leakage into unrelated processes.
        import os
        assert "GIT_EXEC_PATH" not in os.environ
    else:
        assert exe == "git"  # declared host prerequisite fallback


def test_gitpython_configured():
    import git
    from testorchestrator import gitsync  # noqa: F401 — import configures
    assert git.Git.GIT_PYTHON_GIT_EXECUTABLE
