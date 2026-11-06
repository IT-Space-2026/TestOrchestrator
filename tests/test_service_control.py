"""Service-control backend contract — mocked schtasks/systemd paths.
The real Windows behavior remains unverified until a Windows target;
these tests pin the command shapes, validation, and writeback logic."""
import json
import os
from unittest.mock import patch, MagicMock
import pytest

from testorchestrator.system import routes


def _cfg(units, scope):
    env = {"TO_SERVICE_UNITS": units, "TO_SERVICE_SCOPE": scope}
    return patch.dict(os.environ, env, clear=False)


def test_schtasks_scope_validates_task_names():
    with _cfg("TestOrchestratorWeb,TestOrchestratorWorker", "schtasks"):
        assert routes._managed_config()["valid"] is True
    with _cfg("Bad;Task&Name", "schtasks"):
        assert routes._managed_config()["valid"] is False


def test_schtasks_default_units_when_env_empty():
    with patch.dict(os.environ, {"TO_SERVICE_SCOPE": "schtasks",
                                 "TO_SERVICE_UNITS": ""}):
        cfg = routes._managed_config()
        assert cfg["scope"] == "schtasks"
        assert cfg["units"] == ["TestOrchestratorWeb",
                                "TestOrchestratorWorker",
                                "TestOrchestratorDagu"]


def test_invalid_scope_rejected():
    with _cfg("x.service", "bogus"):
        assert routes._managed_config()["valid"] is False


def test_service_status_schtasks_queries_each_task():
    """Each configured task is queried; a missing task reads inactive."""
    with _cfg("T1,T2", "schtasks"), \
         patch("subprocess.run") as m:
        m.side_effect = [
            MagicMock(returncode=0, stdout="Status: Running"),
            MagicMock(returncode=1, stdout="", stderr="not found"),
        ]
        import shutil
        with patch.object(shutil, "which", lambda x: "/usr/bin/schtasks"):
            st = routes._service_status()
        assert st["scope"] == "schtasks"
        calls = [c.args[0] for c in m.call_args_list]
        assert calls[0] == ["schtasks", "/Query", "/TN", "T1",
                            "/FO", "LIST"]
        assert calls[1][3] == "T2"
        assert st["services"][1]["state"] == "inactive"


def test_service_status_unmanaged_without_manager():
    with _cfg("a.service", "system"), \
         patch("shutil.which",
               lambda x: None):
        st = routes._service_status()
        assert st["managed"] is False
        assert all(s["state"] == "unmanaged" for s in st["services"])


def test_pending_resolution_done_and_failed(tmp_path, app):
    """Health-poll writeback requires HELPER EVIDENCE of a new
    invocation — never mere unit liveness."""
    marker = tmp_path / "restart-request.json"
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    # helper-confirmed: new PID != old -> done
    rec = {"requested_by": "admin", "requested_at": now.isoformat(),
           "units": ["a.service"], "scope": "system",
           "pre_invocations": {"a.service": "100"},
           "evidence": {"a.service": {"old": "100", "new": "200",
                                      "restarted": True}},
           "result": "pending"}
    marker.write_text(json.dumps(rec))
    status_ok = {"services": [{"unit": "a.service", "state": "active"}]}
    with app.app_context():
        routes._resolve_pending_restart(status_ok, marker)
        d = json.loads(marker.read_text())
        assert d["result"] == "done" and d["basis"] == "helper_confirmed"
        # aged + no evidence -> failed, never done
        rec.update(result="pending", evidence={})
        rec["requested_at"] = (now - timedelta(seconds=90)).isoformat()
        marker.write_text(json.dumps(rec))
        routes._resolve_pending_restart(status_ok, marker)
        d = json.loads(marker.read_text())
        assert d["result"] == "failed"
        assert d["basis"] == "no_helper_evidence"


def test_active_units_without_evidence_never_done(tmp_path, app):
    """Regression: all units active + helper never launched (no
    evidence) must NOT resolve to done — they may be the original
    processes."""
    marker = tmp_path / "restart-request.json"
    from datetime import datetime, timezone
    rec = {"requested_by": "admin",
           "requested_at": datetime.now(timezone.utc).isoformat(),
           "units": ["a.service"], "scope": "system",
           "pre_invocations": {"a.service": "100"},
           "result": "pending"}          # no evidence: helper never ran
    marker.write_text(json.dumps(rec))
    status = {"services": [{"unit": "a.service", "state": "active"}]}
    with app.app_context():
        routes._resolve_pending_restart(status, marker)
        assert json.loads(marker.read_text())["result"] == "pending"


