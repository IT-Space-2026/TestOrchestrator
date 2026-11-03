"""v1.8 certificate acceptance: CERT01–CERT04."""

import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from testorchestrator.certs import (CertificateError, generate_local,
                                    import_pem, san_covers_host)


def _key(size=2048):
    return rsa.generate_private_key(public_exponent=65537, key_size=size)


def _pem_key(key):
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


def _pem_cert(cert):
    return cert.public_bytes(serialization.Encoding.PEM)


def _selfsigned_ca():
    key = _key(3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test CA")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                       critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _leaf(ca_key, ca_cert, host, days=90, expired=False):
    key = _key()
    now = datetime.now(timezone.utc)
    san = x509.SubjectAlternativeName([x509.DNSName(host)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "test leaf")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=365 if expired else 1))
        .not_valid_after(
            now - timedelta(days=30) if expired else now + timedelta(days=days)
        )
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


@pytest.fixture()
def chain(tmp_path):
    ca_key, ca_cert = _selfsigned_ca()
    leaf_key, leaf = _leaf(ca_key, ca_cert, "tests.example.com")
    return {
        "cert_pem": _pem_cert(leaf) + _pem_cert(ca_cert),
        "key_pem": _pem_key(leaf_key),
        "host": "tests.example.com",
    }


# CERT01 — valid import succeeds; bad inputs are blocked.
def test_cert01_valid_import(chain, tmp_path):
    meta = import_pem(chain["cert_pem"], chain["key_pem"], chain["host"],
                      cert_dir=tmp_path / "certs")
    assert meta["mode"] == "imported"
    assert meta["subject"]
    assert meta["fingerprint_sha256"]
    assert (tmp_path / "certs" / "fullchain.pem").exists()
    assert (tmp_path / "certs" / "private-key.pem").exists()


def test_cert01_mismatched_key_rejected(chain, tmp_path):
    with pytest.raises(CertificateError, match="does not match"):
        import_pem(chain["cert_pem"], _pem_key(_key()), chain["host"],
                   cert_dir=tmp_path)


def test_cert01_expired_rejected(tmp_path):
    ca_key, ca_cert = _selfsigned_ca()
    leaf_key, leaf = _leaf(ca_key, ca_cert, "tests.example.com", expired=True)
    with pytest.raises(CertificateError, match="expired"):
        import_pem(_pem_cert(leaf) + _pem_cert(ca_cert), _pem_key(leaf_key),
                   "tests.example.com", cert_dir=tmp_path)


def test_cert01_malformed_rejected(tmp_path):
    with pytest.raises(CertificateError):
        import_pem(b"not a pem", b"also not", "localhost", cert_dir=tmp_path)


def test_cert01_san_mismatch_rejected(chain, tmp_path):
    with pytest.raises(CertificateError, match="SANs do not cover"):
        import_pem(chain["cert_pem"], chain["key_pem"], "other.example.org",
                   cert_dir=tmp_path)


def test_cert01_incomplete_chain_rejected(tmp_path):
    """Leaf alone (missing issuing CA) is an incomplete chain."""
    ca_key, ca_cert = _selfsigned_ca()
    leaf_key, leaf = _leaf(ca_key, ca_cert, "tests.example.com")
    with pytest.raises(CertificateError, match="incomplete chain"):
        import_pem(_pem_cert(leaf), _pem_key(leaf_key), "tests.example.com",
                   cert_dir=tmp_path)


def test_cert01_ip_san_coverage(tmp_path):
    ca_key, ca_cert = _selfsigned_ca()
    key = _key()
    now = datetime.now(timezone.utc)
    import ipaddress as _ip
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "ip leaf")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(_ip.IPv4Address("192.0.2.9"))]),
            critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    meta = import_pem(_pem_cert(leaf) + _pem_cert(ca_cert), _pem_key(key),
                      "192.0.2.9", cert_dir=tmp_path)
    assert "192.0.2.9" in meta["sans"]


# CERT02 — generated certificates are unique per installation.
def test_cert02_unique_per_install(tmp_path):
    a = generate_local(["localhost", "127.0.0.1"], tmp_path / "a")
    b = generate_local(["localhost", "127.0.0.1"], tmp_path / "b")
    assert a["ca_fingerprint_sha256"] != b["ca_fingerprint_sha256"]
    assert a["fingerprint_sha256"] != b["fingerprint_sha256"]
    assert a["mode"] == "local"
    assert set(a["sans"]) == {"localhost", "127.0.0.1"}


# CERT03 — keys are restricted on disk and absent from UI/audit.
def test_cert03_key_permissions(tmp_path):
    cert_dir = tmp_path / "secure"
    generate_local(["localhost"], cert_dir)
    assert stat.S_IMODE(os.stat(cert_dir).st_mode) & 0o777 == 0o700
    key_mode = stat.S_IMODE(
        os.stat(cert_dir / "private-key.pem").st_mode) & 0o777
    assert key_mode == 0o600


def test_cert03_no_key_material_in_ui_or_audit(client, login_as, app):
    login_as("admin")
    html = client.get("/system/network").data.decode()
    assert "PRIVATE KEY" not in html
    from testorchestrator.models import AuditEvent
    with app.app_context():
        for event in AuditEvent.query.all():
            assert "PRIVATE KEY" not in (event.detail or "")
            assert "BEGIN" not in (event.detail or "")
