"""Repository-result import (2.0.1): detection, parsing, dedup,
workflow, permissions — real Katalon-layout fixtures via dulwich."""
from pathlib import Path


from testorchestrator import importer
from testorchestrator.execution import (Execution, ExecutionState,
                                        GitBinding, ImportSession,
                                        Outcome, Project, TestResult)
from testorchestrator.extensions import db
from types import SimpleNamespace

from tests.git_helpers import bare_repo, clone, seed_repo

JUNIT = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="{suite}" id="{suite}" hostname="win-01"
             timestamp="{ts}" tests="{n}" failures="{f}" errors="0"
             skipped="0" time="{t}">
    {cases}
  </testsuite>
</testsuites>
"""
CASE_PASS = ('<testcase classname="{cls}" name="{nm}" time="1.5"/>')
CASE_FAIL = ('<testcase classname="{cls}" name="{nm}" time="2.0">'
             '<failure type="AssertionError">boom</failure></testcase>')
PROPS = "os=Windows 10 64bit\nbrowserName=Chrome\n"
UUID = "5f0b6a3e-3c9e-4d9a-9f62-0a1b2c3d4e5f"


def kre_run(suite="TS_Login", ts="2026-11-10T09:30:00", cases=None,
            uuid=UUID):
    """Files for one Katalon report dir."""
    if cases is None:
        cases = [CASE_PASS.format(cls="TS_Login", nm="login"),
                 CASE_FAIL.format(cls="TS_Login", nm="logout")]
    return {
        "JUnit_Report.xml": JUNIT.format(
            suite=suite, ts=ts, n=len(cases), f=sum("<failure" in c
                                                  for c in cases),
            t=3.5, cases="\n    ".join(cases)),
        "execution.uuid": uuid,
        "execution.properties": PROPS,
        "execution0.log": "INFO start\nINFO end\n",
        "index.html": "<html>report</html>",
    }


def make_binding(tmp_path, app, files, name="proj-a"):
    """Seed a repo, clone to a worktree, wire a GitBinding + Project."""
    origin = bare_repo(tmp_path / f"{name}-origin")
    seed_repo(str(origin), tmp_path / f"{name}-wc", files)
    with app.app_context():
        project = Project(name=name, description="")
        db.session.add(project)
        db.session.flush()
        wc = clone(str(origin), tmp_path / f"{name}-binding")
        binding = GitBinding(
            project_id=project.id, name=name, url=str(origin),
            ref="master", subdirectory="",
            worktree_path=str(wc))
        db.session.add(binding)
        db.session.commit()
        return project.id, binding.id


def preview(app, project_id, binding_id, result_dir="Reports"):
    with app.app_context():
        project = db.session.get(Project, project_id)
        binding = db.session.get(GitBinding, binding_id)
        return importer.create_preview(
            project, binding, "HEAD", result_dir, "test-manager").id


def sess(app, session_id):
    with app.app_context():
        s = db.session.get(ImportSession, session_id)
        return SimpleNamespace(
            id=s.id, resolved_sha=s.resolved_sha, status=s.status,
            candidates=sorted(c.id for c in s.candidates))


def cand(app, session_id, i=0, run_dir_suffix=None):
    with app.app_context():
        cs = db.session.get(ImportSession, session_id).candidates
        c = ([c for c in cs if run_dir_suffix in c.run_dir]
             if run_dir_suffix else cs)[i]
        return SimpleNamespace(
            id=c.id, decision=c.decision, run_dir=c.run_dir,
            match_execution_id=c.match_execution_id,
            run_uuid=c.run_uuid, case_count=c.case_count,
            suite_name=c.suite_name, started_at=c.started_at)


def confirm(app, session_id, select="all", force=None):
    with app.app_context():
        s = db.session.get(ImportSession, session_id)
        ids = [c.id for c in s.candidates] if select == "all" else select
        return importer.confirm_import(s, ids, force or [], "test-manager")


def test_detect_and_parse_kre_run(tmp_path, app):
    run = kre_run()
    files = {f"Reports/20261110_093000/{k}": v for k, v in run.items()}
    with app.app_context():
        pid, bid = make_binding(tmp_path, app, files)
        c = cand(app, preview(app, pid, bid))
        assert c.decision == "new"
        assert c.case_count == 2
        assert c.run_uuid == UUID
        assert c.suite_name == "TS_Login"
        assert c.started_at is not None


def test_import_creates_execution_results_artifacts(tmp_path, app):
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run().items()}
    with app.app_context():
        pid, bid = make_binding(tmp_path, app, files)
        sid = preview(app, pid, bid)
        assert Execution.query.filter_by(project_id=pid).count() == 0
        out = confirm(app, sid)
        assert len(out["imported"]) == 1
        ex = db.session.get(Execution, out["imported"][0][1])
        assert ex.origin == "imported"
        assert ex.framework == "katalon"
        assert ex.state == ExecutionState.COMPLETED
        assert ex.outcome == Outcome.FAILED
        assert ex.imported_by == "test-manager"
        assert ex.imported_at is not None
        assert ex.started_at is not None
        assert ex.source_commit == sess(app, sid).resolved_sha
        assert ex.run_uuid == UUID
        assert ex.content_hash
        assert len(ex.results) == 2
        assert {r.status for r in ex.results} == {"passed", "failed"}
        kinds = {a.kind for a in ex.artifacts}
        assert "junit" in kinds and "html" in kinds
        # evidence is durable, outside the staging checkout
        assert not Path(ex.report_dir).is_relative_to(
            Path(app.instance_path) / "import-staging")
        assert (Path(ex.report_dir) / "Reports" / "20261110_093000"
                / "JUnit_Report.xml").is_file()


def test_repeat_import_is_duplicate(tmp_path, app):
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run().items()}
    pid, bid = make_binding(tmp_path, app, files)
    confirm(app, preview(app, pid, bid))
    s2 = preview(app, pid, bid)
    assert cand(app, s2).decision == "duplicate"
    out = confirm(app, s2)
    assert len(out["imported"]) == 0 and len(out["skipped"]) == 1
    with app.app_context():
        assert Execution.query.filter_by(project_id=pid).count() == 1


def test_copied_renamed_files_are_duplicate(tmp_path, app):
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run().items()}
    files["Backup/JUnit_Report.xml"] = files[
        "Reports/20261110_093000/JUnit_Report.xml"]
    files["Backup/execution.uuid"] = UUID
    files["Backup/extra.txt"] = "copied dir, new evidence file"
    pid, bid = make_binding(tmp_path, app, files, "copy")
    s = preview(app, pid, bid, result_dir=".")
    assert {cand(app, s, i).decision
            for i in range(len(sess(app, s).candidates))} == {"new"}
    confirm(app, s)
    with app.app_context():
        assert Execution.query.filter_by(project_id=pid).count() == 1


def test_distinct_rerun_same_outcomes_kept(tmp_path, app):
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run(uuid=UUID).items()}
    files.update({f"Reports/20261110_143000/{k}": v
                  for k, v in kre_run(
                      ts="2026-11-10T14:30:00",
                      uuid="a1a1a1a1-b2b2-c3c3-d4d4-e5e5e5e5e5e5").items()})
    pid, bid = make_binding(tmp_path, app, files, "rerun")
    s = preview(app, pid, bid)
    assert all(cand(app, s, i).decision == "new"
               for i in range(len(sess(app, s).candidates)))
    confirm(app, s)
    with app.app_context():
        assert Execution.query.filter_by(project_id=pid).count() == 2


def test_ambiguous_same_ts_different_cases(tmp_path, app):
    run_a = kre_run()
    run_b = kre_run(cases=[CASE_PASS.format(cls="TS_Login", nm="login"),
                           CASE_PASS.format(cls="TS_Login", nm="extra")])
    files = {f"R/20261110_093000/{k}": v for k, v in run_a.items()}
    # same uuid? no — same suite+timestamp but different case multiset
    run_b.pop("execution.uuid", None)
    files.update({f"R/20261110_093001/{k}": v for k, v in run_b.items()})
    pid, bid = make_binding(tmp_path, app, files, "ambig")
    s = preview(app, pid, bid, result_dir="R")
    confirm(app, s, select=[cand(app, s).id])
    s2 = preview(app, pid, bid, result_dir="R")
    cand2 = cand(app, s2, run_dir_suffix="093001")
    assert cand2.decision == "ambiguous"
    out = confirm(app, s2)   # not forced → not imported
    assert cand2.id in out["ambiguous"] or out["skipped"] or not out[
        "imported"]
    with app.app_context():
        assert Execution.query.filter_by(project_id=pid).count() == 1
    # explicit force-new imports it as a separate execution
    s3 = preview(app, pid, bid, result_dir="R")
    force_id = cand(app, s3, run_dir_suffix="093001").id
    out3 = confirm(app, s3, force=[force_id])
    assert len(out3["imported"]) == 1
    with app.app_context():
        assert Execution.query.filter_by(project_id=pid).count() == 2


def test_import_matches_existing_live_execution(tmp_path, app):
    run = kre_run()
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in run.items()}
    pid, bid = make_binding(tmp_path, app, files, "live-match")
    # simulate the live-executed twin: same uuid + content hash stamped
    from testorchestrator.results import parse_junit
    with app.app_context():
        wc = Path(db.session.get(GitBinding, bid).worktree_path)
        parsed = parse_junit(wc / "Reports/20261110_093000"
                             "/JUnit_Report.xml")
        suite_keys = [(s["id"] or s["name"], s["timestamp"])
                      for s in parsed.suites]
        ch = importer.content_fingerprint(
            suite_keys, [s["timestamp"] for s in parsed.suites],
            parsed.cases, parsed.producer_tests)
        live = Execution(project_id=pid, origin="live",
                         framework="katalon",
                         state=ExecutionState.COMPLETED,
                         outcome=Outcome.FAILED, run_uuid=UUID,
                         content_hash=ch, created_by="tester",
                         report_dir=str(
                             wc / "Reports/20261110_093000"))
        db.session.add(live)
        db.session.commit()
        live_id = live.id
    c = cand(app, preview(app, pid, bid))
    assert c.decision in ("duplicate", "supplement")
    assert c.match_execution_id == live_id
    confirm(app, preview(app, pid, bid))
    with app.app_context():
        assert Execution.query.filter_by(project_id=pid).count() == 1


def test_supplement_fills_missing_finished_at(tmp_path, app):
    run = kre_run()
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in run.items()}
    pid, bid = make_binding(tmp_path, app, files, "suppl")
    with app.app_context():
        live = Execution(project_id=pid, origin="live",
                         framework="katalon",
                         state=ExecutionState.COMPLETED,
                         outcome=Outcome.FAILED, run_uuid=UUID,
                         started_at=None, finished_at=None,
                         created_by="tester",
                         report_dir="/nonexistent")
        db.session.add(live)
        db.session.commit()
        live_id = live.id
    c = cand(app, preview(app, pid, bid))
    assert c.decision == "supplement"
    confirm(app, preview(app, pid, bid))
    with app.app_context():
        ex = db.session.get(Execution, live_id)
        assert ex.finished_at is not None
        assert ex.origin == "live"          # provenance preserved


def test_unsupported_and_malformed_dirs(tmp_path, app):
    files = {
        "Reports/20261110_093000/result.html": "<html>only html</html>",
        "Reports/20261110_093000/summary.csv": "a,b\n",
        "Reports/bad/bad.xml": "<testsuite><testcase",   # malformed
        "Reports/20261110_093000/JUnit_Report.xml":
            JUNIT.format(suite="TS_X", ts="2026-11-10T09:30:00", n=1,
                         f=0, t=1.0,
                         cases=CASE_PASS.format(cls="x", nm="y")),
    }
    pid, bid = make_binding(tmp_path, app, files, "mixed")
    s = preview(app, pid, bid)
    with app.app_context():
        run_dirs = [c.run_dir for c in
                    db.session.get(ImportSession, s).candidates]
        assert any("093000" in d for d in run_dirs)


def test_malformed_junit_not_detected_as_run(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    (d / "broken.xml").write_text("<testsuite><testcase")   # no close
    run_dirs, _ = importer.detect_run_dirs(d)
    assert run_dirs == []


def test_preview_creates_no_executions(tmp_path, app):
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run().items()}
    pid, bid = make_binding(tmp_path, app, files)
    preview(app, pid, bid)
    with app.app_context():
        assert Execution.query.count() == 0


def test_project_scoped_dedup(tmp_path, app):
    run = kre_run()
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in run.items()}
    pid1, bid1 = make_binding(tmp_path, app, files, "p1")
    pid2, bid2 = make_binding(tmp_path, app, files, "p2")
    confirm(app, preview(app, pid1, bid1))
    # same content in another project → new run, not a cross-project
    # duplicate (matches never cross project boundaries)
    s2 = preview(app, pid2, bid2)
    assert cand(app, s2).decision == "new"
    confirm(app, s2)
    with app.app_context():
        assert Execution.query.count() == 2


def test_duplicate_retry_attempts_no_dup_rows(tmp_path, app):
    """Repeated identical imports must not add TestResult/attempt rows."""
    run = kre_run()
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in run.items()}
    pid, bid = make_binding(tmp_path, app, files, "att")
    confirm(app, preview(app, pid, bid))
    confirm(app, preview(app, pid, bid))
    confirm(app, preview(app, pid, bid))
    with app.app_context():
        assert TestResult.query.count() == 2
        ex = Execution.query.first()
        assert sum(1 for a in ex.artifacts if a.kind == "junit") == 1


def test_changed_between_preview_and_confirm(tmp_path, app):
    """If the run gets imported by someone else between preview and
    confirm, confirm must skip it (transactional recheck)."""
    run = kre_run()
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in run.items()}
    pid, bid = make_binding(tmp_path, app, files, "race")
    s1 = preview(app, pid, bid)
    s2 = preview(app, pid, bid)     # second preview, still "new"
    confirm(app, s2)                # someone confirms first
    out = confirm(app, s1)          # our stale preview commits
    assert len(out["imported"]) == 0
    with app.app_context():
        assert Execution.query.filter_by(project_id=pid).count() == 1


# ---------- route / permission coverage ----------

def _post(client, url, data=None):
    return client.post(url, data=data or {}, follow_redirects=True)


def test_import_routes_require_auth_and_role(client, tmp_path, app):
    # unauthenticated → redirect to login
    r = client.get("/projects/x/imports/new")
    assert r.status_code in (302, 401)
    from tests.conftest import login
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run().items()}
    pid, bid = make_binding(tmp_path, app, files, "perm")
    # PO cannot preview
    login(client, "po")
    r = client.get(f"/projects/{pid}/imports/new")
    assert r.status_code in (302, 403)
    # Test Manager sees the form and runs the workflow
    login(client, "test_manager")
    r = client.get(f"/projects/{pid}/imports/new")
    assert r.status_code == 200
    r = _post(client, f"/projects/{pid}/imports/preview",
              {"binding_id": bid, "revision": "HEAD",
               "result_dir": "Reports"})
    assert r.status_code == 200
    assert b"Import preview" in r.data


def test_imported_execution_visible_in_history(client, tmp_path, app):
    from tests.conftest import login
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run().items()}
    pid, bid = make_binding(tmp_path, app, files, "hist")
    confirm(app, preview(app, pid, bid))
    login(client, "po")
    r = client.get(f"/executions?project_id={pid}&origin=imported")
    assert r.status_code == 200 and b"imported" in r.data
    with app.app_context():
        ex = Execution.query.filter_by(project_id=pid).first()
        r = client.get(f"/executions/{ex.id}")
        assert r.status_code == 200
        assert b"imported" in r.data
        assert b"Source commit" in r.data
        # live-only actions are hidden for imported executions
        assert b"Retry (new attempt)" not in r.data
        assert b"Cancel execution" not in r.data


def test_import_persists_across_reopen(tmp_path, app):
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run().items()}
    pid, bid = make_binding(tmp_path, app, files, "persist")
    confirm(app, preview(app, pid, bid))
    db_path = app.config["SQLALCHEMY_DATABASE_URI"].split("///", 1)[1]
    with app.app_context():
        db.session.remove()
        db.get_engine().dispose()
    from tests.conftest import make_app
    from testorchestrator.extensions import db as _db2
    app2 = make_app(Path(db_path).parent)
    with app2.app_context():
        _db2.create_all()
        ex = Execution.query.filter_by(project_id=pid).first()
        assert ex is not None and ex.origin == "imported"
        assert len(ex.results) == 2


def test_reporting_labs_generates_for_imported(tmp_path, app):
    """Imported runs feed the same reportingLabs path as live runs —
    canonical results + durable evidence are all the adapter needs."""
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run().items()}
    pid, bid = make_binding(tmp_path, app, files, "rlabs")
    out = confirm(app, preview(app, pid, bid))
    with app.app_context():
        ex = db.session.get(Execution, out["imported"][0][1])
        from testorchestrator.adapters import reporting_labs
        res = reporting_labs.generate_for(ex)
        assert res["status"] != "failed", res
        assert res.get("cases") == 2
        assert ex.rl_status == "ready"


def test_collection_run_shared_uuid_one_execution(tmp_path, app):
    """Katalon collection runs emit one report dir per suite sharing a
    single execution.uuid — they must merge into ONE execution."""
    coll_uuid = "cccccccc-1111-2222-3333-444444444444"
    files = {}
    for suite in ("Wiki", "IT-Space"):
        run = kre_run(suite=f"TS_{suite}", ts="2026-11-16T16:31:15",
                      uuid=coll_uuid)
        files.update({f"Reports/{suite}/20261116_163115/{k}": v
                      for k, v in run.items()})
    pid, bid = make_binding(tmp_path, app, files, "coll")
    sid = preview(app, pid, bid)
    with app.app_context():
        cands = db.session.get(ImportSession, sid).candidates
        assert len(cands) == 1, "shared-uuid dirs must merge"
        assert cands[0].case_count == 4
    out = confirm(app, sid)
    assert len(out["imported"]) == 1
    with app.app_context():
        ex = Execution.query.filter_by(project_id=pid).one()
        assert len(ex.results) == 4
        # both suite dirs' evidence is preserved under distinct paths
        rels = {a.rel_path for a in ex.artifacts}
        assert any("Wiki" in r for r in rels)
        assert any("IT-Space" in r for r in rels)


# ---------- identity edge cases (release-gate verification) ----------

def test_distinct_uuids_identical_content_stay_separate(tmp_path, app):
    """Two different producer uuids with byte-identical results are two
    executions — identical outcomes do not merge runs."""
    shared = dict(suite="TS_Login", ts="2026-11-10T09:30:00")
    files = {}
    for d, u in (("20261110_093000", UUID),
                 ("20261110_093001", "bbbbbbbb-1111-2222-3333-444444444444")):
        run = kre_run(uuid=u)
        files.update({f"Reports/{d}/{k}": v for k, v in run.items()})
    pid, bid = make_binding(tmp_path, app, files, "dupres")
    out = confirm(app, preview(app, pid, bid))
    assert len(out["imported"]) == 2
    with app.app_context():
        assert Execution.query.filter_by(project_id=pid).count() == 2


def test_identical_content_no_uuid_no_ts_is_ambiguous(tmp_path, app):
    """Identical content with no uuid and no timestamps cannot prove a
    single run — ambiguous, and force-new must actually insert."""
    junit = JUNIT.format(suite="TS_X", ts="", n=1, f=0, t=1.0,
                         cases=CASE_PASS.format(cls="x", nm="y"))
    files = {}
    for d in ("a", "b"):
        files[f"Reports/{d}/JUnit_Report.xml"] = junit
        files[f"Reports/{d}/execution0.log"] = "log"
    pid, bid = make_binding(tmp_path, app, files, "nots")
    s1 = preview(app, pid, bid)
    confirm(app, s1, select=[cand(app, s1, 0).id])
    s2 = preview(app, pid, bid)
    c2 = cand(app, s2, run_dir_suffix="/b")
    assert c2.decision == "ambiguous", \
        "identical content without uuid/timestamps must be ambiguous"
    # force-new succeeds — the content fingerprint is evidence, not a
    # uniqueness constraint
    out = confirm(app, s2, force=[c2.id])
    assert len(out["imported"]) == 1
    with app.app_context():
        assert Execution.query.filter_by(project_id=pid).count() == 2


def test_same_uuid_conflicting_content_is_ambiguous(tmp_path, app):
    """Same execution.uuid with conflicting results → review; the
    producer id cannot identify two different executions."""
    run_b = kre_run(cases=[CASE_PASS.format(cls="TS_Login", nm="login"),
                           CASE_PASS.format(cls="TS_Login", nm="logout")])
    files = {f"Reports/a/{k}": v for k, v in kre_run().items()}
    files.update({f"Reports/b/{k}": v for k, v in run_b.items()})
    pid, bid = make_binding(tmp_path, app, files, "uconflict")
    s = preview(app, pid, bid, result_dir="Reports/a")
    confirm(app, s)
    s2 = preview(app, pid, bid, result_dir="Reports/b")
    c = cand(app, s2)
    assert c.decision == "ambiguous"
    with app.app_context():
        assert Execution.query.filter_by(project_id=pid).count() == 1


def test_same_uuid_late_suite_supplements(tmp_path, app):
    """A collection's suite arriving in a later import (same uuid,
    additional cases) supplements the existing execution."""
    coll_uuid = "dddddddd-1111-2222-3333-444444444444"
    files = {}
    for suite, d, cases in (
            ("Wiki", "w", None),
            ("IT-Space", "i", [CASE_PASS.format(cls="TS_ITS", nm="open"),
                               CASE_FAIL.format(cls="TS_ITS", nm="edit")])):
        run = kre_run(suite=f"TS_{suite}",
                      ts="2026-11-16T16:31:15", uuid=coll_uuid,
                      cases=cases)
        files.update({f"Reports/{d}/{k}": v for k, v in run.items()})
    pid, bid = make_binding(tmp_path, app, files, "late")
    # first import: only the Wiki dir is selected
    s = preview(app, pid, bid, result_dir="Reports/w")
    out = confirm(app, s)
    ex_id = out["imported"][0][1]
    with app.app_context():
        assert len(db.session.get(Execution, ex_id).results) == 2
    # second import picks up the IT-Space dir (same uuid, more cases)
    s2 = preview(app, pid, bid, result_dir="Reports/i")
    c2 = cand(app, s2)
    assert c2.decision == "supplement"
    out2 = confirm(app, s2, select=[c2.id])
    assert len(out2["supplemented"]) == 1
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert len(ex.results) == 4
        assert Execution.query.filter_by(project_id=pid).count() == 1
        # late suite's key unions into the persisted suite_keys — the
        # no-uuid fast path must see both suites, not only the first
        names = {k[0] for k in ex.suite_keys()}
        assert names == {"TS_Wiki", "TS_IT-Space"}


def test_generic_junit_framework_unknown(tmp_path, app):
    """A bare JUnit file without KRE markers is format=junit but
    framework=unknown — the producer is not established."""
    files = {"out/results.xml": JUNIT.format(
        suite="SomeSuite", ts="2026-11-10T09:30:00", n=1, f=0, t=1.0,
        cases=CASE_PASS.format(cls="pkg.Cls", nm="m1"))}
    pid, bid = make_binding(tmp_path, app, files, "genjunit")
    out = confirm(app, preview(app, pid, bid, result_dir="out"))
    with app.app_context():
        ex = db.session.get(Execution, out["imported"][0][1])
        assert ex.framework == "unknown"
        assert ex.result_format == "junit"


def test_katalon_markers_give_katalon(tmp_path, app):
    files = {f"Reports/20261110_093000/{k}": v
             for k, v in kre_run().items()}
    pid, bid = make_binding(tmp_path, app, files, "kmark")
    out = confirm(app, preview(app, pid, bid))
    with app.app_context():
        ex = db.session.get(Execution, out["imported"][0][1])
        assert ex.framework == "katalon"
        assert ex.result_format == "junit"
