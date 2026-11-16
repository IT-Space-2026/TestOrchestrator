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
        html = Path(ex.report_dir) / ex.rl_path
        assert out["status"] == "ready", out
        assert out["cases"] == 4 and out["attached"] == 1
        assert out["unmapped"] == 1
        assert html.exists() and html.stat().st_size > 100_000
        body = html.read_text(encoding="utf-8", errors="replace")
        for needle in ("ok", "bad", "unmatched"):
            assert needle in body, needle
        # persisted state independent of allure_status/report_status
        assert ex.rl_status == "ready"
        parts = Path(ex.rl_path).parts
        assert parts[0] == "reporting-labs"
        assert parts[-2].startswith("gen-")
        assert parts[-1] == "index.html"
        assert ex.rl_version


def test_regenerate_idempotent(app, tmp_path):
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert rl.generate_for(ex)["status"] == "ready"
        first = (Path(ex.report_dir) / ex.rl_path).read_bytes()
        assert rl.generate_for(ex)["status"] == "ready"
        again = (Path(ex.report_dir) / ex.rl_path).read_bytes()
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
        good = (Path(ex.report_dir) / ex.rl_path).read_bytes()
        monkeypatch.setattr(rl, "_emit",
                            lambda *_a, **_k: 1 / 0)
        out = rl.generate_for(ex)
        assert out["status"] == "failed"
        assert ex.rl_status == "failed"
        # previous report intact and still referenced
        parts = Path(ex.rl_path).parts
        assert parts[0] == "reporting-labs"
        assert parts[-2].startswith("gen-")
        assert parts[-1] == "index.html"
        assert (Path(ex.report_dir) / ex.rl_path).read_bytes() == good
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
        assert (root / ex.rl_path).exists()


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
        # filesystem-legal on Windows (no <>"\) yet still hostile markup
        shots=("x' onerror='alert(1).png",),
        results=[(("C.tc", evil), "failed")])
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        out = rl.generate_for(ex)
        body = (Path(ex.report_dir) / ex.rl_path
                ).read_text(encoding="utf-8",
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
        html = Path(ex.report_dir) / ex.rl_path
        assert html.exists() and html.stat().st_size > 100_000
        body = html.read_text(encoding="utf-8", errors="replace")
        assert "pass" in body
        assert ex.rl_status in ("ready", "failed")
        assert not list(Path(ex.report_dir)
                        .glob("reporting-labs.tmp.*"))


def test_reader_never_sees_missing_or_partial_report(app, tmp_path):
    """A reader racing regeneration resolves the `current` pointer per
    request (as the portal resolves rl_path): every read lands on ONE
    complete generation — old or new, never missing/partial/mixed."""
    import threading
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert rl.generate_for(ex)["status"] == "ready"
        root = Path(ex.report_dir)
        problems, stop = [], threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    gen = (root / "reporting-labs" / "current"
                           ).read_text().strip()
                    body = (root / "reporting-labs" / gen /
                            "index.html").read_text(
                                encoding="utf-8", errors="replace")
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


def test_generations_are_distinguishable_and_not_mixed(app, tmp_path,
                                                       monkeypatch):
    """Two generations carry different marker content; the published
    bundle referenced by rl_path is always ONE generation — never a
    blend of old and new files."""
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    orig = rl._emit
    counter = {"n": 0}

    def marked(ex, out):
        res = orig(ex, out)
        counter["n"] += 1
        html = Path(out) / "index.html"
        html.write_text(html.read_text(encoding="utf-8") +
                        f"<!-- GENMARK-{counter['n']} -->",
                        encoding="utf-8")
        (Path(out) / "report.json").write_text(
            f'{{"genmark": {counter["n"]}}}', encoding="utf-8")
        return res
    monkeypatch.setattr(rl, "_emit", marked)

    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        out1 = rl.generate_for(ex)
        assert out1["status"] == "ready", out1
        root = Path(ex.report_dir)
        g1 = Path(ex.rl_path).parent.name
        body1 = (root / ex.rl_path).read_text(encoding="utf-8")
        assert "GENMARK-1" in body1 and g1.startswith("gen-")
        out2 = rl.generate_for(ex)
        assert out2["status"] == "ready", out2
        g2 = Path(ex.rl_path).parent.name
        assert g2 != g1
        body2 = (root / ex.rl_path).read_text(encoding="utf-8")
        assert "GENMARK-2" in body2
        # the served generation is internally consistent: report.json
        # in the SAME dir belongs to the same generation
        meta2 = (root / ex.rl_path).parent.joinpath(
            "report.json").read_text()
        assert '"genmark": 2' in meta2
        # current pointer agrees with rl_path
        assert (root / "reporting-labs" / "current"
                ).read_text().strip() == g2


def test_publish_failure_midway_keeps_last_generation(app, tmp_path,
                                                      monkeypatch):
    """Failure after the gen dir lands but before the pointer flip:
    current/rl_path keep referencing the last complete generation and
    the orphan gen dir is removed — no partial publication."""
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert rl.generate_for(ex)["status"] == "ready"
        root = Path(ex.report_dir)
        good_path, good_bytes = ex.rl_path, \
            (root / ex.rl_path).read_bytes()
        cur = (root / "reporting-labs" / "current").read_text().strip()

        def boom(*a, **k):
            raise OSError("simulated pointer-swap failure")
        monkeypatch.setattr(rl.os, "replace", boom)
        out = rl.generate_for(ex)
        assert out["status"] == "failed" and "publish" in out["error"]
        # nothing visible changed
        assert ex.rl_path == good_path
        assert (root / ex.rl_path).read_bytes() == good_bytes
        assert (root / "reporting-labs" / "current"
                ).read_text().strip() == cur
        # no orphan gen dirs beyond current+previous retention policy
        gens = [d.name for d in
                (root / "reporting-labs").glob("gen-*")]
        assert cur in gens and len(gens) <= 2


def test_historical_allure_serving_needs_no_generator(client, login_as,
                                                    app, tmp_path,
                                                    monkeypatch):
    """Acceptance check: a RETAINED Allure report (index.html + assets)
    stays viewable/downloadable with the generator runtime absent —
    the serving path is file I/O, not a Node dependency."""
    from testorchestrator import allure_runtime
    ex_id = _mk(app, tmp_path)
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        adir = Path(ex.report_dir) / "allure"
        adir.mkdir(parents=True)
        (adir / "index.html").write_text("<html>allure app</html>")
        (adir / "app.js").write_text("console.log(1)")
        ex.allure_status = "ready"
        ex.allure_path = "allure/index.html"
        db.session.commit()
        # simulate an installed package with Node/Allure REMOVED
        monkeypatch.setattr(allure_runtime, "_allure_cmd",
                            lambda: None)
        monkeypatch.setattr(allure_runtime, "available",
                            lambda: False)
    login_as("admin")
    assert client.get(f"/executions/{ex_id}/allure").status_code == 302
    r = client.get(f"/executions/{ex_id}/allure/")
    assert r.status_code == 200 and b"allure app" in r.data
    assert client.get(
        f"/executions/{ex_id}/allure/app.js").status_code == 200
    z = client.get(f"/executions/{ex_id}/allure.zip")
    assert z.status_code == 200


def test_previous_generation_files_survive_regen(app, tmp_path):
    """A page opened from generation N keeps loading report.json and
    attachments from gen N while a NEWER generation publishes — the
    prune keeps current+previous. One regen stale is the boundary."""
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        rl.generate_for(ex)
        root = Path(ex.report_dir)
        g1_dir = Path(ex.rl_path).parent
        g1_json = root / g1_dir / "report.json"
        rl.generate_for(ex)
        # in-flight reader of gen1: every file still resolves
        assert (root / g1_dir / "index.html").exists()
        assert g1_json.exists()
        rl.generate_for(ex)
        # TWO regens stale -> oldest pruned; only current+prev remain
        assert not (root / g1_dir / "index.html").exists()
        gens = sorted((root / "reporting-labs").glob("gen-*"))
        assert len(gens) == 2


def test_db_crash_after_publish_leaves_servable_state(app, tmp_path,
                                                      monkeypatch):
    """Interruption between filesystem publish and the DB update: the
    previous rl_path still serves a complete report, and the next
    generation run restores full consistency."""
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert rl.generate_for(ex)["status"] == "ready"
        root = Path(ex.report_dir)
        old_path = ex.rl_path
        real_commit = db.session.commit
        calls = {"n": 0}

        def flaky_commit():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated crash after pointer flip")
            return real_commit()
        monkeypatch.setattr(db.session, "commit", flaky_commit)
        import pytest as _pt
        with _pt.raises(RuntimeError):
            rl.generate_for(ex)
        # DB state is stale (old path) — still servable, complete
        db.session.rollback()
        ex = db.session.get(Execution, ex_id)
        assert (root / old_path).exists()
        # next generation succeeds and re-syncs everything
        assert rl.generate_for(ex)["status"] == "ready"
        assert ex.rl_status == "ready"
        assert (root / ex.rl_path).exists()


def test_windows_locking_branch(app, tmp_path, monkeypatch):
    """The msvcrt (Windows) lock path is exercised — fcntl absent."""
    import sys
    import types
    fake_msvcrt = types.ModuleType("msvcrt")
    fake_msvcrt.LK_LOCK = 1
    fake_msvcrt.locking = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "fcntl", None)   # ImportError
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    ex_id = _mk(app, tmp_path, shots=("pass.png",))
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert rl.generate_for(ex)["status"] == "ready"
        assert (Path(ex.report_dir) / ex.rl_path).exists()


def test_reporting_page_diagnostics_accurate(client, login_as, app):
    """Admin reporting page describes the CURRENT backends — bundled
    Node.js for Allure (no external Java requirement) + the optional
    reporting-labs backend."""
    login_as("admin")
    body = client.get("/system/reporting").data.decode()
    assert "external Java" not in body
    assert "Java runtime" not in body
    assert "bundled Node.js" in body
    assert "reporting-labs" in body
    assert "report_backend" in body or "Preferred enhanced backend" \
        in body


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
