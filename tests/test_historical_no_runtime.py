"""Installed-package historical-serving check: with the bundled
Allure/Node generators physically absent from the deployment tree,
authenticated report viewing, assets, and downloads must still work,
startup must succeed, and regeneration must fail with an explicit
unavailable-generator message — never break historical access.

Runs green on CI checkouts (3rdparty runtimes are provisioned at
package-build time, never committed) and locally with the runtime
dirs moved aside."""
import io
import zipfile

import pytest

from testorchestrator import allure_runtime
from testorchestrator.extensions import db
from testorchestrator.execution import Execution, TestResult


@pytest.fixture()
def historical_exec(app, tmp_path):
    """Sanitised hand-made report tree — no generator involved."""
    def _seed(ex_id, with_results):
        rd = tmp_path / "ev" / ex_id
        (rd / "kre").mkdir(parents=True)
        (rd / "kre" / "report.html").write_text(
            "<html>native KRE</html>")
        (rd / "kre" / "screen.png").write_bytes(b"\x89PNG\x00")
        (rd / "allure" / "data" / "test-cases").mkdir(parents=True)
        (rd / "allure" / "index.html").write_text(
            "<html><body>allure app</body>"
            "<script src='./app.js'></script></html>")
        (rd / "allure" / "app.js").write_text("console.log(1)")
        (rd / "allure" / "data" / "test-cases" / "t1.json"
         ).write_text('{"name":"login","status":"passed"}')
        ex = Execution(id=ex_id, project_id="p1", created_by="t",
                       report_dir=str(rd),
                       report_status="ready",
                       report_path="kre/report.html",
                       allure_status="ready",
                       allure_path="allure/index.html",
                       allure_version="3.x")
        db.session.add(ex)
        if with_results:
            db.session.add(TestResult(
                execution_id=ex_id, case_ref="C.tc",
                case_name="login", classname="C.tc",
                status="passed", duration_s=1.0))
        db.session.commit()
        return ex_id

    def _mk(ex_id, with_results=True):
        with app.app_context():
            return _seed(ex_id, with_results)

    return _mk


def _skip_if_generator_present():
    """Absence-specific checks only apply where the bundled runtime
    is physically excluded (CI checkout / stripped package)."""
    if allure_runtime._allure_cmd() is not None:
        pytest.skip("bundled generators provisioned — "
                    "absence-specific check")


def test_generator_absent_status(app, historical_exec):
    """Runtime check reports the generator as unavailable — the state
    a stripped package ships in."""
    _skip_if_generator_present()
    historical_exec("h0")
    st = allure_runtime.status()
    assert st["ready"] is False
    assert "not installed" in st["reason"]


def test_historical_allure_view_assets_zip(client, login_as,
                                           historical_exec):
    ex_id = historical_exec("h1")
    login_as("admin")
    assert client.get(f"/executions/{ex_id}/allure").status_code == 302
    idx = client.get(f"/executions/{ex_id}/allure/")
    assert idx.status_code == 200 and b"allure" in idx.data
    for rel in ("app.js", "data/test-cases/t1.json"):
        r = client.get(f"/executions/{ex_id}/allure/{rel}")
        assert r.status_code == 200, rel
    z = client.get(f"/executions/{ex_id}/allure.zip")
    assert z.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(z.data)).namelist()
    assert {"index.html", "app.js",
            "data/test-cases/t1.json"} <= set(names)


def test_native_report_still_served(client, login_as, historical_exec):
    ex_id = historical_exec("h2")
    login_as("admin")
    r = client.get(f"/executions/{ex_id}/report")
    assert r.status_code == 200 and b"native" in r.data
    a = client.get(f"/executions/{ex_id}/report/files/screen.png")
    assert a.status_code == 200


def test_regeneration_reports_unavailable_preserves_history(
        client, login_as, app, historical_exec):
    """Attempted regeneration surfaces the explicit unavailable-
    generator message; the failed status keeps the previous usable
    report servable with a warning."""
    _skip_if_generator_present()
    ex_id = historical_exec("h3")
    login_as("admin")
    r = client.post(f"/executions/{ex_id}/allure/generate",
                    follow_redirects=True)
    assert r.status_code == 200
    assert b"not installed" in r.data or b"unavailable" in r.data
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert ex.allure_status == "failed"       # honest last-gen state
        assert ex.allure_path == "allure/index.html"  # report preserved
    # historical report still served; the failure warning flashes on
    # the next portal page render (send_file can't render flashes)
    idx = client.get(f"/executions/{ex_id}/allure/")
    assert idx.status_code == 200 and b"allure" in idx.data
    page = client.get(f"/executions/{ex_id}")
    assert b"previous usable" in page.data


def test_regen_without_results_preserves_history(
        client, login_as, app, historical_exec):
    """Regeneration when canonical results are gone must not downgrade
    or hide an existing historical report."""
    ex_id = historical_exec("h4", with_results=False)
    login_as("admin")
    r = client.post(f"/executions/{ex_id}/allure/generate",
                    follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert ex.allure_status == "ready"
        assert ex.allure_path == "allure/index.html"
    idx = client.get(f"/executions/{ex_id}/allure/")
    assert idx.status_code == 200 and b"allure" in idx.data


def test_anonymous_still_redirected(client, historical_exec):
    ex_id = historical_exec("h5")
    for url in (f"/executions/{ex_id}/allure/",
                f"/executions/{ex_id}/allure/app.js",
                f"/executions/{ex_id}/allure.zip",
                f"/executions/{ex_id}/report"):
        r = client.get(url)
        assert r.status_code == 302 and \
            "/login" in r.headers["Location"]
