"""Informational-licensing regression tests — suite-wide bypass OFF.

The application licence is informational only: it NEVER gates install,
login, dispatch, worker, import, report, admin, or API behaviour. The
single user-visible effect of a non-valid licence is the exact notice
``This version is not supported`` on the login page. Authentication,
roles, and project isolation stay fully enforced.

Signature/persistence machinery (parse, stored-row re-verification,
prod-key autoload) remains real and is exercised against the real
checks here.
"""
import base64, datetime, json
import re
import uuid

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
NOTICE = "This version is not supported"


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
def informational(app, monkeypatch):
    """Real licence status — no suite-wide usable() stub."""
    import testorchestrator.licensing as lic
    monkeypatch.undo()
    lic._VERIFY_KEYS.clear()
    lic.register_key("test", PUB)
    with app.app_context():
        from testorchestrator.extensions import db
        db.create_all()
    yield app
    lic._VERIFY_KEYS.clear()


def _activate(client, code):
    page = client.get("/licence").get_data(as_text=True)
    m = re.search(r'csrf_token" value="([^"]+)"', page)
    return client.post("/licence-activate",
                       data={"licence_code": code,
                             "csrf_token": m.group(1) if m else ""},
                       follow_redirects=False)


def _suite(db):
    from testorchestrator.execution import Project, Suite
    pr = Project(id=uuid.uuid4().hex, name="p")
    su = Suite(id=uuid.uuid4().hex, project_id=pr.id, name="s")
    db.session.add_all([pr, su])
    db.session.flush()
    return su


# ---------------------------------------------------------------------
# login-screen notice (exact text, non-blocking, no extra prompts)
# ---------------------------------------------------------------------

def test_missing_licence_shows_exact_notice(informational):
    c = informational.test_client()
    html = c.get("/login").get_data(as_text=True)
    assert NOTICE in html
    # the notice is the ONLY licence text — no activation prompt,
    # purchase link, licence details, or extra warning
    body = html.split(NOTICE, 1)
    assert "licence_code" not in html
    assert "Activate" not in html
    assert "Acme" not in html
    # login form still fully rendered + usable
    assert 'name="username"' in html and 'name="password"' in html


def test_valid_licence_suppresses_notice(informational):
    app = informational
    with app.app_context():
        c = app.test_client()
        _activate(c, _code(lid="GOOD"))
        assert licensing.current_status()[0] == "valid"
        html = c.get("/login").get_data(as_text=True)
        assert NOTICE not in html


def test_expired_licence_shows_notice_and_login_works(informational):
    app = informational
    with app.app_context():
        c = app.test_client()
        _activate(c, _code(vf="2020-01-01T00:00:00+00:00",
                           vu="2021-01-01T00:00:00+00:00", lid="OLD"))
        assert licensing.current_status()[0] == "expired"
        html = c.get("/login").get_data(as_text=True)
        assert NOTICE in html
        # authenticated login is not blocked by the expired licence
        r = c.post("/login", data={"username": "admin",
                                 "password": "wrong"},
                   follow_redirects=False)
        # wrong-password response is a form re-render, not a licence wall
        assert r.status_code == 200
        assert licensing.current_status()[0] == "expired"


def test_malformed_and_invalid_signature_non_blocking(informational):
    app = informational
    with app.app_context():
        c = app.test_client()
        # garbage stored state -> status invalid -> notice, no crash
        from testorchestrator import settings
        settings.set("licences", json.dumps([{"licence_id": "X",
                                              "customer": "Y",
                                              "valid_from": "2020-01-01",
                                              "valid_until": "2030-01-01",
                                              "key_id": "test",
                                              "code": "TO1.garbage.sig"}]))
        assert licensing.current_status()[0] != "valid"
        html = c.get("/login").get_data(as_text=True)
        assert NOTICE in html
        assert "garbage" not in html  # no stored content disclosure
        # tampered-signature code is rejected but activation stays
        # reachable
        bad = _code(customer="Evil", lid="BAD")[:-5] + "AAAAA"
        _activate(c, bad)
        assert licensing.current_status()[0] != "valid"
        assert NOTICE in c.get("/login").get_data(as_text=True)


