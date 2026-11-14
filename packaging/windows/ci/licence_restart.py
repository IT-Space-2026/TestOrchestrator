"""Restart persistence check: a fresh process re-loads the stored licence row
and re-verifies its signature (stored state is not blindly trusted)."""
import pathlib

from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat, load_pem_private_key)

from testorchestrator import create_app
from testorchestrator import licensing as L
from testorchestrator.extensions import db

app = create_app()
with app.app_context():
    db.create_all()
    priv = load_pem_private_key(
        pathlib.Path(r"C:\to-smoke-lic\ci-priv.pem").read_bytes(),
        password=None)
    L.register_key("ci", priv.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    st, _ = L.current_status()
    assert st == "valid", "stored licence not valid after restart: " + st
    print("PASS persistence + re-verification after restart: status =", st)
