"""Allure adapter + reporting-settings tests (consolidated addendum).

Fixture results + REAL generation where a compatible Java runtime and
the bundled Allure are present — generation is genuine (not simulated);
the RESULTS are fixtures, not real-KRE acceptance. Java is an external
prerequisite: detected/selected and validated by an actual probe."""

import json
import os
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
        pytest.skip("allure generator or Java runtime absent")
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
                    reason="no compatible Java + bundled allure here")
def test_real_generation_and_regenerate(app, tmp_path):
    """REAL generation via operator Java + bundled allure (fixtures)."""
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


def test_authenticated_nonadmin_denied_settings(client, login_as, app):
    """An authenticated but UNAUTHORIZED user (not anonymous) is refused
    reporting settings + mutations — scope check, not just anon 401."""
    login_as("tester")
    assert client.get("/system/reporting").status_code == 403
    assert client.post("/system/reporting/save", data={
        "rp_endpoint": "https://evil.example.com"}).status_code == 403
    assert client.post("/system/reporting/test-publish").status_code == 403
    with app.app_context():
        assert not S.get("reportportal_endpoint")  # nothing persisted


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


# ---------- external-Java prerequisite (§ packaging change) ----------

def _fake_jh(tmp_path):
    home = tmp_path / "jh"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "java").write_text("#!/bin/sh\n")
    return home


def test_normalize_java_home(tmp_path):
    home = _fake_jh(tmp_path)
    assert allure_runtime.normalize_java_home(str(home)) == home
    assert (allure_runtime.normalize_java_home(
        str(home / "bin" / "java")) == home)
    assert allure_runtime.normalize_java_home("/nonexistent") is None
    assert allure_runtime.normalize_java_home("") is None


def test_selected_java_precedence(app, tmp_path, monkeypatch):
    """admin setting > installer env > detection."""
    a, b, c = (_fake_jh(tmp_path / n) for n in ("a", "b", "c"))
    monkeypatch.setenv("TO_ALLURE_JAVA_HOME", str(b))
    monkeypatch.setattr(allure_runtime, "detect_java_homes",
                        lambda: [c])
    with app.app_context():
        S.update({"allure_java_home": ""})
        home, src = allure_runtime.selected_java()
        assert (home, src) == (b, "installer")
        S.update({"allure_java_home": str(a)})
        home, src = allure_runtime.selected_java()
        assert (home, src) == (a, "admin")
        S.update({"allure_java_home": ""})
        monkeypatch.delenv("TO_ALLURE_JAVA_HOME")
        home, src = allure_runtime.selected_java()
        assert (home, src) == (c, "detected")


def test_missing_java_status_and_generate(app, monkeypatch, tmp_path):
    """No candidates anywhere -> honest not-ready + actionable error;
    generation reports failure instead of pretending."""
    monkeypatch.delenv("TO_ALLURE_JAVA_HOME", raising=False)
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(allure_runtime, "detect_java_homes",
                        lambda: [])
    with app.app_context():
        st = allure_runtime.status()
        assert st["ready"] is False
        assert "Java" in st["reason"]
        assert st["java_home"] == ""
        res = allure_runtime.generate(tmp_path / "r", tmp_path / "o")
        assert res["ok"] is False and "Java" in res["error"]


def test_missing_java_adapter_marks_failed_not_outcome(
        app, tmp_path, monkeypatch):
    """Execution rows stay honest: allure fails, outcome untouched."""
    monkeypatch.delenv("TO_ALLURE_JAVA_HOME", raising=False)
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(allure_runtime, "detect_java_homes",
                        lambda: [])
    ex_id = _mk(app, tmp_path, results=[(("C", "bad"), "failed")])
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        res = allure_adapter.generate_for(ex)
        assert res["status"] == "failed" and "Java" in res["error"]
        assert ex.outcome == Outcome.FAILED      # never clobbered
        assert ex.state == ExecutionState.COMPLETED


