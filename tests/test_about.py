"""v1.7 acceptance checks AB01–AB07 for About and IT-Space branding."""

import json
import re
from pathlib import Path

import pytest

from testorchestrator.release import (
    ReleaseMetadataError,
    load_release_metadata,
)
from testorchestrator.models import Role

BASE_DIR = Path(__file__).resolve().parent.parent

ALL_ROLES = [r.value for r in Role]
NON_ADMIN_ROLES = [r.value for r in Role if r != Role.ADMIN]


# AB01 — all four authenticated roles can open About.
@pytest.mark.parametrize("role", ALL_ROLES)
def test_ab01_every_role_can_open_about(client, login_as, role):
    login_as(role)
    page = client.get("/")
    assert page.status_code == 200
    # About entry point present in the user menu for this role.
    assert b'id="about-menu-item"' in page.data
    assert b">About</a>" in page.data
    # In-app dialog markup is rendered for this role.
    assert b'id="about-dialog"' in page.data
    assert client.get("/about").status_code == 200


def test_ab01_about_requires_authentication(client):
    response = client.get("/about")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


# AB02 — exact company name, logo, HTTPS homepage.
@pytest.mark.parametrize("role", ALL_ROLES)
def test_ab02_about_content(client, login_as, role):
    login_as(role)
    html = client.get("/about").data.decode()
    assert "TestOrchestrator" in html
    assert "IT-Space Kft." in html
    assert 'alt="IT-Space Kft. logo"' in html
    assert 'href="https://it-space.info"' in html
    assert 'rel="noopener noreferrer"' in html
    assert 'target="_blank"' in html


# AB03 — About renders offline; no external requests until homepage click.
def test_ab03_no_external_resources(client, login_as):
    login_as("tester")
    for path in ("/", "/about", "/about/notices"):
        html = client.get(path).data.decode()
        external = re.findall(r'(?:src|href)\s*=\s*"(https?://[^"]+)"', html)
        # Only the explicit user-clicked homepage link may appear; every
        # src= (sub-resource) must be same-origin.
        assert set(external) <= {"https://it-space.info"}, (
            f"{path} references external resources: {external}"
        )
        assert not re.search(r'src\s*=\s*"https?://', html), (
            f"{path} loads an external sub-resource"
        )


def test_ab03_csp_forbids_external_subresources(client, login_as):
    login_as("tester")
    csp = client.get("/").headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "img-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "font-src 'self'" in csp


def test_ab03_static_assets_are_local():
    """No CDN, external font, tracking pixel, or hotlink in bundled assets."""
    static_dir = BASE_DIR / "testorchestrator" / "static"
    for path in static_dir.rglob("*"):
        if path.is_file() and path.suffix in {".css", ".js", ".svg"}:
            text = path.read_text(encoding="utf-8")
            # xmlns namespace identifiers are not resource fetches.
            text = text.replace('xmlns="http://www.w3.org/2000/svg"', "")
            assert "http://" not in text and "https://" not in text, (
                f"external reference in {path}"
            )


# AB04 — version/build come from bundled release metadata.
def test_ab04_version_and_build_rendered(client, login_as):
    login_as("admin")
    metadata = json.loads(
        (BASE_DIR / "release" / "release.json").read_text(encoding="utf-8")
    )
    html = client.get("/about").data.decode()
    assert metadata["version"] in html
    assert metadata["build"] in html


def test_ab04_release_metadata_loading(tmp_path):
    """Upgrade/rollback installs their own release.json; About reflects it."""
    (tmp_path / "release").mkdir()
    (tmp_path / "release" / "release.json").write_text(
        json.dumps({"version": "9.9.9", "build": "rollback-build-42"})
    )
    data = load_release_metadata(tmp_path)
    assert data["version"] == "9.9.9"
    assert data["build"] == "rollback-build-42"


def test_ab04_missing_metadata_fails_loudly(tmp_path):
    with pytest.raises(ReleaseMetadataError):
        load_release_metadata(tmp_path)


# AB05 — no admin-only data via About; notices readable locally.
@pytest.mark.parametrize("role", NON_ADMIN_ROLES)
def test_ab05_no_admin_data_for_non_admin_roles(client, login_as, role):
    login_as(role)
    for path in ("/", "/about"):
        html = client.get(path).data.decode()
        assert "changelog" not in html.lower(), f"{path} leaks changelog to {role}"
        assert "audit" not in html.lower(), f"{path} leaks audit to {role}"
    assert client.get("/system/changelog").status_code == 403
    assert client.get("/system/audit").status_code == 403


def test_ab05_admin_sees_changelog_shortcut(client, login_as):
    login_as("admin")
    html = client.get("/about").data.decode()
    assert 'href="/system/changelog"' in html
    assert client.get("/system/changelog").status_code == 200


def test_ab05_third_party_notices_readable(client, login_as):
    login_as("po")
    page = client.get("/about/notices")
    assert page.status_code == 200
    # At least one bundled licence file is listed and served locally.
    match = re.search(r'href="(/3rdparty/[^"]+)"', page.data.decode())
    assert match, "no licence file linked"
    assert client.get(match.group(1)).status_code == 200


# AB06 — local logo, alt text, keyboard-accessible dialog markup.
def test_ab06_logo_and_dialog_accessibility(client, login_as):
    login_as("tester")
    html = client.get("/").data.decode()
    assert 'alt="IT-Space Kft. logo"' in html
    # Dialog semantics + focus management hooks.
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'aria-labelledby="about-title"' in html
    # Close control is a real button (keyboard operable).
    assert 'id="about-close"' in html
    # Logo is packaged locally — src stays on-origin.
    logo_src = re.search(
        r'alt="IT-Space Kft\. logo"[^>]*src="([^"]+)"|src="([^"]+)"[^>]*alt="IT-Space Kft\. logo"',
        html,
    )
    src = (logo_src.group(1) or logo_src.group(2)) if logo_src else ""
    assert src.startswith("/static/"), f"logo not local: {src}"
    # Bundled asset fetches and is the owner-supplied official PNG.
    resp = client.get(src)
    assert resp.status_code == 200
    assert resp.data[:4] == b"\x89PNG"
    assert "iT-space-logo" in src


# AB07 — the owner-supplied official logo is tracked and active.
def test_ab07_official_logo_tracked_and_supplied():
    readme = (BASE_DIR / "assets" / "branding" / "README.md").read_text(
        encoding="utf-8"
    )
    assert "SUPPLIED" in readme
    assert (BASE_DIR / "assets" / "branding"
            / "iT-space-logo-400x400.png").exists()
    from testorchestrator.main.routes import (LOGO_APPROVED,
                                              LOGO_STATIC_PATH)
    assert LOGO_APPROVED is True
    assert LOGO_STATIC_PATH == "branding/iT-space-logo.png"


def test_ab07_official_logo_in_ui_no_placeholder_disclaimer(
        client, login_as):
    login_as("admin")
    html = client.get("/about").data.decode()
    assert "iT-space-logo.png" in html
    assert "Temporary placeholder" not in html
    assert "it-space-logo.placeholder.svg" not in html
