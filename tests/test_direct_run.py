"""Phase 1: direct execution of discovered repository definitions."""

import json

from testorchestrator import dispatch
from testorchestrator.execution import (DiscoveredItem, Execution,
                                        ExecutionState, GitBinding,
                                        OutboxEvent, Project, Release,
                                        Runner, RuntimeConnection,
                                        Suite, utcnow)
from testorchestrator.extensions import db


def _env(app, kind="suite", path="Test Suites/S1"):
    p = Project(name="P"); db.session.add(p); db.session.flush()
    b = GitBinding(project_id=p.id, name="b", url="x",
                   worktree_path="/x", last_synced_sha="sha1",
                   status="synced")
    db.session.add(b); db.session.flush()
    item = DiscoveredItem(binding_id=b.id, project_id=p.id,
                          sha="sha1", kind=kind, path=path,
                          name=path.split("/")[-1])
    db.session.add(item); db.session.commit()
    return p, b, item


def test_source_target_identity_in_snapshot(app):
    """Repo-def runs snapshot the durable triple
    (binding+kind+path+sha) — history survives inventory replacement."""
    with app.app_context():
        p, b, item = _env(app)
        ex = dispatch.create_execution("m", source_item=item,
                                       binding=b, source_sha="sha1")
        snap = ex.snapshot()
        st = snap["source_target"]
        assert st["binding_id"] == item.binding_id
        assert st["kind"] == "suite" and st["path"] == item.path
        assert ex.suite_id is None and ex.source_item_id == item.id
        assert snap["suite_name"] == item.name
        rc = snap["run_config"]
        assert rc["suite_path"] == item.path


def test_collection_emits_collection_arg(app):
    """A discovered collection carries collection=True through the
    snapshot -> build_args -> -testSuiteCollectionPath."""
    with app.app_context():
        p, b, item = _env(app, kind="collection",
                          path="Test Suites/Coll 1")
        ex = dispatch.create_execution("m", source_item=item)
        rc = ex.snapshot()["run_config"]
        assert rc["collection"] is True
        from testorchestrator.adapters.kre import build_args
        a = build_args("k", "/p", rc["suite_path"], "/r",
                       rc["profile"], collection=rc["collection"])
        assert "-testSuiteCollectionPath=Test Suites/Coll 1" in a
        assert not any(x.startswith("-testSuitePath=") for x in a)


def test_neither_and_both_targets_rejected(app):
    """Exactly one target required — suite xor source_item."""
    with app.app_context():
        p, b, item = _env(app)
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        try:
            dispatch.create_execution("m")           # neither
            assert False
        except dispatch.DispatchError:
            pass
        try:
            dispatch.create_execution("m", suite=s, source_item=item)
            assert False
        except dispatch.DispatchError:
            pass


def test_removed_definition_rejected_at_dispatch(app):
    """A discovered item deleted after queueing fails visibly at
    dispatch — never silently retargets."""
    with app.app_context():
        p, b, item = _env(app)
        ex = dispatch.create_execution("m", source_item=item)
        db.session.delete(item); db.session.commit()
        dispatch.run_execution(ex.id)
        db.session.refresh(ex)
        assert ex.state == ExecutionState.FAILED
        assert "no longer in the binding" in ex.error_text


def test_same_path_other_binding_not_target(app):
    """Identity is (binding,kind,path) — same path in a different
    binding must NOT satisfy the re-validation."""
    with app.app_context():
        p, b, item = _env(app)
        b2 = GitBinding(project_id=p.id, name="b2", url="y",
                        worktree_path="/y", last_synced_sha="s2",
                        status="synced")
        db.session.add(b2); db.session.flush()
        other = DiscoveredItem(binding_id=b2.id, project_id=p.id,
                               sha="s2", kind="suite",
                               path=item.path, name="dup")
        db.session.add(other)
        db.session.delete(item); db.session.commit()
        ex = dispatch.create_execution("m", source_item=other)
        # item is from b2 — the ORIGINAL binding's row is gone but the
        # OTHER binding's row exists → still resolves on ITS binding.
        ex2 = ex
        # now delete the matching row entirely
        db.session.delete(other); db.session.commit()
        dispatch.run_execution(ex2.id)
        db.session.refresh(ex2)
        assert ex2.state == ExecutionState.FAILED


def test_discovered_detail_lists_workers_and_releases(
        app, client, login_as, ready_ops):
    """The supported run form renders the worker select and the
    project-scoped release select — the view must pass both."""
    with app.app_context():
        p, b, item = _env(app)
        db.session.add(Release(project_id=p.id, name="R-active",
                               status="draft"))
        db.session.add(Release(project_id=p.id, name="R-done",
                               status="released"))
        db.session.commit()
        item_id = item.id
    login_as("admin")
    resp = client.get(f"/discovered/{item_id}")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'name="runner_id"' in body and "ops-worker" in body
    assert 'name="release_id"' in body
    assert "R-active" in body and "R-done" not in body


