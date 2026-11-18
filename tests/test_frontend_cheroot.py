"""Spike: Cheroot frontend (TO_FRONTEND=cheroot) vs the verified
Caddy behavior set — HTTPS serving, redirect listener, host
allowlist, managed certs, hot cert reload. Runs on Linux; Windows
results are marked provisional in the spike report."""
import os
import ssl
import time
import urllib.request
import urllib.error

import pytest


def _settings(**over):
    s = {
        "https_port": 0, "http_port": 0,
        "local_host": "localhost",
        "external_enabled": False, "external_host": "",
        "http_mode": "redirect",
        "internal_app_host": "127.0.0.1", "internal_app_port": 18080,
    }
    s.update(over)
    return s


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ctx_trust(ca_pem=None):
    ctx = ssl.create_default_context()
    if ca_pem:
        ctx.load_verify_locations(cafile=str(ca_pem))
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


@pytest.fixture()
def frontend(app, monkeypatch):
    from testorchestrator import frontend as fe
    monkeypatch.setenv(fe.FRONTEND_ENV, fe.FRONTEND_CHEROOT)
    yield fe


def test_https_serves_app(frontend, app):
    hp, rp = _free_port(), _free_port()
    with app.app_context():
        fh = frontend.serve(app, _settings(https_port=hp, http_port=rp))
    try:
        r = urllib.request.urlopen(
            f"https://localhost:{hp}/login",
            context=_ctx_trust(), timeout=5)
        body = r.read().decode()
        assert r.status == 200
        assert "Sign in" in body or "TestOrchestrator" in body
        # the notice is present when no valid licence exists — the
        # informational licensing behaviour must survive the tier swap
        assert "This version is not supported" in body
    finally:
        fh.stop()


def test_http_redirect_preserves_uri(frontend, app):
    hp, rp = _free_port(), _free_port()
    with app.app_context():
        fh = frontend.serve(app, _settings(https_port=hp, http_port=rp))
    try:

        import http.client
        conn = http.client.HTTPConnection("localhost", rp, timeout=5)
        conn.request("GET", "/login?next=/projects")
        resp = conn.getresponse()
        assert resp.status == 301
        loc = resp.getheader("Location")
        conn.close()
        assert loc == f"https://localhost:{hp}/login?next=/projects"
        assert not loc.startswith("http://")
    finally:
        fh.stop()


def test_http_disabled_no_redirect_listener(frontend, app):
    hp, rp = _free_port(), _free_port()
    with app.app_context():
        fh = frontend.serve(
            app, _settings(https_port=hp, http_port=rp,
                           http_mode="disabled"))
    try:
        import socket
        with pytest.raises((ConnectionRefusedError, socket.timeout,
                            urllib.error.URLError)):
            urllib.request.urlopen(
                f"http://localhost:{rp}/", timeout=2)
    finally:
        fh.stop()


def test_host_allowlist_421(frontend, app):
    hp = _free_port()
    with app.app_context():
        fh = frontend.serve(
            app, _settings(https_port=hp, http_port=_free_port()))
    try:
        ctx = _ctx_trust()
        req = urllib.request.Request(
            f"https://localhost:{hp}/login",
            headers={"Host": "evil.example.com"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, context=ctx, timeout=5)
        assert e.value.code == 421
    finally:
        fh.stop()


def test_cert_files_generated_and_used(frontend, app):
    hp = _free_port()
    with app.app_context():
        fh = frontend.serve(
            app, _settings(https_port=hp, http_port=_free_port()))
        cert_dir = app.config["CERT_DIR"]
        assert (cert_dir / "fullchain.pem").exists()
        assert (cert_dir / "private-key.pem").exists()
        assert (cert_dir / "local-ca.pem").exists()
        # served cert chains to our generated CA — verify with trust
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cafile=str(cert_dir / "local-ca.pem"))
        ctx.check_hostname = False
        # SAN covers localhost
        ctx.check_hostname = True
        r = urllib.request.urlopen(
            f"https://localhost:{hp}/login", context=ctx, timeout=5)
        assert r.status == 200
    fh.stop()


def test_hot_cert_reload(frontend, app):
    """Admin regenerate/import swaps the TLS context — no restart."""
    hp = _free_port()
    with app.app_context():
        fh = frontend.serve(
            app, _settings(https_port=hp, http_port=_free_port()))
        cert_dir = app.config["CERT_DIR"]

        def served_fp():
            ctx = _ctx_trust()
            s = ctx.wrap_socket(__import__("socket").create_connection(
                ("127.0.0.1", hp), timeout=5), server_hostname="localhost")
            cert = s.getpeercert(binary_form=True)
            s.close()
            import hashlib
            return hashlib.sha256(cert).hexdigest()

        before = served_fp()
        from testorchestrator.certs import generate_local
        generate_local(["localhost"], cert_dir)
        # watcher polls at 2s
        deadline = time.time() + 8
        after = before
        while time.time() < deadline and after == before:
            time.sleep(0.5)
            after = served_fp()
        assert after != before, "TLS context never reloaded"
    fh.stop()


def test_server_header_masked(frontend, app):
    hp = _free_port()
    with app.app_context():
        fh = frontend.serve(
            app, _settings(https_port=hp, http_port=_free_port()))
    try:
        r = urllib.request.urlopen(
            f"https://localhost:{hp}/login", context=_ctx_trust(),
            timeout=5)
        server = r.headers.get("Server", "")
        assert "Cheroot" not in server
    finally:
        fh.stop()
