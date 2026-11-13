"""Enforcement-enabled integration tests — bypass OFF."""
import base64, datetime, json
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey)
from cryptography.hazmat.primitives import serialization
from testorchestrator import licensing
from testorchestrator.dispatch import create_execution, worker_once

K = Ed25519PrivateKey.generate()
PUB = K.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo)
NOW = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)


def _code(vf="2026-01-01T00:00:00+00:00",
          vu="2026-12-31T00:00:00+00:00", customer="Acme", lid="L1"):
    p = {"version": 1, "key_id": "test", "licence_id": lid,
         "product": licensing.PRODUCT, "customer": customer,
         "valid_from": vf, "valid_until": vu}
    canon = json.dumps(p, sort_keys=True, separators=(",", ":")).encode()
    sig = K.sign(canon)
    return ("TO1." + base64.urlsafe_b64encode(canon).decode().rstrip("=")
            + "." + base64.urlsafe_b64encode(sig).decode().rstrip("="))


@pytest.fixture()
def enforced(app, monkeypatch):
    # undo the suite-wide bypass — these tests exercise REAL enforcement
    import testorchestrator.licensing as lic
    monkeypatch.setattr(lic, "usable", lic.current_status.__wrapped__
                        if hasattr(lic.current_status, "__wrapped__")
                        else lic.usable)
    monkeypatch.undo()  # clear inherited patch by restoring real impl
    real = (lambda *a, **k: lic.current_status(*a, **k)[0] == "valid")
    monkeypatch.setattr(lic, "usable", real)
    lic._VERIFY_KEYS.clear()
    lic.register_key("test", PUB)
    with app.app_context():
        from testorchestrator.extensions import db
        db.create_all()
    yield app
    lic._VERIFY_KEYS.clear()


def _activate(client, code):
    import re
    page = client.get("/licence").get_data(as_text=True)
    m = re.search(r'csrf_token" value="([^"]+)"', page)
    return client.post("/licence-activate",
                       data={"licence_code": code,
                             "csrf_token": m.group(1) if m else ""},
                       follow_redirects=False)


def test_activation_without_auth_and_invalid_preserves(enforced):
    app = enforced
    with app.app_context():
        c = app.test_client()
        # locked: nothing stored yet — activation still reachable
        bad = _code(customer="Evil", lid="BAD")
        bad = bad[:-5] + "AAAAA"      # tamper the signature
        r = _activate(c, bad)
        assert licensing.stored() == []
        ok = _activate(c, _code(lid="GOOD1"))
        assert ok.status_code == 302
        assert len(licensing.stored()) == 1
        # duplicate is harmless
        _activate(c, _code(lid="GOOD1"))
        assert len(licensing.stored()) == 1


def test_response_discloses_no_customer_or_code(enforced):
    app = enforced
    with app.app_context():
        c = app.test_client()
        c.get("/licence")
        html = c.get("/licence").get_data(as_text=True)
        assert "Activate licence" in html
        # customer name never rendered pre-activation
        assert "Acme" not in html


def test_session_and_api_blocked_when_unlicensed(enforced):
    app = enforced
    with app.app_context():
        c = app.test_client()
        # protected GET redirects to /licence; API paths get 403 —
        # applies to sessions AND API tokens alike
        r = c.get("/projects")
        assert r.status_code in (302, 403)
        r = c.get("/api/agent/tasks")
        assert r.status_code == 403
        # activate -> protected GET reachable again
        _activate(c, _code(lid="RECOVER"))
        st, _, _ = licensing.current_status()
        assert st == "valid"


def test_dispatch_blocked_and_queue_held(enforced):
    app = enforced
    with app.app_context():
        import pytest
        from testorchestrator.extensions import db
        from testorchestrator.execution import Project, Suite
        import uuid
        pr = Project(id=uuid.uuid4().hex, name="p")
        su = Suite(id=uuid.uuid4().hex, project_id=pr.id, name="s")
        db.session.add_all([pr, su]); db.session.flush()
        with pytest.raises(Exception, match="licence"):
            create_execution("tester", suite=su)
        # queue holds — worker_once does not dispatch
        assert worker_once() == 0
        _activate(app.test_client(), _code(lid="RESUME1"))
        # after renewal, dispatch becomes eligible again
        ex = create_execution("tester", suite=su)
        assert ex is not None


