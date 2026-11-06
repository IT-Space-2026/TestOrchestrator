"""Contract test vs the BUNDLED pinned Dagu (not a unit test).

Prereqs: 3rdparty/dagu tarball unpacked OR DAGU_BIN set; free ports.
Run:  .venv/bin/python -m pytest tests/contract -q
Self-starts the bundled server, submits, polls status, cancels via the
verified CLI path — matching docs/dagu-verified-api.md.
"""
import os
import subprocess
import time
from pathlib import Path

import pytest
import requests

REPO = Path(__file__).resolve().parents[2]
DAGU = os.environ.get("DAGU_BIN", "")
PORT = 29977


def _binary(tmp_path):
    if DAGU and Path(DAGU).exists():
        return DAGU
    tar = REPO / "3rdparty" / "dagu" / "dagu_2.25.8_linux_amd64.tar.gz"
    if not tar.exists():
        pytest.skip("bundled dagu tarball missing")
    import tarfile
    with tarfile.open(tar) as t:
        t.extractall(tmp_path, filter="data")
    exe = tmp_path / "dagu"
    exe.chmod(0o755)
    return str(exe)


@pytest.mark.contract
def test_submit_status_cancel(tmp_path):
    exe = _binary(tmp_path)
    home = tmp_path / "home"; (home / "dags").mkdir(parents=True)
    (home / "dags" / "to-contract.yaml").write_text(
        "steps:\n  - name: s\n    command: sleep 30\n")
    (home / "server.yaml").write_text("auth:\n  mode: none\n")
    env = {**os.environ, "DAGU_HOME": str(home),
           "DAGU_COORDINATOR_ENABLED": "false"}
    proc = subprocess.Popen(
        [exe, "start-all", "--port", str(PORT),
         "--config", str(home / "server.yaml"),
         "--dags", str(home / "dags")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(30):
            try:
                if requests.get(
                        f"http://127.0.0.1:{PORT}/api/v1/dags",
                        timeout=2).ok:
                    break
            except requests.RequestException:
                time.sleep(0.5)
        else:
            pytest.fail("dagu did not start")
        r = requests.post(
            f"http://127.0.0.1:{PORT}/api/v1/dags/to-contract/start",
            json={"params": "execution_id=ct1"}, timeout=5)
        assert r.ok and r.json().get("dagRunId"), r.text[:200]
        run_id = r.json()["dagRunId"]
        time.sleep(1.5)
        st = requests.get(
            f"http://127.0.0.1:{PORT}/api/v1/dags/to-contract"
            f"/dag-runs/{run_id}", timeout=5).json()["dagRun"]
        assert st["statusLabel"] in ("running", "succeeded")
        # Verified cancellation path (CLI stop --run-id).
        stop = subprocess.run(
            [exe, "stop", "to-contract", "--run-id", run_id],
            env=env, capture_output=True, timeout=15)
        assert stop.returncode == 0, stop.stderr[:200]
        time.sleep(1.5)
        st = requests.get(
            f"http://127.0.0.1:{PORT}/api/v1/dags/to-contract"
            f"/dag-runs/{run_id}", timeout=5).json()["dagRun"]
        assert st["statusLabel"] in ("aborted", "succeeded")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
