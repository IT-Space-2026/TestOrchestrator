"""Project membership acceptance (§access) — the additional
authorization layer on top of roles:

    allowed = role permits the action AND user holds membership on
    the owning project

Acceptance matrix covered here: two projects × two same-role users
with different assignments; direct URLs, filtered lists, identifier
substitution, artifact/report downloads, API tokens bounded by the
owner's CURRENT role+membership, mid-session revocation, dispatch-
boundary recheck for pending work, admin-retains-all, audit of
changes, and the unassigned-user empty state.
"""
import hashlib
import json

import pytest

from testorchestrator import dispatch as d
from testorchestrator.execution import (ApiToken, Artifact, Execution,
                                        ExecutionState, Outcome,
                                        Project, Runner,
                                        RuntimeConnection, Suite)
from testorchestrator.extensions import db
from testorchestrator.models import AuditEvent, Role, User
from tests.conftest import login


@pytest.fixture()
def env(app):
    """Two projects, each with a suite + one finished execution;
    two TESTER users: ta -> p1 only, tb -> p2 only; tn -> nothing."""
    with app.app_context():
        p1 = Project(name="P1"); p2 = Project(name="P2")
        db.session.add_all([p1, p2]); db.session.flush()
        s1 = Suite(project_id=p1.id, name="S1", content_hash="h1")
        s2 = Suite(project_id=p2.id, name="S2", content_hash="h2")
        db.session.add_all([s1, s2]); db.session.flush()
        e1 = Execution(project_id=p1.id, suite_id=s1.id,
                       state=ExecutionState.COMPLETED,
                       outcome=Outcome.PASSED, created_by="ta",
                       snapshot_json='{"suite_name":"S1"}')
        e2 = Execution(project_id=p2.id, suite_id=s2.id,
                       state=ExecutionState.COMPLETED,
                       outcome=Outcome.FAILED, created_by="tb",
                       snapshot_json='{"suite_name":"S2"}')
        db.session.add_all([e1, e2])
        for uname, plist in (("ta", [p1.id]), ("tb", [p2.id]),
                             ("tn", [])):
            u = User(username=uname, role=Role.TESTER)
            u.set_password("TestPassword-12345")
            u.set_assigned_projects(plist)
            db.session.add(u)
        db.session.commit()
        return {"p1": p1.id, "p2": p2.id, "s1": s1.id, "s2": s2.id,
                "e1": e1.id, "e2": e2.id}


def _user(uname):
    return User.query.filter_by(username=uname).first()


# ----------------------------------------------------------- list scope

def test_projects_list_scopes_and_empty_state(client, env):
    login(client, "ta")
    r = client.get("/projects")
    assert b"P1" in r.data and b"P2" not in r.data
    login(client, "tn")
    r = client.get("/projects")
    assert b"No projects assigned" in r.data
    assert b"P1" not in r.data and b"P2" not in r.data


def test_direct_project_url_denied(client, env):
    login(client, "tb")
    assert client.get(f"/projects/{env['p1']}").status_code == 403
    # identifier substitution yields nothing distinguishable
    assert client.get(f"/suites/{env['s1']}").status_code == 403


def test_execution_list_and_filters_scoped(client, env):
    login(client, "ta")
    r = client.get("/executions")
    assert env["e1"][:8].encode() in r.data
    assert env["e2"][:8].encode() not in r.data
    # explicit out-of-scope filter returns an empty page, not data
    r = client.get(f"/executions?project_id={env['p2']}")
    assert env["e2"][:8].encode() not in r.data
    # filter dropdown must not leak other projects' names
    assert b"P2" not in r.data


def test_execution_detail_artifact_report_denied(client, env, tmp_path):
    with client.application.app_context():
        root = tmp_path / "ev" / env["e2"]
        root.mkdir(parents=True)
        (root / "report.html").write_text("x")
        ex = db.session.get(Execution, env["e2"])
        ex.report_dir = str(root)
        a = Artifact(execution_id=env["e2"], kind="report",
                     rel_path="report.html",
                     sha256="deadbeef" * 8, size=1)
        db.session.add(a)
        db.session.commit()
        aid = a.id
    login(client, "ta")
    assert client.get(
        f"/executions/{env['e2']}").status_code == 403
    assert client.get(
        f"/executions/{env['e2']}/artifacts/{aid}").status_code == 403
    assert client.get(
        f"/executions/{env['e2']}/report.zip").status_code == 403
    assert client.get(
        f"/executions/{env['e2']}/report").status_code == 403


