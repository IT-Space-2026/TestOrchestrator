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
    """Health-poll writeback: all-active -> done; aged+inactive ->
    failed with failed_states."""
    marker = tmp_path / "restart-request.json"
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    rec = {"requested_by": "admin", "requested_at": now.isoformat(),
           "units": ["a.service"], "scope": "system",
           "result": "pending"}
    marker.write_text(json.dumps(rec))
    status_ok = {"services": [{"unit": "a.service", "state": "active"}]}
    with app.app_context():
        with patch.object(routes, "_managed_config",
                          return_value={"scope": "system"}):
            routes._resolve_pending_restart(status_ok, marker)
            assert json.loads(marker.read_text())["result"] == "done"
        rec["result"] = "pending"
        rec["requested_at"] = (now - timedelta(seconds=90)).isoformat()
        marker.write_text(json.dumps(rec))
        status_bad = {"services": [{"unit": "a.service",
                                    "state": "failed"}]}
        routes._resolve_pending_restart(status_bad, marker)
        d = json.loads(marker.read_text())
        assert d["result"] == "failed"
        assert d["failed_states"]["a.service"] == "failed"
