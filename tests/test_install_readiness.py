"""Installer readiness attribution (item 2) + schedule fire safety."""

import http.server
import socketserver
import threading
from datetime import timedelta

from testorchestrator import cli, dispatch
from testorchestrator.execution import (Execution, Project, Schedule, Suite,
                                        utcnow)
from testorchestrator.extensions import db


def test_foreign_listener_blocks_fresh_install(tmp_path, monkeypatch):
    """A pre-existing 200-listener must NOT yield install success — the
    port precheck refuses a fresh install before any payload work."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers()
        def log_message(self, *a):
            pass
    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        args = type("A", (), {"payload": str(tmp_path / "p"),
                              "data": str(tmp_path / "d"),
                              "config": str(tmp_path / "c"),
                              "url": f"http://127.0.0.1:{port}",
                              "upgrade": False, "dry_run": False})
        # Precheck alone: occupied port + fresh install -> exit 2.
        assert cli._port_occupied(args.url)
        monkeypatch.setattr(cli, "_copy_tree", lambda s, d: None)
        monkeypatch.setattr(cli, "_run_toctl",
                            lambda c, e, timeout=900:
                            type("R", (), {"returncode": 0,
                                           "stdout": "{}", "stderr": ""})())
        monkeypatch.setattr(cli, "_app_dir",
                            lambda: tmp_path / "nosuch")
        for k in ("secret_key", "java_home", "no_allure",
                  "no_service_start", "https_port", "service_user",
                  "yes"):
            setattr(args, k, True if k == "no_allure" or k ==
                    "no_service_start" or k == "yes"
                    else (443 if k == "https_port" else
                          ("testorchestrator" if k == "service_user"
                           else "")))
        rc = cli.cmd_install(args)
        assert rc == 2  # refused — no false fresh-install success
    finally:
        srv.shutdown()


def test_install_health_identity_mismatch_rejected(tmp_path):
    """A health endpoint answering with a DIFFERENT install_id can never
    satisfy this install's readiness verification."""
    args = type("A", (), {"url": "http://127.0.0.1:9"})  # closed port
    assert cli._verify_install(args, "abc123") == 4     # unreachable
    class Fake:
        def get(self, k, d=""):
            return {"install_id": "other", "version": "1.9.0"}.get(k, d)
    orig = cli._install_health
    cli._install_health = lambda url: Fake()
    try:
        assert cli._verify_install(args, "abc123") == 4   # mismatch
        cli._install_health = lambda url: {
            "install_id": "abc123", "version": "1.9.0"}
        assert cli._verify_install(args, "abc123") == 0   # own id -> ok
    finally:
        cli._install_health = orig


def test_schedule_claim_is_single_fire(app):
    """Two workers racing the same due schedule: exactly one fires —
    the CAS means only one advances next_run_at/disables."""
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        sched = Schedule(
            project_id=p.id, name="once1", target_type="suite",
            suite_id=s.id, kind="once",
            next_run_at=utcnow() - timedelta(minutes=1),
            created_by="m")
        db.session.add(sched); db.session.commit()
        assert dispatch.fire_due_schedules() == 1
        # Second worker sees it disabled -> no duplicate execution.
        assert dispatch.fire_due_schedules() == 0
        assert Execution.query.filter_by(suite_id=s.id).count() == 1


def test_schedule_fire_failure_rolls_back_claim(app):
    """A fire failure un-claims — the schedule stays due (retried next
    tick) rather than being silently consumed."""
    with app.app_context():
        p = Project(name="P"); db.session.add(p); db.session.flush()
        sched = Schedule(
            project_id=p.id, name="bad", target_type="plan",
            plan_id="nonexistent", kind="once",
            next_run_at=utcnow() - timedelta(minutes=1),
            created_by="m")
        db.session.add(sched); db.session.commit()
        assert dispatch.fire_due_schedules() == 0   # fire failed
        db.session.refresh(sched)
        assert sched.enabled and sched.next_run_at is not None  # still due