def test_membership_never_elevates_role(client, env):
    """Assignment + TESTER role still cannot reach admin surfaces."""
    login(client, "ta")
    assert client.get("/system/users").status_code in (302, 403)
    r = client.get(f"/projects/{env['p1']}")
    assert r.status_code == 200


def test_revocation_takes_effect_without_logout(client, env):
    login(client, "ta")
    assert client.get(f"/projects/{env['p1']}").status_code == 200
    with client.application.app_context():
        _user("ta").set_assigned_projects([])
        db.session.commit()
    # same session — next protected request loses access
    assert client.get(f"/projects/{env['p1']}").status_code == 403
    r = client.get("/projects")
    assert b"No projects assigned" in r.data


def test_admin_retains_all_projects(client, env):
    login(client, "admin")
    r = client.get("/projects")
    assert b"P1" in r.data and b"P2" in r.data
    assert client.get(f"/projects/{env['p2']}").status_code == 200


# --------------------------------------------------------------- API

_tok_seq = [0]


def _api_token(scope="run", projects="[]", created_by="ta"):
    _tok_seq[0] += 1
    tok = "to_" + hashlib.sha256(
        f"pa-{scope}-{_tok_seq[0]}".encode()).hexdigest()[:48]
    row = ApiToken(name="pa" + str(_tok_seq[0]), scope=scope,
                   token_hash=hashlib.sha256(
                       tok.encode()).hexdigest(),
                   allowed_projects=projects, created_by=created_by)
    db.session.add(row)
    db.session.commit()
    return tok


def _hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_token_bounded_by_owner_membership(client, env):
    with client.application.app_context():
        # token scoped wider than the owner's membership
        tok = _api_token(projects=json.dumps(
            [env["p1"], env["p2"]]), created_by="ta")
    r = client.get("/api/v1/executions", headers=_hdr(tok))
    ids = {e["id"] for e in r.get_json()["executions"]}
    assert env["e1"] in ids and env["e2"] not in ids
    # direct object access outside owner membership is invisible
    assert client.get(f"/api/v1/executions/{env['e2']}",
                      headers=_hdr(tok)).status_code == 404


def test_token_membership_revocation_applies_next_request(client, env):
    with client.application.app_context():
        tok = _api_token(projects="[]", created_by="ta")  # token: all
    r = client.get("/api/v1/executions", headers=_hdr(tok))
    assert env["e1"] in {e["id"] for e in
                         r.get_json()["executions"]}
    with client.application.app_context():
        _user("ta").set_assigned_projects([])
        db.session.commit()
    r = client.get("/api/v1/executions", headers=_hdr(tok))
    assert r.get_json()["executions"] == []


def test_token_cannot_exceed_owner_role(client, env):
    with client.application.app_context():
        # PO-role owner cannot mutate even with a 'run' scope token
        tok = _api_token(scope="run", projects="[]", created_by="po")
    r = client.post("/api/v1/executions",
                    json={"suite_id": env["s1"]}, headers=_hdr(tok))
    assert r.status_code == 403
    # a deactivated owner kills the token entirely
    with client.application.app_context():
        u = _user("ta"); u.is_active = False; db.session.commit()
        tok2 = _api_token(projects="[]", created_by="ta")
    r = client.get("/api/v1/executions", headers=_hdr(tok2))
    assert r.status_code == 401


# ------------------------------------------------- dispatch boundary

def _runner_runtime(app):
    with app.app_context():
        r = Runner(name="rr", connection_mode="local",
                   target_type="local", os_arch="linux", max_slots=2)
        db.session.add(r); db.session.flush()
        rt = RuntimeConnection(name="rt", runner_id=r.id,
                               executable_path="/x", enabled=True)
        db.session.add(rt); db.session.commit()
        return r.id, rt.id


