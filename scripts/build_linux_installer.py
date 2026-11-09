#!/usr/bin/env python3
"""Wrap the produced testorchestrator-<ver>-linux-x86_64.tar.gz into a
self-extracting `<ver>-linux-x86_64.run` installer — ONE file the user
executes; it unpacks its own payload and runs install.sh automatically.
No external extraction or separate setup step."""
import json, os, stat, sys, tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def build(tarball: Path, out: Path) -> Path:
    meta = json.loads((REPO / "release" / "release.json").read_text())
    if not tarball.is_file():
        raise SystemExit(f"missing payload {tarball} — run build_bundle first")
    header = out.with_suffix(".run.header")
    # self-extracting stub: appends the tarball, extracts to a private
    # temp dir, invokes install.sh, cleans up on exit.
    stub = f'''#!/bin/sh
# Self-extracting TestOrchestrator installer — single file.
# Extracts its embedded payload to a private dir and runs the
# application's install step automatically. No manual extraction.
set -eu
SELF=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/"$(basename -- "$0")"
MARKER="__TO_ARCHIVE_BELOW__"
# full-line match so this assignment isn't matched
LINE=$(grep -an -m1 "^$MARKER$" "$SELF" | cut -d: -f1)
LINE=$((LINE + 1))
[ -n "$LINE" ] || {{ echo "corrupt installer: no payload" >&2; exit 1; }}
WORK=$(mktemp -d "${{TMPDIR:-/tmp}}/to-install-XXXXXX")
# flags: --dir <target> forces the install destination; else the
# packaged default (installer-mode) applies.
tail -n +"$LINE" "$SELF" | tar xzf - -C "$WORK"
# _killtree kills install.sh's descendant tree (children before
# parents) without a TTY or touching the invoking shell's group.
# pgrep (procps) is required to discover descendants — degrade to
# direct-child kill when absent (declared prerequisite).
# two passes: TERM the whole tree, then KILL any survivor — a child
# that ignores TERM is still reaped. No sleep inside the trap.
_tree() {{
  for _c in $(pgrep -P "$1" 2>/dev/null); do _tree "$_c" "$2"; done
  kill "-$2" "$1" 2>/dev/null || :
}}
_killtree() {{
  _tree "$1" TERM
  _tree "$1" KILL
}}
if ! command -v pgrep >/dev/null 2>&1; then
  echo "note: pgrep not found — interruption cleanup limited to the" \
       "direct child (install.sh); descendants are not reaped" >&2
fi
"$WORK/install.sh" "$@" & CHILD=$!
trap 'rc=$?; _killtree "$CHILD"; rm -rf -- "$WORK"; exit "$rc"' INT TERM
wait "$CHILD"; rc=$?
rm -rf -- "$WORK"
exit $rc
__TO_ARCHIVE_BELOW__
'''
    out.write_text(stub, encoding="utf-8")
    # append the tarball payload
    with out.open("ab") as f:
        f.write(tarball.read_bytes())
    os.chmod(out, os.stat(out).st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return out


if __name__ == "__main__":
    tb = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        REPO / "dist" /
        "testorchestrator-1.9.0-linux-x86_64.tar.gz")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else (
        tb.with_suffix(".run"))
    p = build(tb, out)
    print(p, p.stat().st_size)
