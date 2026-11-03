"""Certificate setup (v1.8 §5).

Two modes, both handled by the maintained ``cryptography`` library — no
external OpenSSL CLI prerequisite:

A. Local generated certificate — unique per installation: a per-install CA
   signs a leaf for the selected DNS/IP SANs. The public CA certificate may
   be exported for browser trust-store installation (explicit consent); the
   private key is never offered as a download.

B. Import existing certificate — PEM certificate/full chain + matching
   private key (+ optional separate intermediates). Validation covers format,
   key/cert public-key match, SAN coverage of the advertised host, current
   validity, and chain completeness. PKCS#12/PFX is not implemented (spec:
   only if a tested parser exists).

Private keys are written to the protected cert directory (0700 dir, 0600
files), never appear in audit logs, Git exports, or UI.
"""

import hashlib
import ipaddress
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


class CertificateError(ValueError):
    """User-facing validation failure for certificate operations."""


@dataclass
class CertMetadata:
    """Public certificate fields safe to display (no key material)."""

    subject: str
    issuer: str
    sans: list = field(default_factory=list)
    not_before: str = ""
    not_after: str = ""
    fingerprint_sha256: str = ""
    expired: bool = False
    days_until_expiry: int = 0


def _rdn(name: x509.Name) -> str:
    return ", ".join(
        f"{attr.oid._name}={attr.value}" for attr in name
    )


def metadata_for(cert: x509.Certificate) -> CertMetadata:
    try:
        san_ext = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
        sans = san_ext.value.get_values_for_type(x509.DNSName)
        sans += [
            str(ip) for ip in san_ext.value.get_values_for_type(x509.IPAddress)
        ]
    except x509.ExtensionNotFound:
        sans = []

    now = datetime.now(timezone.utc)
    not_after = cert.not_valid_after_utc
    return CertMetadata(
        subject=_rdn(cert.subject),
        issuer=_rdn(cert.issuer),
        sans=sorted(sans),
        not_before=cert.not_valid_before_utc.isoformat(timespec="seconds"),
        not_after=not_after.isoformat(timespec="seconds"),
        fingerprint_sha256=cert.fingerprint(hashes.SHA256()).hex(":"),
        expired=not_after <= now,
        days_until_expiry=(not_after - now).days,
    )


def san_covers_host(cert: x509.Certificate, host: str) -> bool:
    """Does the cert's SAN list cover the advertised host (IP or DNS)?"""
    try:
        san_ext = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
    except x509.ExtensionNotFound:
        return False
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
        return ip in san_ext.value.get_values_for_type(x509.IPAddress)
    except ValueError:
        pass
    dns_names = san_ext.value.get_values_for_type(x509.DNSName)
    host = host.lower().rstrip(".")
    for pattern in dns_names:
        p = pattern.lower().rstrip(".")
        if p == host:
            return True
        if p.startswith("*.") and host.endswith(p[1:]) and host.count(".") == p.count("."):
            return True
    return False


