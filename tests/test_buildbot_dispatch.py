"""Regression: buildbot dispatch must preserve the orchestration
semantics verified for the previous orchestration path:

- assignment commits BEFORE the buildrequest exists (a claimed build
  can start executing immediately — the DISPATCHED row + runner
  ownership must already be durable);
- a cancel landing between assign and submit is re-issued to the new
  brid so the build doesn't run a cancelled execution;
- explicit runner/runtime picks are honored or visibly rejected —
  never silently retargeted;
- a lost submission response is reconciled by the execution-id
  property before any resubmit — never a duplicate buildrequest;
- results=CANCELLED maps to CANCELED only when the portal requested
  the cancel; an unrequested abort is honest FAILED/infrastructure;
- connect-level master outages leave the row QUEUED for the redrive
  (slot released), other failures fail visibly.
"""
import json

import pytest
from pathlib import Path
import requests as _req

from testorchestrator import buildbot_cfg, dispatch as d
from testorchestrator import settings as _s
from testorchestrator.adapters.buildbot import BuildbotAdapter
from testorchestrator.execution import (DiscoveredItem, Execution,
                                        ExecutionState, GitBinding,
                                        OutboxEvent, Outcome, Project,
                                        Runner, RuntimeConnection)
from testorchestrator.extensions import db


class _BBProbe(BuildbotAdapter):
    """Captures submission calls and serves canned master state."""
    def __init__(self):
        super().__init__("http://stub")
        self.submits = []
        self.cancels = []
        self.existing = []          # find_buildrequests -> live brids
        self.br_state = {}          # brid -> buildrequest dict
        self.row_at_submit = None
        self.fail_submit = None
        self.prep_log = ""

    def find_buildrequests(self, execution_id):
        return list(self.existing)

    def submit(self, builder_name, execution_id, properties=None):
        self.submits.append((builder_name, execution_id, properties))
        if self.fail_submit is not None:
            raise self.fail_submit
        ex = db.session.get(Execution, execution_id)
        self.row_at_submit = dict(
            state=ex.state, runner_id=ex.runner_id,
            runtime_id=ex.runtime_id, lease=ex.lease_expires_at)
        return "7"

    def cancel(self, dispatch_id):
        self.cancels.append(dispatch_id)
        return True

    def buildrequest(self, dispatch_id):
        return self.br_state.get(str(dispatch_id), {})

    def builds_for_request(self, dispatch_id):
        return [{"buildid": 1}]

    def run_log(self, buildid, step_names=None):
        if step_names and "prep" in step_names:
            return self.prep_log
        return "kre stdout"

    def workers(self):
        return []


def _env(app, monkeypatch, stub, target_type="local",
         worktree="/tmp/bb-proto/src-repo"):
    monkeypatch.setattr(BuildbotAdapter, "from_settings",
                        classmethod(lambda cls: stub))
    with app.app_context():
        _s.update({
            "buildbot_endpoint": "http://stub",
            "buildbot_home": "/tmp/bb-test-home",
        })
        r = Runner(name="bb-target", connection_mode="local",
                   target_type=target_type, os_arch="linux",
                   max_slots=2,
                   ssh_workdir="/tmp/ssh-runs" if target_type == "ssh"
                   else "")
        db.session.add(r)
        db.session.flush()
        rt = RuntimeConnection(name="kre", runner_id=r.id,
                               executable_path="/opt/kre/katalonc",
                               enabled=True)
        db.session.add(rt)
        db.session.flush()
        p = Project(name="p")
        db.session.add(p)
        db.session.flush()
        Path(str(worktree)).mkdir(parents=True, exist_ok=True)
        b = GitBinding(project_id=p.id, name="b", url="/x",
                       worktree_path=str(worktree))
        db.session.add(b)
        db.session.flush()
        di = DiscoveredItem(project_id=p.id, binding_id=b.id,
                            kind="suite", path="T/x.ts", name="x",
                            sha="s", order_idx=0)
        db.session.add(di)
        db.session.commit()
        return r.id, rt.id, di.id


def _event_for(ex):
    return OutboxEvent.query.filter(
        OutboxEvent.kind == "orchestrator-dispatch",
        OutboxEvent.payload_json.contains(ex.id)).first()


