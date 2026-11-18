#!/usr/bin/env python3
"""TestOrchestrator installer (§13) — native Linux path.

Provisions payload + persistent data/config areas
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
SERVICE_USER = "testorchestrator"

# Everything the supported runtime needs — a missing or silently-skipped
# item is an installer defect (2.0.2 shipped without reporting_labs and
# report generation failed on installed systems). Every name must exist
# at the package root; an absent source aborts the install instead of
# producing a half-complete payload. Vendored reporting-labs arrives via
# 3rdparty/reporting_labs inside the "3rdparty" item.
PAYLOAD_ITEMS = [
    "testorchestrator", "wsgi.py", "requirements.txt",
    "release", "3rdparty", "migrations", "deploy", "scripts",
]
# Files that MUST be present in the payload after copying — checked
# before the install is allowed to proceed.
REQUIRED_PAYLOAD_FILES = (
    "testorchestrator/__init__.py",
    "wsgi.py",
    "3rdparty/reporting_labs/__init__.py",
    "3rdparty/reporting_labs/LICENSE",
)


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
    # Optional licence seed — the application licence is informational
    # only (it never gates install or functionality). When a code is
    # supplied via TO_LICENCE_CODE (never logged) it is validated early
    # and persisted for the portal's status display; invalid or absent
    # codes never abort the install.
    code = os.environ.get("TO_LICENCE_CODE", "").strip()
    if not code and not args.yes and not args.dry_run:
        code = input("Licence code (optional — press Enter to skip): "
                     ).strip()
    if not args.dry_run and code:
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
        # Application licences are informational only: install never
        # fails over licence state. A valid code is persisted so the
        # portal can report status; rejected/expired codes warn and the
        # installation proceeds without one.
        pub = (REPO / "release" / "licence_pubkey.pem")
        if not pub.exists():
            print("[licence] warn: verification key missing "
                  "(release/licence_pubkey.pem) — continuing without "
                  "a licence")
        else:
            register_key("prod", pub.read_bytes())
            r = parse(code)
            bad = None
            if isinstance(r, str):
                bad = f"rejected — {r}"
            else:
                import datetime as _dt
                _now = _dt.datetime.now(_dt.timezone.utc)
                if _now < r["_from"]:
                    bad = (f"not yet valid "
                           f"(starts {r['valid_from'][:10]} UTC)")
                elif _now >= r["_until"]:
                    bad = f"expired {r['valid_until'][:10]} UTC"
            if bad:
                print(f"[licence] warn: licence {bad} — continuing "
                      "without a licence")
            else:
                print(f"[licence] valid — {r['customer']} "
                      f"({r['valid_from'][:10]} to "
                      f"{r['valid_until'][:10]} UTC)")
                # persist for the installed app's status display
                lic = {k: r[k] for k in ("licence_id","customer",
                                         "valid_from","valid_until",
                                         "key_id")}
                lic["code"] = code
                os.makedirs(args.data, exist_ok=True)
                import json as _j
                (Path(args.data)/"licence.json").write_text(
                    _j.dumps([lic]))

    if not args.dry_run and not port_free(args.https_port):
        sys.exit(
            f"error: port {args.https_port} is occupied. Re-run with an "
            "explicit --https-port after validating the alternative."
        )

    steps = [
        ("payload copy", payload, PAYLOAD_ITEMS),
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
            if not src.exists():
                sys.exit(f"error: payload source missing: {item} — "
                         "incomplete package, refusing to install")
            if src.is_dir():
                copy_payload_tree(src, dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        missing = [r for r in REQUIRED_PAYLOAD_FILES
                   if not (payload / r).is_file()]
        if missing:
            sys.exit("error: payload incomplete after copy — "
                     f"missing: {', '.join(missing)}")
        os.chmod(data / "certs", 0o700)

        # Upgrade cleanup: 2.0 dropped bundled Allure/Node. The copy
        # above merges into an existing payload, so remove ONLY the
        # application-owned runtime dirs left over from a previous
        # install. System-wide node installations are untouched.
        for _stale in ("nodejs", "allure3"):
            _d = payload / "3rdparty" / _stale
            if _d.is_dir():
                shutil.rmtree(_d, ignore_errors=True)
                print(f"[install] removed stale {_d}")
        # reporting_labs moved to 3rdparty/reporting_labs: drop a stale
        # root-level copy (2.0.x payload or operator-applied workaround)
        # so the vendored package exists exactly once in the payload.
        _stale_rl = payload / "reporting_labs"
        if _stale_rl.is_dir():
            shutil.rmtree(_stale_rl, ignore_errors=True)
            print(f"[install] removed superseded {_stale_rl} "
                  "(now at 3rdparty/reporting_labs)")

        # Bundled Buildbot-worker NSIS installer — the portal serves it as a
        # download; the tarball ships it under packaging/windows but the
        # app payload must hold it too.
        install_worker_installer_asset(REPO, payload)

        # Offline venv from the bundled runtime + vendored wheels —
        # no host python/pip and no network required.
        try:
            venv = create_venv_offline(payload)
            print(f"[install] venv: {venv}")
        except SystemExit as exc:
            print(f"[install] WARNING: {exc} — venv step skipped")

        # Buildbot orchestrator (docs/orchestration-replacement.md) —
        # the sole execution backend. The stack needs its own
        # interpreter env (buildbot + paramiko): a bundled
        # buildbot-venv in packaged installs, or TO_BUILDBOT_PYTHON
        # on development hosts. Files + units are always staged;
        # --setup (basedir creation) needs the env.
        bb_home = data / "buildbot"
        bb_venv = payload / "buildbot-venv"
        bb_py = str(bb_venv / "bin" / "python3")
        if not Path(bb_py).exists():
            bb_wheels = payload / "3rdparty" / "buildbot-wheels"
            if bb_wheels.is_dir():
                # Offline controller-side stack — created once;
                # existing installs keep their venv on rerun.
                print(f"[install] buildbot venv: {bb_venv} "
                      f"(offline from {bb_wheels})")
                subprocess.check_call(
                    [sys.executable, "-m", "venv", str(bb_venv)])
                subprocess.check_call(
                    [bb_py, "-m", "pip", "install", "--no-index",
                     "--find-links", str(bb_wheels),
                     "buildbot[bundled]", "buildbot-worker",
                     "buildbot-www", "paramiko"])
            else:
                bb_py = os.environ.get("TO_BUILDBOT_PYTHON") or ""
        sys.path.insert(0, str(REPO / "scripts"))
        import provision_buildbot
        bb_args = ["--home", str(bb_home),
                   "--repo-root", str(REPO)]
        if bb_py:
            bb_args += ["--python", bb_py, "--setup"]
        else:
            print("[install] buildbot files+units staged; "
                  "basedirs NOT created — set TO_BUILDBOT_PYTHON "
                  "to a python with buildbot installed and re-run "
                  "provision_buildbot.py --setup")
        provision_buildbot.main(bb_args)
        for unit in (bb_home / "units").glob("*.service"):
            shutil.copy2(unit, config / unit.name)
        print(f"[install] buildbot provisioned: {bb_home}")

        # Service units: web (Cheroot in-process tier), dispatch
        # worker, buildbot master + workers — full lifecycle wiring,
        # activation needs root on the target host.
        _stage_web_unit(config, payload, data)
        _write_web_env(config, data, args.https_port,
                       args.http_port)
        (config / "testorchestrator-worker.service").write_text(
            f"[Unit]\nDescription=TestOrchestrator dispatch worker\n"
            f"After=network.target\n\n[Service]\nType=simple\n"
            f"User=testorchestrator\nGroup=testorchestrator\n"
            f"WorkingDirectory={payload}\n"
            f"EnvironmentFile={config}/web.env\n"
            f"Environment=FLASK_APP=wsgi:app\n"
            f"ExecStartPre={payload}/venv/bin/python "
            "-m testorchestrator db-upgrade\n"
            f"ExecStart={payload}/venv/bin/flask worker\n"
            f"Restart=on-failure\nRestartSec=5\n"
            f"NoNewPrivileges=true\nPrivateTmp=true\n"
            f"ProtectSystem=strict\nProtectHome=true\n"
            f"ReadWritePaths={data}\n\n"
            f"[Install]\nWantedBy=multi-user.target\n")

        # Migrate now so the first service start doesn't race an empty
        # DB (the web/worker units also carry ExecStartPre=db-upgrade).
        if not args.dry_run:
            env = dict(os.environ)
            for line in (config / "web.env").read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v
            subprocess.check_call(
                [str(payload / "venv" / "bin" / "python"),
                 "-m", "testorchestrator", "db-upgrade"],
                cwd=payload, env=env)
            print("[install] database migrated to head")

        # Enable + start when authorized; --no-service-start or non-root
        # leaves units staged under config for manual activation.
        enable_services(args, config)

    print(f"[install] local URL: https://localhost"
          f"{'' if args.https_port == 443 else f':{args.https_port}'}/")
    print("[install] logs: journalctl -u testorchestrator-web "
          "-u testorchestrator-worker "
          "-u testorchestrator-buildbot-master "
          "-u testorchestrator-buildbot-local "
          "-u testorchestrator-buildbot-ssh")
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
              f"{' '.join(_enabled_units(args))}; bootstrap "
              "Admin via local browser or `flask set-admin-password` "
              "(headless).")


def bundled_python(payload: Path) -> Path:
    rt = payload / "3rdparty" / "python-runtime" / "python" / "bin" / "python3"
    return rt if rt.exists() else None


# --- services + readiness ------------------------------------------------

UNITS = ("testorchestrator-web", "testorchestrator-worker")


def _write_web_env(config: Path, data: Path,
                   https_port: int = 443,
                   http_port: int = 8080) -> None:
    """Write the EnvironmentFile the staged units require. Secret values
    already on disk are PRESERVED across reruns — regenerating
    TO_SECRET_KEY would silently invalidate every session and
    TO_INSTALL_ID identifies the install location, not the build."""
    env_file = config / "web.env"
    kept = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            key, _, val = line.partition("=")
            if key.strip() in ("TO_SECRET_KEY", "TO_INSTALL_ID"):
                kept[key.strip()] = val.strip()
    preserved = "TO_SECRET_KEY" in kept
    kept.setdefault("TO_SECRET_KEY", os.urandom(24).hex())
    kept.setdefault("TO_INSTALL_ID", os.urandom(12).hex())
    env_file.write_text(
        f"TO_INSTANCE_DIR={data}\n"
        f"TO_CONFIG_DIR={config}\n"
        "TO_SECURE_COOKIES=1\n"
        f"TO_SECRET_KEY={kept['TO_SECRET_KEY']}\n"
        f"TO_INSTALL_ID={kept['TO_INSTALL_ID']}\n"
        f"TO_BUILDBOT_ENDPOINT=http://127.0.0.1:8010\n"
        f"TO_BUILDBOT_HOME={data}/buildbot\n"
        f"TO_BUILDBOT_PIDFILE={data}/buildbot/master/twistd.pid\n"
        f"TO_BUILDBOT_PB_PORT=9989\n"
        f"TO_BUILDBOT_WWW_PORT=8010\n"
        f"TO_HTTPS_PORT={https_port}\n"
        f"TO_HTTP_PORT={http_port}\n")
    os.chmod(env_file, 0o640)
    print(f"[install] web.env: {env_file} "
          f"(secrets {'preserved' if preserved else 'generated'})")


BB_UNITS = (
    "testorchestrator-buildbot-master",
    "testorchestrator-buildbot-local",
    "testorchestrator-buildbot-ssh",
)


def _stage_web_unit(config: Path, payload: Path, data: Path) -> None:
    """Stage the web unit under its canonical name — the Cheroot
    in-process tier is the only shipped frontend. The deploy template
    carries the default /opt paths; substitute the requested dirs."""
    text = (REPO / "deploy" / "testorchestrator-web.service").read_text()
    text = (text
            .replace("/opt/testorchestrator", str(payload))
            .replace("/etc/testorchestrator", str(config))
            .replace("/var/lib/testorchestrator", str(data))
            # Fresh install must not crash-loop the services on an
            # unmigrated DB — migrate before every web start (matches
            # the frozen toctl unit's ExecStartPre).
            .replace("ExecStart=",
                     f"ExecStartPre={payload}/venv/bin/python "
                     "-m testorchestrator db-upgrade\nExecStart=", 1))
    (config / "testorchestrator-web.service").write_text(text)


def _enabled_units(args) -> tuple:
    """The shipped unit set: web + dispatch worker + buildbot
    master/local/ssh workers."""
    return UNITS + BB_UNITS


def _systemctl(*argv, check=True):
    return subprocess.run(["systemctl", *argv], check=check,
                          stdout=subprocess.DEVNULL)


def enable_services(args, config: Path) -> bool:
    """Install units and enable/start them — only when authorized
    (root + systemd present) and not --no-service-start. The web unit is
    restarted when its staged unit content changed."""
    if args.no_service_start or args.dry_run:
        return False
    if os.geteuid() != 0 or not shutil.which("systemctl"):
        print("[install] units staged only — enable manually when "
              "root/systemd is available")
        return False
    units = _enabled_units(args)
    staged_web = (config / "testorchestrator-web.service").read_bytes()
    installed_web = Path(
        "/etc/systemd/system/testorchestrator-web.service")
    web_changed = (not installed_web.exists()
                   or installed_web.read_bytes() != staged_web)
    for u in units:
        src = config / f"{u}.service"
        if src.exists():
            shutil.copy2(src, f"/etc/systemd/system/{u}.service")
        else:
            print(f"[install] WARNING: unit source missing for {u}")
    subprocess.check_call(["systemctl", "daemon-reload"])
    web_active = _systemctl("is-active", "--quiet",
                            "testorchestrator-web",
                            check=False).returncode == 0
    # Tolerate a Caddy-era unit left by an older install — the shipped
    # release has exactly one frontend (the in-process tier), so any
    # stale proxy unit is stopped and disabled.
    _systemctl("disable", "--now", "testorchestrator-caddy",
               check=False)
    if web_active and web_changed:
        # Unit content changed (frontend switch or template update) —
        # enable --now alone never restarts a running service.
        subprocess.check_call(
            ["systemctl", "restart", "testorchestrator-web"])
    subprocess.check_call(["systemctl", "enable", "--now", *units])
    print(f"[install] services enabled+started: {', '.join(units)}")
    return True


def readiness_check(args) -> str:
    """Post-start probe, in order: every unit active -> app answers on
    loopback -> the configured PUBLIC HTTPS endpoint serves. A reachable
    login page alone is NOT readiness — workers,
    buildbot, and the TLS endpoint are each verified."""
    import time, urllib.request, ssl
    time.sleep(2)
    failed = [u for u in _enabled_units(args)
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
                "the web unit (Cheroot frontend) + cert provisioning")
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


def install_worker_installer_asset(repo: Path, payload: Path) -> Path:
    """Install the bundled Buildbot-worker NSIS installer into the
    payload so the portal's /infrastructure/buildbot-worker-installer/
    download serves it — the package ships it, the installer must not
    strand it. Removes superseded versions left by an older install.
    A missing artifact warns (dev builds without GPL approval) — the
    release checklist verifies its presence."""
    src_dir = repo / "packaging" / "windows"
    exes = sorted(src_dir.glob(
        "TestOrchestrator-Buildbot-Worker-Setup-*.exe"))
    dst_dir = payload / "packaging" / "windows"
    dst_dir.mkdir(parents=True, exist_ok=True)
    for stale in dst_dir.glob(
            "TestOrchestrator-Buildbot-Worker-Setup-*.exe"):
        stale.unlink()
    for stale in dst_dir.glob(
            "TestOrchestrator-Buildbot-Sources-*.tar.gz"):
        stale.unlink()
    if not exes:
        print("[install] WARNING: no buildbot-worker installer in the "
              "package — Windows worker download will show the "
              "not-bundled error")
        return None
    dst = dst_dir / exes[-1].name
    shutil.copy2(exes[-1], dst)
    print(f"[install] worker installer asset: {dst.name}")
    # The corresponding-source archive must sit beside the .exe — the
    # Admin-UI download serves it from this same directory and the
    # §3(a) pairing is broken (404) if the installer strands it.
    for src in sorted(src_dir.glob(
            "TestOrchestrator-Buildbot-Sources-*.tar.gz")):
        shutil.copy2(src, dst_dir / src.name)
        print(f"[install] corresponding-sources asset: {src.name}")
    return dst


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
