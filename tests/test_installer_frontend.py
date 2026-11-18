"""Frontend selection in the Linux installer (scripts/install.py).

`--frontend` resolves: explicit flag > persisted ``config/frontend``
marker > ``caddy``. Fresh installs (and upgrades of 2.0.x installs,
which carry no marker) therefore stay on Caddy, a Cheroot opt-in
survives reruns/upgrades, and an explicit flag is what switches tiers.
"""
import argparse
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import install as inst  # noqa: E402  scripts/install.py


def _args(frontend="auto"):
    return argparse.Namespace(frontend=frontend)


def test_auto_defaults_to_caddy_without_marker(tmp_path):
    """Fresh install: no marker -> caddy. Same resolution applies to a
    2.0.x upgrade — no marker exists, Caddy stays the frontend."""
    assert inst._resolve_frontend(_args("auto"), tmp_path) == "caddy"


def test_auto_reads_persisted_marker(tmp_path):
    (tmp_path / "frontend").write_text("cheroot\n")
    assert inst._resolve_frontend(_args("auto"), tmp_path) == "cheroot"
    (tmp_path / "frontend").write_text("caddy\n")
    assert inst._resolve_frontend(_args("auto"), tmp_path) == "caddy"


def test_explicit_flag_overrides_marker(tmp_path):
    """--frontend is the supported way to switch tiers."""
    (tmp_path / "frontend").write_text("cheroot\n")
    assert inst._resolve_frontend(_args("caddy"), tmp_path) == "caddy"
    (tmp_path / "frontend").write_text("caddy\n")
    assert inst._resolve_frontend(_args("cheroot"), tmp_path) == "cheroot"


def test_corrupt_marker_falls_back_to_caddy(tmp_path):
    for junk in ("", "nginx", "CHEROOT", "cheroot caddy\n"):
        (tmp_path / "frontend").write_text(junk)
        assert inst._resolve_frontend(_args("auto"), tmp_path) == "caddy"


def test_cheroot_excludes_caddy_from_enabled_set():
    """Only one frontend may own the public ports."""
    assert "testorchestrator-caddy" not in \
        inst._enabled_units(_args("cheroot"))
    assert "testorchestrator-caddy" in inst._enabled_units(_args("caddy"))
    for u in ("testorchestrator-web", "testorchestrator-worker",
              "testorchestrator-dagu"):
        assert u in inst._enabled_units(_args("cheroot"))


def test_staged_web_unit_matches_tier(tmp_path):
    """The canonical testorchestrator-web.service name always exists —
    its template differs by tier. Caddy's unit is staged either way so
    rollback is a unit swap, not a reinstall."""
    inst._stage_frontend_units("cheroot", tmp_path)
    web = (tmp_path / "testorchestrator-web.service").read_text()
    assert "TO_FRONTEND=cheroot" in web
    assert "CAP_NET_BIND_SERVICE" in web
    assert (tmp_path / "testorchestrator-caddy.service").is_file()
    assert (tmp_path / "frontend").read_text().strip() == "cheroot"

    inst._stage_frontend_units("caddy", tmp_path)
    web = (tmp_path / "testorchestrator-web.service").read_text()
    assert "TO_FRONTEND" not in web
    assert "waitress-serve" in web
    assert (tmp_path / "frontend").read_text().strip() == "caddy"


def test_marker_roundtrip_persists_selection(tmp_path):
    """Staging writes the marker auto resolves from — a rerun without
    --frontend keeps the tier."""
    inst._stage_frontend_units("cheroot", tmp_path)
    assert inst._resolve_frontend(_args("auto"), tmp_path) == "cheroot"


def test_cheroot_unit_templates_ship_in_repo():
    """Both web-tier templates and the caddy unit exist in deploy/ —
    staging must never reference a missing source."""
    for name in ("testorchestrator-web.service",
                 "testorchestrator-web-cheroot.service",
                 "testorchestrator-caddy.service"):
        assert (REPO / "deploy" / name).is_file(), name
