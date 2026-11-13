"""Browser-level licensing flows — live app + chromium."""
import base64, json, threading, time
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey)
from cryptography.hazmat.primitives import serialization
from playwright.sync_api import sync_playwright
from testorchestrator import licensing

K = Ed25519PrivateKey.generate()
PUB = K.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo)


def _code(vf="2026-01-01T00:00:00+00:00",
          vu="2026-12-31T00:00:00+00:00", lid="L1", customer="Acme"):
    p = {"version": 1, "key_id": "test", "licence_id": lid,
         "product": licensing.PRODUCT, "customer": customer,
         "valid_from": vf, "valid_until": vu}
    canon = json.dumps(p, sort_keys=True, separators=(",", ":")).encode()
    sig = K.sign(canon)
    return ("TO1." + base64.urlsafe_b64encode(canon).decode().rstrip("=")
            + "." + base64.urlsafe_b64encode(sig).decode().rstrip("="))


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("licbrowser")
    from tests.conftest import make_app
    licensing._VERIFY_KEYS.clear()
    licensing.register_key("test", PUB)
    # restore real enforcement — suite conftest bypasses licensing
    import testorchestrator.licensing as lic
    lic.usable = lambda *a, **k: lic.current_status(*a, **k)[0]         == "valid"
    app = make_app(tmp)
    with app.app_context():
        from testorchestrator.extensions import db
        from testorchestrator.models import User, Role
        db.create_all()
        u = User(username="admin", role=Role.ADMIN, is_active=True)
        u.set_password("TestPassword-12345")
        db.session.add(u); db.session.commit()
    yield_url = None
    import werkzeug.serving
    srv = werkzeug.serving.make_server("127.0.0.1", 0, app)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}", app
    srv.shutdown()


@pytest.fixture()
def pg(live):
    url, _ = live
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page()
        yield url, page
        b.close()


def test_licence_page_renders_activation_form(pg):
    url, page = pg
    page.goto(f"{url}/licence")
    assert page.locator("input[name=licence_code]").count() == 1
    assert "Licence" in page.title() or "licence" in page.content()


def test_login_blocked_shows_licence_field(pg):
    url, page = pg
    page.goto(f"{url}/login")
    # unlicensed — the login page must surface the activation field
    assert page.locator("input[name=licence_code]").count() == 1


def test_activate_via_licence_page(pg):
    url, page = pg
    page.goto(f"{url}/licence")
    page.fill("input[name=licence_code]", _code(lid="B1"))
    page.click("button[type=submit]")
    page.wait_for_timeout(400)
    # successful activation redirects to login — sign-in now available
    assert "/login" in page.url or "Sign in" in page.content()


def test_protected_route_gated(pg):
    """Protected GETs never render content without auth/licence —
    after activation they land on login (unauthenticated), before
    activation on /licence."""
    url, page = pg
    page.goto(f"{url}/projects")
    blocked = ("/licence" in page.url or "/login" in page.url
               or "Sign in" in page.content()
               or "licence" in page.content())
    assert blocked


def _login(page, url):
    page.goto(f"{url}/login")
    page.fill("input[name=username]", "admin")
    page.fill("input[name=password]", "TestPassword-12345")
    page.click("button[type=submit]")
    page.wait_for_timeout(300)


def test_admin_licence_page(pg):
    """Authenticated admin reaches /system/licence; it renders status
    and the activation form."""
    url, page = pg
    _login(page, url)
    page.goto(f"{url}/system/licence")
    body = page.content()
    assert "Licence" in body
    assert page.locator("input[name=licence_code]").count() == 1
