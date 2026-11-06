"""TestOrchestrator — Flask application factory.

Architecture notes (v1.0–v1.6 foundation + v1.7 About/branding):

- Server-rendered Jinja2 templates, locally bundled CSS, minimal vanilla JS.
- Waitress is the production WSGI server bound to 127.0.0.1:8080; Caddy is
  the public HTTPS reverse proxy. The Flask development server is never used
  in production.
- Integrations (Dagu, Allure, KRE, runner agent, updater) live behind
  adapters in ``testorchestrator.adapters`` and run outside request handlers.
"""

import os
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask

from .config import BASE_DIR, Config, INSTANCE_DIR
from .extensions import csrf, db, login_manager, migrate
from .release import load_release_metadata
from .security import TrustedProxyFix, security_headers


def create_app(config_object=Config) -> Flask:
    app = Flask(__name__, instance_path=str(INSTANCE_DIR))
    app.config.from_object(config_object)

    if not app.config["SECRET_KEY"]:
        # Development convenience: ephemeral per-process key. Installations
        # must provide TO_SECRET_KEY via environment/instance config.
        app.config["SECRET_KEY"] = secrets.token_hex(32)
        app.logger.warning(
            "TO_SECRET_KEY is not set; generated an ephemeral development key. "
            "Set TO_SECRET_KEY in the instance configuration for production."
        )

    app.permanent_session_lifetime = timedelta(
        hours=app.config["PERMANENT_SESSION_LIFETIME_HOURS"]
    )

    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    # Protected certificate/key storage (CERT03): 0700 dir, 0600 keys.
    cert_dir = Path(app.config["CERT_DIR"])
    cert_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cert_dir, 0o700)

    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    # Verified bundled release metadata — loaded once at startup (AB04).
    app.config["RELEASE_METADATA"] = load_release_metadata(BASE_DIR)
    # cache-buster: app.css mtime so the browser refetches on every
    # stylesheet change.
    from datetime import datetime as _dt, timezone as _tz
    app.jinja_env.globals["now"] = _dt.now
    app.jinja_env.globals["today"] = (
        lambda: _dt.now(_tz.utc).date())
    _css = Path(BASE_DIR) / "testorchestrator/static/css/app.css"
    app.jinja_env.globals["app_v"] = (
        str(int(_css.stat().st_mtime)) if _css.exists() else "0")
    _js = Path(BASE_DIR) / "testorchestrator/static/js/case-selector.js"
    app.jinja_env.globals["js_v"] = (
        str(int(_js.stat().st_mtime)) if _js.exists() else "0")

    app.wsgi_app = TrustedProxyFix(
        app.wsgi_app, app.config["TRUSTED_PROXY_IPS"]
    )
    app.after_request(security_headers)

    from . import models  # noqa: F401  (register models with SQLAlchemy)
    from .auth import auth_bp
    from .main import main_bp
    from .system import system_bp
    from .portal import portal_bp
    from .agent_api import agent_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(system_bp)
    app.register_blueprint(portal_bp)
    app.register_blueprint(agent_bp)
    from .system.setup_routes import setup_bp
    app.register_blueprint(setup_bp)

    @app.route("/healthz")
    def healthz():
        """Install/instance identity probe — lets the installer verify
        the answering service is THIS install (never a foreign listener)."""
        from flask import jsonify
        meta = app.config.get("RELEASE_METADATA", {})
        return jsonify({
            "status": "ok",
            "install_id": os.environ.get("TO_INSTALL_ID", ""),
            "version": meta.get("version", ""),
            "build": meta.get("build", ""),
        })
    # Agent API is bearer-token authenticated, not session-based — CSRF
    # does not apply (no cookies honoured by this surface).
    csrf.exempt(agent_bp)

    # v1.8 bootstrap + network gates (loopback-only until setup completes and
    # external access is enabled; host allowlist; forced password change).
    # Provisioning happens inside the request gate, not here — a CLI/migration
    # invocation must never try to write users before the schema is current.
    from .bootstrap import install_gates

    install_gates(app)

    @login_manager.user_loader
    def load_user(user_id):
        user = db.session.get(models.User, int(user_id))
        if user is None or not user.is_active:
            return None
        # Session revocation: cookie must carry the current version.
        from flask import session
        if session.get("session_version") != user.session_version:
            return None
        return user

    register_cli(app)
    return app


