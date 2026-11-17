#!/bin/sh
# Full TestOrchestrator Windows application installer — stages every
# redistributable dependency for a Windows amd64 host, all
# checksum-pinned. KRE is NOT included (separately licensed).#
# SUPERSEDED for release builds — use build-scripts/build_linux.py
# or build-scripts/build_windows.py (one-command, verified path).
# Retained as a manual/debug helper; pins may drift from
# build-scripts/assets.json.

set -eu
cd "$(dirname "$0")"
REPO="$(cd ../.. && pwd)"

SHA_PY="0d57bb6cb078b74d23dbfe91f77d6780d45bed328911609f1f7ee2ba1606bf44"
SHA_CAD="ad93112d335ee687100c4c9f11bced16ca590377976b7fe7883882b9e797e472"

fetch() { # url dest sha256
    [ -f "$2" ] || curl -fsSL -o "$2" "$1"
    echo "$3  $2" | sha256sum -c
}

rm -rf app-payload && mkdir -p app-payload/{python,dagu,caddy}
fetch "https://www.python.org/ftp/python/3.12.7/python-3.12.7-embed-amd64.zip" \
      "/tmp/python-3.12.7-embed-amd64.zip" "$SHA_PY"
unzip -q -o /tmp/python-3.12.7-embed-amd64.zip -d app-payload/python
tar xzf "$REPO/3rdparty/dagu/dagu_2.27.0_windows_amd64.tar.gz" \
    -C app-payload/dagu dagu.exe LICENSE
fetch "https://github.com/caddyserver/caddy/releases/download/v2.11.9/caddy_2.11.9_windows_amd64.zip" \
      "/tmp/caddy-win.zip" "$SHA_CAD"
unzip -q -o /tmp/caddy-win.zip -d app-payload/caddy
cp -r "$REPO/testorchestrator" "$REPO/migrations" "$REPO/wsgi.py" \
      "$REPO/run_worker.py" "$REPO/requirements.txt" "$REPO/release" \
      "$REPO/deploy" "$REPO/scripts/provision_dagu.py" \
      "$REPO/reporting_labs" app-payload/   # vendored reporting-labs (MIT)
# python packages for win_amd64 — vendored, NOT a bootstrap download:
# install-time wheels land in site-packages wired via python312._pth.
${PIP:-python -m pip} download --platform win_amd64 --only-binary=:all: \
    --python-version 3.12 -r "$REPO/requirements.txt" \
    -d app-payload/wheels
mkdir -p app-payload/site-packages
for w in app-payload/wheels/*.whl; do unzip -q -o "$w" -d app-payload/site-packages; done
# git: not staged — default backend is Dulwich (pure python, no git
# executable). TO_GIT_BACKEND=cli needs operator-installed Git; that is
# a declared prerequisite, not a bundled dep.
echo "payload staged: $(du -sh app-payload | cut -f1)"
command -v makensis >/dev/null || { echo "makensis missing" >&2; exit 1; }
bash check_payload.sh
makensis -V2 app-installer.nsi