def test_status_error_non_blocking_and_sanitised(informational,
                                                 monkeypatch):
    """A status-computation failure must not take the login page down
    or leak exception/licence internals."""
    monkeypatch.setattr(
        licensing, "current_status",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError(
            "SECRET diagnostic internals")))
    c = informational.test_client()
    r = c.get("/login")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert NOTICE in html
    assert "SECRET" not in html and "diagnostic" not in html


# ---------------------------------------------------------------------
# functionality is never licence-gated
# ---------------------------------------------------------------------

def test_dispatch_and_queue_never_held(informational):
    app = informational
    with app.app_context():
        from testorchestrator.extensions import db
        su = _suite(db)
        # missing licence: dispatch proceeds, no hold flag
        ex = create_execution("tester", suite=su)
        assert ex is not None
        from testorchestrator import settings
        worker_once()
        assert settings.get("licence_dispatch_held") != "1"
        # expired licence: same — never held
        from testorchestrator import licensing as lic
        settings.set("licences", json.dumps([{
            "licence_id": "E", "customer": "C",
            "valid_from": "2020-01-01T00:00:00+00:00",
            "valid_until": "2021-01-01T00:00:00+00:00",
            "key_id": "test",
            "code": _code(vf="2020-01-01T00:00:00+00:00",
                          vu="2021-01-01T00:00:00+00:00", lid="E")}]))
        assert lic.current_status()[0] == "expired"
        ex2 = create_execution("tester", suite=su)
        assert ex2 is not None
        worker_once()
        assert settings.get("licence_dispatch_held") != "1"


def test_protected_routes_auth_gated_not_licence_gated(informational):
    """Unauthenticated requests still hit AUTH controls; licence state
    never redirects to /licence or 403s."""
    c = informational.test_client()
    r = c.get("/projects")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert "/licence" not in r.headers["Location"]
    r = c.get("/licence")
    assert r.status_code == 200  # informational page stays reachable


def test_activation_still_available(informational):
    app = informational
    with app.app_context():
        c = app.test_client()
        ok = _activate(c, _code(lid="GOOD1"))
        assert ok.status_code == 302
        assert len(licensing.stored()) == 1
        _activate(c, _code(lid="GOOD1"))  # duplicate harmless
        assert len(licensing.stored()) == 1


def test_licence_page_is_informational(informational):
    html = informational.test_client().get(
        "/licence").get_data(as_text=True)
    assert "Licence required" not in html
    assert "informational" in html


def test_resume_route_is_harmless_stub(informational, client, login_as):
    """POST /system/licence/resume still exists for older automation —
    it no longer changes any hold state (there is none)."""
    login_as("admin")
    r = client.post("/system/licence/resume")
    assert r.status_code in (302, 303)
    with informational.app_context():
        from testorchestrator import settings
        assert settings.get("licence_dispatch_held") != "1"


# ---------------------------------------------------------------------
# machinery still enforced: signatures, stored-row re-verify, prod key
# ---------------------------------------------------------------------

@pytest.fixture()
def informational_csrf(tmp_path, monkeypatch):
    """Real status + real CSRF enabled (suite disables it)."""
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


def test_protection_controls(informational_csrf):
    app = informational_csrf
    with app.app_context():
        c = app.test_client()
        r = c.post("/licence-activate", data={"licence_code": _code()},
                   headers={"Referer": "https://localhost/licence"})
        assert r.status_code in (400, 403)
        assert licensing.stored() == []
        page = c.get("/licence").get_data(as_text=True)
        tok = re.search(r'csrf_token[^>]*value="([^"]+)"', page).group(1)
        r = c.post("/licence-activate",
                   data={"licence_code": _code(), "csrf_token": tok,
                         "website": "spammy"},
                   headers={"Referer": "https://localhost/licence"})
        assert licensing.stored() == []
        r = c.post("/licence-activate",
                   data={"licence_code": "x" * 70000, "csrf_token": tok},
                   headers={"Content-Length": "70000",
                            "Referer": "https://localhost/licence"})
        assert r.status_code in (413, 400, 403, 302)
        assert licensing.stored() == []
        r = c.post("/licence-activate",
                   data={"licence_code": _code(lid="LEGIT"),
                         "csrf_token": tok},
                   headers={"Referer": "https://localhost/licence"})
        assert r.status_code == 302
        assert len(licensing.stored()) == 1


