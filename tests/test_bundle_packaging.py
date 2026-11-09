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

import pytest

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
        "dagu_endpoint": "https://dagu.internal:8080",
        "dagu_credential_ref": "dagu-api-key",
        "dagu_username": "",
        "git_export_repo": "/srv/evidence/repo",
        "git_export_credential_ref": "",
        "git_export_ref": "main"})
    from testorchestrator import settings as sm
    with app.app_context():
        assert sm.get("dagu_endpoint") == "https://dagu.internal:8080"
        assert sm.get("git_export_ref") == "main"


def test_settings_guidance_rendering(client, login_as, app):
    """Every config section renders its help/example blocks; secrets
    never appear in any config page body."""
    login_as("admin")
    checks = {"/system/reporting": ["Example — accepted configuration",
                                    "Java home vs executable"],
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


def test_acquisition_checksum_gate(tmp_path):
    """Positive + negative: verified archive passes; missing/mismatch
    fails loudly — never silently falls back."""
    import hashlib, base64, json
    from scripts.build_bundle import verify_acquisition
    node_bytes = b"fake-node-tarball"
    allure_bytes = b"fake-allure-tarball"
    man = {"nodejs": {"version": "22.18.0",
            "linux_x64": {"sha256":
                          hashlib.sha256(node_bytes).hexdigest()}},
           "allure": {"version": "3.33.0",
            "sha512": base64.b64encode(
                hashlib.sha512(allure_bytes).digest()).decode()}}
    mp = tmp_path / "manifest.json"; mp.write_text(json.dumps(man))
    stage = tmp_path / "stage"; stage.mkdir()
    (stage / "node.tar.xz").write_bytes(node_bytes)
    (stage / "allure.tgz").write_bytes(allure_bytes)
    res = verify_acquisition(mp, stage)
    assert res["nodejs"]["ok"] and res["allure"]["ok"]


def test_acquisition_missing_payload_fails(tmp_path):
    import json
    from scripts.build_bundle import verify_acquisition
    import pytest
    man = {"nodejs": {"linux_x64": {"sha256": "x"}},
           "allure": {"sha512": "y"}}
    mp = tmp_path / "m.json"; mp.write_text(json.dumps(man))
    stage = tmp_path / "s"; stage.mkdir()
    with pytest.raises(RuntimeError, match="missing required payload"):
        verify_acquisition(mp, stage)


def test_acquisition_checksum_mismatch_fails(tmp_path):
    import json, hashlib
    from scripts.build_bundle import verify_acquisition
    import pytest
    man = {"nodejs": {"linux_x64": {"sha256":
                          hashlib.sha256(b"expected").hexdigest()}},
           "allure": {"sha512": "x"}}
    mp = tmp_path / "m.json"; mp.write_text(json.dumps(man))
    stage = tmp_path / "s"; stage.mkdir()
    (stage / "node.tar.xz").write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        verify_acquisition(mp, stage)


def test_extracted_runtime_completeness(tmp_path):
    """Extracted-runtime completeness is separate from the archive
    checksum — a partial stage fails before packaging."""
    from scripts.build_bundle import verify_extracted_runtime
    import json
    man = {"allure": {"cli": "node_modules/allure/cli.js"}}
    mp = tmp_path / "m.json"; mp.write_text(json.dumps(man))
    bundle = tmp_path / "bundle"
    # missing everything -> not ok
    r = verify_extracted_runtime(bundle, mp)
    assert r["ok"] is False and r["problems"]
    # staged node + allure cli -> ok
    (bundle / "nodejs" / "node-v22").mkdir(parents=True)
    (bundle / "allure3" / "node_modules" / "allure").mkdir(parents=True)
    (bundle / "allure3" / "node_modules" / "allure"
     / "cli.js").write_text("//")
    r = verify_extracted_runtime(bundle, mp)
    assert r["ok"] is True


def test_hidden_dagu_keys_rejected_not_updated(app, client, login_as):
    """A normal settings POST carrying backend-managed dagu_* keys must
    not change stored values — they're internally managed, not
    user-configurable."""
    from testorchestrator import settings as sm
    login_as("admin")
    with app.app_context():
        # seed a known stored value
        try: sm.update({"dagu_endpoint": "https://coord.internal:50055"})
        except Exception: pass
        before = sm.get("dagu_endpoint")
    r = client.post("/system/integrations/save",
                    data={"dagu_endpoint": "https://evil:1",
                          "dagu_username": "attacker",
                          "git_export_repo": "/tmp/exp"},
                    follow_redirects=True)
    with app.app_context():
        assert sm.get("dagu_endpoint") == before  # unchanged
        assert sm.get("dagu_username") != "attacker"
        assert sm.get("git_export_repo") == "/tmp/exp"  # allowed field saved
    assert "Coordinator settings" in r.get_data(as_text=True) or \
           r.status_code == 200
