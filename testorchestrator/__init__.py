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

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(system_bp)

    # v1.8 bootstrap + network gates (loopback-only until setup completes and
    # external access is enabled; host allowlist; forced password change).
    from .bootstrap import ensure_bootstrap_admin, install_gates

    install_gates(app)
    with app.app_context():
        ensure_bootstrap_admin()

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(models.User, int(user_id))

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
