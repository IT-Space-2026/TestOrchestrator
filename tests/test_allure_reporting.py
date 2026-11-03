"""Allure adapter + reporting-settings tests (consolidated addendum).

Fixture results + REAL bundled generation where the vendored JRE/Allure
is present — generation is genuine (not simulated); the RESULTS are
fixtures, not real-KRE acceptance."""

import json
import zipfile
import io
from pathlib import Path

import pytest

from testorchestrator import allure_runtime, dispatch, settings as S
from testorchestrator.adapters import allure as allure_adapter
from testorchestrator.execution import (Artifact, Execution, ExecutionState,
                                        Outcome, Project, Suite, SuiteEntry,
                                        TestResult)
from testorchestrator.extensions import db
from testorchestrator.results import sha256_file


def _mk(app, tmp_path, shots=(), results=((("C.tc","pass"),"passed"),)):
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


def test_status_mapping_honest(app, tmp_path):
    ex_id = _mk(app, tmp_path, results=[
        (("C","ok"), "passed"), (("C","bad"), "failed"),
        (("C","err"), "error"), (("C","skip"), "skipped")])
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        dest = Path(ex.report_dir) / "ar"
        allure_adapter._write_results(ex, dest)
        statuses = {}
        for f in dest.glob("*-result.json"):
            b = json.loads(f.read_text())
            statuses[b["name"]] = b["status"]
        assert statuses == {"ok": "passed", "bad": "failed",
                            "err": "broken", "skip": "skipped"}


def test_screenshot_maps_to_correct_case(app, tmp_path):
    ex_id = _mk(app, tmp_path, shots=("bad-fail.png", "other.png"),
                results=[(("C", "bad"), "failed"),
                         (("C", "ok"), "passed")])
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        dest = Path(ex.report_dir) / "ar"
        allure_adapter._write_results(ex, dest)
        bodies = [json.loads(f.read_text())
                  for f in dest.glob("*-result.json")]
        bad = next(b for b in bodies if b["name"] == "bad")
        ok = next(b for b in bodies if b["name"] == "ok")
        assert [a["name"] for a in bad["attachments"]] == ["bad-fail.png"]
        assert "attachments" not in ok
        # Unmatched evidence is a VISIBLE labelled result entry.
        um = next(b for b in bodies if "unmatched" in b["name"])
        assert um["status"] == "unknown"
        assert um["attachments"][0]["name"] == "other.png"


def test_duplicate_case_names_never_guessed(app, tmp_path):
    """Two results named 'login' across suites: a login.png can never be
    resolved unambiguously -> unmatched, attached to NOTHING."""
    ex_id = _mk(app, tmp_path, shots=("login.png",),
                results=[(("S1", "login"), "failed"),
                         (("S2", "login"), "failed")])
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        dest = Path(ex.report_dir) / "ar"
        allure_adapter._write_results(ex, dest)
        bodies = [json.loads(f.read_text())
                  for f in dest.glob("*-result.json")]
        for b in bodies:
            if b["name"] == "login":
                assert "attachments" not in b
        um = next(b for b in bodies if "unmatched" in b["name"])
        assert um["attachments"][0]["name"] == "login.png"


def test_similar_filenames_never_guessed(app, tmp_path):
    """'bad-fail.png' tokenizes to {bad,fail}: with cases 'bad' and
    'bad-fail' both matching -> ambiguous -> unmatched."""
    ex_id = _mk(app, tmp_path, shots=("bad-fail.png",),
                results=[(("C", "bad"), "failed"),
                         (("C", "bad-fail"), "failed")])
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        dest = Path(ex.report_dir) / "ar"
        allure_adapter._write_results(ex, dest)
        bodies = [json.loads(f.read_text())
                  for f in dest.glob("*-result.json")]
        for b in bodies:
            if b["name"] in ("bad", "bad-fail"):
                assert "attachments" not in b
        um = next(b for b in bodies if "unmatched" in b["name"])
        assert um["attachments"][0]["name"] == "bad-fail.png"


def test_missing_screenshots_leave_clean_cases(app, tmp_path):
    ex_id = _mk(app, tmp_path, results=[(("C", "ok"), "passed")])
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        dest = Path(ex.report_dir) / "ar"
        stats = allure_adapter._write_results(ex, dest)
        assert stats["unmapped"] == 0 and stats["cases"] == 1


def test_regenerate_failure_preserves_last_usable(app, tmp_path, monkeypatch):
    """A failed regen leaves the previous index.html intact (atomic
    publish) and marks allure_status failed — never a half report."""
    if not allure_runtime.available():
        pytest.skip("bundled allure absent")
    ex_id = _mk(app, tmp_path, results=[(("C", "bad"), "failed")])
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert allure_adapter.generate_for(ex)["status"] == "ready"
        good = (Path(ex.report_dir) / ex.allure_path).read_bytes()
        # Next generation blows up inside the generator.
        monkeypatch.setattr(allure_adapter, "run_generator",
                            lambda *a, **k: {"ok": False,
                                             "error": "simulated crash"})
        res = allure_adapter.generate_for(ex)
        assert res["status"] == "failed"
        still = Path(ex.report_dir) / ex.allure_path
        assert still.exists() and still.read_bytes() == good
        assert ex.outcome == Outcome.FAILED  # outcome untouched


