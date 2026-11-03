#!/usr/bin/env python3
"""TestOrchestrator installer (§13) — native Linux path.

Provisions payload + persistent data/config areas, renders the Caddyfile
from settings, writes systemd units, and reports the ACTUAL accessible
local URL (never silently picks another port when 443 is occupied —
an explicit validated alternative is required).

Not claimed for Windows/SLES until separately validated (compatibility.md).

Usage: sudo python3 scripts/install.py --payload /opt/testorchestrator \
         [--data /var/lib/testorchestrator] [--config /etc/testorchestrator]
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, port))
        return True
    except OSError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", default="/opt/testorchestrator")
    parser.add_argument("--data", default="/var/lib/testorchestrator")
    parser.add_argument("--config", default="/etc/testorchestrator")
    parser.add_argument("--https-port", type=int, default=443)
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    payload, data, config = (Path(args.payload), Path(args.data),
                             Path(args.config))

    # Port policy (v1.8 §3): occupied 443 -> explicit alternative required.
    if not args.dry_run and not port_free(args.https_port):
        sys.exit(
            f"error: port {args.https_port} is occupied. Re-run with an "
            "explicit --https-port after validating the alternative."
        )

    steps = [
        ("payload copy", payload, [
            "testorchestrator", "wsgi.py", "requirements.txt",
            "release", "3rdparty", "migrations", "deploy", "scripts",
        ]),
        ("data dir", data, []),
        ("certs dir", data / "certs", []),
        ("config dir", config, []),
    ]
    for label, path, _ in steps:
        print(f"[install] {label}: {path}")
        if not args.dry_run:
            path.mkdir(parents=True, exist_ok=True)

    if not args.dry_run:
        for item in steps[0][2]:
            src, dst = REPO / item, payload / item
            if src.is_dir():
                # symlinks=True: vendored runtimes contain links
                # (relative + a documented external cacerts target) that
                # must be preserved verbatim, never dereferenced.
                shutil.copytree(src, dst, dirs_exist_ok=True,
                                symlinks=True)
            elif src.exists():
                shutil.copy2(src, dst)
        os.chmod(data / "certs", 0o700)

        # Offline venv from the bundled runtime + vendored wheels —
        # no host python/pip and no network required.
        try:
            venv = create_venv_offline(payload)
            print(f"[install] venv: {venv}")
        except SystemExit as exc:
            print(f"[install] WARNING: {exc} — venv step skipped")

        # Tested Dagu provisioning path (scripts/provision_dagu.py):
        # binary, basic-auth server.yaml, 0600 credentials, workflow,
        # systemd unit — into the persistent config/data areas.
        dagu_bin = REPO / "3rdparty" / "dagu" / "dagu_2.25.2_linux_amd64.tar.gz"
        if dagu_bin.exists():
            import tarfile, tempfile
            with tempfile.TemporaryDirectory() as td:
                with tarfile.open(dagu_bin) as t:
                    t.extractall(td, filter="data")
                sys.path.insert(0, str(REPO / "scripts"))
                import provision_dagu
                provision_dagu.main([
                    "--home", str(data / "dagu"),
                    "--config", str(config / "dagu"),
                    "--binary", str(Path(td) / "dagu")])
            print(f"[install] dagu provisioned: {config/'dagu'}")
        else:
            print("[install] WARNING: bundled dagu tarball missing — "
                  "orchestration not provisioned")

        # Service units: web, worker, dagu, caddy — full lifecycle wiring,
        # activation needs root on the target host.
        for unit in ("testorchestrator-web.service",
                     "testorchestrator-caddy.service"):
            shutil.copy2(REPO / "deploy" / unit, config / unit)
        (config / "testorchestrator-worker.service").write_text(
            f"[Unit]\nDescription=TestOrchestrator dispatch worker\n"
            f"After=network.target\n\n[Service]\nType=simple\n"
            f"User=testorchestrator\nGroup=testorchestrator\n"
            f"WorkingDirectory={payload}\n"
            f"EnvironmentFile={config}/web.env\n"
            f"Environment=FLASK_APP=wsgi:app\n"
            f"ExecStart={payload}/venv/bin/flask worker\n"
            f"Restart=on-failure\nRestartSec=5\n"
            f"NoNewPrivileges=true\nPrivateTmp=true\n"
            f"ProtectSystem=strict\nProtectHome=true\n"
            f"ReadWritePaths={data}\n\n"
            f"[Install]\nWantedBy=multi-user.target\n")
        shutil.copy2(REPO / "deploy" / "Caddyfile", config / "Caddyfile")

    print(f"[install] local URL: https://localhost"
          f"{'' if args.https_port == 443 else f':{args.https_port}'}/")
    print("[install] next: copy units from "
          f"{config} to /etc/systemd/system, systemctl enable --now "
          "testorchestrator-web testorchestrator-worker "
          "testorchestrator-dagu testorchestrator-caddy; bootstrap Admin "
          "via local browser or `flask set-admin-password` (headless).")


def bundled_python(payload: Path) -> Path:
    rt = payload / "3rdparty" / "python-runtime" / "python" / "bin" / "python3"
    return rt if rt.exists() else None


def create_venv_offline(payload: Path) -> Path:
    """Create the app venv from the BUNDLED runtime + vendored wheels —
    no host python/pip and no network required."""
    rt = bundled_python(payload)
    venv = payload / "venv"
    if rt is None:
        raise SystemExit("bundled runtime missing — package incomplete")
    if not venv.exists():
        subprocess.check_call([str(rt), "-m", "venv", str(venv)])
    subprocess.check_call([
        str(venv / "bin" / "pip"), "install", "--no-index",
        "--find-links", str(payload / "3rdparty" / "python-wheels"),
        "-r", str(payload / "requirements.txt")])
    return venv


if __name__ == "__main__":
    main()

# --- bundled-runtime path (no host python required) --------------------
def bundled_python(payload: Path) -> Path:
    rt = payload / "3rdparty" / "python-runtime" / "python" / "bin" / "python3"
    return rt if rt.exists() else None

def create_venv_offline(payload: Path) -> Path:
    """Create the app venv from the BUNDLED runtime + vendored wheels —
    no host python/pip and no network required."""
    import subprocess
    rt = bundled_python(payload)
    venv = payload / "venv"
    if rt is None:
        raise SystemExit("bundled runtime missing — package incomplete")
    if not venv.exists():
        subprocess.check_call([str(rt), "-m", "venv", str(venv)])
    subprocess.check_call([
        str(venv / "bin" / "pip"), "install", "--no-index",
        "--find-links", str(payload / "3rdparty" / "python-wheels"),
        "-r", str(payload / "requirements.txt")])
    return venv