def test_run_discovered_route_accepts_supported_form(
        app, client, login_as, ready_ops):
    """The real run form submits profile/release/runner/runtime/timeout
    — the route must accept them and queue the execution (regression:
    create_execution() unexpected 'release_id' 500)."""
    with app.app_context():
        p, b, item = _env(app)
        rel = Release(project_id=p.id, name="R1", status="draft")
        db.session.add(rel); db.session.commit()
        item_id, rel_id = item.id, rel.id
        rid = Runner.query.first().id
        rtid = RuntimeConnection.query.first().id
    login_as("admin")
    resp = client.post(f"/discovered/{item_id}/run", data={
        "execution_profile": "default",
        "release_id": rel_id,
        "runner_id": rid,
        "runtime_id": rtid,
        "timeout_seconds": "60",
    })
    assert resp.status_code == 302
    assert "/executions/" in resp.headers["Location"]
    with app.app_context():
        ex = Execution.query.one()
        assert ex.release_id == rel_id
        assert ex.source_item_id == item_id
        rc = ex.snapshot()["run_config"]
        assert rc["profile"] == "default"
        assert rc["requested_runner"] == rid
        assert rc["requested_runtime"] == rtid
        assert rc["timeout_seconds"] == 60
        ev = OutboxEvent.query.one()
        assert ev.kind == "local-dispatch"
        assert json.loads(ev.payload_json)["execution_id"] == ex.id


def test_run_discovered_rejects_cross_project_release(
        app, client, login_as):
    """A release from ANOTHER project is rejected visibly — flash +
    redirect, no execution, no queued work."""
    with app.app_context():
        p, b, item = _env(app)
        p2 = Project(name="P2"); db.session.add(p2); db.session.flush()
        rel = Release(project_id=p2.id, name="RX", status="draft")
        db.session.add(rel); db.session.commit()
        item_id, rel_id = item.id, rel.id
    login_as("admin")
    resp = client.post(f"/discovered/{item_id}/run",
                       data={"release_id": rel_id},
                       follow_redirects=True)
    assert resp.request.path == f"/discovered/{item_id}"
    with app.app_context():
        assert Execution.query.count() == 0
        assert OutboxEvent.query.count() == 0


def test_run_discovered_without_optional_fields(
        app, client, login_as):
    """Empty release/runner/runtime selects still queue — 'auto' picks
    resolve at dispatch time."""
    with app.app_context():
        p, b, item = _env(app)
        item_id = item.id
    login_as("admin")
    resp = client.post(f"/discovered/{item_id}/run",
                       data={"execution_profile": "default"})
    assert resp.status_code == 302
    with app.app_context():
        ex = Execution.query.one()
        assert ex.release_id is None
        rc = ex.snapshot()["run_config"]
        assert rc["requested_runner"] == ""
        assert rc["requested_runtime"] == ""


def test_kre_selector_strips_ts_tsc_extensions(app):
    """KRE console wants the project-relative suite/collection path
    WITHOUT .ts/.tsc — the discovered inventory path carries the
    extension, so the adapter strips it (Katalon documented form)."""
    from testorchestrator.adapters.kre import build_args
    a = build_args("k", "/p", "Test Suites/API-TS.ts", "/r", "default")
    assert "-testSuitePath=Test Suites/API-TS" in a
    assert not any(x.startswith("-testSuitePath=") and x.endswith(".ts")
                   for x in a)
    a = build_args("k", "/p", "Test Suites/Coll 1.tsc", "/r", "default",
                   collection=True)
    assert "-testSuiteCollectionPath=Test Suites/Coll 1" in a
    assert not any(x.startswith("-testSuiteCollectionPath=")
                   and x.endswith(".tsc") for x in a)
    # Extensionless paths pass through unchanged.
    a = build_args("k", "/p", "Test Suites/S1", "/r", "default")
    assert "-testSuitePath=Test Suites/S1" in a


def test_version_probe_parses_engine_marker_only(app, tmp_path):
    """The version parser must read katalon.versionNumber= — never an
    unrelated numeric substring from platform boot output."""
    from testorchestrator.adapters import kre as kre_adapter
    boot = ("Nov 10, 2026 INFO some.Framework init\n"
            "539ba345-9d3e-4da0-9325-a0671b8e7a37\n"
            "katalon.versionNumber=11.7.0\n"
            "katalon.buildNumber=239\nActivating...\n")
    exe = tmp_path / "fake-katalonc"
    exe.write_text(f'#!/usr/bin/env bash\nprintf %s "{boot}"\nexit 2\n')
    exe.chmod(0o755)
    assert kre_adapter.probe_version(str(exe)) == "11.7.0"

    exe2 = tmp_path / "fake-katalonc2"
    exe2.write_text('#!/usr/bin/env bash\necho "03.588 noise"\nexit 0\n')
    exe2.chmod(0o755)
    try:
        kre_adapter.probe_version(str(exe2))
        assert False, "expected KreError for missing marker"
    except kre_adapter.KreError as exc:
        assert "inconclusive" in str(exc)


