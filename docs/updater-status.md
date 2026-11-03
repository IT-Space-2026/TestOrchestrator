# Updater status (§14)

## Implemented (`testorchestrator/updater.py`)

- Signed offline bundle verification: `SHA256SUMS` + RSA-PSS/SHA-256
  detached signature (`SHA256SUMS.sig`) verified with the `cryptography`
  library — no custom crypto.
- Per-file digest verification; missing/mismatched files rejected.
- Platform guard (`manifest.json` `platform` vs host).
- Update lock flag in settings during staging.
- Consistent backup: DB + instance data + protected cert dir to a tar.gz
  under `instance/backups/` (preserves users/settings as-is — never
  reactivates bootstrap credentials).
- Staging extract (tar `filter="data"`), checkpoint persisted to settings.

## Trusted signing key provisioning

- The trusted public key lives at `TO_UPDATE_KEY`, provisioned out-of-band
  by the installer/operator — **outside** the payload and never in the
  update bundle (a key inside the signed bundle would verify nothing).
- Rotation = operator replaces the key file; audited as metadata only.

## Missing behaviour (explicitly not implemented)

- **Drain**: no execution drain/quiesce before apply (must stop new
  dispatches and wait for active runs — `update_lock` only flags).
- **Migration step**: apply assumes `flask db upgrade` runs on service
  start; no preflight migration dry-run or incompatibility check.
- **Activation/switch**: staged payload is never atomically switched into
  the live path (no symlink switch / service-unit swap).
- **Restart**: no service restart orchestration.
- **Health check**: no post-apply probe before resuming traffic
  (`flask health` exists but isn't chained).
- **Recovery/resume**: `update_checkpoint` is recorded but there is no
  resume/recover CLI that performs backup restore + rollback switchback
  (old-binary switchback requires storage compatibility check, unwritten).
- **Component updates**: runner-connector updates (never KRE) untouched.

Each missing item is a code gap, not an operator blocker.
