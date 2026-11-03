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
    args = ap.parse_args(argv)
    fn = {"web": cmd_web, "worker": cmd_worker, "db-upgrade": cmd_db_upgrade,
          "readiness": cmd_readiness, "backup": cmd_backup,
          "recover": cmd_recover, "install-info": cmd_install_info}
    return fn[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
