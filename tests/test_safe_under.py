"""Regression: safe_under path containment — the released prefix check
(str(root) + "/") never matches on Windows ('D:\\...' vs 'D:\\.../'),
so EVERY report-serving route 404'd on installed Windows packages.
These tests pin the cross-platform containment semantics."""
import os
import zipfile, io

import pytest

from testorchestrator import reporting
from testorchestrator.extensions import db
from testorchestrator.execution import Execution, TestResult


# ---------- pure containment semantics ----------

def test_valid_nested_path_resolves(tmp_path):
    root = tmp_path / "ev" / "ex1"; (root / "allure").mkdir(parents=True)
    f = root / "allure" / "app.js"; f.write_text("x")
    assert reporting.safe_under(root, "allure/app.js") == f.resolve()


def test_valid_path_need_not_exist_yet(tmp_path):
    root = tmp_path / "ev" / "ex1"; root.mkdir(parents=True)
    got = reporting.safe_under(root, "allure/future.js")
    assert got == (root / "allure" / "future.js").resolve()


@pytest.mark.parametrize("rel", [
    "..", "../escape.txt", "allure/../../escape.txt",
    "sub/../../outside/x",
])
def test_dotdot_traversal_rejected(tmp_path, rel):
    root = tmp_path / "ev" / "ex1"; root.mkdir(parents=True)
    with pytest.raises(ValueError):
        reporting.safe_under(root, rel)


def test_absolute_path_escape_rejected(tmp_path):
    root = tmp_path / "ev" / "ex1"; root.mkdir(parents=True)
    outside = (tmp_path / "outside" / "x.txt").resolve()
    with pytest.raises(ValueError):
        reporting.safe_under(root, str(outside))


def test_sibling_prefix_escape_rejected(tmp_path):
    """/ev/ex11 shares a string prefix with /ev/ex1 — containment must
    be component-wise, not a string startswith."""
    root = tmp_path / "ev" / "ex1"; root.mkdir(parents=True)
    sib = tmp_path / "ev" / "ex11"; (sib / "f.txt").mkdir(parents=True)
    with pytest.raises(ValueError):
        reporting.safe_under(root, "../ex11/f.txt")


def test_root_itself_rejected(tmp_path):
    root = tmp_path / "ev" / "ex1"; root.mkdir(parents=True)
    for rel in ("", "."):
        with pytest.raises(ValueError):
            reporting.safe_under(root, rel)


def test_symlink_escape_rejected(tmp_path):
    root = tmp_path / "ev" / "ex1"; (root / "allure").mkdir(parents=True)
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "secret.txt").write_text("x")
    try:
        os.symlink(outside, root / "allure" / "link")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable on this platform")
    with pytest.raises(ValueError):
        reporting.safe_under(root, "allure/link/secret.txt")


# ---------- route-level: authenticated serving through safe_under --
# Hand-built report trees (no generator needed) prove the served path
# itself resolves on every supported OS.

@pytest.fixture()
def served_exec(app, tmp_path):
    def _mk(ex_id="srvx", with_results=True):
        with app.app_context():
            rd = tmp_path / "ev" / ex_id
            (rd / "kre").mkdir(parents=True)
            (rd / "kre" / "report.html").write_text("<html>native</html>")
            (rd / "kre" / "shot.png").write_bytes(b"\x89PNG")
            (rd / "allure" / "data").mkdir(parents=True)
            (rd / "allure" / "index.html").write_text("<html>allure</html>")
            (rd / "allure" / "app.js").write_text("console.log(1)")
            (rd / "allure" / "data" / "r.json").write_text("{}")
            ex = Execution(id=ex_id, project_id="p1", created_by="t",
                           report_dir=str(rd),
                           report_status="ready",
                           report_path="kre/report.html",
                           allure_status="ready",
                           allure_path="allure/index.html")
            db.session.add(ex)
            if with_results:
                db.session.add(TestResult(
                    execution_id=ex_id, case_ref="C.tc",
                    case_name="login", classname="C.tc",
                    status="passed", duration_s=1.0))
            db.session.commit()
            return ex_id
    return _mk