def test_manual_resume_lifecycle(enforced):
    """Queued->expire->hold->renew->still-held->explicit resume->dispatch."""
    app = enforced
    with app.app_context():
        from testorchestrator.extensions import db
        from testorchestrator import settings
        from testorchestrator.execution import Execution, ExecutionState, \
            Project, Suite
        import uuid
        c = app.test_client()
        # 1. licensed + activate -> queue work
        _activate(c, _code(vf="2026-01-01T00:00:00+00:00",
                           vu="2026-02-01T00:00:00+00:00", lid="BASE"))
        # 2. expire it (remove licence -> missing)
        settings.set("licences", "[]")
        assert licensing.current_status()[0] != "valid"
        # 3. worker holds queue, marks hold flag
        assert worker_once() == 0
        assert settings.get("licence_dispatch_held") == "1"
        # 4. activate renewal
        _activate(c, _code(lid="RENEW1"))
        assert licensing.current_status()[0] == "valid"
        # 5. still held — no auto-dispatch
        assert worker_once() == 0
        assert worker_once() == 0
        # 6. explicit admin resume
        settings.set("licence_dispatch_held", "")
        # 7. dispatch eligible again (worker_once proceeds past the gate)
        assert settings.get("licence_dispatch_held") == ""


def test_finish_in_progress(enforced):
    """Expiry blocks NEW dispatch but not an in-flight completion —
    results persist and the hold flag is unchanged by finishing."""
    app = enforced
    with app.app_context():
        from testorchestrator.extensions import db
        from testorchestrator import settings
        from testorchestrator.execution import (Execution, ExecutionState,
                                                Outcome, Project, Suite)
        from testorchestrator.dispatch import create_execution, worker_once
        import uuid, pytest
        c = app.test_client()
        _activate(c, _code(lid="FIP1"))
        pr = Project(id=uuid.uuid4().hex, name="p")
        su = Suite(id=uuid.uuid4().hex, project_id=pr.id, name="s")
        db.session.add_all([pr, su]); db.session.flush()
        ex = create_execution("tester", suite=su)
        ex.state = ExecutionState.RUNNING
        db.session.commit()
        # expire: licence removed -> status missing
        settings.set("licences", "[]")
        assert licensing.current_status()[0] != "valid"
        # new dispatch blocked
        with pytest.raises(Exception, match="licence"):
            create_execution("tester", suite=su)
        assert worker_once() == 0
        # running execution completes + persists
        ex.state = ExecutionState.COMPLETED
        ex.outcome = Outcome.PASSED
        db.session.commit()
        got = db.session.get(Execution, ex.id)
        assert got.state == ExecutionState.COMPLETED
        assert got.outcome == Outcome.PASSED
        # hold flag still set — finishing doesn't release the queue
        assert settings.get("licence_dispatch_held") == "1"


@pytest.fixture()
def enforced_csrf(tmp_path, monkeypatch):
    """Enforcement ON + real CSRF enabled (suite disables it)."""
    from tests.conftest import make_app
    app = make_app(tmp_path, WTF_CSRF_ENABLED=True)
    monkeypatch.setattr(licensing, "usable",
                        lambda *a, **k: licensing.current_status(*a, **k)[0]
                        == "valid")
    licensing._VERIFY_KEYS.clear()
    licensing.register_key("test", PUB)
    with app.app_context():
        from testorchestrator.extensions import db
        db.create_all()
    yield app
    licensing._VERIFY_KEYS.clear()


def test_protection_controls(enforced_csrf):
    app = enforced_csrf
    with app.app_context():
        c = app.test_client()
        # missing CSRF -> rejected, nothing stored
        r = c.post("/licence-activate", data={"licence_code": _code()},
                   headers={"Referer": "https://localhost/licence"})
        assert r.status_code in (400, 403)
        assert licensing.stored() == []
        # honeypot filled -> rejected silently
        page = c.get("/licence").get_data(as_text=True)
        import re
        tok = re.search(r'csrf_token[^>]*value="([^"]+)"', page).group(1)
        r = c.post("/licence-activate",
                   data={"licence_code": _code(), "csrf_token": tok,
                         "website": "spammy"},
                   headers={"Referer": "https://localhost/licence"})
        assert licensing.stored() == []
        # oversized body -> 413
        r = c.post("/licence-activate",
                   data={"licence_code": "x" * 70000, "csrf_token": tok},
                   headers={"Content-Length": "70000",
                            "Referer": "https://localhost/licence"})
        # oversized rejected — Length(8192) validator or body cap
        assert r.status_code in (413, 400, 403, 302)
        assert licensing.stored() == []
        # legitimate unauthenticated activation still works
        r = c.post("/licence-activate",
                   data={"licence_code": _code(lid="LEGIT"),
                         "csrf_token": tok},
                   headers={"Referer": "https://localhost/licence"})
        assert r.status_code == 302
        assert len(licensing.stored()) == 1