def _write_protected(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _load_pem_certificates(data: bytes, label: str) -> list:
    try:
        certs = x509.load_pem_x509_certificates(data)
    except ValueError as exc:
        raise CertificateError(f"could not parse {label} as PEM") from exc
    if not certs:
        raise CertificateError(f"no PEM certificates found in {label}")
    return certs


def _load_pem_private_key(data: bytes):
    try:
        return serialization.load_pem_private_key(data, password=None)
    except TypeError as exc:
        raise CertificateError(
            "encrypted private keys are not supported; supply an unencrypted PEM key"
        ) from exc
    except ValueError as exc:
        raise CertificateError("could not parse PEM private key") from exc


def _public_numbers(obj):
    return obj.public_key().public_numbers()


def import_pem(
    cert_pem: bytes,
    key_pem: bytes,
    advertised_host: str,
    intermediates_pem: bytes = b"",
    cert_dir: Path = None,
) -> dict:
    """Validate an imported certificate/key pair and store it protected.

    Returns public metadata dict (stored in settings); raises
    CertificateError on any validation failure.
    """

    chain = _load_pem_certificates(cert_pem, "certificate file")
    intermediates = (
        _load_pem_certificates(intermediates_pem, "intermediate file")
        if intermediates_pem
        else []
    )
    leaf = chain[0]
    full_chain = chain + intermediates
    key = _load_pem_private_key(key_pem)

    # 1. Private key must correspond to the leaf's public key.
    if _public_numbers(key) != _public_numbers(leaf):
        raise CertificateError(
            "private key does not match the certificate public key"
        )

    meta = metadata_for(leaf)

    # 2. Current validity.
    now = datetime.now(timezone.utc)
    if leaf.not_valid_before_utc > now:
        raise CertificateError("certificate is not yet valid")
    if meta.expired:
        raise CertificateError("certificate is expired")

    # 3. SAN coverage of the advertised host (NET05).
    if not san_covers_host(leaf, advertised_host):
        raise CertificateError(
            f"certificate SANs do not cover {advertised_host!r} "
            f"(SANs: {', '.join(meta.sans) or 'none'})"
        )

    # 4. Chain completeness: every cert after the leaf must be issued by a
    #    later element or a supplied intermediate; the root must be
    #    self-issued. (Trust on any given client still cannot be guaranteed.)
    issuers = {c.subject: c for c in full_chain}
    for i, cert in enumerate(full_chain):
        if i == len(full_chain) - 1:
            if cert.issuer != cert.subject:
                raise CertificateError(
                    "incomplete chain: the topmost certificate is not "
                    "self-issued; supply the missing intermediate(s)"
                )
        elif cert.issuer not in issuers:
            raise CertificateError(
                f"incomplete chain: issuer { _rdn(cert.issuer) } not supplied"
            )

    if cert_dir is not None:
        _write_protected(cert_dir / "fullchain.pem", b"".join(
            c.public_bytes(serialization.Encoding.PEM) for c in full_chain
        ), mode=0o644)
        _write_protected(cert_dir / "private-key.pem", key_pem, mode=0o600)

    result = meta.__dict__.copy()
    result["mode"] = "imported"
    return result


def generate_local(
    hosts: list,
    cert_dir: Path,
    common_name: str = "TestOrchestrator Local CA",
) -> dict:
    """Mode A: per-install CA + leaf signed for the selected SANs.

    Returns public metadata; ``ca_cert_pem`` may be exported for the
    trust-store consent flow. Private key/CA key are stored 0600 in
    ``cert_dir`` and never leave the machine.
    """

    now = datetime.now(timezone.utc)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0), critical=True
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    san_names = []
    for host in hosts:
        try:
            san_names.append(x509.IPAddress(ipaddress.ip_address(host.strip("[]"))))
        except ValueError:
            san_names.append(x509.DNSName(host.rstrip(".").lower()))
    if not san_names:
        raise CertificateError("no SAN hosts selected for the certificate")

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [x509.NameAttribute(NameOID.COMMON_NAME, "TestOrchestrator")]
            )
        )
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san_names), critical=False)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    _write_protected(
        cert_dir / "fullchain.pem",
        leaf.public_bytes(serialization.Encoding.PEM)
        + ca_cert.public_bytes(serialization.Encoding.PEM),
        mode=0o644,
    )
    _write_protected(
        cert_dir / "private-key.pem",
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
    )
    _write_protected(
        cert_dir / "local-ca-key.pem",
        ca_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
    )
    _write_protected(
        cert_dir / "local-ca.pem",
        ca_cert.public_bytes(serialization.Encoding.PEM),
        mode=0o644,
    )

    meta = metadata_for(leaf).__dict__
    meta["mode"] = "local"
    meta["ca_fingerprint_sha256"] = ca_cert.fingerprint(hashes.SHA256()).hex(":")
    return meta