def test_same_pid_evidence_not_a_restart(tmp_path, app):
    """Helper ran but the unit kept the same PID -> not confirmed."""
    marker = tmp_path / "restart-request.json"
    from datetime import datetime, timezone, timedelta
    rec = {"requested_by": "admin",
           "requested_at": (datetime.now(timezone.utc)
                            - timedelta(seconds=90)).isoformat(),
           "units": ["a.service"], "scope": "system",
           "pre_invocations": {"a.service": "100"},
           "evidence": {"a.service": {"old": "100", "new": "100",
                                      "restarted": True}},
           "result": "pending"}
    marker.write_text(json.dumps(rec))
    status = {"services": [{"unit": "a.service", "state": "active"}]}
    with app.app_context():
        routes._resolve_pending_restart(status, marker)
        d = json.loads(marker.read_text())
        assert d["result"] == "failed"
        assert d["basis"] == "partial_or_no_new_invocation"


def test_schtasks_create_failure_returns_false():
    """If /Create fails (permissions etc.), /Run must NOT be attempted —
    a stale task under a fixed name could have been relaunched."""
    from unittest.mock import call
    with patch("subprocess.run") as m, \
         patch("shutil.which", lambda x: "/usr/bin/schtasks"):
        m.side_effect = [
            MagicMock(returncode=1, stderr="access denied"),  # /Create
        ]
        ok = routes._launch_restart_helper("/tmp/h.py", "schtasks")
    assert ok is False
    assert m.call_count == 1                     # /Run never attempted


def test_schtasks_run_failure_deletes_own_task():
    """/Run failure cleans up only THIS request's task."""
    with patch("subprocess.run") as m, \
         patch("shutil.which", lambda x: "/usr/bin/schtasks"):
        m.side_effect = [
            MagicMock(returncode=0),             # /Create
            MagicMock(returncode=1),             # /Run fails
            MagicMock(returncode=0),             # /Delete
        ]
        ok = routes._launch_restart_helper("/tmp/h.py", "schtasks")
    assert ok is False
    delete = m.call_args_list[2].args[0]
    assert "/Delete" in delete and "TestOrchestrator-RestartHelper-" \
        in delete[3]
    # unique name: the TN value carries a uuid suffix
    assert delete[3] != "TestOrchestrator-RestartHelper"


def test_schtasks_spaces_in_paths_quoted():
    """Executable + script paths containing spaces must be quoted in
    /TR — bare join splits them into separate args."""
    with patch("subprocess.run") as m, \
         patch("shutil.which", lambda x: "/usr/bin/schtasks"):
        m.side_effect = [MagicMock(returncode=0),
                         MagicMock(returncode=0)]
        ok = routes._launch_restart_helper(
            "/tmp/dir with space/helper file.py", "schtasks")
    tr_arg = m.call_args_list[0].args[0]
    idx = tr_arg.index("/TR")
    tr = tr_arg[idx + 1]
    assert tr.startswith('"') and tr.endswith('"')
    # the space-containing script path appears fully inside one
    # quoted segment, never split by the join
    assert '"/tmp/dir with space/helper file.py"' in tr


def test_systemd_launch_is_async_transient_service():
    """systemd-run must NOT use --scope (synchronous); it names a
    unique transient unit and returns on dispatch."""
    with patch("subprocess.run") as m, \
         patch("shutil.which", lambda x: "/usr/bin/systemd-run"):
        m.return_value = MagicMock(returncode=0, stderr="")
        unit = routes._launch_restart_helper("/tmp/h.py", "user")
    cmd = m.call_args.args[0]
    assert "--scope" not in cmd
    assert any(a.startswith("--unit=to-restart-helper-")
               for a in cmd)
    assert unit.startswith("to-restart-helper-")


def test_no_launcher_returns_false_not_popen():
    """With neither systemd-run nor schtasks, there is NO silent Popen
    fallback — the launch reports failure explicitly."""
    with patch("shutil.which", lambda x: None):
        assert routes._launch_restart_helper("/tmp/h.py",
                                             "system") is False


