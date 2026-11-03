"""v1.8 bootstrap acceptance: BOOT01–BOOT04."""

from testorchestrator.extensions import db
from testorchestrator.models import User
from testorchestrator.settings import get as get_setting

REMOTE = {"REMOTE_ADDR": "203.0.113.10"}
STRONG = "Strong-NewPassw0rd!"


def _admin(app):
    with app.app_context():
        return User.query.filter_by(username="Admin").first()


# BOOT01 — fresh install: exactly one Admin/Admin, loopback-only setup.
def test_boot01_fresh_install_provisions_admin(fresh_app):
    admin = _admin(fresh_app)
    assert admin is not None
    assert admin.must_change_password is True
    assert admin.check_password("Admin")
    # Stored only as a salted Argon2id verifier — never plaintext.
    assert admin.password_hash.startswith("$argon2id$")
    assert "Admin" not in admin.password_hash
    with fresh_app.app_context():
        assert get_setting("setup_completed") is False


def test_boot01_bootstrap_ui_loopback_only(fresh_app):
    client = fresh_app.test_client()
    # Local loopback may reach the login page.
    assert client.get("/login", environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).status_code == 200
    # A remote client is refused before setup completes.
    assert client.get("/login", environ_overrides=REMOTE).status_code == 403
    assert client.get("/", environ_overrides=REMOTE).status_code == 403


def test_boot01_admin_admin_login_forces_change(fresh_app):
    client = fresh_app.test_client()
    response = client.post(
        "/login", data={"username": "Admin", "password": "Admin"},
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert response.status_code == 302
    assert "/change-password" in response.headers["Location"]


# BOOT02 — the restricted session cannot bypass the mandatory change.
def test_boot02_no_bypass_during_forced_change(fresh_app):
    client = fresh_app.test_client()
    env = {"REMOTE_ADDR": "127.0.0.1"}
    client.post("/login", data={"username": "Admin", "password": "Admin"},
                environ_overrides=env)
    for path in ("/", "/about", "/about/notices", "/system/changelog",
                 "/system/network", "/system/audit", "/3rdparty/x"):
        response = client.get(path, environ_overrides=env)
        assert response.status_code == 302
        assert "/change-password" in response.headers["Location"], path


def test_boot02_weak_and_bootstrap_passwords_rejected(fresh_app):
    client = fresh_app.test_client()
    env = {"REMOTE_ADDR": "127.0.0.1"}
    client.post("/login", data={"username": "Admin", "password": "Admin"},
                environ_overrides=env)
    for weak in ("Admin", "admin", "short", "alllowercase1", "NOUPPER123"):
        response = client.post(
            "/change-password",
            data={"current_password": "Admin", "new_password": weak,
                  "confirm_password": weak},
            environ_overrides=env,
        )
        assert response.status_code == 200  # rejected, form re-rendered
        assert _admin(fresh_app).must_change_password is True


def test_boot02_strong_password_completes_setup(fresh_app):
    client = fresh_app.test_client()
    env = {"REMOTE_ADDR": "127.0.0.1"}
    client.post("/login", data={"username": "Admin", "password": "Admin"},
                environ_overrides=env)
    response = client.post(
        "/change-password",
        data={"current_password": "Admin", "new_password": STRONG,
              "confirm_password": STRONG},
        environ_overrides=env,
    )
    assert response.status_code == 302
    admin = _admin(fresh_app)
    assert admin.must_change_password is False
    assert admin.check_password(STRONG)
    assert not admin.check_password("Admin")  # bootstrap credential dead
    with fresh_app.app_context():
        assert get_setting("setup_completed") is True
    # Rotated session can now use the app normally.
    assert client.get("/", environ_overrides=env).status_code == 200


def test_boot02_rate_limited(fresh_app):
    client = fresh_app.test_client()
    env = {"REMOTE_ADDR": "127.0.0.1"}
    for _ in range(5):
        client.post("/login", data={"username": "Admin", "password": "nope"},
                    environ_overrides=env)
    response = client.post(
        "/login", data={"username": "Admin", "password": "Admin"},
        environ_overrides=env,
    )
    assert response.status_code == 429


# BOOT03 — remote stays blocked until setup complete AND external enabled;
# spoofed forwarding headers cannot bypass.
def test_boot03_spoofed_xff_cannot_bypass(fresh_app):
    client = fresh_app.test_client()
    response = client.get(
        "/login",
        environ_overrides=REMOTE,
        headers={"X-Forwarded-For": "127.0.0.1"},
    )
    assert response.status_code == 403


def test_boot03_remote_allowed_only_after_opt_in(fresh_app):
    from testorchestrator import settings as settings_mod

    client = fresh_app.test_client()
    env_local = {"REMOTE_ADDR": "127.0.0.1"}
    # Complete setup.
    client.post("/login", data={"username": "Admin", "password": "Admin"},
                environ_overrides=env_local)
    client.post("/change-password",
                data={"current_password": "Admin", "new_password": STRONG,
                      "confirm_password": STRONG},
                environ_overrides=env_local)
    # Setup complete but external still disabled: remote stays blocked.
    assert client.get("/login", environ_overrides=REMOTE).status_code == 403
    # Admin explicitly enables external access.
    with fresh_app.app_context():
        settings_mod.update({"external_enabled": True,
                             "external_host": "203.0.113.20"})
    # A new (unauthenticated) remote client can now reach the login page.
    remote_client = fresh_app.test_client()
    assert remote_client.get("/login", environ_overrides=REMOTE).status_code == 200


# BOOT04 — a restart over preserved data never recreates/resets Admin/Admin.
def test_boot04_restart_preserves_account(fresh_app, tmp_path):
    from tests.conftest import make_app

    client = fresh_app.test_client()
    env = {"REMOTE_ADDR": "127.0.0.1"}
    client.post("/login", data={"username": "Admin", "password": "Admin"},
                environ_overrides=env)
    response = client.post("/change-password",
                data={"current_password": "Admin", "new_password": STRONG,
                      "confirm_password": STRONG},
                environ_overrides=env)
    assert response.status_code == 302  # change applied before "restart"

    # "Restart": a second app instance on the same database file.
    restarted = make_app(tmp_path, "fresh.db")
    admin = _admin(restarted)
    assert admin is not None
    assert admin.must_change_password is False
    assert not admin.check_password("Admin")
    assert admin.check_password(STRONG)
    with restarted.app_context():
        assert User.query.filter_by(username="Admin").count() == 1


def test_boot04_no_second_provisioning_with_users(fresh_app):
    """Provisioning runs once; existing users prevent Admin/Admin recreation."""
    from testorchestrator.bootstrap import ensure_bootstrap_admin

    with fresh_app.app_context():
        assert ensure_bootstrap_admin() is False  # Admin already exists
        assert User.query.filter_by(username="Admin").count() == 1
