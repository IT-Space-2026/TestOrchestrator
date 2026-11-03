"""v1.8 network acceptance: NET01–NET05."""

import pytest

from testorchestrator.caddyconf import (CaddyApplyError, apply_config,
                                        render_caddyfile)
from testorchestrator.config import Config
from testorchestrator.network import (ValidationError, access_links,
                                      build_url, check_port_conflicts,
                                      detect_address_candidates, parse_host,
                                      validate_port)
from testorchestrator.settings import DEFAULTS
from testorchestrator.settings import all_settings, update


# NET01 — the three owned defaults never collide.
def test_net01_default_ports():
    assert DEFAULTS["https_port"] == 443
    assert DEFAULTS["http_port"] == 8080
    assert DEFAULTS["internal_app_port"] == 18080
    assert Config.WAITRESS_LISTEN == "127.0.0.1:18080"
    check_port_conflicts(
        {"https": 443, "http": 8080, "internal": 18080}
    )  # no raise


# NET02 — links correct for hostname, IPv4, IPv6, nondefault ports.
@pytest.mark.parametrize(
    "host,port,scheme,expected",
    [
        ("localhost", 443, "https", "https://localhost/"),
        ("localhost", 8443, "https", "https://localhost:8443/"),
        ("192.0.2.10", 443, "https", "https://192.0.2.10/"),
        ("tests.example.com", 443, "https", "https://tests.example.com/"),
        ("2001:db8::5", 443, "https", "https://[2001:db8::5]/"),
        ("2001:db8::5", 8080, "http", "http://[2001:db8::5]:8080/"),
        ("192.0.2.10", 8080, "http", "http://192.0.2.10:8080/"),
    ],
)
def test_net02_build_url(host, port, scheme, expected):
    assert build_url(host, port, scheme) == expected


def test_net02_access_links_defaults():
    links = access_links(DEFAULTS)
    assert links["local"] == "https://localhost/"
    assert links["external"] is None  # disabled until explicitly enabled


def test_net02_access_links_external():
    settings = dict(DEFAULTS, external_enabled=True,
                    external_host="2001:db8::9")
    links = access_links(settings)
    assert links["external"] == "https://[2001:db8::9]/"
    assert links["http_redirect"] == "http://[2001:db8::9]:8080/"


def test_net02_parse_host_rejects_urls():
    for bad in ("https://x.example/", "user@host", "a/b", "host:443",
                "with space", "", "h?query", "h#frag"):
        with pytest.raises(ValidationError):
            parse_host(bad)
    assert parse_host("Example.COM.") == "example.com"
    assert parse_host("192.0.2.5") == "192.0.2.5"
    assert parse_host("2001:db8::7") == "2001:db8::7"


# NET03 — detection returns candidates; private IPs marked, never "public".
def test_net03_candidates_are_suggestions():
    for c in detect_address_candidates():
        assert not c.address.startswith("127.")
        assert c.kind in ("ipv4", "ipv6")
        assert isinstance(c.is_private, bool)


# NET04 — rendered HTTP listener redirects to the configured secure address
# and never reflects the inbound Host header.
def test_net04_redirect_target_is_configured(tmp_path):
    rendered = render_caddyfile(dict(DEFAULTS), tmp_path, "127.0.0.1:18080")
    assert "redir https://localhost{uri}" in rendered
    assert "{host}" not in rendered
    assert "reverse_proxy 127.0.0.1:18080" in rendered
    assert "admin off" in rendered


def test_net04_no_http_listener_when_disabled(tmp_path):
    settings = dict(DEFAULTS, http_mode="disabled")
    rendered = render_caddyfile(settings, tmp_path, "127.0.0.1:18080")
    assert "http://" not in rendered.split("reverse_proxy")[0].replace(
        "https://", ""
    )


def test_net04_no_automatic_port_80(tmp_path):
    rendered = render_caddyfile(dict(DEFAULTS), tmp_path, "127.0.0.1:18080")
    # Exactly one http:// listener (the configured 8080 redirect) — no
    # implicit port-80 listener is opened.
    assert rendered.count("http://localhost:") == 1
    assert "http://localhost:8080" in rendered
    assert "http://localhost:80 " not in rendered
    assert "auto_https disable_redirects" in rendered


# NET05 — invalid/conflicting ports rejected; failed apply keeps prior state.
def test_net05_port_validation():
    for bad in (0, 65536, -1, "abc", ""):
        with pytest.raises(ValidationError):
            validate_port(bad)
    assert validate_port("8443") == 8443
    with pytest.raises(ValidationError):
        check_port_conflicts({"https": 443, "http": 443})


def test_net05_failed_apply_preserves_previous(app, tmp_path):
    """A rejected candidate leaves settings and active config untouched."""
    app.config["CADDY_BIN"] = str(tmp_path / "no-such-caddy")
    app.config["CADDY_CONFIG_DIR"] = tmp_path / "caddycfg"
    with app.app_context():
        before = all_settings()["network_config_version"]
        with pytest.raises(CaddyApplyError):
            apply_config(app, dict(all_settings(), https_port=8443))
        assert all_settings()["network_config_version"] == before
        # No active Caddyfile was installed.
        assert not (tmp_path / "caddycfg" / "Caddyfile").exists()


def test_net05_apply_succeeds_with_bundled_caddy(app, tmp_path):
    """Staged render + real `caddy validate` + install works end-to-end."""
    app.config["CADDY_CONFIG_DIR"] = tmp_path / "caddycfg"
    with app.app_context():
        result = apply_config(app, all_settings())
        assert result["status"] == "applied"
        assert (tmp_path / "caddycfg" / "Caddyfile").exists()
        text = (tmp_path / "caddycfg" / "Caddyfile").read_text()
        assert "reverse_proxy 127.0.0.1:18080" in text


def test_network_page_admin_only(client, login_as):
    login_as("tester")
    assert client.get("/system/network").status_code == 403
    login_as("po")
    assert client.get("/system/network").status_code == 403


def test_network_page_shows_prerequisites(client, login_as):
    """CERT04: DNS/firewall/NAT prerequisites shown separately from settings."""
    login_as("admin")
    html = client.get("/system/network").data.decode()
    assert "DNS" in html and "NAT" in html and "firewall" in html
    assert "Internet-accessible" in html


def test_host_allowlist_blocks_unknown_host(client, login_as):
    login_as("tester")
    response = client.get("/", headers={"Host": "evil.example.com"})
    assert response.status_code == 403
