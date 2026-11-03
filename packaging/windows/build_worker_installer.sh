#!/bin/sh
# Assemble the Windows worker payload + build worker-installer.exe.
#
# Tool: NSIS (makensis). Chosen over WiX/Inno because it produces a real
# Windows .exe from a plain script, runs unattended (/S + /KEY=value
# via ${GetOptions}), builds on Linux for CI, and supports UAC
# elevation only where required (service/task registration).
#
# Stages worker-payload/ with:
#   python/      — Windows embeddable CPython (acquired separately;
#                  NOT a Linux runtime renamed — see docs)
#   agent code   — testorchestrator agent/worker sources
#   agent_service.py, register_agent.py — execution + registration
#
# UNSIGNED build — the .exe name carries -UNSIGNED until an approved
# signing identity exists. No paid cert or signing is assumed.
set -eu
cd "$(dirname "$0")"
command -v makensis >/dev/null || {
    echo "makensis not found — install NSIS (apt install nsis on" >&2
    echo "build hosts) or run this on a Windows build agent." >&2
    exit 1; }
[ -d worker-payload/python ] || {
    echo "worker-payload/python missing — stage the Windows embeddable" >&2
    echo "runtime first (docs/windows-worker-installer.md)." >&2; exit 1; }
makensis -V2 worker-installer.nsi
echo "built testorchestrator-worker-*-setup-UNSIGNED.exe"