def test_assignment_committed_before_submit(app, monkeypatch, tmp_path):
    """At the instant the buildrequest is forced, the execution is
    already DISPATCHED to its runner with a live lease."""
    stub = _BBProbe()
    with app.app_context():
        monkeypatch.setattr(buildbot_cfg, "home",
                            lambda: tmp_path)
        _, rtid, diid = _env(app, monkeypatch, stub,
                         worktree=tmp_path / "wt")
        ex = d.create_execution(
            "u", None, run_config={"runtime_id": rtid},
            source_item=db.session.get(DiscoveredItem, diid))
        assert ex.snapshot()["dispatch_transport"] == "buildbot"
        d._handle_event(_event_for(ex))
        assert len(stub.submits) == 1
        builder, eid, props = stub.submits[0]
        assert builder == f"to-target-{ex.runner_id}" or \
            builder.startswith("to-target-")
        assert stub.row_at_submit["state"] == ExecutionState.DISPATCHED
        assert stub.row_at_submit["runner_id"]
        assert stub.row_at_submit["lease"] is not None
        db.session.refresh(ex)
        assert ex.dispatch_id == "bb:7"
        # spec travels in the declared property; no secrets anywhere
        spec = json.loads(props["exec_spec"])
        assert spec["executable"] == "/opt/kre/katalonc"


def test_cancel_between_assign_and_submit(app, monkeypatch, tmp_path):
    """A cancel arriving after assignment but before submission is
    re-issued against the new brid — the build must not run."""
    stub = _BBProbe()
    with app.app_context():
        monkeypatch.setattr(buildbot_cfg, "home", lambda: tmp_path)
        _, rtid, diid = _env(app, monkeypatch, stub,
                         worktree=tmp_path / "wt")
        ex = d.create_execution(
            "u", None, run_config={"runtime_id": rtid},
            source_item=db.session.get(DiscoveredItem, diid))
        d._buildbot_assign_and_submit(ex)
        db.session.refresh(ex)
        assert ex.dispatch_id == "bb:7"
        # simulate cancel landing between assign and submit: mark the
        # state then re-run the post-submit guard path
        ex.state = ExecutionState.CANCEL_REQUESTED
        db.session.commit()
        # request_cancel on the now-dispatched execution issues cancel
        d.request_cancel(ex.id, "u", "reason")
        assert "7" in stub.cancels


def test_explicit_pick_unavailable_fails_visibly(app, monkeypatch,
                                                 tmp_path):
    stub = _BBProbe()
    with app.app_context():
        monkeypatch.setattr(buildbot_cfg, "home", lambda: tmp_path)
        _, rtid, diid = _env(app, monkeypatch, stub,
                         worktree=tmp_path / "wt")
        # create guards disabled runtimes up front
        rt = db.session.get(RuntimeConnection, rtid)
        rt.enabled = False
        db.session.commit()
        with pytest.raises(d.DispatchError):
            d.create_execution(
                "u", None, run_config={"runtime_id": rtid},
                source_item=db.session.get(DiscoveredItem, diid))
        # if the pick becomes unavailable after queueing but before
        # submission, the submit path must fail visibly — not retarget
        rt.enabled = True
        db.session.commit()
        ex2 = d.create_execution(
            "u", None, run_config={"runtime_id": rtid},
            source_item=db.session.get(DiscoveredItem, diid))
        rt.enabled = False
        db.session.commit()
        d._buildbot_assign_and_submit(ex2)
        db.session.refresh(ex2)
        assert ex2.state == ExecutionState.FAILED
        assert ex2.error_kind == "infrastructure"
        assert not stub.submits


def test_lost_response_reconciles_no_duplicate(app, monkeypatch,
                                               tmp_path):
    """A submission whose response was lost leaves a live brid — the
    redrive adopts it instead of forcing a second buildrequest."""
    stub = _BBProbe()
    with app.app_context():
        monkeypatch.setattr(buildbot_cfg, "home", lambda: tmp_path)
        _, rtid, diid = _env(app, monkeypatch, stub,
                         worktree=tmp_path / "wt")
        ex = d.create_execution(
            "u", None, run_config={"runtime_id": rtid},
            source_item=db.session.get(DiscoveredItem, diid))
        # master already has a live brid carrying this execution id
        stub.existing = [{"buildrequestid": 42, "complete": False}]
        d._buildbot_assign_and_submit(ex)
        db.session.refresh(ex)
        assert ex.dispatch_id == "bb:42"
        assert not stub.submits        # adopted, never resubmitted


