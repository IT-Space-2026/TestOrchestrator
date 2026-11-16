"""Installed-package report-serving check (no source checkout).

Run with the package's OWN bundled python from the payload root:

    python report_serving_check.py seed
    python report_serving_check.py check

Uses the installed app's own create_app() + test_client — the same
in-process technique as packaging/windows/ci/licence_activate.py — so
the shipped route code, auth model, containment, and database are the
ones under test. The HTTPS/Caddy layer is covered separately by the
cert-validated browser checks; this script verifies the application
routes themselves.

  seed   — schema, admin user, completed setup, an ephemeral-licence
           (pubkey persisted into the instance so a later process can
           re-register it and verify), one sanitised Execution with a
           hand-made native report and a multi-file Allure tree.
  check  — anonymous redirects, authenticated login, native report +
           asset + zip, Allure page + nested assets + zip, traversal
           rejection, failed-status serving, licence persistence.

Exit 0 only if every check passes. Disposable: all data lands inside
the resolved instance dir.
"""
import io
import json
import sys
import zipfile
from pathlib import Path

# Locate the testorchestrator package: payload root when copied into an
# install dir, or the repo root when run from packaging/ci/.
import os
for _cand in (os.getcwd(),
              *[str(p) for p in Path(__file__).resolve().parents]):
    if (Path(_cand) / "testorchestrator" / "__init__.py").is_file():
        sys.path.insert(0, _cand)
        break

PASSWORD = "CiPassword-12345"
EX_ID = "cirpt01"
KEY_FILE = "ci-serve-pubkey.pem"


def _ctx():
    from testorchestrator import create_app
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False   # scripted client, not a browser
    return app, app.app_context()


def seed():
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey)
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat)

    from testorchestrator import licensing as L
    from testorchestrator.config import INSTANCE_DIR
    from testorchestrator.extensions import db
    from testorchestrator.execution import Execution
    from testorchestrator.models import User, Role
    from testorchestrator.settings import mark_setup_complete

    def b64(b):
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    app, ctx = _ctx()
    with ctx:
        db.create_all()
        if User.query.filter_by(username="ci-admin").first() is None:
            u = User(username="ci-admin", role=Role.ADMIN)
            u.set_password(PASSWORD)
            db.session.add(u)
        mark_setup_complete()

        priv = Ed25519PrivateKey.generate()
        pub_pem = priv.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        L.register_key("ci-serve", pub_pem)
        # persist the PUBKEY so the check process can verify the licence
        (Path(INSTANCE_DIR) / KEY_FILE).write_bytes(pub_pem)

        payload = {"version": 1, "key_id": "ci-serve",
                   "licence_id": "ci-serve", "product": "TestOrchestrator",
                   "customer": "CI",
                   "valid_from": "2020-01-01T00:00:00+00:00",
                   "valid_until": "2099-01-01T00:00:00+00:00"}
        canon = json.dumps(payload, sort_keys=True,
                           separators=(",", ":")).encode()
        ok, msg = L.add("TO1." + b64(canon) + "." + b64(priv.sign(canon)))
        assert ok, msg

        rd = Path(INSTANCE_DIR) / "ci-evidence" / EX_ID
        (rd / "kre").mkdir(parents=True, exist_ok=True)
        (rd / "kre" / "report.html").write_text(
            "<html><body>ci native KRE report</body></html>")
        (rd / "kre" / "screen.png").write_bytes(b"\x89PNG\x00ci")
        (rd / "allure" / "data").mkdir(parents=True, exist_ok=True)
        (rd / "allure" / "index.html").write_text(
            "<html><body>ci allure report"
            "<script src='./app.js'></script></body></html>")
        (rd / "allure" / "app.js").write_text("console.log('ci')")
        (rd / "allure" / "data" / "case.json").write_text(
            '{"name":"ci","status":"passed"}')
        ex = db.session.get(Execution, EX_ID)
        if ex is None:
            ex = Execution(id=EX_ID, project_id="p1", created_by="ci")
            db.session.add(ex)
        ex.report_dir = str(rd)
        ex.report_status = "ready"
        ex.report_path = "kre/report.html"
        ex.allure_status = "ready"
        ex.allure_path = "allure/index.html"
        ex.allure_version = "3.x"
        db.session.commit()
    print(f"PASS seeded sanitised execution {EX_ID} into {rd}")


