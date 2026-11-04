"""Frozen application command interface (§7) — one binary, all roles.

    toctl web [--listen HOST:PORT]   production WSGI (Waitress, loopback)
    toctl worker                     dispatch/outbox worker (flask worker)
    toctl db-upgrade                 alembic upgrade to head
    toctl readiness [--url URL]      app + units probe (exit 0/3)
    toctl backup --to DIR            state backup -> backup-*.tar.gz
    toctl recover --from FILE --into DIR
    toctl install-info               JSON of detected install context

Every entry point works in the frozen build — no external python, no
flask CLI, no venv. install.sh and systemd units call `toctl`.
"""

import argparse
import sys


def _app():
    from . import create_app
    return create_app()


def cmd_web(args) -> int:
    from waitress import serve
    app = _app()
    serve(app, listen=args.listen or app.config["WAITRESS_LISTEN"])
    return 0


def cmd_worker(args) -> int:
    # The worker is a registered flask CLI command — invoke it in-process.
    import traceback
    app = _app()
    res = app.test_cli_runner().invoke(
        args=["worker"] + list(args.extra or []))
    sys.stdout.write(res.output)
    if res.exception:
        traceback.print_exception(res.exception)
    return res.exit_code or 0


def _migrations_dir() -> str:
    """Alembic scripts path — inside _MEIPASS when frozen, repo root
    otherwise."""
    import sys
    from pathlib import Path
    base = Path(getattr(sys, "_MEIPASS",
                        Path(__file__).resolve().parent.parent))
    return str(base / "migrations")


def cmd_db_upgrade(args) -> int:
    app = _app()
    with app.app_context():
        from flask_migrate import upgrade
        upgrade(directory=_migrations_dir())
    return 0


def cmd_readiness(args) -> int:
    import urllib.request
    url = args.url.rstrip("/") + "/login"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            ok = r.status == 200
    except Exception as exc:
        print(f"not ready — {url}: {exc}", file=sys.stderr)
        return 3
    print(f"ready — {url} -> 200")
    return 0


def _port_occupied(url: str, timeout: float = 2.0) -> bool:
    """Is something already listening on the configured app address?
    An occupied port means a fresh install cannot come up — only an
    --upgrade of that existing install may proceed."""
    import socket
    from urllib.parse import urlparse
    u = urlparse(url)
    try:
        with socket.create_connection(
                (u.hostname or "127.0.0.1", u.port or 80), timeout=timeout):
            return True
    except OSError:
        return False


def _install_health(url: str, timeout: float = 5.0) -> dict:
    """GET <url>/healthz — returns {install_id, version} or {} when the
    service is not actually up."""
    import json as _json
    import urllib.request
    try:
        with urllib.request.urlopen(
                url.rstrip("/") + "/healthz", timeout=timeout) as r:
            return _json.loads(r.read().decode())
    except Exception:
        return {}


def _verify_install(args, install_id: str) -> int:
    """Readiness tied to THIS install: the health endpoint must answer
    with the install_id written into web.env — an unrelated listener
    returning 200 can never satisfy a fresh install."""
    health = _install_health(args.url)
    if not health:
        print("[install] INCOMPLETE — service did not start or is not "
              "reachable; units staged but readiness not verified "
              "(start as root, then re-run `toctl readiness`)",
              file=sys.stderr)
        return 4
    if install_id and health.get("install_id") != install_id:
        print("[install] FAILED — a different TestOrchestrator instance "
              "answers on the configured port; refusing to claim its "
              "readiness for this install", file=sys.stderr)
        return 4
    print(f"[install] ready — install {install_id[:8]} "
          f"build {health.get('version', '?')}")
    return 0


def cmd_backup(args) -> int:
    from pathlib import Path
    app = _app()
    with app.app_context():
        from .updater import backup_state
        out = backup_state(Path(args.to),
                           [Path(app.instance_path),
                            app.config["CERT_DIR"]])
        print(out)
    return 0


def cmd_recover(args) -> int:
    from pathlib import Path
    from .updater import recover
    res = recover(Path(args.from_file), Path(args.into))
    print(res)
    return 0


def cmd_java_probe(args) -> int:
    """JSON Java detection/probe — runs under whatever account invoked
    it (service-account verification = run it AS that account)."""
    import json
    app = _app()
    with app.app_context():
        from . import allure_runtime
        cands = [str(c) for c in allure_runtime.detect_java_homes()]
        home = args.java_home or (cands[0] if cands else "")
        rec = allure_runtime.select_and_validate(home) if home else             {"ok": False, "error": "no Java runtime candidates found"}
        print(json.dumps({"candidates": cands,
                          "selected": home, **rec}))
    return 0 if rec.get("ok") else 1


