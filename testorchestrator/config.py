"""Application configuration.

Persistent configuration, certificates, and application data live outside the
replaceable application binaries. Override any value through ``TO_*``
environment variables or an instance config file.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = Path(os.environ.get("TO_INSTANCE_DIR", BASE_DIR / "instance"))


class Config:
    """Base configuration suitable for local development."""

    # A random key is generated per process when TO_SECRET_KEY is unset, which
    # invalidates sessions on restart. Production deployments MUST set it via
    # the environment or instance config.
    SECRET_KEY = os.environ.get("TO_SECRET_KEY")

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "TO_DATABASE_URI", f"sqlite:///{INSTANCE_DIR / 'testorchestrator.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Session hardening. Cookies are Secure by default because production
    # traffic is always HTTPS through Caddy; local development over plain HTTP
    # sets TO_SECURE_COOKIES=0 explicitly.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("TO_SECURE_COOKIES", "1") == "1"
    PERMANENT_SESSION_LIFETIME_HOURS = int(
        os.environ.get("TO_SESSION_LIFETIME_HOURS", "8")
    )
    PREFERRED_URL_SCHEME = "https"

    # Waitress binds loopback only; Caddy is the sole public entry point.
    # v1.8: internal app port defaults to 18080 (public HTTP owns 8080).
    WAITRESS_LISTEN = os.environ.get("TO_WAITRESS_LISTEN", "127.0.0.1:18080")

    # Proxy handling: forwarding headers are honoured only from these peers
    # (the local Caddy reverse proxy). Anything else is untrusted.
    TRUSTED_PROXY_IPS = tuple(
        ip.strip()
        for ip in os.environ.get("TO_TRUSTED_PROXIES", "127.0.0.1,::1").split(",")
        if ip.strip()
    )

    WTF_CSRF_TIME_LIMIT = None  # CSRF token lifetime is bound to the session.

    # Protected storage for imported/generated private keys (OS ACLs 0700,
    # keys 0600). Lives in application data, never in the repo, images, or
    # Git exports (CERT03).
    CERT_DIR = Path(
        os.environ.get("TO_CERT_DIR", str(INSTANCE_DIR / "certs"))
    )

    # Optional installer path (v1.8 §2): when set before first provisioning,
    # replaces the Admin/Admin bootstrap credential outright. >= 12 chars.
    ADMIN_INITIAL_PASSWORD = os.environ.get("TO_ADMIN_PASSWORD")

    # Login bootstrap rate limit (attempts per window, per username).
    LOGIN_RATE_LIMIT_ATTEMPTS = 5
    LOGIN_RATE_LIMIT_WINDOW_S = 60

    # Rendered Caddy configuration staging directory (installer-owned).
    CADDY_CONFIG_DIR = Path(
        os.environ.get(
            "TO_CADDY_CONFIG_DIR", str(INSTANCE_DIR / "caddy")
        )
    )
    # Path to the bundled Caddy binary used for `caddy validate`.
    CADDY_BIN = os.environ.get(
        "TO_CADDY_BIN", str(BASE_DIR / "3rdparty" / "caddy" / "caddy")
    )