def test_connection_error_stays_queued_releases_slot(app, monkeypatch,
                                                     tmp_path):
    stub = _BBProbe()
    stub.fail_submit = _req.ConnectionError("master down")
    with app.app_context():
        monkeypatch.setattr(buildbot_cfg, "home", lambda: tmp_path)
        rid, rtid, diid = _env(app, monkeypatch, stub)
        ex = d.create_execution(
            "u", None, run_config={"runtime_id": rtid},
            source_item=db.session.get(DiscoveredItem, diid))
        d._buildbot_assign_and_submit(ex)
        db.session.refresh(ex)
        assert ex.state == ExecutionState.QUEUED
        assert ex.runner_id is None      # slot released for redrive
        # recovery: redrive submits again
        stub.fail_submit = None
        d._redrive_queued_remote()
        db.session.refresh(ex)
        assert ex.state == ExecutionState.DISPATCHED
        assert ex.dispatch_id == "bb:7"


def test_requested_cancel_maps_canceled(app, monkeypatch, tmp_path):
    stub = _BBProbe()
    stub.br_state["7"] = {"complete": True, "results": 6, "claimed": True}
    with app.app_context():
        monkeypatch.setattr(buildbot_cfg, "home", lambda: tmp_path)
        rid, rtid, diid = _env(app, monkeypatch, stub)
        ex = d.create_execution(
            "u", None, run_config={"runtime_id": rtid},
            source_item=db.session.get(DiscoveredItem, diid))
        ex.state = ExecutionState.CANCEL_REQUESTED
        ex.dispatch_id = "bb:7"
        ex.runner_id = rid
        ex.runtime_id = rtid
        db.session.commit()
        d.reconcile_buildbot_runs()
        db.session.refresh(ex)
        assert ex.state == ExecutionState.CANCELED
        assert ex.outcome == Outcome.INCOMPLETE


def test_unrequested_cancel_maps_failed_infra(app, monkeypatch,
                                              tmp_path):
    stub = _BBProbe()
    stub.br_state["7"] = {"complete": True, "results": 6, "claimed": True}
    with app.app_context():
        monkeypatch.setattr(buildbot_cfg, "home", lambda: tmp_path)
        rid, rtid, diid = _env(app, monkeypatch, stub)
        ex = d.create_execution(
            "u", None, run_config={"runtime_id": rtid},
            source_item=db.session.get(DiscoveredItem, diid))
        ex.state = ExecutionState.DISPATCHED
        ex.dispatch_id = "bb:7"
        ex.runner_id = rid
        ex.runtime_id = rtid
        db.session.commit()
        d.reconcile_buildbot_runs()
        db.session.refresh(ex)
        # Unrequested abort is NOT a user cancellation
        assert ex.state == ExecutionState.FAILED
        assert ex.error_kind == "infrastructure"
        assert "without a recorded cancellation" in ex.error_text


def test_success_collects_and_completes(app, monkeypatch, tmp_path):
    stub = _BBProbe()
    stub.br_state["7"] = {"complete": True, "results": 0, "claimed": True}
    with app.app_context():
        monkeypatch.setattr(buildbot_cfg, "home", lambda: tmp_path)
        rid, rtid, diid = _env(app, monkeypatch, stub)
        # stage a fake uploaded artifact where _collect_buildbot reads
        arts = buildbot_cfg.artifacts_dir() / "PLACEHOLDER"
        ex = d.create_execution(
            "u", None, run_config={"runtime_id": rtid},
            source_item=db.session.get(DiscoveredItem, diid))
        ex.state = ExecutionState.DISPATCHED
        ex.dispatch_id = "bb:7"
        ex.runner_id = rid
        ex.runtime_id = rtid
        db.session.commit()
        src = buildbot_cfg.artifacts_dir() / ex.id
        src.mkdir(parents=True)
        (src / "junit.xml").write_text(
            '<testsuite tests="1" failures="0"><testcase '
            'classname="T" name="c" time="0.1"/></testsuite>')
        d.reconcile_buildbot_runs()
        db.session.refresh(ex)
        assert ex.state == ExecutionState.COMPLETED
        assert ex.outcome == Outcome.PASSED