def test_native_report_served_authenticated(client, login_as, served_exec):
    served_exec = served_exec()
    login_as("admin")
    r = client.get(f"/executions/{served_exec}/report")
    assert r.status_code == 200 and b"native" in r.data
    a = client.get(f"/executions/{served_exec}/report/files/shot.png")
    assert a.status_code == 200


def test_allure_report_and_assets_served(client, login_as, served_exec):
    served_exec = served_exec()
    login_as("admin")
    assert client.get(f"/executions/{served_exec}/allure").status_code == 302
    idx = client.get(f"/executions/{served_exec}/allure/")
    assert idx.status_code == 200 and b"allure" in idx.data
    for rel in ("app.js", "data/r.json"):
        r = client.get(f"/executions/{served_exec}/allure/{rel}")
        assert r.status_code == 200, rel


def test_allure_zip_served(client, login_as, served_exec):
    served_exec = served_exec()
    login_as("admin")
    z = client.get(f"/executions/{served_exec}/allure.zip")
    assert z.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(z.data)).namelist()
    assert {"index.html", "app.js", "data/r.json"} <= set(names)


def test_anonymous_report_requests_redirect(client, served_exec):
    served_exec = served_exec()
    for url in (f"/executions/{served_exec}/report",
                f"/executions/{served_exec}/allure/",
                f"/executions/{served_exec}/allure/app.js",
                f"/executions/{served_exec}/allure.zip"):
        r = client.get(url)
        assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_route_traversal_rejected(client, login_as, served_exec):
    served_exec = served_exec()
    login_as("admin")
    r = client.get(f"/executions/{served_exec}/report/files/../../x")
    assert r.status_code == 404


# ---------- failed-regeneration preservation (second defect) --------
# A failed regen set allure_status=failed while view/assets/zip gated
# on status=="ready" — the historical report became unservable.

def test_failed_regen_preserves_usable_report(client, login_as, app,
                                              served_exec, monkeypatch):
    """Generator failure → status failed, but the previous usable
    report keeps serving and the warning reaches a portal page."""
    ex_id = served_exec()
    from testorchestrator.adapters import allure as adapter
    monkeypatch.setattr(
        adapter, "run_generator",
        lambda *a, **k: {"ok": False, "error": "generator exploded"})
    login_as("admin")
    r = client.post(f"/executions/{ex_id}/allure/generate",
                    follow_redirects=True)
    assert r.status_code == 200 and b"generator exploded" in r.data
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        assert ex.allure_status == "failed"
        assert ex.allure_path == "allure/index.html"
    idx = client.get(f"/executions/{ex_id}/allure/")
    assert idx.status_code == 200 and b"allure" in idx.data
    # send_file can't render the flash — it lands on the next portal page
    page = client.get(f"/executions/{ex_id}")
    assert b"previous usable" in page.data
    # assets + zip still served on failed status
    assert client.get(
        f"/executions/{ex_id}/allure/app.js").status_code == 200
    assert client.get(
        f"/executions/{ex_id}/allure.zip").status_code == 200


def test_missing_inputs_do_not_downgrade_usable_report(
        client, login_as, app, served_exec):
    """No canonical results to regenerate from: the existing report
    must not be marked missing or hidden."""
    ex_id = served_exec(with_results=False)
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


def test_failed_status_requires_valid_contained_files(
        client, login_as, app, served_exec, tmp_path):
    """status=failed alone is not trusted — a missing or escaping
    report path still gets 404, never a file outside the root."""
    ex_id = served_exec()
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        ex.allure_status = "failed"
        ex.allure_path = "allure/deleted.html"   # status ok, file gone
        db.session.commit()
    login_as("admin")
    assert client.get(f"/executions/{ex_id}/allure/").status_code == 404
    assert client.get(f"/executions/{ex_id}/allure.zip").status_code == 404
    with app.app_context():
        ex = db.session.get(Execution, ex_id)
        ex.allure_path = "../outside/evil.html"   # traversal in metadata
        db.session.commit()
    assert client.get(f"/executions/{ex_id}/allure/").status_code == 404
    assert client.get(
        f"/executions/{ex_id}/allure/../../x").status_code == 404
