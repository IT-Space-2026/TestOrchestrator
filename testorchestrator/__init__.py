"""TestOrchestrator — Flask application factory.

Architecture notes (v1.0–v1.6 foundation + v1.7 About/branding):

- Server-rendered Jinja2 templates, locally bundled CSS, minimal vanilla JS.
- Waitress is the production WSGI server bound to 127.0.0.1:8080; Caddy is
  the public HTTPS reverse proxy. The Flask development server is never used
  in production.
- Integrations (Dagu, ReportPortal, KRE, runner agent, updater) live behind
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
    @click.option("--project", default=".", help="Katalon project dir")
    @click.option("--timeout", default=300, type=int)
    def kre_smoke(executable, project, timeout):
        """Operator smoke test against an EXISTING KRE installation (§5,
        A01 prerequisite): probes the version, runs one console invocation
        into an isolated report dir, and prints what was collected. Does
        not install or license anything. Example:

            flask kre-smoke /opt/katalon/katalonc --project /srv/proj
        """
        from pathlib import Path
        from .adapters import kre as kre_adapter
        exe = str(Path(executable).resolve())
        try:
            version = kre_adapter.probe_version(exe)
            click.echo(f"probe: detected version {version}")
        except kre_adapter.KreError as exc:
            raise click.ClickException(str(exc))
        work = Path(INSTANCE_DIR) / "kre-smoke"
        work.mkdir(parents=True, exist_ok=True)
        report_dir = work / "Reports"
        args = kre_adapter.build_args(
            executable=exe, project_path=str(Path(project).resolve()),
            suite_path="Test Suites/smoke", report_dir=str(report_dir))
        result = kre_adapter.run_invocation(args, work / "logs",
                                          timeout_s=timeout)
        reports = kre_adapter.discover_reports(report_dir)
        click.echo(f"exit={result.exit_status} canceled={result.canceled} "
                   f"timeout={result.timed_out}")
        click.echo(f"reports discovered: {[r.name for r in reports]}")
        click.echo("NOTE: pass/fail verdict requires real JUnit output; "
                   "this smoke run only proves invocation + collection.")

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
