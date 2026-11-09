# First-party Python security review — main@5cb794c

**Scope:** `testorchestrator/` + `scripts/` (55 files). Third-party,
vendored, and bundled-runtime source excluded. Method: AST scan +
targeted manual review — NOT a penetration test or proof of absence.

## Findings

| # | File / area | Severity | Evidence | Disposition |
|---|---|---|---|---|
| 1 | `secrets_store.ensure_coordinator_credential` / `_provision` | **med** | `store_secret` is an upsert; a racing provision silently overwrote the winner's credential (no conflict) | **FIXED** — insert-only `_provision`; UNIQUE-name raises, guard adopts. `test_concurrent_provisioning_race`, `test_existing_credential_not_overwritten_on_rerun` |
| 2 | `operational_logs.js` stale-response | med | inline `<script>` CSP-blocked → live-tail dead; stale responses could update a paused view | **FIXED** — external script + generation guard (all branches). `test_oplogs_livetail.py` (5 checks) + interception cases |
| 3 | Command injection | — | 0 `shell=True`/`os.system`; all `subprocess.run([...])` list-arg | no finding in reviewed scope |
| 4 | eval/exec/unsafe deserialization | — | AST scan | none in scope |
| 5 | Path traversal / archive | — | `send_file`/`open` with `..` | none in scope; report serving uses containment |
| 6 | Unused imports / dead code | low | 37 unused imports | removed `8030ce4` |

## Corrections to earlier overstatements
- **CSRF ≠ hidden-key rejection:** CSRF is enforced by Flask-WTF
  `csrf` on state-changing POSTs (see `extensions.py`); the `dagu_*`
  hidden-key rejection is a separate server-side field filter, not a
  CSRF mechanism. Both verified.
- **Subprocess list-arg ≠ generated-script safety:** list-form args
  prevent shell injection in `subprocess`; the generated `to-worker`
  shell/batch is separately validated (identity/label regex, baked
  coordinator address, no untrusted concat).
- **Sequential idempotency ≠ concurrency:** `if exists → return` proved
  single-caller idempotency only; the insert-only fix + race test now
  covers the concurrent path.
- **AST "no dead functions"** is scoped to the parsed tree — dynamic
  callers, platform-specific paths, and framework registration aren't
  statically provable; preserved as a limitation.

## Limitations / deferred
- Coordinator basic-auth (auth.mode:basic) verified at config + adapter
  level only — `test_provision_and_basic_auth_roundtrip` exercises
  config/adapter code, not a live authenticated endpoint.
- Same-origin report serving: authorization+containment bound report
  scripts but do NOT isolate the origin — unresolved topology decision.
- Provisioning concurrency tested deterministically (conflict+adopt),
  not under real simultaneous HTTP load.
