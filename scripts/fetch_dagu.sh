#!/usr/bin/env bash
# Fetch the pinned Dagu release archives (linux + windows amd64) and verify
# them against the recorded digests (3rdparty/dagu/dagu.sha256). The
# archives are NOT tracked in git — run this before building a release
# payload or running the Dagu contract tests.
set -euo pipefail

VERSION="2.25.8"
DEST="$(cd "$(dirname "$0")/../3rdparty/dagu" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

for arch in linux_amd64 windows_amd64; do
    name="dagu_${VERSION}_${arch}.tar.gz"
    url="https://github.com/dagu-org/dagu/releases/download/v${VERSION}/${name}"
    curl -fsSL -o "$TMP/$name" "$url"
    expected="$(grep " $name\$" "$DEST/dagu.sha256" | awk '{print $1}')"
    actual="$(sha256sum "$TMP/$name" | awk '{print $1}')"
    if [ "$actual" != "$expected" ]; then
        echo "checksum mismatch for $name: expected $expected, got $actual" >&2
        exit 1
    fi
    install -m 0644 "$TMP/$name" "$DEST/$name"
    echo "dagu $arch verified and installed to $DEST/$name"
done