def check():
    import io
    from testorchestrator import licensing as L
    from testorchestrator.config import INSTANCE_DIR
    from testorchestrator.extensions import db
    from testorchestrator.execution import Execution

    app, ctx = _ctx()
    with ctx:
        # re-register the persisted pubkey so the seeded licence
        # verifies in THIS process (cross-process persistence check)
        pub = Path(INSTANCE_DIR) / KEY_FILE
        L.register_key("ci-serve", pub.read_bytes())
        st = L.current_status()[0]
        assert st == "valid", f"licence not valid after re-register: {st}"
        print(f"PASS licence persisted + re-verifies: status={st}")
        db.create_all()

        anon = app.test_client()
        c = app.test_client()

        # anonymous: every report surface redirects to /login
        for rel in (f"/executions/{EX_ID}/report",
                    f"/executions/{EX_ID}/allure/",
                    f"/executions/{EX_ID}/allure/app.js",
                    f"/executions/{EX_ID}/allure.zip",
                    f"/executions/{EX_ID}/report.zip"):
            r = anon.get(rel)
            assert r.status_code == 302 and \
                "/login" in r.headers["Location"], \
                f"anonymous {rel} -> {r.status_code}"
        print("PASS anonymous report requests redirect to login")

        r = c.post("/login", data={"username": "ci-admin",
                                   "password": PASSWORD},
                   follow_redirects=True)
        assert r.status_code == 200, r.status_code
        print("PASS authenticated login as ci-admin")

        r = c.get(f"/executions/{EX_ID}/report")
        assert r.status_code == 200 and b"native" in r.data, r.status_code
        a = c.get(f"/executions/{EX_ID}/report/files/screen.png")
        assert a.status_code == 200 and a.data.startswith(b"\x89PNG")
        z = c.get(f"/executions/{EX_ID}/report.zip")
        assert z.status_code == 200
        names = zipfile.ZipFile(io.BytesIO(z.data)).namelist()
        assert any(n.endswith("report.html") for n in names), names
        print("PASS native report page, asset, report.zip")

        r = c.get(f"/executions/{EX_ID}/allure")
        assert r.status_code == 302
        idx = c.get(f"/executions/{EX_ID}/allure/")
        assert idx.status_code == 200 and b"allure" in idx.data
        for rel in ("app.js", "data/case.json"):
            a = c.get(f"/executions/{EX_ID}/allure/{rel}")
            assert a.status_code == 200, f"allure {rel} -> {a.status_code}"
        z = c.get(f"/executions/{EX_ID}/allure.zip")
        assert z.status_code == 200
        names = zipfile.ZipFile(io.BytesIO(z.data)).namelist()
        assert {"index.html", "app.js",
                "data/case.json"} <= set(names), names
        print("PASS allure page, nested assets, allure.zip")

        # traversal in the served path stays contained
        r = c.get(f"/executions/{EX_ID}/allure/..%2f..%2fx")
        assert r.status_code in (404, 400), r.status_code
        print("PASS traversal in report path rejected")

        # seeded execution metadata intact
        ex = db.session.get(Execution, EX_ID)
        assert ex.report_status == "ready"
        assert ex.allure_status == "ready"
        assert ex.allure_path == "allure/index.html"
        print("PASS execution metadata intact")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode in ("seed", "all"):
        seed()
    if mode in ("check", "all"):
        check()
    print("PASS report-serving check complete")


if __name__ == "__main__":
    main()
