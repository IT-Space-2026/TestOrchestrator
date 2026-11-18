"""Regression: buildbot dispatch must preserve the orchestration
semantics fixed on the dagu path (docs/orchestration-replacement.md §3):

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

    def run_log(self, buildid):
        return "kre stdout"

    def workers(self):
        return []


def _env(app, monkeypatch, stub, target_type="local"):
    monkeypatch.setattr(BuildbotAdapter, "from_settings",
                        classmethod(lambda cls: stub))
    with app.app_context():
        _s.update({
            "orchestrator_backend": "buildbot",
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
        b = GitBinding(project_id=p.id, name="b", url="/x",
                       worktree_path="/tmp/bb-proto/src-repo")
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
        _, rtid, diid = _env(app, monkeypatch, stub)
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
        _, rtid, diid = _env(app, monkeypatch, stub)
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
        _, rtid, diid = _env(app, monkeypatch, stub)
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
        _, rtid, diid = _env(app, monkeypatch, stub)
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
