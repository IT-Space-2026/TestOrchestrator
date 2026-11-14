#!/bin/sh
# Full TestOrchestrator Windows application installer — stages every
# redistributable dependency for a Windows amd64 host, all
# checksum-pinned. KRE is NOT included (separately licensed).
set -eu
cd "$(dirname "$0")"
REPO="$(cd ../.. && pwd)"

SHA_PY="0d57bb6cb078b74d23dbfe91f77d6780d45bed328911609f1f7ee2ba1606bf44"
SHA_CAD="ad93112d335ee687100c4c9f11bced16ca590377976b7fe7883882b9e797e472"

fetch() { # url dest sha256
    [ -f "$2" ] || curl -fsSL -o "$2" "$1"
    echo "$3  $2" | sha256sum -c
}

rm -rf app-payload && mkdir -p app-payload/{python,dagu,nodejs,caddy}
fetch "https://www.python.org/ftp/python/3.12.7/python-3.12.7-embed-amd64.zip" \
      "/tmp/python-3.12.7-embed-amd64.zip" "$SHA_PY"
unzip -q -o /tmp/python-3.12.7-embed-amd64.zip -d app-payload/python
tar xzf "$REPO/3rdparty/dagu/dagu_2.25.8_windows_amd64.tar.gz" \
    -C app-payload/dagu dagu.exe LICENSE
SHA_NODE="e2edda75bfb089654705cf65a13b58933bfd5f57148e8ebda916f58a2474f1f7"
fetch "https://nodejs.org/dist/v26.13.1/node-v26.13.1-win-x64.zip" \
      "/tmp/node-win.zip" "$SHA_NODE"
unzip -q -o /tmp/node-win.zip -d app-payload/nodejs && \
    mv app-payload/nodejs/node-v26.13.1-win-x64/* app-payload/nodejs/ && \
    rmdir app-payload/nodejs/node-v26.13.1-win-x64
fetch "https://github.com/caddyserver/caddy/releases/download/v2.11.9/caddy_2.11.9_windows_amd64.zip" \
      "/tmp/caddy-win.zip" "$SHA_CAD"
unzip -q -o /tmp/caddy-win.zip -d app-payload/caddy
cp -r "$REPO/testorchestrator" "$REPO/migrations" "$REPO/wsgi.py" \
      "$REPO/requirements.txt" "$REPO/release" "$REPO/deploy" app-payload/
# python packages for win_amd64 — vendored, NOT a bootstrap download:
# install-time wheels land in site-packages wired via python312._pth.
${PIP:-python -m pip} download --platform win_amd64 --only-binary=:all: \
    --python-version 3.12 -r "$REPO/requirements.txt" \
    -d app-payload/wheels
mkdir -p app-payload/site-packages
for w in app-payload/wheels/*.whl; do unzip -q -o "$w" -d app-payload/site-packages; done
# allure 3 bundled npm package (allure3)
mkdir -p app-payload/allure3
cp -r "$REPO/3rdparty/allure3/"* app-payload/allure3/
# git: not staged — default backend is Dulwich (pure python, no git
# executable). TO_GIT_BACKEND=cli needs operator-installed Git; that is
# a declared prerequisite, not a bundled dep.
echo "payload staged: $(du -sh app-payload | cut -f1)"
command -v makensis >/dev/null || { echo "makensis missing" >&2; exit 1; }
bash check_payload.sh
makensis -V2 app-installer.nsi
