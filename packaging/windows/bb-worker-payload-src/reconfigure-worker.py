#!/usr/bin/env python3
"""Interactive worker reconfiguration — opened from the tray
'Configure…' item (console window). Prompts for the same three
values the installer collected, then delegates to setup-worker.py:
buildbot.tac is recreated and the Scheduled Task re-registered and
restarted. The password file is rewritten only when a new password
is typed — it is never echoed and never stored in worker.conf.json.
"""
import getpass
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
PYEMBED = HERE / "python-embed"
TASK = "TestOrchestratorBuildbotWorker"


def main() -> int:
    conf_path = HERE / "worker.conf.json"
    conf = json.loads(conf_path.read_text()) \
        if conf_path.exists() else {}
    print("TestOrchestrator Buildbot worker — reconfigure")
    print("(press Enter to keep the current value)\n")

    master = input(
        f"Controller PB endpoint (host:9989, NOT the https URL)"
        f" [{conf.get('master', '')}]: ").strip() \
        or conf.get("master", "")
    name = input(
        f"Worker name [{conf.get('worker_name', '')}]: ").strip() \
        or conf.get("worker_name", "")
    if not master or not name:
        print("Endpoint and worker name are required.")
        return 1

    import os
    basedir = Path(conf.get("basedir") or
                   Path(os.environ.get("ProgramData", r"C:\ProgramData"))
                   / "TestOrchestrator" / "buildbot-worker" / "worker")
    pwfile = basedir.parent / "worker-password.txt"
    pw = getpass.getpass(
        "One-time worker password "
        "[blank = keep the existing password file]: ").strip()
    if pw:
        pwfile.write_text(pw + "\r\n")

    # stop the worker so the basedir is free, then recreate the tac
    print("\nStopping worker task…")
    subprocess.run(["schtasks", "/End", "/TN", TASK],
                   capture_output=True)
    tac = basedir / "buildbot.tac"
    if tac.exists():
        tac.unlink()

    print("Re-running setup…\n")
    subprocess.check_call([
        str(PYEMBED / "python.exe"), str(HERE / "setup-worker.py"),
        "--master", master, "--name", name,
        "--passwd-file", str(pwfile), "--basedir", str(basedir)])
    # setup-worker.py re-registers + starts the task itself
    print("\nDone — the worker should register with the controller "
          "within seconds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
