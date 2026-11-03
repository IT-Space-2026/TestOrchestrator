"""Bootstrap provisioning and access gates (v1.8 §2–§3, BOOT01–BOOT03).

Fresh installation creates exactly one Admin account (Admin/Admin, stored
only as a salted Argon2id verifier, must_change_password=True). Until the
password is replaced AND external access is explicitly enabled, the
application refuses non-loopback clients at request level — this is a bind/
listener policy enforced in addition to Waitress binding 127.0.0.1, so a
spoofed Host header or client-supplied forwarded IP cannot bypass it
(TrustedProxyFix strips untrusted forwarding headers before this runs).
"""

import threading
import time
from collections import deque
from ipaddress import ip_address

from flask import abort, current_app, redirect, request, url_for
from flask_login import current_user
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from . import settings as settings_mod
from .extensions import db
from .models import Role, User, record_audit
from .network import effective_allowed_hosts

BOOTSTRAP_USERNAME = "Admin"
BOOTSTRAP_PASSWORD = "Admin"  # Temporary, invalidated on first change.

# Endpoints reachable while a forced password change is pending (BOOT02:
# nothing else is reachable through any URL or API).
_PASSWORD_CHANGE_ALLOWLIST = {"auth.change_password", "auth.logout", "static"}

_attempts_lock = threading.Lock()
_attempts: dict = {}


def is_loopback(remote_addr) -> bool:
    """Loopback check on the post-proxy-fix peer address. Missing/invalid
    addresses are treated as non-remote (direct sockets/tests always carry a
    real peer when remote access is possible)."""
    if not remote_addr:
        return True
    try:
        return ip_address(remote_addr).is_loopback
    except ValueError:
        return True


def ensure_bootstrap_admin() -> bool:
    """Create Admin/Admin on a fresh install. Idempotent and safe to call on
    every request: it is a no-op once any user exists, and upgrades/
    restarts/restores can never recreate or reset the account (BOOT04).

    TO_ADMIN_PASSWORD (installer path) replaces the bootstrap credential
    before the listener starts serving: the account then starts with a
    completed setup instead of the shared temporary password.
    """
    try:
        if db.session.query(User.id).first() is not None:
            return False

        admin = User(username=BOOTSTRAP_USERNAME, role=Role.ADMIN)
        installer_password = current_app.config.get("ADMIN_INITIAL_PASSWORD")
        if installer_password and len(installer_password) >= 12:
            admin.set_password(installer_password)
            admin.must_change_password = False
            settings_mod.set("setup_completed", True)
        else:
            admin.set_password(BOOTSTRAP_PASSWORD)
            admin.must_change_password = True
        db.session.add(admin)
        db.session.commit()
        record_audit(BOOTSTRAP_USERNAME, "bootstrap.admin_provisioned")
        return True
    except SQLAlchemyError:
        db.session.rollback()
        return False  # Schema not migrated yet; try again next request.


def rate_limit_exceeded(username: str) -> bool:
    """Sliding-window limiter for login/bootstrap attempts."""
    limit = current_app.config["LOGIN_RATE_LIMIT_ATTEMPTS"]
    window = current_app.config["LOGIN_RATE_LIMIT_WINDOW_S"]
    now = time.monotonic()
    with _attempts_lock:
        attempts = _attempts.setdefault(username, deque())
        while attempts and now - attempts[0] > window:
            attempts.popleft()
        if len(attempts) >= limit:
            return True
        attempts.append(now)
    return False


def reset_attempts(username: str) -> None:
    with _attempts_lock:
        _attempts.pop(username, None)


def install_gates(app) -> None:
    """before_request gates, in order:

    1. Host allowlist — every request must arrive under a configured alias.
    2. Loopback-only until setup is complete and external access enabled.
    3. Restricted session while must_change_password is set.
    """

    @app.before_request
    def _gates():
        if request.endpoint == "static":
            return None

        ensure_bootstrap_admin()

        try:
            completed = settings_mod.setup_completed()
            external_enabled = bool(settings_mod.get("external_enabled"))
            allowed = effective_allowed_hosts(settings_mod.all_settings())
        except OperationalError:
            return None  # Pre-migration; let the request fail naturally.

        # 1. Host allowlist (v1.8 §3): configured aliases only. The request
        #    Host is never trusted into redirects or share links.
        host = (request.host or "").split(":")[0].strip("[]").lower()
        if host and host not in {h.lower() for h in allowed}:
            abort(403)

        # 2. Until the Admin password is replaced and external access is
        #    explicitly enabled, only loopback clients may proceed. The real
        #    client address (trusted XFF via the local proxy, or the direct
        #    peer) is checked — a client-supplied header cannot forge it.
        if not (completed and external_enabled):
            if not is_loopback(request.remote_addr):
                record_audit(
                    "<unauthenticated>", "access.remote_blocked", request.path
                )
                abort(403)

        # 3. Forced password change: a bootstrap session is restricted to
        #    the change form until a strong password replaces Admin/Admin.
        if (
            current_user.is_authenticated
            and current_user.must_change_password
            and request.endpoint not in _PASSWORD_CHANGE_ALLOWLIST
        ):
            return redirect(url_for("auth.change_password"))

        return None