def test_rp_destination_pin_blocks_redirect(app, tmp_path, monkeypatch):
    """After publishing to one endpoint, a settings change must not
    silently redirect the retry to another instance."""
    from testorchestrator.adapters.reportportal import (
        NotConfigured, ReportPortalAdapter)
    ex_id = _mk(app, tmp_path)
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        ex.rp_endpoint_used = "https://rp-old.example.com"
        db.session.commit()
        adapter = ReportPortalAdapter("https://rp-new.example.com",
                                      "p", "tok")
        monkeypatch.setattr(adapter, "launch", lambda *a, **k: "L1")
        import pytest as _pt
        with _pt.raises(NotConfigured, match="endpoint changed"):
            adapter.publish_execution(ex)


def test_missing_when_no_results(app, tmp_path):
    ex_id = _mk(app, tmp_path, results=[])
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert allure_adapter.generate_for(ex)["status"] == "missing"
        assert ex.state == ExecutionState.COMPLETED  # outcome untouched


@pytest.mark.skipif(not allure_runtime.available(),
                    reason="bundled JRE/allure absent on this host")
def test_real_bundled_generation_and_regenerate(app, tmp_path):
    """REAL generation via vendored JRE+allure (fixture results)."""
    ex_id = _mk(app, tmp_path, shots=("bad-fail.png",),
                results=[(("C", "bad"), "failed")])
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        res = allure_adapter.generate_for(ex)
        assert res["status"] == "ready"
        html = Path(ex.report_dir) / ex.allure_path
        assert html.name == "index.html" and html.stat().st_size > 1e6
        v1 = ex.allure_version
        res2 = allure_adapter.generate_for(ex)   # retry: regen, no rerun
        assert res2["status"] == "ready" and ex.allure_version == v1
        assert Execution.query.count() == 1      # no duplicate run


def test_allure_routes_and_settings_authz(client, login_as, app, tmp_path):
    ex_id = _mk(app, tmp_path)
    # Settings page is Admin-only.
    anon = app.test_client()
    assert anon.get("/system/reporting").status_code in (302, 401)
    login_as("admin")
    resp = client.get("/system/reporting")
    assert resp.status_code == 200
    # Saving config persists refs — never publishes, never stores values.
    resp = client.post("/system/reporting/save", data={
        "allure_enabled": "1", "rp_enabled": "1",
        "rp_endpoint": "https://rp.example.com/", "rp_project": "p1",
        "rp_credential_ref": "", "rp_launch_name": "{project}/{suite}",
        "rp_verify_tls": "1"}, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert S.get("allure_enabled") is True
        assert S.get("reportportal_endpoint") == "https://rp.example.com"
        assert S.get("rp_launch_name") == "{project}/{suite}"
    # Allure generate action on the run page.
    if allure_runtime.available():
        resp = client.post(f"/executions/{ex_id}/allure/generate",
                           follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            ex = db.session.get(Execution, ex_id)
            assert ex.allure_status == "ready"
        r = client.get(f"/executions/{ex_id}/allure")
        assert r.status_code == 200
        assert r.headers["Content-Security-Policy"] == "sandbox"
        z = client.get(f"/executions/{ex_id}/allure.zip")
        assert z.status_code == 200
        assert "index.html" in zipfile.ZipFile(io.BytesIO(z.data)).namelist()


def test_settings_survive_restart_and_secrets_never_render(
        client, login_as, app):
    """Settings persist across a fresh app context (restart); secret
    values never appear in pages, flashes, or downloads — only refs."""
    login_as("admin")
    client.post("/system/reporting/save", data={
        "allure_enabled": "1", "rp_endpoint": "https://rp.example.com",
        "rp_project": "p1", "rp_credential_ref": "",
        "rp_launch_name": "{project}/{suite}"})
    with app.app_context():            # new context ~ restart
        assert S.get("allure_enabled") is True
        assert S.get("rp_launch_name") == "{project}/{suite}"
    # Store a real credential, then prove it never renders.
    with app.app_context():
        from testorchestrator.secrets_store import store_secret
        from flask import current_app
        store_secret(current_app, "rp-token", "rp", "TOPSECRET-9x")
        S.update({"reportportal_credential_ref": "rp-token"})
    resp = client.get("/system/reporting")
    assert b"TOPSECRET-9x" not in resp.data
    assert b"rp-token" in resp.data          # the REF renders, not value
    resp2 = client.post("/system/reporting/check", follow_redirects=True)
    assert b"TOPSECRET-9x" not in resp2.data
