#!/usr/bin/env python3
"""Append install-unique secrets to the platform env file (installer step).

systemd/NSIS write the deterministic TO_* wiring; the values that must be
unique per installation — TO_SECRET_KEY (session signing) and
TO_INSTALL_ID — are generated here with the embedded interpreter because
neither service manager can produce secrets itself. Idempotent: keys
already present are preserved so reruns/upgrades never invalidate live
sessions or the install identity.

Usage: init_env.py --env-file <path>
"""

import argparse
import secrets
from pathlib import Path

GENERATED = (
    ("TO_SECRET_KEY", lambda: secrets.token_hex(24)),
    ("TO_INSTALL_ID", lambda: secrets.token_hex(12)),
)


def ensure(env_path: Path) -> list:
    text = env_path.read_text(encoding="utf-8") \
        if env_path.exists() else ""
    added = []
    for key, gen in GENERATED:
        if f"{key}=" in text:
            continue
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"{key}={gen()}\n"
        added.append(key)
    env_path.write_text(text, encoding="utf-8")
    return added


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", required=True)
    a = ap.parse_args(argv)
    env_path = Path(a.env_file)
    added = ensure(env_path)
    print(f"[init_env] {env_path}: "
          + (", ".join(added) if added else "secrets preserved"))


if __name__ == "__main__":
    main()
