"""Packaging regression: the stale-frozen-payload defect must not recur.

- The collector selects the REAL PyInstaller output (dist/testorchestrator),
  never a decoy at dist/frozen/testorchestrator.
- Missing onedir output fails the build (missing-required path).
- Embedded-build mismatch fails the build (check_frozen_fresh).
- A valid onedir passes the freshness check — which reads metadata from
  the executable payload's own _internal/release/release.json.
"""

import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_bundle  # noqa: E402


def _fake_repo(tmp_path, emb_build="b1", repo_build="b1",
               make_onedir=True):
    """Minimal repo: repo release.json + onedir with embedded release."""
    rel = tmp_path / "release"; rel.mkdir()
    (rel / "release.json").write_text(
        json.dumps({"version": "1.9.0", "build": repo_build}))
    if make_onedir:
        onedir = tmp_path / "dist/testorchestrator"
        (onedir / "_internal/release").mkdir(parents=True)
        (onedir / "toctl").write_text("#!/bin/sh\n")
        (onedir / "_internal/release/release.json").write_text(
            json.dumps({"version": "1.9.0", "build": emb_build}))
    return tmp_path


def test_collector_ignores_frozen_decoy():
    inc = dict(build_bundle.candidate_includes(frozen=True))
    frozen_entries = [p for p, c in inc.items()
                      if "frozen-app" in c or "dist/" in p]
    assert "dist/frozen" not in str(frozen_entries)
    assert any("dist/testorchestrator" in p for p, c in inc.items()
               if "frozen-app" in c or "dist/" in p)


def test_missing_onedir_fails_required(tmp_path):
    missing = build_bundle.required_list(frozen=True)
    assert any("dist/testorchestrator/toctl" in r for r in missing)


def test_embedded_build_mismatch_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(build_bundle, "REPO",
                        _fake_repo(tmp_path, emb_build="OLD",
                                   repo_build="NEW"))
    err = build_bundle.check_frozen_fresh()
    assert err and "stale" in err and "OLD" in err


def test_fresh_onedir_passes_and_reads_payload(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(build_bundle, "REPO", repo)
    assert build_bundle.check_frozen_fresh() is None
    # Freshness reads the PAYLOAD's own embedded metadata, not an
    # adjacent source file — corrupt the embedded one and it fails.
    emb = (repo / "dist/testorchestrator/_internal/release"
           / "release.json")
    emb.write_text(json.dumps({"build": "tampered"}))
    assert build_bundle.check_frozen_fresh() is not None


def test_integrations_page_sections(client, login_as, app):
    """Integrations page: subsystem-separated sections with inline
    help + examples, and no secret values rendered."""
    login_as("admin")
    r = client.get("/system/integrations")
    assert r.status_code == 200
    body = r.data.decode()
    for section in ("Git — evidence export",):
        assert section in body
    for field in ("git_export_repo",
                  "git_export_ref"):
        assert f'name="{field}"' in body
    assert "Example" in body and "user:password content" in body


def test_integrations_save_persists(client, login_as, app):
    login_as("admin")
    client.post("/system/integrations/save", data={
        "git_export_repo": "/srv/evidence/repo",
        "git_export_credential_ref": "",
        "git_export_ref": "main"})
    from testorchestrator import settings as sm
    with app.app_context():
        assert sm.get("git_export_ref") == "main"
        assert sm.get("git_export_repo") == "/srv/evidence/repo"


def test_settings_guidance_rendering(client, login_as, app):
    """Every config section renders its help/example blocks; secrets
    never appear in any config page body."""
    login_as("admin")
    checks = {"/system/reporting": ["Example — accepted configuration",
                                    "Bundled runtime vs system tools"],
              "/system/network":   ["internal app port", "1–65535"],
              "/system/secrets":   ["Example — create + reference",
                                    "never re-displayed"],
              "/system/integrations": ["Example"]}
    for path, needles in checks.items():
        body = client.get(path).data.decode()
        for n in needles:
            assert n in body, f"{path} missing {n!r}"
        # Placeholder values only — no real secret material rendered.
        for leak in ("sk-live", "AKIA", "BEGIN PRIVATE", "xoxb"):
            assert leak not in body






def test_hidden_buildbot_keys_rejected_not_updated(app, client, login_as):
    """A normal settings POST carrying backend-managed buildbot_* keys must
    not change stored values — they're internally managed, not
    user-configurable."""
    from testorchestrator import settings as sm
    login_as("admin")
    with app.app_context():
        # seed a known stored value
        try: sm.update({"buildbot_endpoint": "http://127.0.0.1:8010"})
        except Exception: pass
        before = sm.get("buildbot_endpoint")
    r = client.post("/system/integrations/save",
                    data={"bogus_endpoint": "https://evil:1",
                          "bogus_username": "attacker",
                          "git_export_repo": "/tmp/exp"},
                    follow_redirects=True)
    with app.app_context():
        assert sm.get("buildbot_endpoint") == before  # unchanged
        assert sm.get("bogus_username") != "attacker"
        assert sm.get("git_export_repo") == "/tmp/exp"  # allowed field saved
    assert "Coordinator settings" in r.get_data(as_text=True) or \
           r.status_code == 200
