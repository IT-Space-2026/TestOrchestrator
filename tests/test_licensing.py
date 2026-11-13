import base64, datetime, json
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey)
from cryptography.hazmat.primitives import serialization
from testorchestrator import licensing

K = Ed25519PrivateKey.generate()
PUB = K.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo)


def _code(customer="Acme", vf="2026-01-01T00:00:00+00:00",
          vu="2027-01-01T00:00:00+00:00", kid="test", product=None,
          lid="L1", key=None, **extra):
    p = {"version": 1, "key_id": kid, "licence_id": lid,
         "product": product or licensing.PRODUCT,
         "customer": customer, "valid_from": vf,
         "valid_until": vu, **extra}
    canon = json.dumps(p, sort_keys=True, separators=(",", ":")).encode()
    sig = (key or K).sign(canon)
    return ("TO1." + base64.urlsafe_b64encode(canon).decode().rstrip("=")
            + "." + base64.urlsafe_b64encode(sig).decode().rstrip("="))


@pytest.fixture(autouse=True)
def key():
    licensing._VERIFY_KEYS.clear()
    licensing.register_key("test", PUB)
    yield
    licensing._VERIFY_KEYS.clear()


def test_valid_code_parses():
    assert not isinstance(licensing.parse(_code()), str)


def test_whitespace_tolerated():
    assert not isinstance(licensing.parse(f"  {_code()}\n"), str)


def test_tampered_payload_rejected():
    code = _code()
    head, tail = code.rsplit(".", 1)
    assert licensing.parse(head + ".AAAA") == "invalid signature"


def test_unknown_key_rejected():
    k2 = Ed25519PrivateKey.generate()
    code = _code(kid="test", key=k2)
    parts = code.split(".")
    # same sig shape but different key_id registered only
    assert licensing.parse(_code(kid="nope")) == "unknown signing key"


def test_wrong_product_rejected():
    assert licensing.parse(_code(product="Other")) == "wrong product"


def test_malformed_rejected():
    assert licensing.parse("garbage") == "malformed code"
    assert licensing.parse("TO1.x.y") == "malformed code"


def test_bad_dates_rejected():
    assert licensing.parse(
        _code(vu="2026-01-01T00:00:00+00:00")) == "invalid date range"


def _p(vf, vu):
    return {"licence_id": "x", "customer": "c", "key_id": "t",
            "valid_from": vf, "valid_until": vu,
            "_from": datetime.datetime.fromisoformat(vf),
            "_until": datetime.datetime.fromisoformat(vu)}


NOW = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)


def test_states():
    assert licensing.status([], NOW)[0] == "missing"
    assert licensing.status([_p("2026-01-01T00:00:00+00:00",
                                "2026-12-31T00:00:00+00:00")],
                            NOW)[0] == "valid"
    assert licensing.status([_p("2027-01-01T00:00:00+00:00",
                                "2027-12-31T00:00:00+00:00")],
                            NOW)[0] == "not-yet-valid"
    assert licensing.status([_p("2025-01-01T00:00:00+00:00",
                                "2025-12-31T00:00:00+00:00")],
                            NOW)[0] == "expired"


def test_future_renewal_does_not_block_current():
    rows = [_p("2026-01-01T00:00:00+00:00", "2026-12-31T00:00:00+00:00"),
            _p("2027-01-01T00:00:00+00:00", "2027-12-31T00:00:00+00:00")]
    assert licensing.status(rows, NOW)[0] == "valid"


def test_gap_stays_a_gap():
    rows = [_p("2025-01-01T00:00:00+00:00", "2025-06-30T00:00:00+00:00"),
            _p("2027-01-01T00:00:00+00:00", "2027-12-31T00:00:00+00:00")]
    st, _ = licensing.status(rows, NOW)
    # neither licence covers NOW — 'not-yet-valid' names the gap
    # honestly (a pending renewal exists but has not started);
    # the app must NOT report 'valid' or continuous coverage.
    assert st in ('expired', 'not-yet-valid')
    assert st != 'valid'
