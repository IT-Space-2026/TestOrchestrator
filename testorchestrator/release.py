"""Bundled release metadata.

The About view reads the installed version and build identifier exclusively
from the release metadata bundled with the installed payload
(``release/release.json``). No live external lookup is performed — opening
About must work fully offline (AB03/AB04).
"""

import json
from pathlib import Path

REQUIRED_KEYS = ("version", "build")


class ReleaseMetadataError(RuntimeError):
    pass


def release_file(base_dir: Path) -> Path:
    return Path(base_dir) / "release" / "release.json"


def load_release_metadata(base_dir: Path) -> dict:
    """Load and validate bundled release metadata.

    Raises ReleaseMetadataError when the file is missing or incomplete, so a
    mis-packaged install fails loudly instead of showing fabricated data.
    """

    path = release_file(base_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseMetadataError(
            f"Cannot read bundled release metadata at {path}: {exc}"
        ) from exc

    missing = [key for key in REQUIRED_KEYS if not data.get(key)]
    if missing:
        raise ReleaseMetadataError(
            f"Bundled release metadata at {path} is missing keys: {missing}"
        )
    return data