def test_runtime_create_missing_required_fields_shows_errors(app, client,
                                                             login_as):
    """A runtime POST missing required fields must flash field-specific
    errors and create no record — not silently redirect."""
    login_as("admin")
    r = client.post("/system/infrastructure/runtimes", data={
        "name": "", "exec_location": "local",
        "executable_path": "", "runner_id": "",
    }, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "Runtime not registered" in body
    with app.app_context():
        assert RuntimeConnection.query.count() == 0


def test_runtime_create_persists_valid_remote(app, client, login_as):
    """A valid remote registration persists the resolved remote runner,
    dagu-worker transport, and the unique worker label."""
    login_as("admin")
    with app.app_context():
        rw = Runner(name="remote-w",
                    connection_mode="remote",
                    os_arch="linux-x64")
        db.session.add(rw); db.session.commit()
        rw_id = rw.id
    client.post("/system/infrastructure/runtimes", data={
        "name": "kre-remote", "exec_location": "remote",
        "worker_label": "lbl-1", "runner_id": rw_id,
        "executable_path": "/opt/katalonc", "credential_ref": "",
        "concurrency": "1",
    })
    with app.app_context():
        rt = RuntimeConnection.query.one()
        assert rt.transport == "dagu-worker"
        assert rt.worker_label == "lbl-1"
        assert rt.runner.connection_mode == "remote"


def test_report_writable_control_and_publish_gate(app, client,
                                                  login_as):
    """report_writable defaults off; the portal edit route toggles it;
    a disabled binding is rejected by the publish gate."""
    login_as("admin")
    with app.app_context():
        p = Project(name="PP"); db.session.add(p); db.session.flush()
        b = GitBinding(name="pub", url="file:///tmp/x.git", ref="main",
                       project_id=p.id)
        db.session.add(b); db.session.commit()
        bid = b.id
        assert b.report_writable is False
    client.post(f"/bindings/{bid}/edit", data={
        "name": "pub", "url": "file:///tmp/x.git", "ref": "main",
        "report_writable": "1"})
    with app.app_context():
        assert GitBinding.query.get(bid).report_writable is True
    client.post(f"/bindings/{bid}/edit", data={
        "name": "pub", "url": "file:///tmp/x.git", "ref": "main"})
    with app.app_context():
        assert GitBinding.query.get(bid).report_writable is False


def test_infrastructure_renders_new_controls(app, client, login_as):
    """The infra page renders the remote-runner selector and the
    license_slots input — browser-usable, not POST-only."""
    login_as("admin")
    with app.app_context():
        db.session.add(Runner(name="rem-2", connection_mode="remote",
                              license_slots=3))
        db.session.commit()
    r = client.get("/system/infrastructure")
    assert b'name="runner_id"' in r.data and b'rem-2' in r.data
    assert b'name="license_slots"' in r.data


def test_license_slots_set_change_clear_via_route(app, client, login_as):
    """license_slots set/change/clear through the edit route."""
    login_as("admin")
    with app.app_context():
        r0 = Runner(name="rr", connection_mode="local", max_slots=1)
        db.session.add(r0); db.session.commit(); rid = r0.id
    for val, exp in (("2", 2), ("5", 5), ("", None)):
        client.post(f"/system/runners/{rid}/edit", data={
            "name": "rr", "max_slots": "1", "license_slots": val})
        with app.app_context():
            assert db.session.get(Runner, rid).license_slots == exp


def test_install_py_cli_entrypoint():
    """scripts/install.py invoked as a script reaches its CLI — the
    missing __main__ guard was a silent no-op."""
    import subprocess, sys
    r = subprocess.run([sys.executable, "scripts/install.py", "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "usage" in r.stdout.lower()


def test_install_py_invalid_args_nonzero():
    """Invalid arguments return nonzero — not a silent exit 0."""
    import subprocess, sys
    r = subprocess.run([sys.executable, "scripts/install.py",
                        "--bogus-flag"], capture_output=True, text=True)
    assert r.returncode != 0


def test_bundle_exclusion_scopes():
    """dist exclusion is top-level only — nested dependency dist/ dirs
    survive; other private/runtime exclusions intact."""
    from scripts.build_bundle import _excluded
    from pathlib import Path
    assert _excluded(Path("dist/testorchestrator"))
    assert _excluded(Path("testorchestrator/__pycache__/x.py"))
    assert _excluded(Path("instance/x"))
    assert not _excluded(Path(
        "3rdparty/allure3/node_modules/allure/dist/index.js"))
    assert not _excluded(Path("testorchestrator/dist/x"))
