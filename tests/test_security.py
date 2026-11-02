"""Auth, session, and header checks."""

from tests.conftest import login


def test_dashboard_requires_login(client):
    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_login_success_and_logout(client):
    response = login(client, "tester")
    assert response.status_code == 200
    assert b"Dashboard" in response.data

    response = client.post("/logout", follow_redirects=True)
    assert response.status_code == 200
    assert client.get("/").status_code == 302


def test_login_rejects_bad_password(client):
    response = login(client, "tester", "wrong-password")
    assert response.status_code == 200
    assert b"Invalid username or password" in response.data


def test_logout_requires_post(client):
    login(client, "tester")
    assert client.get("/logout").status_code == 405


def test_security_headers_present(client):
    response = client.get("/login")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in response.headers


def test_open_redirect_rejected(client):
    # The ?next= parameter must never redirect off-origin.
    response = client.post(
        "/login?next=https://evil.example/",
        data={"username": "tester", "password": "TestPassword-12345"},
    )
    assert response.status_code == 302
    assert "evil.example" not in response.headers["Location"]