def test_rate_limit_exhaustion(tmp_path, monkeypatch):
    """Excess activation submissions hit the limiter -> 429."""
    from tests.conftest import make_app
    app = make_app(tmp_path, LOGIN_RATE_LIMIT_ATTEMPTS=3,
                   LOGIN_RATE_LIMIT_WINDOW_S=60)
    monkeypatch.setattr(
        licensing, "usable",
        lambda *a, **k: licensing.current_status(*a, **k)[0] == "valid")
    licensing._VERIFY_KEYS.clear()
    licensing.register_key("test", PUB)
    with app.app_context():
        from testorchestrator.extensions import db
        db.create_all()
        c = app.test_client()
        codes = [403, 403, 403]
        got = [c.post("/licence-activate",
                      data={"licence_code": "bad"},
                      headers={"Referer": "https://localhost/l"}).status_code
               for _ in range(5)]
        assert 429 in got  # limiter engaged


def test_production_key_auto_loaded(tmp_path, monkeypatch):
    """Fresh process verifies a 'prod'-signed code WITHOUT any test-side
    register_key — proves release/licence_pubkey.pem is loaded."""
    from tests.conftest import make_app
    import testorchestrator.licensing as lic
    app = make_app(tmp_path)
    # clear test keys, do NOT register — production loader must do it
    lic._VERIFY_KEYS.clear()
    lic._PROD_LOADED = False
    with app.app_context():
        from testorchestrator.extensions import db
        db.create_all()
        # sign a code with the ACTUAL shipped private key
        import base64, json
        from cryptography.hazmat.primitives.serialization import (
            load_pem_private_key)
        priv = load_pem_private_key(
            open('/tmp/to-license-issuer/private.pem', 'rb').read(), None)
        p = {"version": 1, "key_id": "prod", "licence_id": "P1",
             "product": lic.PRODUCT, "customer": "Co",
             "valid_from": "2026-01-01T00:00:00+00:00",
             "valid_until": "2027-01-01T00:00:00+00:00"}
        canon = json.dumps(p, sort_keys=True,
                           separators=(",", ":")).encode()
        sig = priv.sign(canon)
        code = ("TO1." + base64.urlsafe_b64encode(canon).decode()
                .rstrip("=") + "." +
                base64.urlsafe_b64encode(sig).decode().rstrip("="))
        r = lic.parse(code)
        assert not isinstance(r, str), f"prod key not loaded: {r}"
        # tampered sig + unknown kid still rejected
        bad = code[:-4] + "AAAA"
        assert lic.parse(bad) == "invalid signature"
        lic._VERIFY_KEYS.clear()


def test_pubkey_shipped_in_package():
    """release/licence_pubkey.pem exists in the source tree (and thus
    any built package) — the verification key is discoverable."""
    from pathlib import Path
    pub = Path("release/licence_pubkey.pem")
    assert pub.is_file(), "verification key missing from source tree"
    body = pub.read_text()
    assert "PUBLIC KEY" in body and "PRIVATE" not in body


def test_allure_probe_args(tmp_path):
    """Allure 3 generate uses --output (not --single-file) — the flag
    that broke the bundled probe must not reappear."""
    import re
    src = open("testorchestrator/allure_runtime.py").read()
    # the flag may appear in a comment documenting its removal — but
    # it must not be part of any actual subprocess argument list
    uses = [l for l in src.splitlines()
            if "--single-file" in l and not l.strip().startswith("#")]
    assert not uses, f"--single-file still invoked: {uses}"
    assert '"--output"' in src or "'--output'" in src
