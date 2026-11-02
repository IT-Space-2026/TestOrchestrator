"""Security helpers: role checks, response headers, trusted-proxy handling."""

import functools
from ipaddress import ip_address

from flask import abort, current_app, request
from flask_login import current_user
from werkzeug.middleware.proxy_fix import ProxyFix


def role_required(*roles):
    """Restrict a view to the named roles (Role members or their values)."""

    allowed = {getattr(r, "value", r) for r in roles}

    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return current_app.login_manager.unauthorized()
            if current_user.role.value not in allowed:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def security_headers(response):
    """Response headers applied to every response.

    The Content-Security-Policy forbids loading any sub-resource from outside
    the origin: the UI (including About) must render fully offline and make no
    external requests. Navigation (e.g. the explicit https://it-space.info
    link) is a user-initiated top-level navigation and is not a sub-resource
    load, so it is unaffected.
    """

    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self'; "
        "style-src 'self'; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


class TrustedProxyFix:
    """Apply ProxyFix only when the immediate peer is a known proxy.

    Waitress only sees the loopback Caddy proxy in supported deployments; any
    other client supplying forwarding headers is ignored rather than trusted.
    """

    def __init__(self, app, trusted_ips):
        self.app = ProxyFix(app, x_for=1, x_proto=1, x_host=1, x_port=1)
        self.trusted = set(trusted_ips)

    def __call__(self, environ, start_response):
        peer = environ.get("REMOTE_ADDR", "")
        try:
            trusted = ip_address(peer) in {
                ip_address(ip) for ip in self.trusted
            }
        except ValueError:
            trusted = False
        if trusted:
            return self.app(environ, start_response)
        # Untrusted peer: strip client-supplied forwarding headers entirely.
        for header in (
            "HTTP_X_FORWARDED_FOR",
            "HTTP_X_FORWARDED_PROTO",
            "HTTP_X_FORWARDED_HOST",
            "HTTP_X_FORWARDED_PORT",
            "HTTP_X_FORWARDED_PREFIX",
        ):
            environ.pop(header, None)
        return self.app.app(environ, start_response)
