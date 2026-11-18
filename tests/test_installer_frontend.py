"""Web tier + unit wiring in the Linux installer (scripts/install.py).

The shipped release has exactly one frontend: the Cheroot in-process
web tier behind the canonical ``testorchestrator-web.service`` unit.
Buildbot is the sole orchestrator — master + local worker + ssh
dispatcher units are always staged and enabled alongside the app
units; there is no backend-switch flag or orchestrator choice.
"""
import argparse
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import install as inst  # noqa: E402  scripts/install.py


def _args():
    return argparse.Namespace()


def test_enabled_units_are_buildbot_only():
    """The shipped unit set: web + dispatch worker + buildbot
    master/local/ssh — nothing Dagu or Caddy."""
    units = inst._enabled_units(_args())
    assert units == (
        "testorchestrator-web",
        "testorchestrator-worker",
        "testorchestrator-buildbot-master",
        "testorchestrator-buildbot-local",
        "testorchestrator-buildbot-ssh",
    )
    for u in units:
        assert "dagu" not in u and "caddy" not in u


def test_web_unit_template_ships_in_repo():
    """The canonical web unit exists in deploy/ — staging must never
    reference a missing source."""
    assert (REPO / "deploy" / "testorchestrator-web.service").is_file()


def test_no_dagu_or_caddy_units_in_repo():
    """The Dagu-free/Caddy-free release ships no legacy units."""
    names = {p.name for p in (REPO / "deploy").glob("*.service")}
    assert not any("dagu" in n or "caddy" in n for n in names)


def test_stage_web_unit_writes_canonical_name(tmp_path):
    cfg, payload, data = (tmp_path / p for p in ("config", "payload", "data"))
    for d in (cfg, payload, data):
        d.mkdir()
    inst._stage_web_unit(cfg, payload, data)
    unit = cfg / "testorchestrator-web.service"
    assert unit.is_file()
    text = unit.read_text()
    # requested dirs replace the deploy template's production paths,
    # and the unit re-migrates before every start
    assert str(payload) in text and str(data) in text and str(cfg) in text
    assert "/opt/testorchestrator" not in text
    assert "ExecStartPre=" in text and "db-upgrade" in text


def test_web_env_written_with_buildbot_wiring(tmp_path):
    """The staged units require EnvironmentFile=web.env — it carries
    the loopback Buildbot settings the portal bootstrap seeds from,
    and a rerun preserves TO_SECRET_KEY + TO_INSTALL_ID rather than
    silently invalidating sessions and install identity."""
    cfg = tmp_path / "config"; cfg.mkdir()
    data = tmp_path / "data"
    inst._write_web_env(cfg, data)
    first = (cfg / "web.env").read_text()
    assert f"TO_INSTANCE_DIR={data}" in first
    assert f"TO_CONFIG_DIR={cfg}" in first
    assert "TO_SECRET_KEY=" in first and "TO_INSTALL_ID=" in first
    # Buildbot loopback wiring — no backend switch required.
    assert "TO_BUILDBOT_ENDPOINT=http://127.0.0.1:8010" in first
    assert f"TO_BUILDBOT_HOME={data}/buildbot" in first
    assert "TO_BUILDBOT_PB_PORT=9989" in first
    assert "TO_BUILDBOT_WWW_PORT=8010" in first
    key1 = [l for l in first.splitlines()
            if l.startswith("TO_SECRET_KEY=")][0]
    inst._write_web_env(cfg, data)
    second = (cfg / "web.env").read_text()
    key2 = [l for l in second.splitlines()
            if l.startswith("TO_SECRET_KEY=")][0]
    assert key1 == key2
    assert oct((cfg / "web.env").stat().st_mode & 0o777) == "0o640"
