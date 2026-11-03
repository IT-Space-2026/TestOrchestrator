import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testorchestrator import create_app
from testorchestrator.config import Config
from testorchestrator.extensions import db as _db
from testorchestrator.models import Role, User
from testorchestrator.settings import mark_setup_complete


class TestConfig(Config):
    TESTING = True
    SECRET_KEY = "test-secret-key"
    WTF_CSRF_ENABLED = False  # CSRF exercised separately in browser QA.
    SESSION_COOKIE_SECURE = False


def make_app(tmp_path, db_name="test.db", **overrides):
    class Cfg(TestConfig):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / db_name}"

    for key, value in overrides.items():
        setattr(Cfg, key, value)
    return create_app(Cfg)


def seed_users(app, setup_complete=True):
    with app.app_context():
        _db.create_all()
        for role in Role:
            user = User(username=role.value, role=role)
            user.set_password("TestPassword-12345")
            _db.session.add(user)
        _db.session.commit()
        if setup_complete:
            mark_setup_complete()


@pytest.fixture()
def app(tmp_path):
    application = make_app(tmp_path)
    seed_users(application)
    yield application


@pytest.fixture()
def fresh_app(tmp_path):
    """Simulates a fresh installation: empty DB, setup not completed."""
    from testorchestrator.bootstrap import ensure_bootstrap_admin

    application = make_app(tmp_path, "fresh.db")
    with application.app_context():
        _db.create_all()
        ensure_bootstrap_admin()  # installer provisioning path
    yield application


@pytest.fixture(autouse=True)
def _clear_rate_limiter():
    """The login rate limiter is process-global; reset between tests."""
    from testorchestrator.bootstrap import _attempts

    _attempts.clear()
    yield
    _attempts.clear()


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, username, password="TestPassword-12345", **kwargs):
    return client.post(
        "/login", data={"username": username, "password": password},
        follow_redirects=True, **kwargs,
    )


@pytest.fixture()
def login_as(client):
    def _login(role_value):
        return login(client, role_value)

    return _login
