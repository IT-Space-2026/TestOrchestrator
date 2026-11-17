# Licence format and date semantics

## Code
`TO1.<b64url(payload-json)>.<b64url(ed25519 signature)>` — pasted into the
installer, `/licence` activation page, or Admin → Licence.

## Payload (canonical JSON, sorted keys, compact separators)
`version`, `key_id`, `licence_id`, `product` (`TestOrchestrator`),
`customer`, `valid_from`, `valid_until` (ISO-8601 UTC). No installation
binding — licences are portable (owner decision).

## Cryptography
Ed25519 sign/verify. The app holds only the public key
(`release/licence_pubkey.pem`, key_id `prod`); the issuer holds the
private key. Tampered payloads, bad signatures, unknown key_ids,
wrong products, malformed codes, and invalid date ranges are rejected.

## Dates
UTC, internally half-open `[valid_from, valid_until)`. States:
missing / invalid / not-yet-valid / valid / expired. Relies on the
host clock — no tamper-proof expiry is claimed.

## Status is informational (since 2.0.4)
The application licence is NOT an enforcement gate. Install, startup,
login, dispatch, workers, imports, reports, admin routes, and the CLI
all work with a missing/expired/invalid licence. The sole
user-visible effect of a non-valid licence is the exact notice
`This version is not supported` on the login page (login itself is
never blocked). `/licence` and `/licence-activate` remain for status
viewing and recording a code; `scripts/install.py` accepts an
optional `TO_LICENCE_CODE` and warns — never aborts — on a rejected
code. Signature verification and stored-row re-verification are
unchanged; this is separate from KRE licensing, which is untouched.
