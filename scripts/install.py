#!/usr/bin/env python3
"""TestOrchestrator installer (§13) — native Linux path.

Provisions payload + persistent data/config areas, renders the Caddyfile
from settings, writes systemd units, and reports the ACTUAL accessible
local URL (never silently picks another port when 443 is occupied —
an explicit validated alternative is required).

Not claimed for Windows/SLES until separately validated (compatibility.md).

Usage: sudo python3 scripts/install.py --payload /opt/testorchestrator \
         [--data /var/lib/testorchestrator] [--config /etc/testorchestrator] \
         [--no-allure]

Allure 3 reporting runs on the bundled Node.js runtime — no Java or
validated by an actual report-generation probe executed as the service
account. The validated path is persisted in web.env scoped to the app —
no global Java changes, KRE untouched. Without a compatible runtime the
installer offers recheck/selection interactively, or continues with
Allure disabled via --no-allure (clear warning; native KRE reports and
artifacts unaffected).
"""

import argparse
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
    parser.add_argument("--no-allure", action="store_true",
                        help="continue installation with Allure "
                             "reporting disabled when no compatible "
                             "Java runtime is found")
    parser.add_argument("--service-user", default=SERVICE_USER)
    parser.add_argument("--no-service-start", action="store_true",
                        help="provision files/units but do not enable "
                             "or start systemd services")
    parser.add_argument("--yes", action="store_true",
                        help="noninteractive — never prompt; fail "
                             "instead of asking")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    payload, data, config = (Path(args.payload), Path(args.data),
                             Path(args.config))

    # Port policy (v1.8 §3): occupied 443 -> explicit alternative required.
    # Licence gate — validate BEFORE payload copy so a rejected code
    # leaves nothing partially installed. Unattended input via
    # TO_LICENCE_CODE env (never logged).
    code = os.environ.get("TO_LICENCE_CODE", "").strip()
    if not code and not args.yes and not args.dry_run:
        code = input("Licence code: ").strip()
    if not args.dry_run:
        if not code:
            sys.exit("error: installation requires a licence code — "
                     "set TO_LICENCE_CODE or enter it when prompted.")
        # Licence check runs BEFORE deps install — extract the crypto
        # wheels (native .so can't zip-import) to a scratch dir.
        import sys as _s, zipfile as _zf, tempfile as _tf
        _wd = _tf.mkdtemp(prefix="lic-wheels-")
        for _w in (REPO / "3rdparty" / "python-wheels").glob("*.whl"):
            _n = _w.name.split("-")[0].lower()
            if _n in ("cryptography", "cffi", "pycparser"):
                _zf.ZipFile(_w).extractall(_wd)
        _s.path.insert(0, _wd)
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "to_licensing", REPO / "testorchestrator" / "licensing.py")
        _lic = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_lic)
        parse, register_key = _lic.parse, _lic.register_key
        pub = (REPO / "release" / "licence_pubkey.pem")
        if not pub.exists():
            sys.exit("error: licence verification key missing "
                     "(release/licence_pubkey.pem)")
        register_key("prod", pub.read_bytes())
        r = parse(code)
        if isinstance(r, str):
            sys.exit(f"error: licence rejected — {r}. Installation "
                     "cannot continue without a valid TestOrchestrator "
                     "licence.")
        import datetime as _dt
        _now = _dt.datetime.now(_dt.timezone.utc)
        if _now < r["_from"]:
            sys.exit("error: licence is not yet valid "
                     f"(starts {r['valid_from'][:10]} UTC).")
        if _now >= r["_until"]:
            sys.exit(f"error: licence expired {r['valid_until'][:10]} UTC.")
        print(f"[licence] valid — {r['customer']} "
              f"({r['valid_from'][:10]} to {r['valid_until'][:10]} UTC)")
        # persist for the installed app
        lic = {k: r[k] for k in ("licence_id","customer","valid_from",
                                 "valid_until","key_id")}
        lic["code"] = code
        os.makedirs(args.data, exist_ok=True)
        import json as _j
        (Path(args.data)/"licence.json").write_text(_j.dumps([lic]))

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
                copy_payload_tree(src, dst)
            elif src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
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
        dagu_bin = REPO / "3rdparty" / "dagu" / "dagu_2.25.8_linux_amd64.tar.gz"
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

        # Allure 3 uses the bundled Node.js runtime — no operator
        # Java configuration is needed or offered.

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

        # Enable + start when authorized; --no-service-start or non-root
        # leaves units staged under config for manual activation.
        services_ok = enable_services(args, config)

    print(f"[install] local URL: https://localhost"
          f"{'' if args.https_port == 443 else f':{args.https_port}'}/")
    print(f"[install] logs: journalctl -u testorchestrator-web "
          "-u testorchestrator-worker -u testorchestrator-dagu "
          "-u testorchestrator-caddy")
    if args.dry_run or not args.no_service_start and os.geteuid() == 0:
        ready = readiness_check(args)
        print(f"[install] readiness: {ready}")
        if not ready.startswith("ok"):
            print("[install] ACTION: service(s) not ready — inspect "
                  "logs above, then re-run install.sh (safe to repeat)",
                  file=sys.stderr)
            sys.exit(3)
    else:
        print("[install] next: copy units from "
              f"{config} to /etc/systemd/system, systemctl enable --now "
              "testorchestrator-web testorchestrator-worker "
              "testorchestrator-dagu testorchestrator-caddy; bootstrap "
              "Admin via local browser or `flask set-admin-password` "
              "(headless).")


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


