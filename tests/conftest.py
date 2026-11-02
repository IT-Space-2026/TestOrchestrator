import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testorchestrator import create_app
from testorchestrator.config import Config
from testorchestrator.extensions import db as _db
from testorchestrator.models import Role, User


class TestConfig(Config):
    TESTING = True
    SECRET_KEY = "test-secret-key"
    WTF_CSRF_ENABLED = False  # CSRF exercised separately in browser QA.
    SESSION_COOKIE_SECURE = False


@pytest.fixture()
def app(tmp_path):
    class Cfg(TestConfig):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'test.db'}"

    application = create_app(Cfg)
    with application.app_context():
        _db.create_all()
        for role in Role:
            user = User(username=role.value, role=role)
            user.set_password("TestPassword-12345")
            _db.session.add(user)
        _db.session.commit()
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, username, password="TestPassword-12345"):
    return client.post(
        "/login", data={"username": username, "password": password},
        follow_redirects=True,
    )


@pytest.fixture()
def login_as(client):
    def _login(role_value):
        return login(client, role_value)

    return _login
