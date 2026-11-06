#!/bin/sh
# Full TestOrchestrator Windows application installer — stages every
# redistributable dependency for a Windows amd64 host, all
# checksum-pinned. KRE is NOT included (separately licensed).
set -eu
cd "$(dirname "$0")"
REPO="$(cd ../.. && pwd)"

SHA_PY="0d57bb6cb078b74d23dbfe91f77d6780d45bed328911609f1f7ee2ba1606bf44"
SHA_JRE="2332c0b4cff6bed722da3567c91d5e47568bcba863fa4945d0a8d6fc76158628"
SHA_CAD="fc99862101cd015a1fd2c1b38376109b543a5de5b6fa63ecf35a491b41e3613a"

fetch() { # url dest sha256
    [ -f "$2" ] || curl -fsSL -o "$2" "$1"
    echo "$3  $2" | sha256sum -c
}

rm -rf app-payload && mkdir -p app-payload/{python,dagu,jre,caddy}
fetch "https://www.python.org/ftp/python/3.12.7/python-3.12.7-embed-amd64.zip" \
      "/tmp/python-3.12.7-embed-amd64.zip" "$SHA_PY"
unzip -q -o /tmp/python-3.12.7-embed-amd64.zip -d app-payload/python
tar xzf "$REPO/3rdparty/dagu/dagu_2.25.8_windows_amd64.tar.gz" \
    -C app-payload/dagu dagu.exe LICENSE
fetch "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.13%2B9/OpenJDK21U-jre_x64_windows_hotspot_21.0.13_9.zip" \
      "/tmp/jre-win.zip" "$SHA_JRE"
mkdir -p app-payload/jre && unzip -q -o /tmp/jre-win.zip -d app-payload/jre && \
    mv app-payload/jre/jdk-*/jre/* app-payload/jre/ 2>/dev/null || \
    mv app-payload/jre/jdk-*/* app-payload/jre/
fetch "https://github.com/caddyserver/caddy/releases/download/v2.9.1/caddy_2.9.1_windows_amd64.zip" \
      "/tmp/caddy-win.zip" "$SHA_CAD"
unzip -q -o /tmp/caddy-win.zip -d app-payload/caddy
cp -r "$REPO/testorchestrator" "$REPO/migrations" "$REPO/wsgi.py" \
      "$REPO/requirements.txt" "$REPO/release" "$REPO/deploy" app-payload/
# python packages for win_amd64 — vendored, NOT a bootstrap download:
# install-time wheels land in site-packages wired via python312._pth.
"$REPO/.venv/bin/pip" download --platform win_amd64 --only-binary=:all: \
    --python-version 3.12 -r "$REPO/requirements.txt" \
    -d app-payload/wheels
mkdir -p app-payload/site-packages
for w in app-payload/wheels/*.whl; do unzip -q -o "$w" -d app-payload/site-packages; done
# allure commandline (ships allure.bat — works on the Windows JRE)
mkdir -p app-payload/allure
tar xzf "$REPO/3rdparty/allure/"*.tar.gz -C app-payload/allure \
    --strip-components=1 2>/dev/null || cp -r "$REPO/3rdparty/allure/"* \
    app-payload/allure/
# portable git for windows (7z self-extracting exe — extracted at install)
fetch "https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4/PortableGit-2.56.0.4-64-bit.7z.exe" \
    "/tmp/PortableGit-2.56.0.4-64-bit.7z.exe" \
    "b1f3e52216e2ed6882d1bebfd4d3fdf319bec40d2edbbf9fd26ec4fd3fce7ed1"
cp /tmp/pg.7z.exe app-payload/git-setup.7z.exe
echo "payload staged: $(du -sh app-payload | cut -f1)"
command -v makensis >/dev/null || { echo "makensis missing" >&2; exit 1; }
bash check_payload.sh
makensis -V2 app-installer.nsi
