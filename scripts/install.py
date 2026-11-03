#!/usr/bin/env python3
"""TestOrchestrator installer (§13) — native Linux path.

Provisions payload + persistent data/config areas, renders the Caddyfile
from settings, writes systemd units, and reports the ACTUAL accessible
local URL (never silently picks another port when 443 is occupied —
an explicit validated alternative is required).

Not claimed for Windows/SLES until separately validated (compatibility.md).

Usage: sudo python3 scripts/install.py --payload /opt/testorchestrator \
         [--data /var/lib/testorchestrator] [--config /etc/testorchestrator] \
         [--java-home /path/to/jdk] [--no-allure]

Java (Allure reporting prerequisite): detected or --java-home, then
validated by an actual report-generation probe executed as the service
account. The validated path is persisted in web.env scoped to the app —
no global Java changes, KRE untouched. Without a compatible runtime the
installer offers recheck/selection interactively, or continues with
Allure disabled via --no-allure (clear warning; native KRE reports and
artifacts unaffected).
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVICE_USER = "testorchestrator"


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
    parser.add_argument("--java-home", default="",
                        help="JAVA_HOME for Allure reporting (dir or "
                             "path to the java binary). Empty = "
                             "auto-detect.")
    parser.add_argument("--no-allure", action="store_true",
                        help="continue installation with Allure "
                             "reporting disabled when no compatible "
                             "Java runtime is found")
    parser.add_argument("--service-user", default=SERVICE_USER)
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

        # Java prerequisite for Allure: detect or use --java-home,
        # validate with a REAL generation probe running as the service
        # account, then persist TO_ALLURE_JAVA_HOME in web.env (scoped
        # to the app services — never a global Java setting).
        ensure_service_user(args.service_user, data)
        configure_java(args, payload, data, config)

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


# --- Java prerequisite for Allure (external runtime, scoped) ----------

def ensure_service_user(user: str, data: Path) -> None:
    """The service units run as this account — create it when absent.
    Skipped silently off-root (dev/dry installs keep their user)."""
    import pwd
    try:
        pwd.getpwnam(user)
        return
    except KeyError:
        pass
    if os.geteuid() != 0:
        print(f"[install] NOTE: service account {user} does not exist "
              "and installer is not root — create it before enabling "
              "the units")
        return
    subprocess.check_call([
        "useradd", "--system", "--user-group",
        "--home-dir", str(data), "--shell", "/usr/sbin/nologin", user])
    print(f"[install] service account created: {user}")


def _probe_python(payload: Path) -> str:
    """Prefer the just-created venv python; fall back to the bundled
    runtime, then the installer interpreter."""
    for cand in (payload / "venv" / "bin" / "python",
                 payload / "3rdparty" / "python-runtime" / "python" /
                 "bin" / "python3"):
        if cand.exists():
            return str(cand)
    return sys.executable


def _run_as(user: str, cmd: list) -> subprocess.CompletedProcess:
    """Run cmd as the service account when we can (root + runuser);
    otherwise run it directly. The probe must prove the SERVICE account
    can execute Java — running it as root alone is not evidence."""
    import pwd
    if os.geteuid() == 0:
        try:
            pwd.getpwnam(user)
            return subprocess.run(["runuser", "-u", user, "--"] + cmd,
                                  capture_output=True, text=True)
        except KeyError:
            pass
    return subprocess.run(cmd, capture_output=True, text=True)


def probe_java(payload: Path, java_home: str, user: str) -> dict:
    """Actual-generation probe via the standalone launcher — validates
    that THIS Java produces a report with the pinned bundled Allure,
    executed as the application service account."""
    cmd = [_probe_python(payload),
           str(payload / "scripts" / "java_probe.py"),
           "--runtime", str(payload / "testorchestrator" /
                            "allure_runtime.py")]
    if java_home:
        cmd += ["--java-home", java_home]
    res = _run_as(user, cmd)
    try:
        record = json.loads(res.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError, json.JSONDecodeError):
        record = {"ok": False,
                  "error": (res.stderr or res.stdout or
                            "probe failed to run")[-300:]}
    return record


def detect_java(payload: Path, user: str) -> list:
    cmd = [_probe_python(payload),
           str(payload / "scripts" / "java_probe.py"),
           "--runtime", str(payload / "testorchestrator" /
                            "allure_runtime.py"),
           "--detect"]
    res = _run_as(user, cmd)
    try:
        return json.loads(res.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError, json.JSONDecodeError):
        return []


def persist_java(config: Path, java_home: str, record: dict,
                 payload: Path) -> None:
    """Persist the validated selection where the services read it —
    TO_ALLURE_JAVA_HOME lives ONLY in the app's EnvironmentFile, never
    in /etc/environment, profile.d, or any global Java config."""
    env_file = config / "web.env"
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    keys = ("TO_ALLURE_JAVA_HOME", "TO_ALLURE_JAVA_RECORD")
    lines = [l for l in lines if not any(l.startswith(f"{k}=")
                                         for k in keys)]
    lines.append(f"TO_ALLURE_JAVA_HOME={java_home}")
    record_file = config / "allure-java.json"
    lines.append(f"TO_ALLURE_JAVA_RECORD={record_file}")
    env_file.write_text("\n".join(lines) + "\n")
    os.chmod(env_file, 0o640)
    record_file.write_text(json.dumps(
        {**record, "allure_version": _allure_version(payload)},
        indent=2))
    os.chmod(record_file, 0o640)
    if os.geteuid() == 0:
        import grp
        try:
            gid = grp.getgrnam(SERVICE_USER).gr_gid
            os.chown(env_file, 0, gid)
            os.chown(record_file, 0, gid)
        except KeyError:
            pass


def _allure_version(payload: Path) -> str:
    dirs = sorted((payload / "3rdparty" / "allure").glob("allure-*"))
    return dirs[-1].name.split("-", 1)[1] if dirs else ""


def configure_java(args, payload: Path, data: Path, config: Path) -> None:
    """Select + validate + persist a Java runtime for Allure, or mark
    Allure disabled with a clear warning. Never modifies global Java
    settings and never touches KRE's own runtime."""
    attempted = []
    candidates = ([args.java_home] if args.java_home
                  else detect_java(payload, args.service_user))
    record = {"ok": False, "error": "no Java runtime candidates found"}
    for cand in candidates:
        record = probe_java(payload, cand, args.service_user)
        attempted.append((cand, record.get("ok"), record.get("error", "")))
        if record.get("ok"):
            persist_java(config, record["java_home"], record, payload)
            print(f"[install] allure java: {record['java_home']} "
                  f"(Java {record.get('java_version', '?')}) — validated "
                  "by report-generation probe as "
                  f"{args.service_user}")
            return
    # Nothing validated: interactive recheck, or continue disabled.
    for cand, ok, err in attempted:
        print(f"[install] java probe FAILED at {cand}: {err}")
    if not args.no_allure and sys.stdin.isatty():
        while True:
            ans = input("[install] No compatible Java found for Allure. "
                        "Enter a JAVA_HOME path, 'r' to re-scan, or 'c' "
                        "to continue with Allure disabled: ").strip()
            if ans.lower() == "c":
                break
            if ans.lower() != "r":
                record = probe_java(payload, ans, args.service_user)
                if record.get("ok"):
                    persist_java(config, record["java_home"], record, payload)
                    print("[install] allure java: "
                          f"{record['java_home']} — validated")
                    return
                print(f"[install] probe FAILED: {record.get('error')}")
                continue
            for cand in detect_java(payload, args.service_user):
                record = probe_java(payload, cand, args.service_user)
                if record.get("ok"):
                    persist_java(config, record["java_home"], record, payload)
                    print("[install] allure java: "
                          f"{record['java_home']} — validated")
                    return
    elif not args.no_allure:
        sys.exit(
            "error: no compatible Java runtime found for Allure "
            "reporting. Install a JRE/JDK (OpenJDK 21+ recommended), "
            "re-run with --java-home <path> to select it, or pass "
            "--no-allure to continue with Allure reporting disabled.")
    record = {**record, "allure_disabled": True}
    persist_java(config, "", record, payload)
    print("[install] WARNING: continuing with Allure reporting DISABLED "
          "— native KRE reports and artifacts are unaffected; validate "
          "Java later via Admin -> Reporting -> Check readiness.")


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
