"""Offline date-based licence verification (Ed25519).

Code format: TO1.<b64url(payload-json)>.<b64url(ed25519 signature)>
Payload is canonical JSON (sorted keys, compact separators) containing:
  version, key_id, licence_id, product, customer, valid_from, valid_until
Licences are portable (no installation binding — owner decision).
States: missing | invalid | not-yet-valid | valid | expired.
Validity = half-open [valid_from, valid_until), UTC.
"""
import base64, binascii, datetime, json, os

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

PRODUCT = "TestOrchestrator"
FORMAT = "TO1"

# Verification keys by key_id — production key provisioned via config,
# tests inject their own via register_key().
_VERIFY_KEYS: dict[str, Ed25519PublicKey] = {}


def register_key(key_id: str, pem: bytes) -> None:
    _VERIFY_KEYS[key_id] = load_pem_public_key(pem)


_PROD_LOADED = False


def _load_production_key() -> None:
    """Register the shipped public verification key (release/
    licence_pubkey.pem) once — resolves beside the package in both
    source and installed layouts."""
    global _PROD_LOADED
    if _PROD_LOADED:
        return
    _PROD_LOADED = True
    from pathlib import Path
    pem = Path(__file__).resolve().parent.parent / "release" /         "licence_pubkey.pem"
    if pem.is_file():
        register_key("prod", pem.read_bytes())


def _canon(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True,
                      separators=(",", ":")).encode()


def parse(code: str) -> dict | str:
    """Return payload dict, or a 'reason' string on rejection."""
    _load_production_key()
    code = code.strip()
    parts = code.split(".")
    if len(parts) != 3 or parts[0] != FORMAT:
        return "malformed code"
    try:
        payload = json.loads(base64.urlsafe_b64decode(
            parts[1] + "==").decode())
        sig = base64.urlsafe_b64decode(parts[2] + "==")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return "malformed code"
    if not isinstance(payload, dict):
        return "malformed code"
    if payload.get("version") != 1:
        return "unsupported format"
    if payload.get("product") != PRODUCT:
        return "wrong product"
    kid = payload.get("key_id")
    if kid not in _VERIFY_KEYS:
        return "unknown signing key"
    try:
        _VERIFY_KEYS[kid].verify(sig, _canon(
            {k: v for k, v in payload.items()}))
    except InvalidSignature:
        return "invalid signature"
    for f in ("licence_id", "customer", "valid_from", "valid_until"):
        if not payload.get(f):
            return f"missing {f}"
    try:
        payload["_from"] = datetime.datetime.fromisoformat(
            payload["valid_from"])
        payload["_until"] = datetime.datetime.fromisoformat(
            payload["valid_until"])
    except ValueError:
        return "invalid date range"
    if payload["_until"] <= payload["_from"]:
        return "invalid date range"
    return payload


def status(payloads: list[dict], now=None) -> tuple[str, dict | None]:
    """Best state across stored licences — future renewal never blocks
    a currently valid licence; a gap stays a gap."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if not payloads:
        return "missing", None
    best = None
    for p in payloads:
        if p["_from"] <= now < p["_until"]:
            return "valid", p
        if now < p["_from"]:
            best = best or p if (best is None or
                                 p["_from"] < best["_from"]) else best
        elif best is None or p["_until"] > best.get("_until",
                                                   now - datetime.timedelta(1)):
            best = best if best and best.get("_from") else best or p
    # classify: all expired vs all future
    if all(now >= p["_until"] for p in payloads):
        return "expired", max(payloads, key=lambda p: p["_until"])
    return "not-yet-valid", min(payloads, key=lambda p: p["_from"])


# ---------- persistence (settings-backed) ----------

def stored() -> list[dict]:
    from . import settings
    raw = settings.get("licences", "[]")
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


def add(code: str) -> tuple[bool, str]:
    """Validate and persist a pasted code. Returns (ok, message)."""
    p = parse(code)
    if isinstance(p, str):
        return False, f"Licence rejected: {p}."
    lic = {k: p[k] for k in ("licence_id", "customer", "valid_from",
                             "valid_until", "key_id")}
    lic["code"] = code.strip()
    rows = stored()
    if any(r["licence_id"] == lic["licence_id"] for r in rows):
        return True, "That licence is already activated."
    rows.append(lic)
    from . import settings
    settings.set("licences", json.dumps(rows))
    _audit(lic)
    return True, (f"Licence activated for {lic['customer']} — valid "
                  f"{lic['valid_from'][:10]} to {lic['valid_until'][:10]}.")


def _audit(lic):
    try:
        from .models import record_audit
        record_audit("licence", "activated",
                     f"id={lic['licence_id']} customer={lic['customer']}")
    except Exception:
        pass


def seed_from_file(path) -> int:
    """Import installer-seeded licences (data/licence.json) into the
    settings store — runs once at startup; no re-import when the
    settings store already holds licences."""
    import os
    if stored() or not os.path.exists(path):
        return 0
    try:
        rows = json.loads(open(path).read())
    except (ValueError, OSError):
        return 0
    from . import settings
    settings.set('licences', json.dumps(rows))
    return len(rows)


def current_status(now=None):
    """(state, best_licence_dict, all_stored)."""
    inst = os.environ.get('TO_INSTANCE_DIR', '')
    if inst:
        seed_from_file(os.path.join(inst, 'licence.json'))
    rows = stored()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    parsed = []
    for r in rows:
        try:
            p = dict(r)
            p["_from"] = datetime.datetime.fromisoformat(r["valid_from"])
            p["_until"] = datetime.datetime.fromisoformat(r["valid_until"])
            parsed.append(p)
        except (KeyError, ValueError):
            continue
    st, best = status(parsed, now)
    return st, best, parsed


def usable(now=None) -> bool:
    return current_status(now)[0] == "valid"
