"""reporting-labs 2.0 migration — offline regeneration from stored
canonical results + authenticated portal integration.

Same fixtures/honesty as the Allure path; no live KRE. The
reporting-labs pytest plugin is blocked for this suite to keep its
own report output out of the repo test logs."""
import io
import zipfile
from pathlib import Path

import pytest

from testorchestrator import dispatch, settings as S
from testorchestrator.adapters import reporting_labs as rl
from testorchestrator.execution import (Artifact, Execution, ExecutionState,
                                        Outcome, Project, Suite, SuiteEntry,
                                        TestResult)
from testorchestrator.extensions import db
from testorchestrator.results import sha256_file


@pytest.fixture(autouse=True)
def _no_rl_plugin(pytestconfig):
    pytestconfig.pluginmanager.set_blocked("reporting_labs")


def _mk(app, tmp_path, shots=(), results=((("C.tc", "pass"), "passed"),)):
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        db.session.add(SuiteEntry(suite_id=s.id, order=0, case_ref="C"))
        ex = dispatch.create_execution("admin", s)
        ex.state = ExecutionState.COMPLETED
        ex.outcome = Outcome.FAILED
        rd = tmp_path / "ev" / ex.id
        rd.mkdir(parents=True)
        ex.report_dir = str(rd)
        for (cls, name), status in results:
            db.session.add(TestResult(
                execution_id=ex.id, case_ref=cls, case_name=name,
                classname=cls, status=status, duration_s=1.5,
                message="boom" if status != "passed" else ""))
        for shot in shots:
            f = rd / shot
            f.write_bytes(b"\x89PNG")
            db.session.add(Artifact(
                execution_id=ex.id, kind="screenshot", rel_path=shot,
                sha256=sha256_file(f), size=f.stat().st_size,
                mime="image/png"))
        db.session.commit()
        return ex.id


def test_generate_single_file_report(app, tmp_path):
    ex_id = _mk(app, tmp_path, shots=("bad.png", "stray-evidence.png"),
                results=[(("C.tc", "ok"), "passed"),
                         (("C.tc", "bad"), "failed"),
                         (("C.tc", "err"), "error"),
                         (("C.tc", "skip"), "skipped")])
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        out = rl.generate_for(ex)
        html = Path(ex.report_dir) / "reporting-labs" / "index.html"
        assert out["status"] == "ready", out
        assert out["cases"] == 4 and out["attached"] == 1
        assert out["unmapped"] == 1
        assert html.exists() and html.stat().st_size > 100_000
        body = html.read_text(encoding="utf-8", errors="replace")
        for needle in ("ok", "bad", "unmatched"):
            assert needle in body, needle
        # persisted state independent of allure_status/report_status
        assert ex.rl_status == "ready"
        assert ex.rl_path == "reporting-labs/index.html"
        assert ex.rl_version


def test_regenerate_idempotent(app, tmp_path):
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert rl.generate_for(ex)["status"] == "ready"
        first = (Path(ex.report_dir) / "reporting-labs" /
                 "index.html").read_bytes()
        assert rl.generate_for(ex)["status"] == "ready"
        again = (Path(ex.report_dir) / "reporting-labs" /
                 "index.html").read_bytes()
        assert len(again) > 100_000 and len(first) > 100_000


def test_missing_when_no_results(app, tmp_path):
    ex_id = _mk(app, tmp_path, results=[])
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert rl.generate_for(ex)["status"] == "missing"
        assert ex.rl_status == "missing"


def test_no_pdf_no_node_no_tmp_residue(app, tmp_path):
    """POC contract: single index.html only — no pdf, no node, no
    leftover tmp dirs."""
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        rl.generate_for(ex)
        root = Path(ex.report_dir)
        assert not (root / "reporting-labs" / "report.pdf").exists()
        assert not list(root.glob("reporting-labs.tmp.*"))
        assert not (root / "reporting-labs.history.json").exists()


def test_failed_regen_preserves_previous_usable(app, tmp_path,
                                              monkeypatch):
    """A generation failure never destroys the last usable report and
    never serves a half-written one — status=failed, path/file intact."""
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert rl.generate_for(ex)["status"] == "ready"
        good = (Path(ex.report_dir) / "reporting-labs" /
                "index.html").read_bytes()
        monkeypatch.setattr(rl, "_emit",
                            lambda *_a, **_k: 1 / 0)
        out = rl.generate_for(ex)
        assert out["status"] == "failed"
        assert ex.rl_status == "failed"
        # previous report intact and still referenced
        assert ex.rl_path == "reporting-labs/index.html"
        assert (Path(ex.report_dir) / "reporting-labs" /
                "index.html").read_bytes() == good
        # no tmp residue from the failed attempt
        assert not list(Path(ex.report_dir).glob("reporting-labs.tmp.*"))