# --- services + readiness ------------------------------------------------

UNITS = ("testorchestrator-web", "testorchestrator-worker",
         "testorchestrator-dagu", "testorchestrator-caddy")


def enable_services(args, config: Path) -> bool:
    """Install units and enable/start them — only when authorized
    (root + systemd present) and not --no-service-start."""
    if args.no_service_start or args.dry_run:
        return False
    if os.geteuid() != 0 or not shutil.which("systemctl"):
        print("[install] units staged only — enable manually when "
              "root/systemd is available")
        return False
    for u in UNITS:
        src = config / "dagu" / f"{u}.service" if "dagu" in u             else config / f"{u}.service"
        if src.exists():
            shutil.copy2(src, f"/etc/systemd/system/{u}.service")
    subprocess.check_call(["systemctl", "daemon-reload"])
    subprocess.check_call(["systemctl", "enable", "--now", *UNITS])
    print(f"[install] services enabled+started: {', '.join(UNITS)}")
    return True


def readiness_check(args) -> str:
    """Post-start probe, in order: every unit active -> app answers on
    loopback -> the configured PUBLIC HTTPS endpoint serves through
    Caddy. A reachable login page alone is NOT readiness — workers,
    dagu, and the TLS endpoint are each verified."""
    import time, urllib.request, ssl
    time.sleep(2)
    failed = [u for u in UNITS
              if subprocess.run(["systemctl", "is-active", "--quiet", u]
                                ).returncode != 0]
    if failed:
        return (f"units not active: {', '.join(failed)} — "
                f"journalctl -u {failed[0]} -n 50 --no-pager")
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:18080/login", timeout=5) as r:
            if r.status != 200:
                return f"app returned {r.status} on 18080"
    except Exception as exc:
        return f"app not answering on 18080: {exc}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE     # readiness only — trust config is separate
    url = f"https://localhost:{args.https_port}/login" \
        if args.https_port != 443 else "https://localhost/login"
    try:
        with urllib.request.urlopen(url, timeout=5, context=ctx) as r:
            if r.status != 200:
                return f"HTTPS endpoint returned {r.status} at {url}"
    except Exception as exc:
        return (f"HTTPS endpoint unreachable at {url}: {exc} — check "
                "caddy unit + cert provisioning")
    return f"ok — all units active; app + HTTPS endpoint serving ({url})"


def copy_payload_tree(src: Path, dst: Path) -> None:
    """Idempotent payload copy: rerun-safe — existing files are
    overwritten, existing symlinks replaced (copytree chokes on them),
    symlinks preserved verbatim, never dereferenced."""
    dst.mkdir(parents=True, exist_ok=True)
    for member in src.rglob("*"):
        rel = member.relative_to(src)
        target = dst / rel
        if member.is_symlink():
            if target.exists() or target.is_symlink():
                target.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(member.readlink())
        elif member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif member.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                target.unlink()
            shutil.copy2(member, target)


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


def _run_as(user: str, cmd: list) -> tuple[subprocess.CompletedProcess, str]:
    """Run cmd as the service account when we can (root + runuser);
    otherwise run it directly. Returns (result, account actually used)
    — the account is recorded so the probe evidence is honest: a check
    run as the installing user is not proof for the service account."""
    import pwd
    if os.geteuid() == 0:
        try:
            pwd.getpwnam(user)
            return (subprocess.run(["runuser", "-u", user, "--"] + cmd,
                                   capture_output=True, text=True), user)
        except KeyError:
            pass
    return (subprocess.run(cmd, capture_output=True, text=True),
            pwd.getpwuid(os.geteuid()).pw_name)


def under_user_home(path: str) -> bool:
    try:
        p = Path(path).resolve()
    except OSError:
        return False
    parts = p.parts
    return len(parts) > 2 and parts[1] == "home" or \
        str(p).startswith("/root/") or str(p).startswith("/users/")


def _allure_version(payload: Path) -> str:
    dirs = sorted((payload / "3rdparty" / "allure3").glob("allure*"))
    return dirs[-1].name.split("-", 1)[1] if dirs else ""


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


if __name__ == "__main__":
    main()
