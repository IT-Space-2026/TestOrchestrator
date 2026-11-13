# Installation verification procedures — FOR APPROVAL

## A. Clean-target Linux (proposed baseline: Ubuntu 24.04 amd64)

**Target:** `docker run --rm -it ubuntu:24.04` or a disposable VM.
No dev-host tools required inside the container — only the tarball.

```bash
# host side
docker run --rm -d --name to-lnx -v $PWD/dist:/dist:ro ubuntu:24.04 sleep infinity
docker exec to-lnx bash -c '
set -e
apt-get update -qq && apt-get install -y -qq systemd >/dev/null  # for service-manager path
tar xzf /dist/testorchestrator-1.0.0-pre.5-linux-x86_64.tar.gz -C /opt
cd /opt/testorchestrator-1.0.0-pre.5
./install.sh --non-interactive          # or interactive; records prompts
'
```

**Assertions (inside the container):**
1. `install.sh` exits 0; `/etc/systemd/system/testorchestrator-*.service` created.
2. `systemctl start testorchestrator-web` → `systemctl is-active` = active.
3. `curl -sf http://127.0.0.1:<port>/login` → 200 (schema migrated, bootstrap Admin created).
4. `python -c "import dulwich"` inside the bundled runtime → 1.2.20, no git on PATH needed.
5. Bundled Node.js + Allure 3 resolves reporting; the generation probe produces a report.
6. Download route lists `dagu_2.25.8_*_amd64.tar.gz` + worker installer, checksums match.
7. `ldd` sweep of every bundled executable → no `not found` outside libc/glibc-2.39+.
8. `TO_GIT_BACKEND=dulwich` clone of a local `file://` repo succeeds with no git binary.

**Cannot exercise in this environment:** real KRE execution, remote worker TLS, upgrades from a prior install, browser UI review.
**Cleanup:** `docker rm -f to-lnx` — nothing persists.

## B. Windows amd64 (Win10/11 or Server 2019+ VM)

```powershell
# Run: testorchestrator-1.0.0-pre.5-windows-amd64-setup-UNSIGNED.exe
# SmartScreen warning expected — unsigned build.
# Then VERIFY-WINDOWS.md checklist:
# 1. pythonw/python.exe imports flask, sqlalchemy, dulwich, waitress
# 2. Allure 3 via bundled node.exe + allure3 generates a report
# 3. schtasks tasks created (Web/Worker/Dagu); UI restart round-trips
# 4. Upgrade over an existing install preserves %ProgramData% config+db
# 5. Uninstall leaves %ProgramData% unless opted-in removal
```

**Open design item — task principal:** the installer currently registers tasks under the installing user; `_launch_restart_helper` uses `/RU SYSTEM`, which requires the portal task itself to run as SYSTEM/admin. Resolution needed before runtime acceptance: either register the portal tasks under SYSTEM at install, or drop `/RU` and let tasks inherit the caller's account. Unresolved on Linux-only testing.
