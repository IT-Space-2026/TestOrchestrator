"""Optional Cheroot web tier — spike replacement for Caddy.

Opt-in via ``TO_FRONTEND=cheroot`` (Caddy stays the default). Replaces
exactly the verified Caddy responsibilities:

- TLS termination on the public HTTPS listener — cert files come from
  the managed cert dir for every mode; an ``internal``-mode install
  generates a per-installation local CA through ``certs.generate_local``
  at first start (no ACME is involved in either tier).
- Direct WSGI serving of the Flask app (removes the proxy hop; Waitress
  keeps its loopback listener for internal probes).
- Optional HTTP -> HTTPS redirect listener (``http_mode == 'redirect'``)
  with a configured target — the inbound Host is never reflected.
- Host allowlist matching the rendered site labels
  (local_host + optional external_host); other hosts get 421.
- Hot certificate reload: cert-file changes (admin regenerate/import)
  rebuild the TLS context for new connections without a restart.

Not covered by design: listener rebind without service restart (the
restart controls in the Network page apply), Caddy's admin API (unused),
and anything beyond the verified feature set. ``-Server`` parity: the
Cheroot version token is masked to the product name.
"""

import logging
import os
import ssl
import threading
from pathlib import Path

log = logging.getLogger(__name__)

FRONTEND_ENV = "TO_FRONTEND"
FRONTEND_CHEROOT = "cheroot"


def enabled() -> bool:
    return os.environ.get(FRONTEND_ENV, "").strip().lower() == FRONTEND_CHEROOT


def _allowed_hosts(settings: dict) -> set:
    hosts = {"localhost", "127.0.0.1", "::1", settings["local_host"]}
    if settings["external_enabled"] and settings["external_host"]:
        hosts.add(settings["external_host"])
    return {h.lower() for h in hosts}


def _host_allowed(host_header: str, allowed: set) -> bool:
    # Host may carry a port ("name:443") or be an IPv6 literal ("[::1]:443").
    host = host_header.strip()
    if host.startswith("["):
        host = host.split("]", 1)[0].strip("[]")
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    return host.lower() in allowed


class _DirectPeer:
    """Environ fixes for direct serving (no proxy hop).

    - IPv4-mapped IPv6 peers (``::ffff:a.b.c.d`` from a dual-stack ``::``
      bind) are normalised to the dotted quad — the same address Caddy
      would have forwarded. Without this, loopback checks see a
      non-loopback IPv6 peer.
    - There is no trusted proxy in this tier: client-supplied
      X-Forwarded-* are always removed so they cannot spoof the
      client address. TrustedProxyFix still protects the loopback
      Waitress path used with Caddy."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        peer = environ.get("REMOTE_ADDR", "")
        if peer.startswith("::ffff:"):
            environ["REMOTE_ADDR"] = peer[7:]
        for header in (
            "HTTP_X_FORWARDED_FOR", "HTTP_X_FORWARDED_PROTO",
            "HTTP_X_FORWARDED_HOST", "HTTP_X_FORWARDED_PORT",
            "HTTP_X_FORWARDED_PREFIX",
        ):
            environ.pop(header, None)
        return self.app(environ, start_response)


class _HostGate:
    """421 for Host values outside the configured site labels —
    mirrors Caddy's behaviour of only answering configured names."""

    def __init__(self, app, allowed: set):
        self.app = app
        self.allowed = allowed

    def __call__(self, environ, start_response):
        host = environ.get("HTTP_HOST", "")
        if not _host_allowed(host, self.allowed):
            start_response(
                "421 Misdirected Request",
                [("Content-Type", "text/plain")],
            )
            return [b"421 Misdirected Request"]
        return self.app(environ, start_response)


def _redirect_app(target: str):
    """301 -> configured HTTPS address, preserving path + query —
    matches the Caddyfile `redir {target}{uri} permanent` directive."""
    target = target.rstrip("/")

    def app(environ, start_response):
        path = environ.get("PATH_INFO", "/") or "/"
        qs = environ.get("QUERY_STRING", "")
        location = target + path + (("?" + qs) if qs else "")
        start_response(
            "301 Moved Permanently",
            [("Location", location), ("Content-Length", "0")],
        )
        return [b""]

    return app