# --- route-level regression for the live-test bugs ---------------------

def _managed_status(units):
    return {"managed": True, "scope": "user", "config_valid": True,
            "services": [{"unit": u, "state": "active"} for u in units]}


def test_restart_dispatch_returns_redirect(app, client, login_as,
                                           monkeypatch, tmp_path):
    """Regression: the success path must return a redirect — the
    live test caught the view falling off the end (500/None)."""
    from testorchestrator.system import routes as r
    monkeypatch.setattr(r, "_service_status",
                        lambda: _managed_status(["u1.service"]))
    monkeypatch.setattr(r, "_active_work_count", lambda: 0)
    monkeypatch.setattr(r, "_managed_config",
                        lambda: {"units": ["u1.service"],
                                 "scope": "user", "valid": True})
    monkeypatch.setattr(r, "_unit_invocations",
                        lambda units, scope: {u: "1" for u in units})
    monkeypatch.setattr(r, "_restart_marker",
                        lambda: tmp_path / "rr.json")
    monkeypatch.setattr(r, "_launch_restart_helper",
                        lambda hp, scope: "to-restart-helper-t1.service")
    login_as("admin")
    resp = client.post("/system/services/restart",
                       follow_redirects=False)
    assert resp.status_code in (301, 302)
    assert "health" in (resp.headers.get("Location") or "")


def test_restart_records_exact_helper_unit(app, client, login_as,
                                           monkeypatch, tmp_path):
    """Regression: the marker binds THIS request's helper unit — not a
    glob — so evidence/cleanup are request-specific."""
    import json
    from testorchestrator.system import routes as r
    monkeypatch.setattr(r, "_service_status",
                        lambda: _managed_status(["u1.service"]))
    monkeypatch.setattr(r, "_active_work_count", lambda: 0)
    monkeypatch.setattr(r, "_managed_config",
                        lambda: {"units": ["u1.service"],
                                 "scope": "user", "valid": True})
    monkeypatch.setattr(r, "_unit_invocations",
                        lambda units, scope: {u: "1" for u in units})
    monkeypatch.setattr(r, "_restart_marker",
                        lambda: tmp_path / "rr.json")
    monkeypatch.setattr(r, "_launch_restart_helper",
                        lambda hp, scope: "to-restart-helper-u9.service")
    login_as("admin")
    client.post("/system/services/restart")
    marker = tmp_path / "rr.json"
    rec = json.loads(marker.read_text())
    assert rec["helper_unit"] == "to-restart-helper-u9.service"
    assert rec["result"] == "pending"
    assert rec["pre_invocations"]["u1.service"] == "1"


def test_generated_helper_uses_path_for_marker(app, client, login_as,
                                               monkeypatch, tmp_path):
    """Regression: the helper crashed on m.read_text() with a string
    path — the generated file must wrap the marker in Path."""
    import pathlib
    from testorchestrator.system import routes as r
    monkeypatch.setattr(r, "_service_status",
                        lambda: _managed_status(["u1.service"]))
    monkeypatch.setattr(r, "_active_work_count", lambda: 0)
    monkeypatch.setattr(r, "_managed_config",
                        lambda: {"units": ["u1.service"],
                                 "scope": "user", "valid": True})
    monkeypatch.setattr(r, "_unit_invocations",
                        lambda units, scope: {u: "1" for u in units})
    monkeypatch.setattr(r, "_restart_marker",
                        lambda: tmp_path / "rr.json")
    monkeypatch.setattr(r, "_launch_restart_helper",
                        lambda hp, scope: "h.service")
    login_as("admin")
    client.post("/system/services/restart")
    helper = (pathlib.Path(app.instance_path) / "restart-helper.py")
    src = helper.read_text()
    assert "from pathlib import Path" in src
    assert "m=Path(" in src
    assert "m.read_text()" in src
    compile(src, str(helper), "exec")          # valid python


def test_restart_rejects_non_admin(app, client, login_as):
    """Authorization is checked separately from CSRF: an authenticated
    non-admin must be rejected (403), not merely an unauthenticated
    request."""
    login_as("tester")
    assert client.post("/system/services/restart").status_code == 403
