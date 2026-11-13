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
UTC, end-date inclusive → internally half-open
`[valid_from, valid_until)` where `valid_until` = the instant after the
chosen end date. States: missing / invalid / not-yet-valid / valid /
expired. Relies on the host clock — no tamper-proof expiry is claimed.

## Enforcement points
Login + protected requests (gate), dispatch (`create_execution`),
worker polling (`worker_once`), installer (`scripts/install.py`).
Unauthenticated `/licence-activate` recovers an expired installation.
