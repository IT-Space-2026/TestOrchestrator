"""Provision -> boot -> authenticate: regression test for the Dagu
basic-auth wiring against the BUNDLED pinned binary (not mocked).

Run:  .venv/bin/python -m pytest tests/contract -q -m contract
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

REPO = Path(__file__).resolve().parents[2]
PORT = 29978


def _dagu_bin(tmp_path):
    tar = REPO / "3rdparty" / "dagu" / "dagu_2.25.2_linux_amd64.tar.gz"
    if not tar.exists():
        pytest.skip("bundled dagu tarball missing")
    import tarfile
    with tarfile.open(tar) as t:
        t.extractall(tmp_path, filter="data")
    exe = tmp_path / "dagu"
    exe.chmod(0o755)
    return exe


@pytest.mark.contract
def test_provision_and_basic_auth_roundtrip(tmp_path):
    exe = _dagu_bin(tmp_path / "bin")
    home, cfg = tmp_path / "home", tmp_path / "cfg"
    # Run the real provisioning script into tmp dirs.
    sys.path.insert(0, str(REPO / "scripts"))
    import provision_dagu
    provision_dagu.main([
        "--home", str(home), "--config", str(cfg),
        "--port", str(PORT), "--binary", str(exe)])
    assert (home / "dags" / "testorchestrator-execution.yaml").exists()
    creds = dict(
        l.split("=", 1)
        for l in (cfg / "credentials").read_text().splitlines())
    assert (cfg / "server.yaml").stat().st_mode & 0o777 == 0o600
    assert "mode: basic" in (cfg / "server.yaml").read_text()

    env = {**os.environ, "DAGU_HOME": str(home),
           "DAGU_COORDINATOR_ENABLED": "false"}
    proc = subprocess.Popen(
        [str(cfg / "dagu"), "start-all", "--port", str(PORT),
         "--config", str(cfg / "server.yaml"),
         "--dags", str(home / "dags")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{PORT}/api/v1"
    try:
        for _ in range(40):
            try:
                if requests.get(f"{base}/health",
                                auth=(creds["username"],
                                      creds["password"]), timeout=2).ok:
                    break
            except requests.RequestException:
                time.sleep(0.5)
        else:
            pytest.fail("provisioned dagu did not start")
        # Unauthenticated and wrong-credential requests are refused.
        assert requests.get(f"{base}/dags", timeout=5).status_code == 401
        assert requests.get(f"{base}/dags", auth=("x", "y"),
                            timeout=5).status_code == 401
        # Provisioned credentials authenticate; workflow is installed.
        health = requests.get(f"{base}/health",
                              auth=(creds["username"], creds["password"]),
                              timeout=5).json()
        assert health["version"].startswith("2.25.2")
        dags = requests.get(f"{base}/dags",
                            auth=(creds["username"], creds["password"]),
                            timeout=5).json()
        names = [(d.get("dag") or {}).get("name") for d in dags["dags"]]
        assert "testorchestrator-execution" in names
        # The provisioned workflow carries the --monitor step (Option B).
        wf = (home / "dags" / "testorchestrator-execution.yaml").read_text()
        assert "--monitor" in wf and "testorchestrator.agent" in wf
    finally:
        proc.terminate()
        proc.wait(timeout=15)
