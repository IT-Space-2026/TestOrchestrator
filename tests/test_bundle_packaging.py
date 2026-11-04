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
