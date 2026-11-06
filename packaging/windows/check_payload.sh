#!/bin/sh
# Payload completeness gate — the installer is NOT complete merely by
# containing python+java+source. Verifies every component the supported
# workflows need is staged BEFORE makensis runs.
set -eu
cd "$(dirname "$0")/app-payload"
fail=0
need() { [ -e "$1" ] || { echo "MISSING: $1"; fail=1; } }
# runtimes
need python/python.exe; need jre/bin/java.exe
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
# reporting + scm tools
need allure/allure-2.46.1/bin/allure.bat; need git-setup.7z.exe
# licences/notices
need dagu/LICENSE
[ $fail -eq 0 ] && echo "payload COMPLETE ($(du -sh . | cut -f1))" || exit 1
