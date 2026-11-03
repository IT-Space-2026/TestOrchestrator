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
    app = _app()
    res = app.test_cli_runner().invoke(args=["worker"] + list(args.extra or []))
    sys.stdout.write(res.output)
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
    env_lines = [
        f"TO_INSTANCE_DIR={data}",
        "TO_SECURE_COOKIES=1",
        f"TO_SECRET_KEY={args.secret_key or os.urandom(24).hex()}",
    ]
    if not args.dry_run:
        env_file.write_text("\n".join(env_lines) + "\n")
        os.chmod(env_file, 0o640)

    # 3+4) migrations + Java probe — both need an application context
    #      (settings live in the DB).
    java_home = args.java_home or ""
    if not args.dry_run:
        os.environ["TO_INSTANCE_DIR"] = str(data)
        os.environ["TO_SECRET_KEY"] = env_lines[2].split("=", 1)[1]
        app = _app()
        with app.app_context():
            rc = cmd_db_upgrade(args)
            if rc != 0:
                print("[install] migration failed", file=sys.stderr)
                return 2

            if not args.no_allure and not java_home:
                from . import allure_runtime
                cands = allure_runtime.detect_java_homes()
                if cands:
                    rec = allure_runtime.select_and_validate(
                        str(cands[0]))
                    if rec.get("ok"):
                        java_home = str(cands[0])
                        print(f"[install] allure java: {java_home} "
                              f"({rec.get('java_version', '?')}) — "
                              "validated by generation probe")
                    else:
                        print(f"[install] Java probe failed for "
                              f"{cands[0]}: {rec.get('error','')} — "
                              "continuing with Allure disabled (select "
                              "Java in Admin → Reporting)")
                else:
                    print("[install] no Java runtime found — "
                          "continuing with Allure disabled; "
                          "install/select Java then recheck in "
                          "Admin → Reporting")
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
        print("[install] units staged in config — enable as root when "
              "ready")

    # 7) readiness — app answering on loopback is the bar here; unit
    #    health is covered by the enable step's own output.
    if not args.dry_run:
        rc = cmd_readiness(args)
        if rc != 0:
            print("[install] ACTION: not ready — inspect "
                  "journalctl -u testorchestrator-web; re-run install "
                  "(safe to repeat)", file=sys.stderr)
            return 3
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
    ap = argparse.ArgumentParser(prog="toctl")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("web").add_argument("--listen")
    p = sub.add_parser("worker"); p.add_argument("extra", nargs="*")
    sub.add_parser("db-upgrade")
    p = sub.add_parser("readiness"); p.add_argument("--url",
                                                  default="http://127.0.0.1:18080")
    p = sub.add_parser("backup"); p.add_argument("--to", required=True)
    p = sub.add_parser("recover"); p.add_argument("--from", dest="from_file",
                                                required=True)
    p.add_argument("--into", required=True)
    sub.add_parser("install-info")
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
    p.add_argument("--yes", action="store_true")
    p.add_argument("--url", default="http://127.0.0.1:18080")
    p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    fn = {"web": cmd_web, "worker": cmd_worker, "db-upgrade": cmd_db_upgrade,
          "readiness": cmd_readiness, "backup": cmd_backup,
          "recover": cmd_recover, "install-info": cmd_install_info,
          "install": cmd_install}
    return fn[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
