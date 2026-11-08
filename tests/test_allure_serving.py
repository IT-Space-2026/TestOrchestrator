"""Allure report serving: multi-file assets, zip completeness,
new-tab link, sandbox CSP, traversal + auth boundaries."""
import io, json, zipfile
from pathlib import Path
from testorchestrator import allure_runtime
from testorchestrator.extensions import db
from testorchestrator.execution import Execution


def _mk_report(app, tmp_path, ex_id="srv1"):
    with app.app_context():
        rd = tmp_path / "ev" / ex_id
        res = rd / "results"; res.mkdir(parents=True)
        (res / "u1-result.json").write_text(json.dumps({
            "uuid": "u1", "historyId": "h1", "name": "login",
            "status": "passed", "stage": "finished",
            "start": 1, "stop": 2, "labels": []}))
        out = rd / "allure"
        assert allure_runtime.generate(res, out)["ok"]
        ex = Execution.query.get(ex_id)
        if ex is None:
            ex = Execution(id=ex_id, project_id="p1",
                           created_by="t")
        ex.report_dir = str(rd); ex.allure_path = "allure/index.html"
        ex.allure_status = "ready"
        db.session.add(ex); db.session.commit()
        return ex_id


def test_allure_root_redirects_to_trailing_slash(client, login_as,
                                                  app, tmp_path):
    ex_id = _mk_report(app, tmp_path)
    login_as("admin")
    r = client.get(f"/executions/{ex_id}/allure")
    assert r.status_code == 302
    assert r.location.endswith("/allure/")


def test_index_and_nested_asset_served(client, login_as, app,
                                       tmp_path):
    ex_id = _mk_report(app, tmp_path)
    login_as("admin")
    r = client.get(f"/executions/{ex_id}/allure/")
    assert r.status_code == 200
    assert r.headers["Content-Security-Policy"] == \
        "sandbox allow-scripts allow-same-origin"
    # a real nested asset resolves through the subtree route
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        afile = next((Path(ex.report_dir) / "allure"
                      ).glob("app-*.js"), None)
    if afile:
        a = client.get(f"/executions/{ex_id}/allure/{afile.name}")
        assert a.status_code == 200


def test_zip_contains_full_subtree(client, login_as, app, tmp_path):
    ex_id = _mk_report(app, tmp_path)
    login_as("admin")
    z = client.get(f"/executions/{ex_id}/allure.zip")
    assert z.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(z.data)).namelist()
    assert "index.html" in names
    assert "manifest.json" in names
    assert any(n.startswith("widgets/") or n.startswith("data/")
               or n.endswith(".js") for n in names)   # multi-file


def test_traversal_rejected(client, login_as, app, tmp_path):
    ex_id = _mk_report(app, tmp_path)
    login_as("admin")
    r = client.get(f"/executions/{ex_id}/allure/../../settings.py")
    assert r.status_code in (400, 404)


def test_project_scope_gates_assets_and_zip(client, login_as, app,
                                            tmp_path):
    # an execution in a project the tester cannot reach is denied on
    # every serving surface — project_allowed() is the gate
    ex_id = _mk_report(app, tmp_path)
    with app.app_context():
        import json as _j
        from testorchestrator.models import User
        ex = db.session.get(Execution, ex_id)
        ex.project_id = "denied-project"
        u = User.query.filter_by(username="tester").first()
        u.allowed_projects = _j.dumps(["other-project"])
        db.session.commit()
    login_as("tester")
    assert client.get(f"/executions/{ex_id}/allure/").status_code \
        in (403, 404)
    assert client.get(f"/executions/{ex_id}/allure.zip").status_code \
        in (403, 404)


def test_open_link_targets_blank(client, login_as, app, tmp_path):
    ex_id = _mk_report(app, tmp_path)
    login_as("admin")
    body = client.get(f"/executions/{ex_id}").data.decode()
    assert 'target="_blank"' in body and 'rel="noopener' in body
