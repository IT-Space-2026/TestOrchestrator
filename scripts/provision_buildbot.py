#!/usr/bin/env python3
"""Buildbot master + controller-local workers provisioning (installer step).

Platform notes: on Linux the installer stages the systemd units this
script writes; on Windows pass --no-units (Task Scheduler wrappers are
created by the NSIS installer instead). Console-script entry points do
not exist in the embeddable Windows Python, so --setup invokes
``python -c "from <pkg>.scripts.runner import run; run()"`` there.

Idempotent, artifact-writing, and credential-PRESERVING on rerun — the
API password and controller-local worker passwords already present in
secrets.json are reused so the portal's stored secrets and the master's
view never diverge.

Layout under <home> (default <instance>/buildbot):
  master.cfg       copied once from deploy/buildbot/master.cfg — it is
                   JSON-driven; per-target changes never touch it
  secrets.json     0600 — REST API user/pass + worker passwords
  targets.json     portal-written builder/worker routing
  staging/         per-execution source archives (FileDownload source)
  artifacts/       DirectoryUpload destination (portal reads directly)
  run-specs/       per-execution ssh spec files + materialized keys (0600)
  master/          buildbot master basedir (state.sqlite, twistd.pid)
  local/           to-local worker basedir (local KRE runs)
  dispatcher/      to-ssh-dispatcher worker basedir (agentless SSH runs)
  units/           staged systemd unit files (skipped with --no-units)

Usage: provision_buildbot.py --home <dir> --python <exe>
                             [--pb-port 9989] [--www-port 8010]
                             [--dry-run]
"""

import argparse
import getpass
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

LOCAL_WORKER = "to-local"
SSH_WORKER = "to-ssh-dispatcher"


def _existing_passwords(secrets_path: Path) -> dict:
    """Recover already-provisioned credentials so a rerun preserves the
    pair the portal secret store was synced to (never regenerates)."""
    try:
        data = json.loads(secrets_path.read_text())
        return {
            "api_user": data.get("api_user", "to-api"),
            "api_password": data.get("api_password", ""),
            "worker_passwords": dict(data.get("worker_passwords") or {}),
        }
    except (OSError, ValueError):
        return {"api_user": "to-api", "api_password": "",
                "worker_passwords": {}}


