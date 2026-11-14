"""CI licence acceptance on the *installed* package (no source checkout).

Generates a throwaway Ed25519 keypair in-process, signs a licence, activates it
via the app's own API, and exercises negative paths. The private key never
leaves the runner and is unrelated to any shipped key.
"""
import base64
import json
import pathlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat)

from testorchestrator import create_app
from testorchestrator import licensing as L
from testorchestrator.extensions import db


def b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def make(priv, kid, until, start="2020-01-01T00:00:00+00:00"):
    p = {"version": 1, "key_id": kid, "licence_id": "ci-lic",
         "product": "TestOrchestrator", "customer": "CI",
         "valid_from": start, "valid_until": until}
    canon = json.dumps(p, sort_keys=True, separators=(",", ":")).encode()
    return "TO1." + b64(canon) + "." + b64(priv.sign(canon))


KEY_DIR = pathlib.Path(r"C:\to-smoke-lic")

app = create_app()
with app.app_context():
    db.create_all()

    # negative: garbage rejected and not persisted
    ok, msg = L.add("TO1.garbage.sig")
    assert not ok, "garbage code accepted"
    assert L.stored() == [], "rejected code persisted"
    print("PASS malformed rejected + not persisted:", msg)

    # ephemeral keypair -> sign valid licence -> activate
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    L.register_key("ci", pub)
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    (KEY_DIR / "ci-priv.pem").write_bytes(priv.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))

    ok, msg = L.add(make(priv, "ci", "2099-01-01T00:00:00+00:00"))
    assert ok, "valid ephemeral code rejected: " + msg
    print("PASS ephemeral valid licence activated")
    st = L.current_status()[0]
    assert st == "valid", st
    print("PASS current_status = valid")

    # expired: signature ok but classified expired
    pe = L.parse(make(priv, "ci", "2021-01-01T00:00:00+00:00"))
    assert not isinstance(pe, str), "expired code failed signature/parse"
    s, _ = L.status([pe])
    assert s == "expired", s
    print("PASS expired code verifies(sig-ok) but status = expired")
