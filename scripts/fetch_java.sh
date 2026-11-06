#!/usr/bin/env bash
# Fetch the pinned Temurin 21 JRE (GPL+CE, redistributable) and verify it
# against 3rdparty/java-runtime/java.sha256 — the package ships this
# application-local runtime so no system Java/JAVA_HOME is required.
set -euo pipefail
DEST="$(cd "$(dirname "$0")/../3rdparty/java-runtime" && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
name="OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz"
url="https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.13%2B9/${name}"
curl -fsSL -o "$TMP/$name" "$url"
expected="$(grep " $name\$" "$DEST/java.sha256" | awk '{print $1}')"
actual="$(sha256sum "$TMP/$name" | awk '{print $1}')"
[ "$actual" = "$expected" ] || { echo "checksum mismatch" >&2; exit 1; }
mkdir -p "$DEST/ubuntu-resolute-amd64/jre"
tar xzf "$TMP/$name" -C "$DEST/ubuntu-resolute-amd64/jre" --strip-components=1
echo "temurin 21.0.13+9 jre verified and installed to $DEST/ubuntu-resolute-amd64/jre"