def _unit(name: str, desc: str, exec_cmd: str,
          workdir: Path, env: dict = None) -> str:
    lines = [f"[Unit]\nDescription={desc}\nAfter=network.target\n",
             "[Service]\nType=simple\n"]
    for k, v in (env or {}).items():
        lines.append(f"Environment={k}={v}\n")
    lines.append(f"ExecStart={exec_cmd}\n"
                 f"WorkingDirectory={workdir}\n"
                 "Restart=on-failure\nRestartSec=5\n"
                 "[Install]\nWantedBy=multi-user.target\n")
    return "".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True,
                    help="buildbot dir (master.cfg/state/staging/etc.)")
    ap.add_argument("--python", default=sys.executable,
                    help="payload python with buildbot+paramiko installed")
    ap.add_argument("--pb-port", type=int, default=9989)
    ap.add_argument("--www-port", type=int, default=8010)
    ap.add_argument("--api-password", default="",
                    help="explicit REST credential (rotation); "
                         "otherwise preserved or generated")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--setup", action="store_true",
                    help="also create the master + worker basedirs "
                         "(buildbot create-master/upgrade-master/"
                         "create-worker)")
    ap.add_argument("--no-units", action="store_true",
                    help="skip systemd unit staging (Windows installs "
                         "use Task Scheduler launchers instead)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    home = Path(a.home).resolve()
    master_dir = home / "master"
    local_dir = home / "local"
    disp_dir = home / "dispatcher"
    units_dir = home / "units"
    secrets_path = home / "secrets.json"
    targets_path = home / "targets.json"
    repo = Path(a.repo_root).resolve()
    # NOT .resolve() — a venv python is a symlink to the interpreter;
    # units must invoke the venv (which has buildbot/paramiko), not
    # the system binary it points at.
    py = str(Path(a.python).absolute())

    print(f"[buildbot] home -> {home}")
    print(f"[buildbot] python -> {py} (user {getpass.getuser()})")

    creds = _existing_passwords(secrets_path)
    source = "preserved" if creds["api_password"] else "generated"
    api_password = a.api_password or creds["api_password"] \
        or "bb-" + secrets.token_urlsafe(24)
    wpw = creds["worker_passwords"]
    for name in (LOCAL_WORKER, SSH_WORKER):
        wpw.setdefault(name, "bb-" + secrets.token_urlsafe(24))
    print(f"[buildbot] credentials: {source} "
          f"(api + {len(wpw)} worker passwords)")

    steps = [
        ("dirs", home, master_dir, local_dir, disp_dir,
         home / "staging", home / "artifacts", home / "run-specs"),
        ("master.cfg", home / "master.cfg"),
        ("secrets.json (0600)", secrets_path),
        ("targets.json skeleton", targets_path),
        ("systemd units", units_dir) if not a.no_units else
        ("systemd units", "(skipped — --no-units)"),
    ]
    for s in steps:
        print(f"[buildbot] {s[0]} -> "
              + ", ".join(str(x) for x in s[1:]))
    if a.dry_run:
        return

    for d in (home, master_dir, local_dir, disp_dir, units_dir,
              home / "staging", home / "artifacts", home / "run-specs"):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(home / "run-specs", 0o700)

    master_cfg_src = repo / "deploy" / "buildbot" / "master.cfg"
    if not (home / "master.cfg").is_file():
        shutil.copy2(master_cfg_src, home / "master.cfg")
    else:
        # Rerun: refresh the shipped template but never the JSON data
        # files — master.cfg itself is generated-only content.
        shutil.copy2(master_cfg_src, home / "master.cfg")

    _secrets = {
        "api_user": "to-api",
        "api_password": api_password,
        "worker_passwords": wpw,
    }
    (secrets_path).write_text(json.dumps(_secrets, indent=1))
    os.chmod(secrets_path, 0o600)

    if not targets_path.exists():
        targets_path.write_text(json.dumps({
            "workers": [{"name": LOCAL_WORKER},
                        {"name": SSH_WORKER}],
            "targets": [],
            "staging_dir": str(home / "staging"),
            "artifacts_dir": str(home / "artifacts"),
            "pb_port": f"tcp:{a.pb_port}:interface=0.0.0.0",
            "www_port": f"tcp:{a.www_port}:interface=127.0.0.1",
        }, indent=1))

    # systemd unit staging — Type=simple nodaemon; activation needs the
    # service manager (user-scope for tests, system scope for installs).
    # NOTE: `python -m buildbot.scripts.runner` imports the module but
    # never calls run() (no __main__ guard) — silent exit. The venv's
    # console entry points are the supported invocation.
    pydir = str(Path(py).parent)
    if a.no_units:
        print("[buildbot] units skipped (--no-units)")
    else:
        units = {
            "testorchestrator-buildbot-master.service": _unit(
                "testorchestrator-buildbot-master",
                "TestOrchestrator Buildbot master",
                f"{pydir}/buildbot start --nodaemon {master_dir}",
                master_dir),
            "testorchestrator-buildbot-local.service": _unit(
                "testorchestrator-buildbot-local",
                "TestOrchestrator Buildbot local worker",
                f"{pydir}/buildbot-worker start --nodaemon "
                f"{local_dir}", local_dir),
            "testorchestrator-buildbot-ssh.service": _unit(
                "testorchestrator-buildbot-ssh",
                "TestOrchestrator Buildbot SSH dispatcher",
                f"{pydir}/buildbot-worker start --nodaemon "
                f"{disp_dir}", disp_dir),
        }
        for name, text in units.items():
            (units_dir / name).write_text(text)
        print(f"[buildbot] units staged under {units_dir}")
    print(f"[buildbot] master REST http://127.0.0.1:{a.www_port} "
          f"(loopback only); PB :{a.pb_port}")

    if a.setup:
        # Console scripts (<pydir>/buildbot) only exist in a real
        # venv; the embeddable Windows Python has none, so invoke the
        # console entry points through `python -c` there.
        if sys.platform == "win32":
            bb = [py, "-c",
                  "from buildbot.scripts.runner import run; run()"]
            bw = [py, "-c",
                  "from buildbot_worker.scripts.runner import run; run()"]
        else:
            bb = [str(Path(py).parent / "buildbot")]
            bw = [str(Path(py).parent / "buildbot-worker")]
        if not (master_dir / "buildbot.tac").is_file():
            print("[buildbot] create-master")
            subprocess.run(bb + ["create-master", str(master_dir)],
                           check=True)
            tac_cfg = master_dir / "master.cfg"
            if tac_cfg.exists() or tac_cfg.is_symlink():
                tac_cfg.unlink()
            _link_master_cfg(home / "master.cfg", tac_cfg)
        print("[buildbot] upgrade-master")
        subprocess.run(bb + ["upgrade-master", str(master_dir)],
                       check=True)
        for name, d in ((LOCAL_WORKER, local_dir),
                        (SSH_WORKER, disp_dir)):
            if (d / "buildbot.tac").is_file():
                continue
            print(f"[buildbot] create-worker {name}")
            subprocess.run(
                bw + ["create-worker", str(d),
                      f"127.0.0.1:{a.pb_port}", name, wpw[name]],
                check=True)
    else:
        print("[buildbot] run again with --setup to create the "
              "master + worker basedirs (installer performs this)")


def _link_master_cfg(real: Path, link: Path) -> None:
    """Place the home-level master.cfg inside the master basedir.

    master.cfg resolves its JSON neighbours via realpath(__file__), so
    a plain copy inside the basedir would break the lookup — a symlink
    keeps the real file (and BB_DIR) at <home>. Windows symlink creation
    needs SeCreateSymbolicLinkPrivilege (held by elevated installers);
    fall back to a thin exec-shim that runs the real file under its own
    __file__ when the link cannot be created.
    """
    try:
        link.symlink_to(real)
    except OSError:
        link.write_text(
            "import os\n"
            f"_real = {str(real)!r}\n"
            "exec(compile(open(_real, 'rb').read(), _real, 'exec'),\n"
            "     {'__file__': _real, '__name__': '__main__'})\n")


if __name__ == "__main__":
    main()
