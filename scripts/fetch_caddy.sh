#!/usr/bin/env bash
# Re-fetch the pinned Caddy release binary and verify it against the recorded
# digest (3rdparty/caddy/caddy.sha256). Used when preparing a release payload.
set -euo pipefail

VERSION="2.9.1"
ARCH="linux_amd64"
DEST="$(cd "$(dirname "$0")/../3rdparty/caddy" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

url="https://github.com/caddyserver/caddy/releases/download/v${VERSION}/caddy_${VERSION}_${ARCH}.tar.gz"
curl -fsSL -o "$TMP/caddy.tar.gz" "$url"
tar -xzf "$TMP/caddy.tar.gz" -C "$TMP" caddy LICENSE

actual="$(sha256sum "$TMP/caddy" | awk '{print $1}')"
expected="$(cat "$DEST/caddy.sha256")"
if [ "$actual" != "$expected" ]; then
    echo "checksum mismatch: expected $expected, got $actual" >&2
    exit 1
fi

install -m 0755 "$TMP/caddy" "$DEST/caddy"
install -m 0644 "$TMP/LICENSE" "$DEST/LICENSE"
echo "caddy $VERSION verified and installed to $DEST"
