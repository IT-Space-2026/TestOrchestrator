#!/usr/bin/env python3
"""Worker-side KRE credential resolver + output scrubber.

Invoked by the Dagu execution DAG instead of the raw KRE executable.
It reads a worker-local credential file, launches KRE with an argv
LIST (no shell anywhere in the chain — file contents and paths are
data, never executable input), and pumps the child's stdout/stderr
through a byte-level scrub so the credential cannot land in Dagu's
own step capture — redaction is applied BEFORE output is persisted
or streamed, at the source, not only when the portal collects it.

Usage:
    kre_cred_exec.py <credential-file> <katalonc> [kre-args...]

Security contract:
  - The DAG spec carries this script's path and the credential FILE
    PATH — never the secret value.
  - The file must be a regular file (no symlink substitution), owned
    by the invoking account, with no group/other permission bits,
    inside the configured directory (or KRE_CRED_DIR when set), and
    contain a narrowly-shaped credential (see _KEY_RE). Validation is
    performed on the opened file descriptor — no check/read race.
  - A pre-supplied -apiKey argument is rejected: exactly one
    credential source.
  - The resolved value is passed to KRE as an argv element
    (-apiKey=<value>) and scrubbed from its output. It appears in
    KRE's argv on the worker host — a documented process-level
    exposure, visible to same-account/privileged inspection only.
    It is never printed by this wrapper, logged, or exported.
"""
from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import sys
import time
import threading

# Narrow credential shape: Katalon API keys are UUID-shaped tokens.
# Anything else is rejected as data — never interpreted.
_KEY_RE = re.compile(rb"^[A-Za-z0-9_-]{1,128}$")
_REDACT = b"***REDACTED***"


def _fail(msg: str) -> "NoReturn":  # noqa: F821
    sys.stderr.write(f"kre_cred_exec: {msg}\n")
    raise SystemExit(3)


def read_credential(path: str, allowed_dir: str = "") -> bytes:
    if not path or not os.path.isabs(path):
        _fail("credential file path must be absolute")
    conf_dir = os.path.realpath(
        allowed_dir or os.path.dirname(path) or os.sep)
    real = os.path.realpath(path)
    if not real.startswith(conf_dir + os.sep):
        _fail("credential file outside permitted directory")
    # O_NOFOLLOW: refuse symlink substitution on the final component.
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        _fail(f"credential file unreadable: {exc.strerror or exc}")
    try:
        st = os.fstat(fd)  # checks on the opened fd — no stat/read race
        if not stat.S_ISREG(st.st_mode):
            _fail("credential file is not a regular file")
        if st.st_uid != os.geteuid():
            _fail("credential file not owned by the invoking account")
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            _fail("credential file has group/other permission bits")
        data = os.read(fd, 513)
    finally:
        os.close(fd)
    data = data.strip()
    if not data:
        _fail("credential file is empty")
    if not _KEY_RE.match(data):
        _fail("credential file contents are not a valid token shape")
    return data


def _pump(src, dst, needle: bytes) -> None:
    """Copy src->dst with the credential scrubbed — safe against
    occurrences split across read chunks. Before emitting, the buffer
    is repeatedly shortened until it does NOT end with a prefix of the
    needle; the stripped part is carried into the next buffer, so a
    straddling key is completed and replaced downstream rather than
    reassembled from fragments."""
    tail = b""
    while True:
        chunk = src.read(65536)
        if not chunk:
            break
        buf = tail + chunk
        tail = b""
        while True:
            k = 0
            for i in range(min(len(needle) - 1, len(buf)), 0, -1):
                if buf.endswith(needle[:i]):
                    k = i
                    break
            if not k:
                break
            tail = buf[-k:] + tail
            buf = buf[:-k]
        dst.write(buf.replace(needle, _REDACT))
        dst.flush()
    if tail:
        dst.write(tail.replace(needle, _REDACT))
        dst.flush()


def main(argv: list) -> int:
    if len(argv) < 3:
        sys.stderr.write(
            "usage: kre_cred_exec.py <credential-file> <katalonc> "
            "[kre-args...]\n")
        return 3
    cred_file, katalonc = argv[1], argv[2]
    kre_args = argv[3:]
    if any(a == "-apiKey" or a.startswith("-apiKey=") for a in kre_args):
        _fail("conflicting credential source: -apiKey already supplied")
    allowed_dir = os.environ.get("KRE_CRED_DIR", "")
    key = read_credential(cred_file, allowed_dir)
    if not os.path.isfile(katalonc):
        _fail("KRE executable not found at the configured path")
    # argv list only — Popen, no shell. Child output is scrubbed so the
    # coordinator's own step capture never persists the value.
    proc = subprocess.Popen(
        [katalonc, *kre_args, "-apiKey=" + key.decode()],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _term(_sig, _frm):
        proc.terminate()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    threads = [
        threading.Thread(target=_pump, daemon=True,
                         args=(proc.stdout, sys.stdout.buffer, key)),
        threading.Thread(target=_pump, daemon=True,
                         args=(proc.stderr, sys.stderr.buffer, key)),
    ]
    for t in threads:
        t.start()
    code = proc.wait()
    # Daemon pumps: a grandchild inheriting the child's stdout/stderr
    # fd keeps the pipe open — a join here would hang the wrapper after
    # the child is gone. Drain briefly, then return; leftover buffered
    # output is terminal evidence the child is dead anyway.
    deadline = time.monotonic() + 2.0
    for t in threads:
        t.join(timeout=max(0.0, deadline - time.monotonic()))
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
