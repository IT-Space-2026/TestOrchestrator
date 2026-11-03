# Compatibility matrix

Required by handoff §3/§13. Every entry states what is actually verified —
"automatic future support" is not claimed anywhere.

## Application runtime

| Component | Version | Status |
|---|---|---|
| Python | 3.12 | tested (dev host, Linux 7.0 x86_64) |
| Flask | 3.1.3 | tested |
| SQLAlchemy + SQLite | 2.1.6 | tested; Alembic migrations verified on v1.7→v1.8→v1.9 paths |
| Waitress | 3.0.2 | tested (loopback bind 127.0.0.1:18080) |
| Caddy (bundled) | 2.9.1 linux/amd64 | binary verified (SHA-256); `caddy validate` exercised in tests; HTTPS serving unverified end-to-end |
| GitPython | 3.2.1 | tested on local file repos; HTTPS/SSH remotes unverified |
| requests | 2.34.2 | client paths written; live services unverified |
| cryptography | 50.0.2 | tested (certs, Fernet) |
| argon2-cffi | 25.1.0 | tested |

## Target platforms (§13) — NOT yet claimed

| Platform | Status |
|---|---|
| Windows 11 | unverified — installer does not exist |
| Ubuntu LTS (current supported) | dev-tested on this host only; packaged install untested |
| SLES 16+ | unverified — requires explicit validation per spec |
| Containerized ReportPortal backend (Ubuntu Docker/Compose, SLES Podman, WSL/VM on Windows) | unverified — architecture note only |

## Verified limits

- Loopback gate assumes a real peer address post-proxy-fix; only tested via
  Werkzeug test client and Waitress on Linux.
- `detect_address_candidates()` uses UDP routing lookup + getaddrinfo —
  Linux-verified; multi-NIC/VPN/NAT behavior needs field verification.
- Process-tree cancel uses `start_new_session` + `killpg` (POSIX). Windows
  needs a Job Object or `taskkill /T` path — not implemented.
- Timezone-aware SQLite datetimes are relied upon throughout.
