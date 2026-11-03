#!/bin/sh
# Publish the dist/ bundle as a GitHub PRERELEASE with its checksum,
# dependency manifest, file inventory, and release notes.
# Requires: gh CLI authenticated (gh auth login) for repo IT-Space-2026/TestOrchestrator.
set -eu
cd "$(dirname "$0")/.."
VER=$(python3 -c "import json;print(json.load(open('release/release.json'))['version'])")
NAME="testorchestrator-${VER}-linux-x86_64"
for f in "dist/${NAME}.tar.gz" "dist/${NAME}.sha256" \
         "dist/${NAME}.manifest.json" "dist/${NAME}.inventory.json" \
         "dist/RELEASE-NOTES.md"; do
    [ -f "$f" ] || { echo "missing $f — run scripts/build_bundle.py"; exit 1; }
done
gh release create "v${VER}" --prerelease \
    --title "TestOrchestrator ${VER} (prerelease)" \
    --notes-file dist/RELEASE-NOTES.md \
    "dist/${NAME}.tar.gz" "dist/${NAME}.sha256" \
    "dist/${NAME}.manifest.json" "dist/${NAME}.inventory.json"
echo "published v${VER} prerelease"
