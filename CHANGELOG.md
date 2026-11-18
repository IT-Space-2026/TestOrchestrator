# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [2.5.0] — 2026-11-19

Initial public release.

### Portal and frontend

- Flask/Jinja2 portal served over HTTPS by **Cheroot, in-process** —
  no separate web-tier binary or unit. Waitress remains internal
  loopback only.
- **Admin → Network & certificates** manages TLS material, HTTPS
  listener, and the optional HTTP→HTTPS redirect listener.
  Certificate changes apply live; listener changes are staged
  through a last-good snapshot with automatic rollback.
- Session security: Argon2 password hashing, CSRF protection,
  `Secure`/`HttpOnly` cookies, forced bootstrap password change,
  role-based access (admin/operator/viewer).

### Orchestration — Buildbot 4.3.0

- **Buildbot master** on loopback REST (`:8010`) + PB (`:9989`),
  provisioned and managed by the installer and the portal —
  credentials are generated, stored encrypted, and preserved across
  reruns.
- Controller-side workers: `to-local` (local execution) and
  `to-ssh-dispatcher` (agentless SSH).
- Three execution modes, all managed from **Admin → Workers &
  runtimes**:
  1. **Local worker** — runs on the controller host.
  2. **Remote Buildbot worker** — bundled offline installer
     (Windows `.exe` / Linux script), outbound PB with per-worker
     credentials issued and rotatable through the Admin UI.
  3. **Agentless SSH** — `sshd` + the test runtime on the host;
     controller-side dispatcher launches and monitors over SSH/SFTP
     with Admin-UI host-key approval. No agent install.
- Dispatch integrity: assignment commits before the buildrequest
  exists, explicit runner/runtime picks are honored or visibly
  rejected, cancel propagates to remote process groups with
  confirmed or honestly-flagged termination, and recovery after
  disconnect resumes in-flight work without duplicates.
- Execution history is durable and never deleted by upgrades;
  cancellation intent is preserved across reconciliation.

### Evidence and reporting

- Per-execution evidence collection (logs, JUnit, artifacts) with
  configurable retention and `toctl evidence-prune`.
- Vendored **reportingLabs** report generation served with
  generation-scoped CSP sandboxing; idempotent regeneration.
- Optional git-based evidence export.
- Audit trail for administrative actions, independent of
  operational logs.

### Application licence

- The application licence is **informational only** — it never
  gates any feature. Without a valid licence the login page shows
  an informational notice and nothing else changes.

### Packaging

- Linux x86_64: tarball + self-extracting `.run`, `install.sh`
  (`toctl install`) with offline payload, install-time migrations,
  systemd unit staging, idempotent reruns that preserve data,
  credentials, and secrets.
- Windows amd64 Buildbot worker: unsigned offline `.exe` served
  from the installed portal next to its matching
  corresponding-source archive.
- GPL third-party components ship as separate processes with
  licence texts, notices, and a written corresponding-source offer
  fulfilled by IT-Space Kft. (see `3rdparty/CORRESPONDING-SOURCES.txt`
  and `docs/redistribution-review.md`).