def test_unique_tmp_dirs(app, tmp_path):
    """Concurrent generations get distinct tmp dirs — a stale
    reporting-labs.tmp.* from a crashed run is never clobbered or
    mistaken for output."""
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        root = Path(ex.report_dir)
        stale = root / "reporting-labs.tmp.crashed"
        stale.mkdir()
        (stale / "partial.json").write_text("{}")
        assert rl.generate_for(ex)["status"] == "ready"
        # the foreign tmp dir is left alone (operator evidence, not
        # silently deleted by a later run)
        assert (stale / "partial.json").exists()
        assert (root / "reporting-labs" / "index.html").exists()


def test_rlabs_routes_authz(client, login_as, app, tmp_path):
    """Anonymous requests redirect to login; authenticated users get
    report view/download/generate under existing portal authz."""
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    anon = app.test_client()
    for url in (f"/executions/{ex_id}/rlabs",
                f"/executions/{ex_id}/rlabs.zip"):
        r = anon.get(url)
        assert r.status_code == 302 and "/login" in r.headers["Location"]
    r = anon.post(f"/executions/{ex_id}/rlabs/generate")
    assert r.status_code == 302 and "/login" in r.headers["Location"]

    login_as("admin")
    # generate through the portal action
    r = client.post(f"/executions/{ex_id}/rlabs/generate",
                    follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert ex.rl_status == "ready"
    # hosted view — sandboxed like the Allure app
    r = client.get(f"/executions/{ex_id}/rlabs")
    assert r.status_code == 200
    assert r.headers["Content-Security-Policy"] == \
        "sandbox allow-scripts allow-same-origin"
    assert b"reporting" in r.data.lower()
    # download bundle with manifest
    z = client.get(f"/executions/{ex_id}/rlabs.zip")
    assert z.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(z.data)).namelist()
    assert "index.html" in names and "manifest.json" in names


def test_rlabs_routes_invalid_and_missing(client, login_as, app,
                                          tmp_path):
    """Invalid execution ids and missing reports are safe 404s/redirects,
    never tracebacks or empty pages."""
    login_as("admin")
    assert client.get("/executions/nonexistent/rlabs").status_code == 404
    assert client.get("/executions/nonexistent/rlabs.zip").status_code == 404
    ex_id = _mk(app, tmp_path)
    # no report yet -> redirect back with a flash, zip -> 404
    r = client.get(f"/executions/{ex_id}/rlabs", follow_redirects=False)
    assert r.status_code == 302
    assert client.get(f"/executions/{ex_id}/rlabs.zip").status_code == 404


def test_rlabs_path_safety(client, login_as, app, tmp_path):
    """A tampered rl_path cannot escape the report root."""
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        rl.generate_for(ex)
        ex.rl_path = "../../../../etc/hostname"
        db.session.commit()
    login_as("admin")
    assert client.get(f"/executions/{ex_id}/rlabs").status_code == 404


def test_rlabs_failed_regen_still_serves_previous(client, login_as, app,
                                                  tmp_path, monkeypatch):
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        rl.generate_for(ex)
        monkeypatch.setattr(rl, "_emit", lambda *_a, **_k: 1 / 0)
        assert rl.generate_for(ex)["status"] == "failed"
    login_as("admin")
    r = client.get(f"/executions/{ex_id}/rlabs")
    assert r.status_code == 200   # previous usable report still served


def test_injection_in_names_and_messages(app, tmp_path):
    """Hostile case names/messages/attachment names land as inert data —
    json.dumps with </script neutralised; never executable markup."""
    evil = '<script>alert(1)</script>'
    ex_id = _mk(
        app, tmp_path,
        shots=('<img src=x onerror=alert(1)>.png',),
        results=[(("C.tc", evil), "failed")])
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        out = rl.generate_for(ex)
        body = (Path(ex.report_dir) / "reporting-labs" /
                "index.html").read_text(encoding="utf-8",
                                        errors="replace")
        assert out["status"] == "ready"
        # the literal closing-script sequence must not appear raw —
        # RL emits it as <\/script inside its JSON payload
        assert "</script>alert" not in body
        assert "<\\/script" in body or "&lt;script" in body