def test_pending_work_blocked_when_access_revoked(app, env):
    """Queued work whose submitter lost membership never launches —
    it fails visibly at the dispatch boundary."""
    rid, rtid = _runner_runtime(app)
    with app.app_context():
        ex = d.create_execution(
            "ta", db.session.get(Suite, env["s1"]),
            run_config={"runtime_id": rtid})
        _user("ta").set_assigned_projects([])   # revoke after queue
        db.session.commit()
        d.run_execution(ex.id)
        db.session.refresh(ex)
        assert ex.state == ExecutionState.FAILED
        assert ex.error_kind == "access"
        assert ex.runner_id is None
        ev = AuditEvent.query.filter_by(
            event="execution.access_blocked").first()
        assert ev is not None and ex.id in ev.detail


def test_pending_work_dispatches_when_member(app, env):
    rid, rtid = _runner_runtime(app)
    with app.app_context():
        ex = d.create_execution(
            "ta", db.session.get(Suite, env["s1"]),
            run_config={"runtime_id": rtid})
        d.run_execution(ex.id)
        db.session.refresh(ex)
        # allowed through the boundary — real dispatch proceeded
        assert ex.state != ExecutionState.FAILED or \
            ex.error_kind != "access"


def test_schedule_created_runs_blocked_after_creator_revoked(app, env):
    """User-owned schedules stamp created_by — after membership is
    removed their pending runs block at dispatch, never becoming
    unowned system work."""
    rid, rtid = _runner_runtime(app)
    with app.app_context():
        ex = d.create_execution(
            "ta", db.session.get(Suite, env["s1"]),
            run_config={"runtime_id": rtid}, schedule_id="sched-x")
        _user("ta").set_assigned_projects([])
        db.session.commit()
        d.run_execution(ex.id)
        db.session.refresh(ex)
        assert ex.state == ExecutionState.FAILED
        assert ex.error_kind == "access"


# ------------------------------------------------------- admin views

def test_user_access_editor_roundtrip_and_audit(client, env):
    login(client, "admin")
    uid = _uid = None
    with client.application.app_context():
        uid = _user("ta").id
    r = client.get(f"/system/users/{uid}/access")
    assert r.status_code == 200
    assert b"Project access" in r.data and b"P2" in r.data
    r = client.post(f"/system/users/{uid}/access",
                    data={"project_ids": [env["p1"], env["p2"]]})
    assert r.status_code in (302, 200)
    with client.application.app_context():
        assert set(_user("ta").assigned_project_ids()) == {
            env["p1"], env["p2"]}
        ev = AuditEvent.query.filter_by(
            event="user.project_access").order_by(
            AuditEvent.id.desc()).first()
        assert ev is not None and "ta" in ev.detail \
            and env["p2"] in ev.detail


def test_admin_user_shows_role_access_not_checkboxes(client, env):
    login(client, "admin")
    with client.application.app_context():
        uid = _user("admin").id
    r = client.get(f"/system/users/{uid}/access")
    assert b"All projects through Administrator role" in r.data
    assert b'name="project_ids"' not in r.data


def test_project_access_view_roundtrip(client, env):
    login(client, "admin")
    r = client.get(f"/projects/{env['p1']}/access")
    assert r.status_code == 200
    assert b"ta" in r.data and b"tn" in r.data
    with client.application.app_context():
        tn_id = _user("tn").id
    client.post(f"/projects/{env['p1']}/access",
                data={"user_ids": [str(tn_id)]})
    with client.application.app_context():
        assert env["p1"] in _user("tn").assigned_project_ids()
        # ta's assignment was NOT silently widened/narrowed — it was
        # unchecked in this submission, so it is removed (explicit)
        assert env["p1"] not in _user("ta").assigned_project_ids()


def test_users_page_flags_unassigned(client, env):
    login(client, "admin")
    r = client.get("/system/users")
    assert "none — needs assignment".encode() in r.data
    assert b"all projects" in r.data