def _run_toctl(cmd: list, env: dict, timeout: int = 900):
    """Re-execute this CLI for env-dependent steps — env is resolved
    at the CHILD's import time (module constants bind at import).
    Frozen: sys.executable IS toctl. Source: python toctl.py."""
    import subprocess
    from pathlib import Path
    if getattr(sys, "frozen", False):
        argv = [sys.executable, *cmd]
    else:
        argv = [sys.executable,
                str(Path(__file__).resolve().parent.parent / "toctl.py"),
                *cmd]
    return subprocess.run(argv, env=env, capture_output=True,
                          text=True, timeout=timeout)


def cmd_install_info(args) -> int:
    import json, os
    app = _app()
    print(json.dumps({"instance": app.instance_path,
                      "db": app.config["SQLALCHEMY_DATABASE_URI"],
                      "cert_dir": str(app.config["CERT_DIR"]),
                      "frozen": bool(getattr(sys, "frozen", False)),
                      "python": sys.executable}, indent=2))
    return 0


# ---------- frozen-package installation (no external python) ----------

def _app_dir() -> "Path":
    """The application payload dir — the onedir containing toctl when
    frozen, else the repo app dir."""
    from pathlib import Path
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent / "app"


def _write_unit(path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def cmd_install(args) -> int:
    """Install the frozen package — payload copy, dirs, web.env,
    migrations, Java probe, unit staging/enable, readiness.

    NO external python, pip, flask CLI, or venv anywhere in this path.
    """
    import json, os, shutil, subprocess
    from pathlib import Path
    payload = Path(args.payload).resolve()
    data = Path(args.data).resolve()
    config = Path(args.config).resolve()
    app_src = _app_dir()              # the frozen onedir (or app/)
    pkg = app_src.parent              # package root: 3rdparty/deploy here
    toctl = payload / "app" / "toctl"

    # 0) Pre-flight: an occupied app port means a fresh install cannot
    #    come up — only an explicit --upgrade may reuse it. This blocks
    #    the "stale listener returns 200" false-success class entirely.
    if not args.dry_run and _port_occupied(args.url) and not args.upgrade:
        print(f"[install] FAILED — {args.url} already answers; a fresh "
              "install cannot start its service there. If this is an "
              "in-place upgrade pass --upgrade (its readiness is then "
              "verified against this install's id).", file=sys.stderr)
        return 2

    if not args.dry_run:
        # 1) payload copy — rerun-safe (files overwrite, symlinks kept)
        if app_src.is_dir():          # the onedir -> payload/app
            _copy_tree(app_src, payload / "app")
        for item in ("3rdparty", "deploy"):
            src = pkg / item
            if src.is_dir():
                _copy_tree(src, payload / item)
        data.mkdir(parents=True, exist_ok=True)
        config.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pkg / "deploy" / "Caddyfile",
                     config / "Caddyfile")

    # 2) web.env — app-scoped config; writable data lives outside payload
    env_file = config / "web.env"
    install_id = os.urandom(12).hex()
    env_lines = [
        f"TO_INSTANCE_DIR={data}",
        "TO_SECURE_COOKIES=1",
        f"TO_SECRET_KEY={args.secret_key or os.urandom(24).hex()}",
        f"TO_INSTALL_ID={install_id}",
    ]
    if not args.dry_run:
        env_file.write_text("\n".join(env_lines) + "\n")
        os.chmod(env_file, 0o640)

    # 3+4) migrations + Java probe — as CHILD toctl processes so env
    #      (TO_INSTANCE_DIR) resolves at their import time. In-process
    #      env-setting is too late: config constants bind at import.
    java_home = args.java_home or ""
    if not args.dry_run:
        env = dict(os.environ,
                   TO_INSTANCE_DIR=str(data),
                   TO_SECRET_KEY=env_lines[2].split("=", 1)[1])
        res = _run_toctl(["db-upgrade"], env)
        if res.returncode != 0:
            sys.stderr.write(res.stderr[-2000:])
            print("[install] migration failed", file=sys.stderr)
            return 2
        if res.stderr.strip():
            sys.stderr.write(res.stderr)

        if not args.no_allure:
            jcmd = ["java-probe"] + (["--java-home", java_home]
                                     if java_home else [])
            jr = _run_toctl(jcmd, env, timeout=600)
            try:
                jrec = json.loads(jr.stdout.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                jrec = {"ok": False,
                        "error": jr.stderr.strip()[:200] or
                                 "probe produced no output"}
            if jrec.get("ok"):
                java_home = jrec.get("selected") or java_home
                print(f"[install] allure java: {java_home} "
                      f"({jrec.get('java_version', '?')}) — validated "
                      "by generation probe")
            else:
                print(f"[install] Java probe: {jrec.get('error','')} — "
                      "continuing with Allure disabled (install/select "
                      "Java then recheck in Admin → Reporting)")
    if java_home and not args.dry_run:
        with open(env_file, "a") as f:
            f.write(f"TO_ALLURE_JAVA_HOME={java_home}\n")

    # 5) systemd units — ExecStart on the frozen binary, never flask
    #    CLI or a venv.
    su = args.service_user
    unit_common = (f"[Unit]\nAfter=network.target\n\n"
                   f"[Service]\nType=simple\nUser={su}\nGroup={su}\n"
                   f"WorkingDirectory={payload}\n"
                   f"EnvironmentFile={env_file}\n"
                   f"Restart=on-failure\nRestartSec=5\n"
                   f"NoNewPrivileges=true\nPrivateTmp=true\n"
                   f"ProtectSystem=strict\nProtectHome=true\n"
                   f"ReadWritePaths={data}\n\n"
                   f"[Install]\nWantedBy=multi-user.target\n")
    units = {
        "testorchestrator-web.service":
            ("TestOrchestrator web",
             f"ExecStart={toctl} web\n"
             f"ExecStartPre={toctl} db-upgrade\n"),
        "testorchestrator-worker.service":
            ("TestOrchestrator dispatch worker",
             f"ExecStart={toctl} worker\n"),
    }
    if not args.dry_run:
        for name, (desc, exec_lines) in units.items():
            head = (f"[Unit]\nDescription={desc}\n")
            body = head + unit_common.split("[Unit]\n", 1)[1]
            # put ExecStartPre before ExecStart inside [Service]
            body = body.replace("[Service]\n",
                                "[Service]\n" + exec_lines, 1)
            _write_unit(config / name, body)

    # 6) enable/start when authorized
    if not args.dry_run and not args.no_service_start \
            and os.geteuid() == 0 and shutil.which("systemctl"):
        for name in units:
            shutil.copy2(config / name, f"/etc/systemd/system/{name}")
        subprocess.check_call(["systemctl", "daemon-reload"])
        subprocess.check_call(
            ["systemctl", "enable", "--now", *units.keys()])
        print(f"[install] services enabled+started: "
              f"{', '.join(units)}")
    elif not args.dry_run:
        print("[install] units staged in config — not started; enable "
              "as root when ready (staging ≠ enablement)")

    # 7) readiness — tied to THIS install via its install_id; a foreign
    #    listener can never satisfy it. Non-started installs get an
    #    explicit INCOMPLETE status, never a claimed-ready.
    if not args.dry_run:
        rc = _verify_install(args, install_id)
        if rc != 0:
            return rc
    print(f"[install] done — URL https://localhost"
          f"{'' if args.https_port == 443 else ':' + str(args.https_port)}")
    return 0


def _copy_tree(src, dst) -> None:
    import shutil
    dst.mkdir(parents=True, exist_ok=True)
    for m in src.rglob("*"):
        rel = m.relative_to(src)
        t = dst / rel
        if m.is_symlink():
            if t.exists() or t.is_symlink():
                t.unlink()
            t.parent.mkdir(parents=True, exist_ok=True)
            t.symlink_to(m.readlink())
        elif m.is_dir():
            t.mkdir(parents=True, exist_ok=True)
        elif m.is_file():
            t.parent.mkdir(parents=True, exist_ok=True)
            if t.is_symlink():
                t.unlink()
            shutil.copy2(m, t)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if argv and argv[0] == "worker":
        # forwarded verbatim to the flask worker command — argparse
        # must not consume its --flags
        extra, argv = argv[1:], argv[:1]
    ap = argparse.ArgumentParser(prog="toctl")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("web").add_argument("--listen")
    sub.add_parser("worker",
                   help="dispatch/outbox worker (extra args forwarded)")
    sub.add_parser("db-upgrade")
    p = sub.add_parser("readiness"); p.add_argument("--url",
                                                  default="http://127.0.0.1:18080")
    p = sub.add_parser("backup"); p.add_argument("--to", required=True)
    p = sub.add_parser("recover"); p.add_argument("--from", dest="from_file",
                                                required=True)
    p.add_argument("--into", required=True)
    sub.add_parser("install-info")
    p = sub.add_parser("java-probe",
                       help="detect + validate Java for Allure (JSON)")
    p.add_argument("--java-home", default="")
    p = sub.add_parser(
        "install",
        help="install the frozen package — payload, config, "
             "migrations, services; no external python")
    p.add_argument("--payload", default="/opt/testorchestrator")
    p.add_argument("--data", default="/var/lib/testorchestrator")
    p.add_argument("--config", default="/etc/testorchestrator")
    p.add_argument("--https-port", type=int, default=443)
    p.add_argument("--service-user", default="testorchestrator")
    p.add_argument("--secret-key", default="")
    p.add_argument("--java-home", default="")
    p.add_argument("--no-allure", action="store_true")
    p.add_argument("--no-service-start", action="store_true")
    p.add_argument("--upgrade", action="store_true",
                   help="in-place upgrade: an occupied app port is "
                        "expected; readiness still verifies the new "
                        "install's own id")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--url", default="http://127.0.0.1:18080")
    p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    args.extra = extra if args.cmd == "worker" else []
    fn = {"web": cmd_web, "worker": cmd_worker, "db-upgrade": cmd_db_upgrade,
          "readiness": cmd_readiness, "backup": cmd_backup,
          "recover": cmd_recover, "install-info": cmd_install_info,
          "install": cmd_install, "java-probe": cmd_java_probe}
    return fn[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
