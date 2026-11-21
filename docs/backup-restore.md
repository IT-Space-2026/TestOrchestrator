# Settings Backup & Restore — format, crypto, and restore contract (2.6)

Scope: **settings only**. Execution history, evidence, reports, and
artifacts are deliberately NOT in this format — they stay with the
instance (immutable evidence is never rewritten by a restore) and will be
a separate, separately sized backup feature later.

## File format

`testorchestrator-settings-<app_version>-<YYYYMMDDTHHMMSSZ>.tobackup`

```
TOBK | uint32le header_len | header_json | AES-256-GCM ciphertext
```

- `header_json` (plaintext): `format_version`, `app_version`,
  `db_revision` (alembic version_num), `platform`, `created_utc`, `kdf`
  (scrypt salt/n/r/p/length), `cipher`, `nonce`. Bound to the ciphertext
  as GCM **additional authenticated data** — any header edit fails the
  authentication tag.
- Payload (one JSON document inside the AEAD envelope): `manifest`
  (format/app versions, db revision, platform, timestamp, per-section row
  counts + SHA-256, per-file SHA-256, the exclusion list with reasons),
  `sections` (row dicts per config table), `files` (`secret.key` only).
- No tar/zip container: the whole payload is one bounded JSON document —
  no extraction surface, therefore no traversal/symlink/bomb classes.
  Section and file names are additionally checked against a fixed
  allowlist. Bounds: 8 MiB archive, 8 MiB payload, 64 KiB header,
  100k rows/section.

## Cryptography

- KDF: **scrypt** (`cryptography.hazmat.primitives.kdf.scrypt`), N=2^15,
  r=8, p=1, length=32 — parameters stored in the header.
- Cipher: **AES-256-GCM** (`cryptography.hazmat.primitives.ciphers.aead.AESGCM`),
  random 16-byte salt + 12-byte nonce per backup.
- No custom crypto; both primitives are the maintained `cryptography`
  library already used elsewhere in the codebase (certs, licensing,
  Fernet secret store).
- Passphrase: user-supplied, min 12 chars + confirmation field, never
  stored, never logged, never in audit details or error messages. **A
  lost passphrase cannot be recovered and the file cannot be restored
  without it.** Anyone holding both the file and the passphrase can read
  the included secrets — treat the pair like a live credential.
- Key handling: the secret store's Fernet key (`instance/secret.key`)
  travels **inside the encrypted payload** as `files["secret.key"]`, so
  restored credentials decrypt on the new host without a separate key
  channel. It never appears outside the AEAD envelope.

## Included / excluded

Included sections (row-level, FK order): `settings`, `users`, `projects`,
`runners`, `runtime_connections`, `git_bindings`, `suites`,
`suite_entries`, `plans`, `plan_entries`, `releases`, `release_items`,
`schedules`, `secret_refs` + file `secret.key`.

Excluded (in the manifest with reasons):

- `executions`, `test_results`, `artifacts` — immutable evidence & run
  history; stays with the instance.
- `outbox_events`, `import_sessions`, `resource_reservations` — live
  process state, meaningless on another host.
- `api_tokens`, `submit_tokens` — bearer/idempotency credentials are
  revoked on restore, never resurrected.
- `audit_events` — audit history is never rewritten by a restore; the
  restore itself is recorded as a new event.
- TLS certificate files, generated exports, logs, session data, locks,
  temp files — host-specific or temporary; regenerated on the new host.
- `DiscoveredItem` rows — repo-discovered targets; re-synced from the
  bindings after restore.

## Authorization

- All backup/restore actions: **Administrator only**, enforced in the
  routes (`Role.ADMIN`), not merely hidden in navigation.
- **Recent re-authentication** (5-minute window) is required for create,
  download, validate, and apply; each sensitive form takes the admin's
  current password (or accepts the still-valid window).
- **Fresh-install restore**: while `setup_completed` is false, the
  restore accepts the **one-time, local, time-limited setup token** —
  `<instance>/.restore-setup-token`, mode 0600, 1-hour lifetime,
  regenerated per boot while unconfigured, consumed on successful
  restore. There is no unauthenticated network restore endpoint; the
  local-file token is the documented local administrative procedure.
- Break-glass: if a backup is corrupt or incomplete, a reinstall always
  bootstraps a fresh bootstrap admin (forced password change) — the
  documented recovery path.

## Restore semantics

- Two modes only: **Restore into a fresh installation** and **Replace
  current configuration**. No merge mode in this release.
- Staged flow: upload → decrypt+validate → preview → confirm → apply →
  verify. The staged file is the *encrypted* container in
  `<instance>/restore-staging/` (0700 dir, 0600 file, 30-min TTL, cleaned
  on apply/cancel/expiry/boot). Plaintext payload never touches disk.
- Apply is **row-level in one transaction**: config tables are replaced,
  history/evidence tables untouched; any failure rolls back atomically.
- `replace` mode refuses while executions are active (same guard as
  service restart) and takes an automatic **pre-restore safety backup**
  (same passphrase, same format) into `<instance>/backups/` (0700).
- **Maintenance mode**: `instance/MAINTENANCE` flag gates
  `dispatch.worker_once()` — no scheduled/dispatched/reconciled work runs
  against partially restored settings; removed after apply.
- Post-restore normalization, all in the same transaction:
  - every user's `session_version` is bumped → all restored login
    sessions die (the acting admin's just-re-authenticated session is
    deliberately retained);
  - `api_tokens` all get `revoked_at`, `submit_tokens` deleted;
  - `resource_reservations` deleted (stale locks);
  - runners get `health="unknown"`, `check_status="pending"` → **needs
    verification**, not healthy;
  - enabled schedules whose fire time passed during the outage are
    re-armed to the next interval (or disabled if `once`) — nothing
    catch-up-fires immediately after restore;
  - at least one active Administrator is required — payloads without one
    are refused before anything changes;
  - `backup.restore_applied` is recorded in the *new* system's audit
    trail (audit history itself is not restored).
- Installation-specific settings (`local_host`, ports, TLS certificate
  references, instance URL, service accounts, `workspace_root`, SSH
  hosts/paths) are restored verbatim — they may not exist on the new
  host. The restore report tells the operator to re-verify under
  System → Network & certificates and re-run worker connection checks.

## Restore matrix

| Backup from → restore into | Result |
|---|---|
| same app version | supported |
| older format_version=1 backup → newer app | supported — restore is row/column-filtered: unknown columns dropped, new columns keep defaults; alembic_version untouched |
| newer format_version → this app | **refused** with "upgrade first" message |

Migration: row-level restore never moves `alembic_version`; the applied
schema is always the running app's head. No manual database editing is
ever required.

## Clean-machine reinstall procedure

1. Install 2.6.0 on the new machine (any supported path).
2. **Do not** complete first-run setup. Open `/system/backup`.
3. Read `<instance>/.restore-setup-token` on that machine (mode 0600).
4. Upload the `.tobackup`, enter its passphrase + the setup token,
   preview, confirm "Restore into a fresh installation".
5. Sign in with a restored administrator account; re-issue CI tokens;
   re-run worker connection checks; review Network & certificates.

## Verification status

- Linux: full unit/HTTP acceptance suite — 14 tests in
  `tests/test_settings_backup.py` covering authz, re-auth, tamper,
  archive bounds, fresh+replace restore, revocation, rollback.
- Windows file-protection/service-account/path behavior: queued in the
  real-host Windows kit (not yet verified).
- Packaged reinstall→restore roundtrip: queued in the packaged
  eight-combination acceptance (not yet verified).
