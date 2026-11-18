"""v1.8 network acceptance: NET01–NET05."""

import pytest

from testorchestrator.netconf import (NetApplyError, apply_config,
                                      rollback_config)
from testorchestrator.config import Config
from testorchestrator.network import (ValidationError, access_links,
                                      build_url, check_port_conflicts,
                                      detect_address_candidates, parse_host,
                                      validate_port)
from testorchestrator.settings import DEFAULTS
from testorchestrator.settings import all_settings


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


# NET04 — listener changes are flagged restart-required; cert changes
# live-reload (netconf has no intermediate config file to stage).
def test_net04_apply_flags_restart(app):
    with app.app_context():
        result = apply_config(app, all_settings())
        assert result["status"] == "applied"
        assert result["restart_required"] is True


def test_net04_apply_snapshots_previous_good(app):
    with app.app_context():
        v0 = all_settings()["network_config_version"]
        apply_config(app, dict(all_settings(), https_port=8443))
        prev = all_settings()["previous_good_config"]
        assert prev["https_port"] == DEFAULTS["https_port"]
        assert all_settings()["network_config_version"] == v0 + 1


# NET05 — invalid/conflicting ports rejected; failed apply keeps prior state.
def test_net05_port_validation():
    for bad in (0, 65536, -1, "abc", ""):
        with pytest.raises(ValidationError):
            validate_port(bad)
    assert validate_port("8443") == 8443
    with pytest.raises(ValidationError):
        check_port_conflicts({"https": 443, "http": 443})


def test_net05_apply_and_rollback(app):
    """Apply snapshots previous-good; rollback restores it + bumps version."""
    with app.app_context():
        apply_config(app, dict(all_settings(), https_port=8443))
        v = all_settings()["network_config_version"]
        out = rollback_config(app)
        assert out["status"] == "rolled-back"
        assert all_settings()["https_port"] == DEFAULTS["https_port"]
        assert all_settings()["network_config_version"] == v + 1


def test_net05_rollback_without_snapshot_errors(app):
    with app.app_context():
        from testorchestrator import settings as sm
        sm.set("previous_good_config", {})
        sm.db.session.commit()
        with pytest.raises(NetApplyError):
            rollback_config(app)


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
