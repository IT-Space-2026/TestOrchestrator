"""Address model helpers (v1.8 §3–§4).

Separates bind interfaces, allowed hostnames, the advertised base URL, the
external address, and ports. A URL label never configures networking by
itself. Nothing here reflects an arbitrary request ``Host`` into trusted
links — every generated URL is built from stored settings only.
"""

import ipaddress
import re
import socket
from dataclasses import dataclass, field

_DNS_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
DNS_HOSTNAME_RE = re.compile(
    rf"^(?:{_DNS_LABEL}\.)*{_DNS_LABEL}\.?$"
)


class ValidationError(ValueError):
    pass


def parse_host(value: str) -> str:
    """Validate an external/LAN host: IP literal or DNS hostname only.

    Rejects arbitrary URLs — no scheme, userinfo, path, query, or port.
    Returns the normalized host string (IPv6 without brackets).
    """

    value = (value or "").strip()
    if not value:
        raise ValidationError("host is empty")
    if any(c in value for c in "/@?# ") or ":" in value and not _is_ipv6(value):
        raise ValidationError(
            "host must be an IP address or DNS hostname, not a URL"
        )
    try:
        return str(ipaddress.ip_address(value.strip("[]")))
    except ValueError:
        pass
    host = value.rstrip(".")
    if len(host) > 253 or not DNS_HOSTNAME_RE.match(host):
        raise ValidationError(f"invalid hostname: {value!r}")
    return host.lower()


def _is_ipv6(value: str) -> bool:
    try:
        ipaddress.IPv6Address(value.strip("[]"))
        return True
    except ValueError:
        return False


def host_kind(host: str) -> str:
    """'ipv4' | 'ipv6' | 'dns' — used for link formatting and validation."""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
        return "ipv6" if ip.version == 6 else "ipv4"
    except ValueError:
        return "dns"


def bracket_host(host: str) -> str:
    """Bracket IPv6 literals for use inside URLs."""
    return f"[{host}]" if host_kind(host) == "ipv6" else host


def validate_port(value, name: str = "port") -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be an integer")
    if not 1 <= port <= 65535:
        raise ValidationError(f"{name} must be in 1-65535")
    return port


def check_port_conflicts(ports: dict) -> None:
    """Owned listeners must not collide (NET01)."""
    seen = {}
    for name, port in ports.items():
        if port in seen:
            raise ValidationError(
                f"{name} ({port}) conflicts with {seen[port]} ({port})"
            )
        seen[port] = name


def port_available(port: int, host: str = "127.0.0.1") -> bool:
    """Runtime availability probe for a candidate port."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
        return True
    except OSError:
        return False


def port_needs_privilege(port: int) -> bool:
    return port < 1024


@dataclass
class AddressCandidate:
    address: str
    kind: str            # 'ipv4' | 'ipv6'
    is_private: bool
    source: str = ""


def detect_address_candidates() -> list:
    """Detected non-loopback server addresses offered as *suggestions*.

    Detection is purely local (socket/ioctl, no external lookup). Results are
    candidates the Admin must choose between — a detected private IP is never
    presented as the public Internet address (NET03).
    """

    candidates = []
    seen = set()

    def add(addr: str, source: str) -> None:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return
        if ip.is_loopback or ip.is_link_local or addr in seen:
            return
        seen.add(addr)
        candidates.append(
            AddressCandidate(
                address=addr,
                kind="ipv6" if ip.version == 6 else "ipv4",
                is_private=ip.is_private,
                source=source,
            )
        )

    # UDP connect() is a routing-table lookup; it transmits nothing.
    for target in ("192.0.2.1", "2001:db8::1"):
        family = socket.AF_INET6 if ":" in target else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.connect((target, 80))
                add(s.getsockname()[0], "default route")
        except OSError:
            pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            add(info[4][0], "hostname resolution")
    except OSError:
        pass

    return candidates


def build_url(host: str, port: int, scheme: str = "https") -> str:
    """Configured URL for display/copy. Omits the default port only."""
    default_port = 443 if scheme == "https" else 80
    base = f"{scheme}://{bracket_host(host)}"
    if port != default_port:
        base += f":{port}"
    return base + "/"


def access_links(settings: dict) -> dict:
    """Read-only computed URLs for display (v1.8 §3). Never request-Host."""
    https_port = int(settings["https_port"])
    http_port = int(settings["http_port"])
    links = {
        "local": build_url(settings["local_host"], https_port, "https"),
        "external": None,
        "http_redirect": None,
    }
    if settings["external_enabled"] and settings["external_host"]:
        ext = settings["external_host"]
        links["external"] = build_url(ext, https_port, "https")
        if settings["http_mode"] == "redirect":
            links["http_redirect"] = build_url(ext, http_port, "http")
    return links


def effective_allowed_hosts(settings: dict) -> list:
    """Host allowlist: configured aliases plus the literal defaults."""
    hosts = {"localhost", "127.0.0.1", "::1", settings["local_host"]}
    if settings["external_enabled"] and settings["external_host"]:
        hosts.add(settings["external_host"])
    hosts.update(settings.get("allowed_hosts") or [])
    return sorted(h.strip("[]") for h in hosts if h)
