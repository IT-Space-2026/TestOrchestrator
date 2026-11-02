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
    WAITRESS_LISTEN = os.environ.get("TO_WAITRESS_LISTEN", "127.0.0.1:8080")

    # Proxy handling: forwarding headers are honoured only from these peers
    # (the local Caddy reverse proxy). Anything else is untrusted.
    TRUSTED_PROXY_IPS = tuple(
        ip.strip()
        for ip in os.environ.get("TO_TRUSTED_PROXIES", "127.0.0.1,::1").split(",")
        if ip.strip()
    )

    WTF_CSRF_TIME_LIMIT = None  # CSRF token lifetime is bound to the session.