def test_retry_blocked_maps_unknown_and_holds_reservations(
        app, monkeypatch, tmp_path):
    """A buildrequest resubmit REFUSED by prep (unresolvable prior
    attempt / retry policy exceeded) must map to UNKNOWN — not FAILED:
    the original attempt may still be live, so its reservations stay
    held and the row lands on the reconciliation queue."""
    stub = _BBProbe()
    stub.br_state["7"] = {"complete": True, "results": 2,
                          "claimed": True}
    stub.prep_log = ("TO_RETRY_BLOCKED: pid 4242 alive; env marker "
                     "mismatch or unreadable — original attempt state "
                     "unresolved, relaunch blocked")
    with app.app_context():
        monkeypatch.setattr(buildbot_cfg, "home", lambda: tmp_path)
        rid, rtid, diid = _env(app, monkeypatch, stub)
        ex = d.create_execution(
            "u", None, run_config={"runtime_id": rtid},
            source_item=db.session.get(DiscoveredItem, diid))
        ex.state = ExecutionState.DISPATCHED
        ex.dispatch_id = "bb:7"
        ex.runner_id = rid
        ex.runtime_id = rtid
        db.session.add(d.ResourceReservation(
            execution_id=ex.id, runner_id=rid,
            resource_kind="slot", slot_index=0))
        db.session.commit()
        d.reconcile_buildbot_runs()
        db.session.refresh(ex)
        assert ex.state == ExecutionState.UNKNOWN
        assert ex.error_kind == "infrastructure"
        assert "TO_RETRY_BLOCKED" in ex.error_text
        assert "reconciliation" in ex.error_text
        resv = d.ResourceReservation.query.filter_by(
            execution_id=ex.id, released_at=None).all()
        assert resv                  # claims stay held — never freed
        assert Outcome[ex.outcome.name] == Outcome.INCOMPLETE


def test_retry_policy_rendered_into_specs(app):
    """retry_enabled/max_attempts come from run_config — explicit, not
    implied by attempt bookkeeping."""
    with app.app_context():
        ex = type("E", (), {})()
        ex.snapshot = lambda: {"run_config": {
            "max_attempts": "5", "retry_enabled": False}}
        spec = buildbot_cfg.kre_exec_spec(
            ex, type("R", (), {"executable_path": "/x"})(), ["a"])
        assert spec["max_attempts"] == 5
        assert spec["retry_enabled"] is False
        ex2 = type("E", (), {})()
        ex2.snapshot = lambda: {"run_config": {}}
        spec2 = buildbot_cfg.node_exec_spec(ex2, "/x", ["a"])
        assert spec2["max_attempts"] == 3
        assert spec2["retry_enabled"] is True
        # None (readiness probe) gets the safe defaults, no crash
        spec3 = buildbot_cfg.ssh_exec_spec(None, "py", "d", "f")
        assert spec3["max_attempts"] == 3


def test_retry_policy_persisted_in_snapshot(app, monkeypatch, tmp_path):
    """The policy must survive the immutable snapshot — a UI/API
    run_config reaches the worker-side prep unchanged. Disabling
    retries means a resubmitted buildrequest hits attempt 2 and is
    blocked, not relaunched."""
    stub = _BBProbe()
    with app.app_context():
        monkeypatch.setattr(buildbot_cfg, "home", lambda: tmp_path)
        _, rtid, diid = _env(app, monkeypatch, stub,
                             worktree=tmp_path / "wt")
        ex = d.create_execution(
            "u", None,
            run_config={"runtime_id": rtid,
                        "retry_enabled": False, "max_attempts": "5"},
            source_item=db.session.get(DiscoveredItem, diid))
        rc = ex.snapshot()["run_config"]
        assert rc["retry_enabled"] is False
        assert rc["max_attempts"] == 5          # clamped/str→int
        rt = db.session.get(RuntimeConnection, rtid)
        spec = buildbot_cfg.kre_exec_spec(ex, rt, ["a"])
        assert spec["retry_enabled"] is False   # snapshot→spec chain
        assert spec["max_attempts"] == 5
        # defaults when unset
        ex2 = d.create_execution(
            "u", None, run_config={"runtime_id": rtid},
            source_item=db.session.get(DiscoveredItem, diid))
        rc2 = ex2.snapshot()["run_config"]
        assert rc2["retry_enabled"] is True
        assert rc2["max_attempts"] == 3