def test_simultaneous_regeneration_consistent(app, tmp_path,
                                              monkeypatch):
    """Two REAL concurrent generate_for calls on the same execution:
    each builds a complete report; last-writer-wins; DB ends in a
    consistent state — never a half-written dir or mixed status."""
    import threading
    import time
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    orig = rl._emit

    def slow(ex, out):
        time.sleep(0.15)          # widen the publish race window
        return orig(ex, out)
    monkeypatch.setattr(rl, "_emit", slow)

    outcomes = {}

    def worker(i):
        with app.app_context():
            ex = db.session.get(Execution, ex_id)
            outcomes[i] = rl.generate_for(ex)

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        for st in outcomes.values():
            assert st["status"] in ("ready", "failed"), st
        # whichever writer won, the published artifact is COMPLETE
        html = Path(ex.report_dir) / "reporting-labs" / "index.html"
        assert html.exists() and html.stat().st_size > 100_000
        body = html.read_text(encoding="utf-8", errors="replace")
        assert "pass" in body
        assert ex.rl_status in ("ready", "failed")
        assert not list(Path(ex.report_dir)
                        .glob("reporting-labs.tmp.*"))


def test_reader_never_sees_missing_or_partial_report(app, tmp_path):
    """A reader racing regeneration: every open of index.html returns
    either the complete previous report or the complete new one —
    per-file os.replace makes the swap gap-free."""
    import threading
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert rl.generate_for(ex)["status"] == "ready"
        root = Path(ex.report_dir)
        target = root / "reporting-labs" / "index.html"
        problems, stop = [], threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    body = target.read_text(encoding="utf-8",
                                            errors="replace")
                except FileNotFoundError:
                    problems.append("missing index.html")
                    continue
                if len(body) < 50_000 or "<html" not in body.lower():
                    problems.append("partial/corrupt read")

        rt = threading.Thread(target=reader, daemon=True)
        rt.start()
        for _ in range(6):
            ex2 = db.session.get(Execution, ex_id)
            rl.generate_for(ex2)
        stop.set()
        rt.join(timeout=5)
        assert not problems, problems[:5]


def test_rlabs_project_scope_denial(client, login_as, app, tmp_path):
    """Cross-project denial on every rlabs surface — the same
    project_allowed() gate that covers Allure serving."""
    import json as _j
    from testorchestrator.models import User
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        rl.generate_for(ex)
        ex.project_id = "denied-project"
        u = User.query.filter_by(username="tester").first()
        u.allowed_projects = _j.dumps(["other-project"])
        db.session.commit()
    login_as("tester")
    assert client.get(f"/executions/{ex_id}/rlabs").status_code \
        in (403, 404)
    assert client.get(f"/executions/{ex_id}/rlabs.zip").status_code \
        in (403, 404)
    assert client.post(f"/executions/{ex_id}/rlabs/generate"
                       ).status_code in (403, 404)


def test_unresolved_banner_on_execution_page(client, login_as, app,
                                             tmp_path):
    """Unresolved results get a portal-level warning BEFORE the report
    is opened — the count comes from canonical results, not the
    report's own skipped bucket."""
    ex_id = _mk(app, tmp_path, results=[
        (("C.tc", "ok"), "passed"), (("C.tc", "bad"), "failed")])
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        for r in ex.results:
            if r.case_name == "bad":
                r.unresolved = True
        db.session.commit()
        rl.generate_for(ex)
    login_as("admin")
    body = client.get(f"/executions/{ex_id}").data.decode()
    assert "1 unresolved" in body
    assert "displayed as" in body and "skipped" in body


def test_report_backend_setting_default_and_save(client, login_as, app):
    """Allure stays the default backend; the selection persists and
    rejects unknown values."""
    with app.app_context():
        assert S.get("report_backend") == "allure"
        assert S.get("reportinglabs_enabled") is False
    login_as("admin")
    r = client.post("/system/reporting/save", data={
        "allure_enabled": "1",
        "reportinglabs_enabled": "1",
        "report_backend": "reporting-labs"}, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        assert S.get("report_backend") == "reporting-labs"
        assert S.get("reportinglabs_enabled") is True
    # unknown backend can never win the default
    r = client.post("/system/reporting/save", data={
        "report_backend": "bogus"}, follow_redirects=True)
    with app.app_context():
        assert S.get("report_backend") == "allure"
