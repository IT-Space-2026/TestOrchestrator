#!/usr/bin/env python3
"""Stamp release/release.json with the packaged build identifier.

Run at packaging time. The About view displays version + build verbatim from
this bundled file (AB04), so each upgrade/rollback payload carries its own.

Usage: python3 scripts/stamp_release.py [--version 1.7.0]
"""

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

RELEASE_FILE = Path(__file__).resolve().parent.parent / "release" / "release.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=None, help="override version string")
    args = parser.parse_args()

    data = json.loads(RELEASE_FILE.read_text(encoding="utf-8"))
    if args.version:
        data["version"] = args.version
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    data["build"] = commit
    data["built_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data.pop("notes", None)
    RELEASE_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"stamped {RELEASE_FILE}: v{data['version']} build {data['build']}")


if __name__ == "__main__":
    main()
