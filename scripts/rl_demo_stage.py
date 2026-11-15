"""reporting-labs demo staging — SYNTHETIC FIXTURE evidence only.

Spins up a disposable TestOrchestrator instance (mkdtemp instance dir +
sqlite), seeds a sanitised execution with mixed outcomes + evidence,
generates the Allure and reporting-labs reports, serves the portal, and
captures screenshots + rendered-DOM security checks via headless
Chromium (Playwright).

These screenshots are fixture demonstrations of the portal/report UX —
NOT real-KRE acceptance evidence.

Usage:  .venv/bin/python scripts/rl_demo_stage.py [shot_dir]
Requires: playwright (dev install) + `playwright install chromium`.
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SHOT_DIR = Path(sys.argv[1] if len(sys.argv) > 1
                else tempfile.mkdtemp(prefix="rl-demo-shots."))
BASE = "http://127.0.0.1:8899"
ADMIN_PASS = "D3m0-Str0ng-Pass!"


def build_app(root: Path):
    os.environ["TO_INSTANCE_DIR"] = str(root / "inst")
    os.environ["TO_DATABASE_URI"] = f"sqlite:///{root / 'm.db'}"
    os.environ["TO_SECURE_COOKIES"] = "0"
    from testorchestrator import create_app
    return create_app()


def seed(app, root: Path) -> str:
    """Execution with passed/failed/skipped/unresolved results, a
    case-matched screenshot, unmatched evidence, and a run log."""
    from testorchestrator import dispatch
    from testorchestrator.execution import (Artifact, Execution,
                                            ExecutionState, Outcome,
                                            Project, Suite, SuiteEntry,
                                            TestResult)
    from testorchestrator.extensions import db
    from testorchestrator.models import Role, User
    from testorchestrator.results import sha256_file

    with app.app_context():
        # demo licence: a TEST-key-signed code registered only inside
        # this disposable app (never the shipped 'prod' key).
        import base64, json as _j
        from cryptography.hazmat.primitives.asymmetric.ed25519 import \
            Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        from testorchestrator import licensing
        k = Ed25519PrivateKey.generate()
        licensing.register_key("test", k.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo))
        p = {"version": 1, "key_id": "test", "licence_id": "DEMO-1",
             "product": licensing.PRODUCT, "customer": "Demo (fixture)",
             "valid_from": "2026-01-01T00:00:00+00:00",
             "valid_until": "2027-12-31T00:00:00+00:00"}
        canon = _j.dumps(p, sort_keys=True,
                         separators=(",", ":")).encode()
        sig = k.sign(canon)
        code = ("TO1." + base64.urlsafe_b64encode(canon).decode()
                .rstrip("=") + "." +
                base64.urlsafe_b64encode(sig).decode().rstrip("="))
        ok, msg = licensing.add(code)
        print("licence:", ok, msg)

        admin = User(username="admin", role=Role.ADMIN)
        admin.set_password(ADMIN_PASS)
        admin.must_change_password = False
        db.session.add(admin)
        p = Project(name="WebStore"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="Checkout suite")
        db.session.add(s); db.session.flush()
        db.session.add(SuiteEntry(suite_id=s.id, order=0,
                                  case_ref="Checkout"))
        ex = Execution(id="demo00000000000000000000000000a1",
                       project_id=p.id, created_by="admin",
                       suite_id=s.id)
        db.session.add(ex)
        ex.state = ExecutionState.COMPLETED
        ex.outcome = Outcome.FAILED
        rd = root / "ev" / ex.id
        rd.mkdir(parents=True)
        ex.report_dir = str(rd)

        cases = [
            ("Checkout", "adds item to cart", "passed", False, 2.1, ""),
            ("Checkout", "applies valid coupon", "passed", False, 1.4, ""),
            ("Checkout", "rejects expired coupon", "failed", False, 3.2,
             "expected 400, got 200 — coupon endpoint ignored expiry"),
            ("Checkout", "charges card via PSP", "error", False, 0.9,
             "ConnectionResetError: psp sandbox unreachable"),
            ("Checkout", "guest checkout blocked", "skipped", False, 0.0,
             "feature flag off in this environment"),
            ("Checkout", "<img src=x onerror=window.__pwned=1>",
             "failed", False, 1.1,
             "assertion failed — hostile name fixture"),
            ("Checkout", "loyalty points applied", "failed", True, 1.8,
             "producer totals (7) != parsed cases (6) — "
             "discrepant, kept unresolved"),
        ]
        for cls, name, status, unresolved, dur, msg in cases:
            db.session.add(TestResult(
                execution_id=ex.id, case_ref=cls, case_name=name,
                classname=cls, status=status, duration_s=dur,
                message=msg, unresolved=unresolved))
        # case-matched screenshot (whole-token, unambiguous)
        shots = {
            "charges card via PSP.png": b"\x89PNG-fake-psp-fail",
            "stray-network-capture.har": b'{"log":{"entries":[]}}',
        }
        for rel, data in shots.items():
            f = rd / rel; f.write_bytes(data)
            kind = "har" if rel.endswith(".har") else "screenshot"
            db.session.add(Artifact(
                execution_id=ex.id, kind=kind, rel_path=rel,
                sha256=sha256_file(f), size=f.stat().st_size,
                mime="application/json" if kind == "har"
                else "image/png"))
        logf = rd / "logs"; logf.mkdir()
        (logf / "kre-execution.log").write_text(
            "INFO  suite started\nINFO  7 cases\n"
            "WARN  psp sandbox unreachable\nINFO  suite finished\n")
        lf = logf / "kre-execution.log"
        db.session.add(Artifact(
            execution_id=ex.id, kind="log",
            rel_path="logs/kre-execution.log",
            sha256=sha256_file(lf), size=lf.stat().st_size,
            mime="text/plain"))
        db.session.commit()
        return ex.id


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="rl-demo."))
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    app = build_app(root)
    from testorchestrator.extensions import db
    with app.app_context():
        db.create_all()
    ex_id = seed(app, root)

    with app.app_context():
        from testorchestrator.execution import Execution
        from testorchestrator.adapters import allure as allure_adapter
        from testorchestrator.adapters import reporting_labs as rl_adapter
        ex = db.session.get(Execution, ex_id)
        a = allure_adapter.generate_for(ex)
        print("allure:", a)
        r = rl_adapter.generate_for(ex)
        print("rlabs:", r)

    threading.Thread(target=lambda: app.run(
        "127.0.0.1", 8899, use_reloader=False), daemon=True).start()
    time.sleep(2)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(f"{BASE}/login")
        page.fill("input[name=username]", "admin")
        page.fill("input[name=password]", ADMIN_PASS)
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")

        page.goto(f"{BASE}/executions/{ex_id}")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=SHOT_DIR / "01-execution-detail.png",
                        full_page=True)

        page.goto(f"{BASE}/executions/{ex_id}/rlabs")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1200)
        page.screenshot(path=SHOT_DIR / "02-rlabs-report.png",
                        full_page=True)

        # run-level logs + unmatched-evidence entries in the report
        body = page.content()
        print("logs entry present:", "run-level logs" in body)
        print("unmatched entry present:", "unmatched" in body)

        # rendered-DOM injection check: hostile case name must not run
        pwned = page.evaluate("() => window.__pwned")
        print("injection executed:", pwned)

        # failed-regen state: previous report stays, failure surfaced
        with app.app_context():
            from testorchestrator.execution import Execution
            ex = db.session.get(Execution, ex_id)
            ex.rl_status = "failed"
            db.session.commit()
        page.goto(f"{BASE}/executions/{ex_id}/rlabs")
        assert page.url.endswith("/rlabs")
        page.goto(f"{BASE}/executions/{ex_id}")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=SHOT_DIR / "03-failed-regen-warning.png",
                        full_page=True)

        page.goto(f"{BASE}/system/reporting")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=SHOT_DIR / "04-admin-reporting.png",
                        full_page=True)
        browser.close()
    print("shots:", SHOT_DIR)


if __name__ == "__main__":
    main()