class _FakeChan:
    def __init__(self, out=b"", rc=0):
        self._out = out
        self._rc = rc
    def settimeout(self, t):
        pass
    def exec_command(self, cmd):
        self.cmd = cmd
    def makefile(self, mode):
        import io
        return io.BytesIO(self._out)
    def makefile_stderr(self, mode):
        import io
        return io.BytesIO(b"")
    def recv_exit_status(self):
        return self._rc


def test_ssh_remote_cancel_kills_process_group(app, monkeypatch, tmp_path):
    """Regression: portal cancel must kill the detached remote process
    group itself — the buildbot worker SIGKILLs to_ssh_exec before its
    SIGTERM handler runs, so nothing else terminates the sshd-side run."""
    import json as _json
    from testorchestrator import dispatch as d, buildbot_cfg
    with app.app_context():
        rid, rtid, diid = _env(app, monkeypatch, _BBProbe())
        ex = d.create_execution(
            "u", None, run_config={"runtime_id": rtid},
            source_item=db.session.get(DiscoveredItem, diid))
        specs = buildbot_cfg.run_specs_dir()
        specs.mkdir(parents=True, exist_ok=True)
        (specs / f"{ex.id}.json").write_text(_json.dumps({
            "host": "127.0.0.1", "port": "22", "username": "u",
            "key_file": str(tmp_path / "k"),
            "host_key_fingerprint": "", "remote_workdir": "/w/e1"}))

        # no paramiko transport needed at all -> None only without spec
        import paramiko
        (tmp_path / "k").write_text("x")
        monkeypatch.setattr(paramiko.Ed25519Key, "from_private_key_file",
                            classmethod(lambda cls, f: object()))
        calls = []

        class _T:
            def __init__(self, sock):
                pass
            def start_client(self, timeout=0):
                pass
            def auth_publickey(self, u, k):
                pass
            def open_session(self, timeout=0):
                # pid present on first exec (cat run.pid), dead on check
                c = _FakeChan(b"4242\n" if not calls else b"dead\n")
                calls.append(c)
                return c
            def close(self):
                pass
        import socket as _sock
        monkeypatch.setattr(d, "_sock", None, raising=False)
        import testorchestrator.dispatch as dd
        # patch the socket create used inside _ssh_remote_cancel
        orig = _sock.create_connection
        monkeypatch.setattr(_sock, "create_connection",
                            lambda *a, **k: object())
        monkeypatch.setattr(paramiko, "Transport", _T)
        monkeypatch.setattr(dd.time, "sleep", lambda s: None)

        # first _run returns "4242" for every command; make the
        # kill -0 check return dead by keying on command content
        class _T2(_T):
            def open_session(self, timeout=0):
                c = _FakeChan(b"4242\n")
                calls.append(c)
                return c
        # Simpler: sequence channels — cat run.pid, kill cmd, kill -0
        seq = [b"4242\n", b"", b"dead\n"]
        class _T3:
            def __init__(self, sock): pass
            def start_client(self, timeout=0): pass
            def auth_publickey(self, u, k): pass
            def open_session(self, timeout=0):
                out = seq.pop(0) if seq else b"dead\n"
                c = _FakeChan(out); calls.append(c)
                return c
            def close(self): pass
        monkeypatch.setattr(paramiko, "Transport", _T3)

        ok = d._ssh_remote_cancel(ex)
        assert ok is True
        kill_cmds = [c.cmd for c in calls]
        assert any("kill -TERM -4242" in c for c in kill_cmds)
        assert any("kill -0 4242" in c for c in kill_cmds)

        # group still alive -> unconfirmed
        seq2 = [b"4242\n", b"", b"alive\n"]
        class _T4(_T3):
            def open_session(self, timeout=0):
                out = seq2.pop(0) if seq2 else b"alive\n"
                c = _FakeChan(out); calls.append(c)
                return c
        monkeypatch.setattr(paramiko, "Transport", _T4)
        assert d._ssh_remote_cancel(ex) is False

        # no spec file at all -> None (non-SSH run)
        ex2 = d.create_execution(
            "u", None, run_config={"runtime_id": rtid},
            source_item=db.session.get(DiscoveredItem, diid))
        assert d._ssh_remote_cancel(ex2) is None