def ensure_certificates(app, settings: dict) -> tuple:
    """Return (fullchain, key) paths for TLS, generating the managed
    local CA + leaf when the selected mode has no usable files yet.

    `internal` means "platform-managed": under Caddy that was its own
    CA; under this tier it is produced by certs.generate_local — the
    trust anchor changes on switch, and reverting to Caddy restores
    its CA untouched (different directories)."""
    cert_dir = Path(app.config["CERT_DIR"])
    fullchain, key = cert_dir / "fullchain.pem", cert_dir / "private-key.pem"
    if fullchain.exists() and key.exists():
        return fullchain, key

    hosts = sorted({settings["local_host"], "localhost"} - {""})
    if settings["external_enabled"] and settings["external_host"]:
        hosts.append(settings["external_host"])
    cert_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cert_dir, 0o700)
    from .certs import generate_local
    meta = generate_local(hosts, cert_dir)
    log.info(
        "frontend: generated local CA + leaf for internal mode "
        "(sans=%s, fingerprint=%s)",
        ",".join(meta.get("sans", hosts)),
        str(meta.get("fingerprint_sha256", "?"))[:16],
    )
    return fullchain, key


def _build_context(fullchain: Path, key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(fullchain), str(key))
    return ctx


def _cert_stamp(fullchain: Path, key: Path) -> tuple:
    return (fullchain.stat().st_mtime_ns, key.stat().st_mtime_ns)


def _watch_certs(server, fullchain: Path, key: Path, stop: threading.Event,
                 baseline: tuple):
    """Swap the TLS context when managed cert files change — an admin
    regenerate/import takes effect on new connections without a
    service restart. The baseline is the stamp captured when the live
    context was built, so a change landing before the first poll is
    still detected."""
    last = baseline
    while not stop.wait(2.0):
        try:
            stamp = _cert_stamp(fullchain, key)
        except OSError:
            continue
        if stamp != last:
            try:
                server.ssl_adapter.context = _build_context(fullchain, key)
                last = stamp
                log.info("frontend: TLS context reloaded after cert change")
            except Exception:
                log.exception("frontend: cert reload failed — keeping "
                              "the previous context")


class Frontend:
    """Handle for the started listeners (test + shutdown)."""

    def __init__(self, servers, stop_event):
        self.servers = servers
        self._stop = stop_event

    def stop(self):
        self._stop.set()
        for srv in self.servers:
            try:
                srv.stop()
            except Exception:
                log.exception("frontend: error stopping a listener")


def serve(app, settings: dict) -> Frontend:
    """Start the Cheroot HTTPS listener (+ optional HTTP redirect).

    Non-blocking — the caller follows with the existing loopback
    Waitress serve. Returns a ``Frontend`` handle."""
    from cheroot.wsgi import Server as WSGIServer
    from cheroot.ssl.builtin import BuiltinSSLAdapter

    https_port = int(settings["https_port"])
    http_port = int(settings["http_port"])
    allowed = _allowed_hosts(settings)
    gated = _HostGate(_DirectPeer(app), allowed)

    fullchain, key = ensure_certificates(app, settings)
    adapter = BuiltinSSLAdapter(str(fullchain), str(key))
    cert_stamp = _cert_stamp(fullchain, key)
    adapter.context = _build_context(fullchain, key)

    servers = []
    bound = False
    for bind in (("::", https_port), ("0.0.0.0", https_port)):
        try:
            # Mask the Server header — parity with Caddy's `-Server`.
            srv = WSGIServer(bind, gated, server_name="TestOrchestrator")
            srv.ssl_adapter = adapter
            srv.prepare()
            bound = True
            servers.append(srv)
            break
        except OSError:
            continue
    if not bound:
        raise RuntimeError(
            f"frontend: could not bind HTTPS listener on :{https_port}")

    if settings["http_mode"] == "redirect":
        from .network import build_url
        target = build_url(settings["local_host"], https_port, "https")
        for bind in (("::", http_port), ("0.0.0.0", http_port)):
            try:
                rsrv = WSGIServer(bind, _redirect_app(target),
                                  server_name="TestOrchestrator")
                rsrv.prepare()
                servers.append(rsrv)
                break
            except OSError:
                continue

    for srv in servers:
        threading.Thread(target=srv.serve, name="frontend-serve",
                         daemon=True).start()

    stop = threading.Event()
    threading.Thread(
        target=_watch_certs,
        args=(servers[0], fullchain, key, stop, cert_stamp),
        name="cert-watch", daemon=True).start()

    log.info("frontend: cheroot HTTPS :%d (hosts: %s)%s",
             https_port, ", ".join(sorted(allowed)),
             f" + redirect :{http_port}"
             if settings["http_mode"] == "redirect" else "")
    return Frontend(servers, stop)