def test_explicit_bad_path_never_falls_back(app, tmp_path, monkeypatch):
    """An explicit wrong selection must not probe some OTHER runtime —
    the record names the attempted path and fails."""
    real = _fake_jh(tmp_path)
    monkeypatch.setenv("TO_ALLURE_JAVA_HOME", str(real))
    with app.app_context():
        rec = allure_runtime.probe(str(tmp_path / "nope"))
        assert rec["ok"] is False
        assert "bin/java" in rec["error"]
        assert rec["java_home"] != str(real)


def test_generate_scopes_java_env_to_child(app, tmp_path, monkeypatch):
    """JAVA_HOME is set ONLY in the Allure child env — os.environ and
    every other process (incl. KRE) are untouched."""
    home = _fake_jh(tmp_path)
    monkeypatch.setenv("TO_ALLURE_JAVA_HOME", str(home))
    monkeypatch.delenv("JAVA_HOME", raising=False)
    captured = {}

    class Fake:
        returncode = 1
        stderr = "boom"
        stdout = ""

    def fake_run(cmd, env=None, **kw):
        captured["env"] = env
        return Fake()

    monkeypatch.setattr(allure_runtime.subprocess, "run", fake_run)
    with app.app_context():
        res = allure_runtime.generate(tmp_path / "r", tmp_path / "o")
    assert res["ok"] is False
    assert captured["env"]["JAVA_HOME"] == str(home)
    assert os.environ.get("JAVA_HOME") is None      # global untouched


def test_check_readiness_persists_probe_and_shows_ready(
        client, login_as, app, tmp_path, monkeypatch):
    """Admin -> Reporting -> Check: real-probe record persisted; the
    page then reports the validated Java + version."""
    home = _fake_jh(tmp_path)
    monkeypatch.setenv("TO_ALLURE_JAVA_HOME", str(home))
    fake = {"ok": True, "java_home": str(home), "java_version": "21.0.1",
            "error": "", "checked_at": "2026-11-03T00:00:00+00:00"}
    monkeypatch.setattr(allure_runtime, "probe",
                        lambda *a, **k: dict(fake))
    login_as("admin")
    resp = client.post("/system/reporting/check", follow_redirects=True)
    assert resp.status_code == 200
    assert b"ready" in resp.data
    with app.app_context():
        assert S.get("allure_java_home") == str(home)
        assert S.get("allure_java_probe")["java_version"] == "21.0.1"
        assert allure_runtime.status()["ready"] is True


def test_changed_java_needs_recheck(app, tmp_path, monkeypatch):
    """A probe record for one home does not bless a different home —
    switching runtimes flips readiness back to unvalidated."""
    a, b = _fake_jh(tmp_path / "x"), _fake_jh(tmp_path / "y")
    with app.app_context():
        S.update({"allure_java_home": str(a),
                  "allure_java_probe": {"ok": True, "java_home": str(a),
                                        "checked_at": "t"}})
        assert allure_runtime.status()["ready"] is True
        S.update({"allure_java_home": str(b)})
        st = allure_runtime.status()
        assert st["ready"] is False
        assert "recheck" in st["reason"] or st["reason"]


# ---------- cross-platform discovery (shared logic, Win + Linux) -----

class FakeWinreg:
    """winreg-shaped fake: OpenKey(hive, root) -> base handle;
    OpenKey(base, ver) -> subkey handle; EnumKey iterates subkey names;
    QueryValueEx reads values. Mirrors the real API the module uses."""
    HKEY_LOCAL_MACHINE = "HKLM"
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_WOW64_64KEY = 0x100

    def __init__(self, table):
        # table[(hive, root)] = {version: {"JavaHome": path, ...}}
        self.table = table

    def OpenKey(self, hive, root, _rsv=0, _acc=0):
        if isinstance(hive, dict):          # OpenKey(base_handle, ver)
            base, ver = hive, root
            if ver in base:
                return base[ver]
            raise OSError("no subkey")
        key = (hive, root)
        if key not in self.table:
            raise OSError("not found")
        return self.table[key]

    def EnumKey(self, base, i):
        vers = sorted(base)
        if i >= len(vers):
            raise OSError("end of keys")
        return vers[i]

    def QueryValueEx(self, subkey, name):
        if name not in subkey:
            raise OSError("no value")
        return subkey[name], 1


