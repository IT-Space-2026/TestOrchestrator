#!/usr/bin/env python3
"""TestOrchestrator Buildbot worker bootstrap — runs INSIDE the
installed payload on the Windows host at install time (and on re-run
for repair). Offline: python-embed carries the interpreter, wheels/
carries pinned packages; nothing is downloaded.

Usage (invoked by the NSIS installer):
    python-embed\\python.exe setup-worker.py --master HOST:9989 \
        --name WORKERNAME --passwd-file FILE [--basedir DIR]

Steps: enable site-packages on the embedded interpreter, install the
pinned wheels (no index), write buildbot.tac via `buildbot-worker
create-worker`, then write worker.conf (connectivity only — the
password file is a separate operator-provided path, never embedded in
the installer or the command line).
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
PYEMBED = HERE / "python-embed"
WHEELS = HERE / "wheels"
GET_PIP = HERE / "get-pip.py"


def _enable_site_packages() -> Path:
    """The embeddable distro disables site-packages via python*._pth;
    point it at ./Lib/site-packages (created under the embed dir)."""
    pth = next(PYEMBED.glob("python*._pth"), None)
    if pth is None:
        raise SystemExit(f"no python*._pth under {PYEMBED}")
    lines = [l.strip() for l in pth.read_text().splitlines()]
    if "Lib\\site-packages" not in lines and "Lib/site-packages" not in lines:
        lines.append("Lib\\site-packages")
    if "#import site" in lines:
        lines[lines.index("#import site")] = "import site"
    elif "import site" not in lines:
        lines.append("import site")
    pth.write_text("\n".join(lines) + "\n")
    return PYEMBED / "Lib" / "site-packages"


def _pip(python: Path, *args: str) -> None:
    subprocess.check_call(
        [str(python), "-m", "pip", "--no-input", *args],
        cwd=str(HERE))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", required=True,
                    help="controller PB endpoint, host:port")
    ap.add_argument("--name", required=True,
                    help="worker name registered in the portal")
    ap.add_argument("--passwd-file", required=True,
                    help="path to a file whose first line is the "
                         "worker password")
    ap.add_argument("--basedir", default=str(HERE / "worker"))
    ap.add_argument("--service-account", default="SYSTEM",
                    help="account the Scheduled Task runs as "
                         "(default SYSTEM)")
    ap.add_argument("--no-task", action="store_true",
                    help="skip Scheduled Task registration")
    args = ap.parse_args()

    python = PYEMBED / "python.exe"
    if not python.exists():
        raise SystemExit(f"embedded interpreter missing: {python}")
    site = _enable_site_packages()
    site.mkdir(parents=True, exist_ok=True)

    # pip bootstrap only when absent — reruns must not downgrade.
    if not (PYEMBED / "Scripts" / "pip.exe").exists():
        subprocess.check_call(
            [str(python), str(GET_PIP), "--no-index",
             "--find-links", str(WHEELS)], cwd=str(HERE))
    _pip(python, "install", "--no-index", "--find-links",
         str(WHEELS), "buildbot-worker", "pystray", "pillow")

    bbw = PYEMBED / "Scripts" / "buildbot-worker.exe"
    if not bbw.exists():
        raise SystemExit("buildbot-worker did not install")
    try:
        passwd = Path(args.passwd_file).read_text() \
            .splitlines()[0].strip()
    except (OSError, IndexError):
        raise SystemExit(
            f"worker password file missing or empty: "
            f"{args.passwd_file} — create it containing the one-time "
            f"password issued by the portal (System → Infrastructure → "
            f"Add execution target), or pass --passwd-file")
    basedir = Path(args.basedir)
    basedir.mkdir(parents=True, exist_ok=True)
    if not (basedir / "buildbot.tac").exists():
        subprocess.check_call(
            [str(bbw), "create-worker", "--keepalive=60",
             str(basedir), args.master, args.name, passwd],
            cwd=str(HERE))
    conf = {
        "master": args.master,
        "worker_name": args.name,
        "basedir": str(basedir),
        "buildbot_worker": str(bbw),
    }
    (HERE / "worker.conf.json").write_text(
        json.dumps(conf, indent=2) + "\n")

    if not args.no_task:
        _register_task(bbw, basedir, args.service_account)
    print(f"[setup-worker] ok: {args.name} -> {args.master}")


TASK_NAME = "TestOrchestratorBuildbotWorker"


def _register_task(bbw: Path, basedir: Path, account: str) -> None:
    """Register + start the boot-time worker task. subprocess arg
    lists — no shell quoting anywhere (the NSIS->cmd->schtasks chain
    mangled embedded quotes in /TR)."""
    task_tr = f'"{bbw}" start --nodaemon "{basedir}"'
    cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "ONSTART",
           "/RU", account, "/RL", "HIGHEST", "/F", "/TR", task_tr]
    if account.upper() != "SYSTEM":
        cmd += ["/RP", ""]
    print(f"[setup-worker] schtasks: {' '.join(cmd)}")
    subprocess.check_call(cmd)
    print(f"[setup-worker] starting {TASK_NAME}")
    subprocess.check_call(
        ["schtasks", "/Run", "/TN", TASK_NAME])


if __name__ == "__main__":
    main()
