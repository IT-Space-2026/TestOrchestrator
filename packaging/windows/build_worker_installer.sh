#!/bin/sh
# Stage the Windows Dagu-worker payload from the PINNED archive and
# build worker-installer.exe via NSIS.
#
# The payload is the checksum-verified dagu_2.25.8_windows_amd64
# archive's dagu.exe — self-contained, no embedded Python or runtime.
# The Dagu worker is the remote-execution component: it runs on the
# Windows machine containing KRE and connects outbound to the portal's
# coordinator — no inbound port needed.
#
# Missing resource for full verification: a Windows amd64 host (or VM)
# to run the produced .exe, confirm the task registers, the worker
# reaches a coordinator, and uninstall cleans up. makensis builds the
# .exe on Linux but cannot execute it.
set -eu
cd "$(dirname "$0")"
REPO="$(cd ../.. && pwd)"
ARCHIVE="$REPO/3rdparty/dagu/dagu_2.25.8_windows_amd64.tar.gz"
command -v makensis >/dev/null || {
    echo "makensis not found — install NSIS (apt install nsis) or run" >&2
    echo "on a Windows build agent." >&2; exit 1; }
[ -f "$ARCHIVE" ] || {
    echo "pinned windows archive missing — run scripts/fetch_dagu.sh" >&2
    exit 1; }
# Checksum-verify the payload source before staging.
expected=$(grep "dagu_2.25.8_windows_amd64.tar.gz" \
    "$REPO/3rdparty/dagu/dagu.sha256" | awk '{print $1}')
actual=$(sha256sum "$ARCHIVE" | awk '{print $1}')
[ "$actual" = "$expected" ] || {
    echo "archive checksum mismatch" >&2; exit 1; }
rm -rf worker-payload && mkdir -p worker-payload
tar xzf "$ARCHIVE" -C worker-payload dagu.exe LICENSE README.md
makensis -V2 worker-installer.nsi
echo "built testorchestrator-dagu-worker-*-setup-UNSIGNED.exe"
