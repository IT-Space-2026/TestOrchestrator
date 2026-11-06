# Component / version / licence manifest (1.9.1-pre1, source 44708b2)

## Included executable runtimes (in the Linux package)
| Component | Version | Source | Licence |
|---|---|---|---|
| Python runtime | 3.12 (ubuntu-resolute-amd64 rootfs) | 3rdparty/python-runtime, acquire-debs.py | PSF |
| Vendored wheels | per 3rdparty/python-wheels | pinned downloads | per wheel |
| Git (cli backend) | ubuntu-resolute-amd64 | 3rdparty/git, acquire_git.py | GPL-2.0 |
| Caddy | 2.9.1 | 3rdparty/caddy, fetch_caddy.sh | Apache-2.0 |
| Dagu (linux+win amd64) | 2.25.8 | 3rdparty/dagu, fetch_dagu.sh, upstream checksums | GPL-3.0-or-later? (see 3rdparty/dagu/LICENSE) |
| Allure commandline | 2.46.1 | 3rdparty/allure | Apache-2.0 |
| Windows Dagu worker installer | 2.25.8 (NSIS build of dagu.exe) | packaging/windows | as Dagu |

## References / download instructions (NOT included)
| Component | Status | Notes |
|---|---|---|
| Java runtime (OpenJDK 21+/25) | `java-ref`: manifest+licences only (68K, no rootfs) | **External prerequisite** — detected at install by scripts/java_probe.py, only for Allure; `--no-allure` continues without it. Package is NOT Java-self-contained. |
| Windows embeddable Python | not staged | would be needed for a full Windows app installer |

## External prerequisites
- KRE (Katalon Runtime Engine): separately installed + licensed by the
  operator — redistribution rights NOT established, so it is never
  bundled; the portal registers the user's own installation.
- OS: Ubuntu x86_64 (systemd for managed services), Windows amd64
  (worker role).
- Network: worker outbound gRPC to coordinator :50055; HTTPS to portal.

## Generated, not in Git
dist/ packages, worker-payload staging, built .exe, instance/ state.