def test_rate_limit_exhaustion(tmp_path, monkeypatch):
    """Excess activation submissions still hit the limiter -> 429."""
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
        got = [c.post("/licence-activate",
                      data={"licence_code": "bad"},
                      headers={"Referer": "https://localhost/l"}).status_code
               for _ in range(5)]
        assert 429 in got


def test_production_key_auto_loaded(tmp_path, monkeypatch):
    """Fresh process verifies a 'prod'-signed code WITHOUT any test-side
    register_key — proves release/licence_pubkey.pem is loaded."""
    from tests.conftest import make_app
    import testorchestrator.licensing as lic
    app = make_app(tmp_path)
    lic._VERIFY_KEYS.clear()
    lic._PROD_LOADED = False
    with app.app_context():
        from testorchestrator.extensions import db
        db.create_all()
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


def test_forged_stored_row_rejected(tmp_path, monkeypatch):
    """A row written directly into the settings store — no valid
    code/signature — must NOT satisfy current_status. Stored state is
    never blindly trusted."""
    from tests.conftest import make_app
    from testorchestrator import licensing, settings
    monkeypatch.delenv("TO_INSTANCE_DIR", raising=False)
    app = make_app(tmp_path)
    with app.app_context():
        from testorchestrator.extensions import db
        db.create_all()
        settings.set("licences", json.dumps([{
            "licence_id": "FORGED", "customer": "Attacker",
            "valid_from": "2020-01-01T00:00:00+00:00",
            "valid_until": "2030-01-01T00:00:00+00:00",
            "key_id": "prod"}]))
        st = licensing.current_status()
        assert st[0] != "valid", "forged stored row accepted"


def test_tampered_stored_metadata_rejected(tmp_path, monkeypatch):
    """Extending valid_until in the settings store after a legitimate
    activation must be caught — stored metadata is re-checked
    against the signed payload."""
    from tests.conftest import make_app
    from testorchestrator import licensing, settings
    monkeypatch.delenv("TO_INSTANCE_DIR", raising=False)
    app = make_app(tmp_path)
    with app.app_context():
        from testorchestrator.extensions import db
        db.create_all()
        rows = licensing.stored()
        for r in rows:
            r["valid_until"] = "2999-01-01T00:00:00+00:00"
        settings.set("licences", json.dumps(rows))
        st = licensing.current_status()
        assert st[0] != "valid", "tampered stored dates accepted"


def test_payload_fields_signed(tmp_path):
    """Swapping signed fields (customer/licence_id) in the payload
    while keeping the original signature must fail verification."""
    from tests.conftest import make_app
    from testorchestrator import licensing
    app = make_app(tmp_path)
    with app.app_context():
        from testorchestrator.extensions import db
        db.create_all()
        code = open('/tmp/lic-valid.txt').read().strip()
        parts = code.split('.')
        import json as j, base64 as b
        p = j.loads(b.urlsafe_b64decode(parts[1] + '=='))
        p['customer'] = 'ForgedCo'
        new_b64 = b.urlsafe_b64encode(j.dumps(
            p, sort_keys=True, separators=(',', ':')).encode()
            ).decode().rstrip('=')
        r = licensing.parse(f'{parts[0]}.{new_b64}.{parts[2]}')
        assert isinstance(r, str)


def test_failclosed_on_garbage(tmp_path):
    """Non-code strings, empty input, truncated codes -> rejected."""
    from tests.conftest import make_app
    from testorchestrator import licensing
    app = make_app(tmp_path)
    with app.app_context():
        for bad in ("", "x", "TO1", "TO1..", "TO1.!!sig",
                    "XX1.eyJ9.e30"):
            r = licensing.parse(bad)
            assert isinstance(r, str), f"fail-open on {bad!r}: {r}"