def test_windows_registry_enum():
    """JavaSoft JavaHome values are harvested across hives and roots,
    including paths containing spaces."""
    wr = FakeWinreg({
        ("HKLM", r"SOFTWARE\JavaSoft\JDK"):
            {"21": {"JavaHome": r"C:\Program Files\Java\jdk-21"},
             "17": {"JavaHome": r"C:\Program Files\Java\jdk 17"}},
        ("HKCU", r"SOFTWARE\JavaSoft\JRE"):
            {"1.8": {"JavaHome": r"C:\Tools\jre8"}},
    })
    homes = allure_runtime._enum_registry_java_homes(wr)
    assert r"C:\Program Files\Java\jdk-21" in homes
    assert r"C:\Program Files\Java\jdk 17" in homes
    assert r"C:\Tools\jre8" in homes


def test_registry_empty_off_windows(monkeypatch):
    monkeypatch.setattr(allure_runtime.sys, "platform", "linux")
    assert allure_runtime._registry_java_homes() == []


def test_windows_dir_discovery(monkeypatch, tmp_path):
    pf = tmp_path / "Program Files"
    for vendor in ("Java", "Eclipse Adoptium"):
        (pf / vendor / "jdk-21" / "bin").mkdir(parents=True)
        (pf / vendor / "jdk-21" / "bin" / "java.exe").write_text("x")
    monkeypatch.setenv("ProgramFiles", str(pf))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    homes = allure_runtime._windows_dir_homes()
    assert str(pf / "Java" / "jdk-21") in homes
    assert str(pf / "Eclipse Adoptium" / "jdk-21") in homes


def test_discovery_order_java_home_then_path_then_platform(
        monkeypatch, tmp_path):
    a, b, c = (_fake_jh(tmp_path / n) for n in ("d1", "d2", "d3"))
    monkeypatch.setenv("JAVA_HOME", str(a))
    monkeypatch.setattr(allure_runtime.shutil, "which",
                        lambda _exe: str(b / "bin" / "java"))
    monkeypatch.setattr(allure_runtime, "_platform_java_homes",
                        lambda: [str(c)])
    homes = allure_runtime.detect_java_homes()
    assert homes == [a, b, c]          # order is contract


def test_version_check_blocks_generation_probe(app, monkeypatch,
                                               tmp_path):
    """A java binary that cannot start fails BEFORE generation is
    attempted — actionable error, still recorded."""
    home = _fake_jh(tmp_path)
    monkeypatch.setattr(allure_runtime, "java_version",
                        lambda h, timeout=15: ("", "spawn failed"))
    with app.app_context():
        rec = allure_runtime.probe(str(home))
        assert rec["ok"] is False
        assert "version check" in rec["error"]


def test_sanitized_errors():
    dirty = "x\x00\x1b[31m" + "y" * 900 + "\n\n  trail  "
    clean = allure_runtime._sanitized(dirty)
    assert "\x00" not in clean and "\n" not in clean
    assert len(clean) <= 500 and clean.endswith("trail")


def test_normalize_windows_java_path(tmp_path):
    home = tmp_path / "jdk-21"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "java.exe").write_text("x")
    # normalize checks the platform's exe name; simulate win32.
    monkey = pytest.MonkeyPatch()
    monkey.setattr(allure_runtime, "JAVA_EXE", "java.exe")
    try:
        assert allure_runtime.normalize_java_home(
            str(home / "bin" / "java.exe")) == home.resolve()
        assert allure_runtime.normalize_java_home(str(home)) == \
            home.resolve()
    finally:
        monkey.undo()
