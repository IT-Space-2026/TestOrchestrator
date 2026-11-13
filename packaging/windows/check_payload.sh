#!/bin/sh
# Payload completeness gate — the installer is NOT complete merely by
# containing python+java+source. Verifies every component the supported
# workflows need is staged BEFORE makensis runs.
set -eu
cd "$(dirname "$0")/app-payload"
fail=0
need() { [ -e "$1" ] || { echo "MISSING: $1"; fail=1; } }
# runtimes
need python/python.exe; need nodejs/node.exe
need dagu/dagu.exe;  need caddy/caddy.exe
# app + assets
need testorchestrator/templates; need testorchestrator/static
need migrations; need wsgi.py; need requirements.txt; need release/release.json
# python deps: every requirements.txt name must have a package dir
missing=$(for p in $(cut -d= -f1 requirements.txt | grep -v '^#'); do
    norm=$(echo "$p" | tr 'A-Z-' 'a-z_')
    ls site-packages/ 2>/dev/null | grep -qi "^${norm}" || echo "  $p"
done)
[ -n "$missing" ] && { echo "MISSING packages:$missing"; fail=1; }
# reporting tool (Windows launcher) — required
need allure3/node_modules/allure/cli.js
# git: NOT staged by default — the Dulwich backend needs no git
# executable. Only the opt-in TO_GIT_BACKEND=cli path wants it; if a
# git-setup.7z.exe was staged, it is installed alongside. Portal-side
# ssh:// remotes additionally need a host ssh client either way.
# licences/notices
need dagu/LICENSE
[ $fail -eq 0 ] && echo "payload COMPLETE ($(du -sh . | cut -f1))" || exit 1