def register_cli(app: Flask) -> None:
    """Operational commands (seed, health). Integrations stay outside these."""

    import click
    from .models import Role, User

    @app.cli.command("seed-users")
    @click.option(
        "--dev-defaults",
        is_flag=True,
        help="Use documented development-only passwords (NOT for production).",
    )
    def seed_users(dev_defaults: bool) -> None:
        """Create the four role accounts.

        Passwords come from TO_SEED_<ROLE>_PASSWORD environment variables.
        --dev-defaults is a development convenience only.
        """

        # The Admin account is owned by the v1.8 bootstrap flow
        # (Admin/Admin -> mandatory change); seeding only covers the
        # remaining roles.
        dev_passwords = {
            Role.TEST_MANAGER: "DevOnly-Manager!1",
            Role.TESTER: "DevOnly-Tester!1",
            Role.PO: "DevOnly-Po!1",
        }
        usernames = {
            Role.TEST_MANAGER: "manager",
            Role.TESTER: "tester",
            Role.PO: "po",
        }

        for role, username in usernames.items():
            env_name = f"TO_SEED_{role.name}_PASSWORD"
            password = os.environ.get(env_name)
            if not password and dev_defaults:
                password = dev_passwords[role]
            if not password:
                click.echo(f"skip {username}: set {env_name} or --dev-defaults")
                continue
            if len(password) < 12:
                raise click.ClickException(
                    f"{env_name}: password must be at least 12 characters"
                )
            user = User.query.filter_by(username=username).first()
            if user is None:
                user = User(username=username, role=role)
                db.session.add(user)
            user.set_password(password)
            db.session.commit()
            click.echo(f"seeded {username} ({role.label})")

        if dev_defaults:
            click.echo(
                "WARNING: development-only passwords in use. Rotate before "
                "any real deployment."
            )

    @app.cli.command("health")
    def health() -> None:
        """Exit 0 when the app and its database are reachable (installer use)."""
        db.session.execute(db.text("SELECT 1"))
        click.echo("ok")

    @app.cli.command("worker")
    @click.option("--once", is_flag=True,
                  help="Process the durable outbox once and exit.")
    def worker(once: bool) -> None:
        """Durable dispatch/sync worker — runs OUTSIDE request handlers
        (handoff §7): outbox events, runner assignment, reconciliation."""
        from . import dispatch
        if once:
            click.echo(f"processed: {dispatch.worker_once()}")
        else:
            click.echo("worker loop started (Ctrl-C to stop)")
            dispatch.worker_loop()

    @app.cli.command("agent")
    @click.option("--portal", required=True, help="Portal base URL, e.g. https://tests.example.com")
    @click.option("--token", required=True, help="Issued agent bearer token (toa_...)")
    @click.option("--work-root", default=None, help="Agent workspace directory")
    @click.option("--once", is_flag=True)
    def agent(portal, token, work_root, once):
        """Remote runner agent (§6): polls for assigned executions and
        invokes the operator-installed KRE on this machine. Never installs
        or updates KRE itself."""
        from pathlib import Path
        from . import agent as agent_mod
        root = Path(work_root or INSTANCE_DIR / "agent-work")
        root.mkdir(parents=True, exist_ok=True)
        agent_mod.run(portal, token, root, once=once)

    @app.cli.command("kre-smoke")
    @click.argument("executable")
    @click.option("--timeout", default=30, type=int)
    def kre_smoke(executable, timeout):
        """Probe an EXISTING KRE installation (§5, A01 prerequisite).

        Probe ONLY: runs ``<exe> -version`` and reports the detected
        version. It never starts project tests, never installs, and never
        licenses anything. For a real smoke execution use ``flask
        kre-exec`` with an explicitly selected suite.
        """
        from pathlib import Path
        from .adapters import kre as kre_adapter
        exe = str(Path(executable).resolve())
        try:
            version = kre_adapter.probe_version(exe, timeout=timeout)
            click.echo(f"probe: detected version {version}")
        except kre_adapter.KreError as exc:
            raise click.ClickException(str(exc))
        click.echo(
            "Verify the detected version matches the installed/licensed "
            "KRE before registering the runtime connection."
        )

    @app.cli.command("kre-exec")
    @click.argument("executable")
    @click.option("--project", required=True, help="Katalon project dir")
    @click.option("--suite", required=True,
                  help="Suite path, e.g. 'Test Suites/Smoke'")
    @click.option("--profile", default="default",
                  help="Execution profile name")
    @click.option("--browser", default="Chrome (headless)",
                  help="KRE browserType value")
    @click.option("--timeout", default=300, type=int)
    def kre_exec(executable, project, suite, profile, browser, timeout):
        """Direct smoke EXECUTION against an EXISTING KRE install (§5):
        one console invocation of the explicitly selected suite into an
        isolated report dir under the instance dir. Bypasses
        portal/Dagu/agent entirely — it is NOT the A01 acceptance run,
        only a CLI check of the console arguments against the installed
        KRE version. Example:

            flask kre-exec /opt/katalon/katalonc --project /srv/proj \\
                --suite "Test Suites/Smoke" --browser "Chrome (headless)"
        """
        from pathlib import Path
        from .adapters import kre as kre_adapter
        exe = str(Path(executable).resolve())
        work = Path(INSTANCE_DIR) / "kre-exec"
        work.mkdir(parents=True, exist_ok=True)
        report_dir = work / "Reports"
        args = kre_adapter.build_args(
            executable=exe, project_path=str(Path(project).resolve()),
            suite_path=suite, report_dir=str(report_dir),
            profile=profile, browser=browser)
        click.echo(f"argv: {' '.join(args)}")
        result = kre_adapter.run_invocation(args, work / "logs",
                                          timeout_s=timeout)
        reports = kre_adapter.discover_reports(report_dir)
        click.echo(f"exit={result.exit_status} canceled={result.canceled} "
                   f"timeout={result.timed_out}")
        click.echo(f"reports discovered: {[r.name for r in reports]}")
        click.echo("NOTE: pass/fail verdict requires real JUnit output; "
                   "this smoke run only proves invocation + collection.")

    @app.cli.command("dagu-config")
    @click.option("--endpoint", required=True,
                  help="Dagu base URL, e.g. http://127.0.0.1:29877")
    @click.option("--credential-ref", default="",
                  help="SecretRef name holding the Dagu credential "
                       "(basic-mode password, or bearer API key)")
    @click.option("--username", default="",
                  help="Dagu basic-auth username; empty = Bearer token mode")
    @click.option("--check", is_flag=True,
                  help="Probe /api/v1/version after saving")
    def dagu_config(endpoint, credential_ref, username, check):
        """Point the portal at the Dagu orchestrator (§7, Option B).
        The credential itself is stored via System -> Credentials —
        only the SecretRef NAME is recorded here. With --username the
        secret must hold that account's password (auth.mode: basic);
        without it the secret must hold a Bearer API key (builtin)."""
        from . import settings as settings_mod
        from .adapters.dagu import DaguAdapter
        from .secrets_store import read_secret
        if credential_ref:
            try:
                read_secret(app, credential_ref)
            except Exception as exc:
                raise click.ClickException(
                    f"credential reference unusable: {exc}")
        settings_mod.update({
            "dagu_endpoint": endpoint.rstrip("/"),
            "dagu_credential_ref": credential_ref,
            "dagu_username": username,
        })
        click.echo(f"saved dagu_endpoint={endpoint.rstrip('/')} "
                   f"dagu_username={username or '(bearer)'} "
                   f"dagu_credential_ref={credential_ref or '(none)'}")
        if check:
            try:
                info = DaguAdapter.from_settings().health()
            except Exception as exc:
                raise click.ClickException(f"Dagu health check failed: {exc}")
            click.echo(
                f"health: {info.get('status') or 'reachable'}, "
                f"version {info['version']}, "
                f"workflow_installed={info['workflow_installed']}")
            if not info["workflow_installed"]:
                raise click.ClickException(
                    "testorchestrator-execution workflow is not installed "
                    "in the Dagu dags directory")

    @app.cli.command("caddy-rollback")
    def caddy_rollback():
        """Local recovery: restore the last known good Caddy config when
        the UI is unreachable (v1.8 staged-apply rollback)."""
        from .caddyconf import rollback_config, CaddyApplyError
        try:
            result = rollback_config(app)
        except CaddyApplyError as exc:
            raise click.ClickException(str(exc))
        click.echo(f"rolled back: {result['restored']}")

    @app.cli.command("set-admin-password")
    @click.argument("password")
    def set_admin_password(password):
        """Headless bootstrap: replace the Admin bootstrap credential from
        the local console (v1.8). Marks setup complete and revokes any
        bootstrap session."""
        from .models import User
        from .settings import mark_setup_complete
        if len(password) < 12:
            raise click.ClickException("password must be >= 12 characters")
        admin = User.query.filter_by(username="Admin").first()
        if admin is None:
            raise click.ClickException("no bootstrap Admin exists")
        admin.set_password(password)
        admin.must_change_password = False
        admin.session_version += 1
        mark_setup_complete()
        from .settings import set as _set
        _set("admin_bootstrap_replaced", "yes")
        db.session.commit()
        click.echo("Admin password set; setup marked complete.")

    @app.cli.command("git-restore")
    @click.argument("project_id")
    def git_restore(project_id):
        """Restore versioned definitions from the export repo (§11, A08)."""
        from . import gitsync
        try:
            restored = gitsync.restore_definitions(project_id)
        except Exception as exc:
            raise click.ClickException(str(exc))
        click.echo(f"restored: {restored}")

    @app.cli.command("update-apply")
    @click.argument("bundle_dir")
    @click.option("--key", "pubkey", default=None,
                  help="trusted public key (default TO_UPDATE_KEY)")
    @click.option("--live", "live_dir", default="/opt/testorchestrator")
    @click.option("--drain-timeout", default=120, type=int)
    def update_apply(bundle_dir, pubkey, live_dir, drain_timeout):
        """Wired update lifecycle (§14): verify -> drain -> backup -> stage
        -> activate -> health check -> recover on failure. Local CLI, no
        web UI required."""
        import os
        from pathlib import Path
        from . import updater
        bundle = Path(bundle_dir)
        key = Path(pubkey or os.environ.get("TO_UPDATE_KEY", ""))
        if not key.exists():
            raise click.ClickException("trusted update key not found")
        try:
            verified = updater.verify_bundle(bundle, key)
            click.echo("bundle signature+digests verified")
            if not updater.drain(drain_timeout):
                raise updater.UpdateError("drain timeout — active runs remain")
            click.echo("drained")
            result = updater.apply_update(
                app, bundle, bundle / "payload", key,
                Path(INSTANCE_DIR) / "update-staged")
            click.echo(f"staged v{result['version']} (backup {result['backup']})")
            act = updater.activate(
                Path(INSTANCE_DIR) / "update-staged", Path(live_dir),
                Path(INSTANCE_DIR) / "backups")
            click.echo(f"activated; previous at {act['retired']}")
            if not updater.health_check(app):
                rec = updater.recover(Path(result["backup"]), Path(live_dir))
                click.echo(f"health check FAILED — recovered: {rec}")
                raise click.ClickException("health check failed; rolled back")
            click.echo("health check ok — update complete")
        except updater.UpdateError as exc:
            raise click.ClickException(str(exc))

    @app.cli.command("update-recover")
    @click.argument("backup_tar")
    @click.option("--into", "target", default="/opt/testorchestrator")
    def update_recover(backup_tar, target):
        """Offline recovery: restore a backup tarball without the web UI."""
        from pathlib import Path
        from . import updater
        click.echo(updater.recover(Path(backup_tar), Path(target)))
