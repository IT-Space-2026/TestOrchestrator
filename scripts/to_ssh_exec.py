#!/usr/bin/env python3
"""Agentless SSH execution dispatcher (docs/orchestration-replacement.md).

Runs inside the controller-side ``to-ssh-dispatcher`` buildbot worker —
never on the remote host. The remote needs only SSH + the test runtime.

Protocol (remote run dir = <workdir>/<execution_id>):
  run.sh        generated argv script (validated spec — no free-form
                portal shell; every path/arg is shlex-quoted)
  run.pid       pgid of the detached run (setsid → run.sh is leader)
  exit_code     written by run.sh when the runner exits
  run.out       captured stdout+stderr
  Reports/      the runner's report tree, SFTP-collected back

Disconnect semantics: the run detaches (setsid), so a dropped session
does not kill it; a reconnecting dispatcher finds run.sh/exit_code and
RESUMES polling instead of launching again. Cancellation kills the
remote process GROUP and is reported as confirmed/unconfirmed via
TO-SSH marker lines on stdout — the portal only reports CANCELED on
``cancel-confirmed``.

Usage: to_ssh_exec.py <spec-file.json>
Exit: 0 runner done/cancel-confirmed · 2 unrecoverable setup error ·
3 runner exit != 0 · 15 cancel-unconfirmed/unreachable.
"""

import json
import os
import shlex
import signal
import sys
import time
from pathlib import Path

import paramiko

POLL_S = 3.0


def _load_spec(path: str) -> dict:
    spec = json.loads(Path(path).read_text())
    for k in ("execution_id", "host", "username", "remote_workdir",
              "executable", "args", "source_tar"):
        if not spec.get(k) and spec.get(k) != 0:
            raise SystemExit(f"spec missing {k}")
    return spec


def _fingerprints(key) -> dict:
    import base64
    import hashlib
    return {
        "SHA256": "SHA256:" + base64.b64encode(
            hashlib.sha256(key.asbytes()).digest()).decode().rstrip("="),
        "MD5": ":".join(f"{b:02x}" for b in key.get_fingerprint()),
    }


class _Client:
    """Thin wrapper so the rest of the dispatcher keeps its
    SSHClient-shaped call sites (exec_command/open_sftp/close) while
    the connect path verifies the pinned key itself."""

    def __init__(self, transport):
        self._t = transport

    def exec_command(self, cmd, timeout=30):
        chan = self._t.open_session(timeout=timeout)
        chan.settimeout(timeout)
        chan.exec_command(cmd)
        return chan, chan.makefile("rb"), chan.makefile_stderr("rb")

    def open_sftp(self):
        return paramiko.SFTPClient.from_transport(self._t)

    def close(self):
        self._t.close()


def _connect(spec) -> _Client:
    """Connect + verify the host key against the ADMIN-APPROVED
    fingerprint BEFORE authenticating — an unexpected key is a hard
    failure (re-verify in the portal), never a silent accept."""
    key_file = spec.get("key_file") or None
    if not (key_file and Path(key_file).is_file()):
        raise SystemExit("spec has no usable key_file")
    pkey = None
    last_exc = None
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey,
                paramiko.ECDSAKey, paramiko.DSSKey):
        try:
            pkey = cls.from_private_key_file(key_file)
            break
        except paramiko.SSHException as exc:
            last_exc = exc
    if pkey is None:
        raise SystemExit(f"ssh key unreadable: {last_exc}")
    import socket as _sock
    transport = paramiko.Transport(_sock.create_connection(
        (spec["host"], int(spec.get("port") or 22)), timeout=20))
    try:
        transport.start_client(timeout=20)
        remote_key = transport.get_remote_server_key()
        fps = _fingerprints(remote_key)
        approved = spec.get("host_key_fingerprint") or ""
        if approved and approved not in fps.values():
            raise SystemExit(
                "HOST KEY MISMATCH: approved fingerprint "
                f"{approved!r} but host presented "
                f"{fps['SHA256']} — re-verify the target in the "
                "portal before running")
        print(f"TO-SSH: host key verified {fps['SHA256']}")
        transport.auth_publickey(spec["username"], pkey)
    except SystemExit:
        transport.close()
        raise
    except Exception:
        transport.close()
        raise
    return _Client(transport)


def _exec(client, cmd: str, timeout: float = 30) -> tuple:
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    return stdout.channel.recv_exit_status(), stdout.read().decode(
        "utf-8", "replace"), stderr.read().decode("utf-8", "replace")


def _remote_state(client, spec) -> str:
    """resume | fresh — resume when the run dir already has run.sh."""
    wd = spec["remote_workdir"]
    rc, out, _ = _exec(client, f"test -f {shlex.quote(wd)}/run.sh "
                             f"&& echo yes || echo no")
    return "resume" if out.strip() == "yes" else "fresh"


def _prepare(client, spec) -> None:
    wd = spec["remote_workdir"]
    sftp = client.open_sftp()
    try:
        _exec(client, f"mkdir -p {shlex.quote(wd)}")
        # stage the portal-built source archive
        sftp.put(spec["source_tar"], wd + "/source.tar")
        remote = shlex.quote
        args = " ".join(remote(a) for a in spec["args"])
        cred = spec.get("credential_file") or ""
        exe = remote(spec["executable"])
        wrapper = ""
        if cred:
            # same contract as worker-local files: path only, wrapper
            # script on the REMOTE host resolves and execs — never a
            # secret in argv.
            wrapper = (remote(spec.get("remote_cred_wrapper")
                                    or "/tmp/kre_cred_exec.py") + " "
                       + remote(cred) + " ")
        # Node runners execute inside the untarred project root
        # (proj/src) — exec_subdir descends into it; env carries
        # runner vars like PLAYWRIGHT_JUNIT_OUTPUT_FILE.
        subdir = (spec.get("exec_subdir") or "").strip("/")
        cd_target = f"{remote(wd)}/proj/{remote(subdir)}" if subdir \
            else f"{remote(wd)}/proj"
        env_exports = "".join(
            f"export {remote(k)}={remote(v)}\n"
            for k, v in (spec.get("env") or {}).items()
            if k.replace("_", "").isalnum() and "\n" not in str(v))
        # Optional dependency step (node runners: npm ci — pinned
        # project deps into the isolated staged workspace).
        deps = spec.get("deps_argv") or []
        deps_line = (
            " ".join(remote(a) for a in deps)
            + f" >>{remote(wd)}/run.out 2>&1 || "
            f"{{ printf 124 > {remote(wd)}/exit_code; exit 124; }}\n"
            if deps else "")
        script = (
            "#!/bin/sh\n"
            f"mkdir -p {remote(wd)}/proj && "
            f"tar -xf {remote(wd)}/source.tar -C {remote(wd)}/proj\n"
            f"cd {cd_target} || exit 125\n"
            f"{env_exports}"
            f"{deps_line}"
            f"{wrapper}{exe} {args} "
            f">>{remote(wd)}/run.out 2>&1\n"
            f"printf '%s' \"$?\" > {remote(wd)}/exit_code\n")
        with sftp.open(wd + "/run.sh", "w") as fh:
            fh.write(script)
        _exec(client, f"chmod 700 {remote(wd)}/run.sh")
    finally:
        sftp.close()


def _launch(client, spec) -> None:
    wd = spec["remote_workdir"]
    q = shlex.quote
    # setsid detaches the run — SSH disconnects cannot kill it. The
    # async job must be ONE simple command with redirected fds: a
    # backgrounded compound (`cd && rm && setsid ... &`) forks a
    # subshell that WAITS for the detached tree while holding the
    # channel's pipes — the client read then times out for the run's
    # entire lifetime. run.pid is written by the session leader
    # itself ($$ inside the setsid'd sh), so it equals the
    # process-group id used for group cancellation — never $! of an
    # intermediate subshell.
    rc, out, err = _exec(
        client,
        f"setsid sh -c 'cd {q(wd)} && rm -f exit_code run.pid && "
        f"printf %s \"$$\" > {q(wd)}/run.pid && "
        f"exec ./run.sh' </dev/null >{q(wd)}/launch.out 2>&1 & "
        f"echo launched")
    if "launched" not in out:
        raise SystemExit(f"remote launch failed: {err.strip()[:400]}")
    print(f"TO-SSH: launched pid at {wd}/run.pid")


def _remote_done(client, spec) -> int | None:
    wd = spec["remote_workdir"]
    q = shlex.quote
    rc, out, _ = _exec(
        client, f"test -f {q(wd)}/exit_code && cat {q(wd)}/exit_code"
                f" || echo RUNNING", timeout=15)
    out = out.strip()
    if out == "RUNNING" or not out.lstrip("-").isdigit():
        return None
    return int(out)


def _kill_remote(client, spec) -> bool:
    """Kill the remote process GROUP (pgid == run.pid); confirm it is
    gone. Returns True only when termination is verified."""
    wd = spec["remote_workdir"]
    q = shlex.quote
    try:
        rc, pid, _ = _exec(client, f"cat {q(wd)}/run.pid 2>/dev/null")
        pid = pid.strip()
        if not pid.isdigit():
            return False
        _exec(client, f"kill -TERM -{pid} 2>/dev/null; sleep 1; "
                      f"kill -KILL -{pid} 2>/dev/null; true")
        rc, out, _ = _exec(client, f"kill -0 {pid} 2>/dev/null "
                                   f"&& echo alive || echo dead")
        return out.strip() == "dead"
    except Exception:
        return False


def _collect(client, spec, local_dir: Path) -> None:
    """SFTP-pull run.out + exit_code + the report tree."""
    wd = spec["remote_workdir"]
    local_dir.mkdir(parents=True, exist_ok=True)
    sftp = client.open_sftp()

    def _get(remote_rel: str, local_rel: str) -> bool:
        try:
            target = local_dir / local_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            sftp.get(f"{wd}/{remote_rel}", str(target))
            return True
        except OSError:
            return False

    def _walk(remote_dir: str, local_rel: str) -> None:
        try:
            for ent in sftp.listdir_attr(remote_dir):
                rpath = remote_dir + "/" + ent.filename
                lrel = local_rel + "/" + ent.filename
                if ent.st_mode & 0o40000:
                    _walk(rpath, lrel)
                else:
                    _get(rpath.replace(wd + "/", ""), lrel)
        except OSError:
            pass

    try:
        _get("run.out", "run.out")
        _get("exit_code", "exit_code")
        # The runner's -reportFolder is an ABSOLUTE path under the run
        # dir (dispatch passes <wd>/<report_rel>) — collect from there.
        report_rel = (spec.get("report_rel") or "Reports").strip("/")
        _walk(f"{wd}/{report_rel}", report_rel)
    finally:
        sftp.close()


def _tail_remote_log(client, spec, lines: int = 200) -> None:
    wd = spec["remote_workdir"]
    try:
        rc, out, _ = _exec(client, f"tail -n {lines} "
                                 f"{shlex.quote(wd)}/run.out 2>/dev/null")
        if out.strip():
            print("TO-SSH: ---- remote run.out ----")
            print(out[:8000])
    except Exception:
        pass


_CANCELLED = {"flag": False}


def main(argv=None) -> int:
    spec_file = (argv or sys.argv[1:])[0]
    spec = _load_spec(spec_file)
    local_artifacts = Path.cwd() / "remote-artifacts"

    def _on_term(signum, frame):
        _CANCELLED["flag"] = True

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    client = None
    try:
        client = _connect(spec)
        state = _remote_state(client, spec)
        if state == "resume":
            print("TO-SSH: reattaching to existing remote run — not "
                  "launching a duplicate")
        else:
            _prepare(client, spec)
            _launch(client, spec)
        deadline = time.monotonic() + int(spec.get("timeout_s") or 3600)
        exit_code = None
        while True:
            try:
                exit_code = _remote_done(client, spec)
            except Exception as exc:
                # Session loss: the REMOTE run continues detached —
                # reconnect and resume, never relaunch.
                print(f"TO-SSH: session lost ({exc}); reconnecting")
                try:
                    client = _connect(spec)
                except Exception as exc2:
                    print(f"TO-SSH: reconnect failed ({exc2}); "
                          "retrying")
                    time.sleep(POLL_S)
                    continue
                continue
            if exit_code is not None:
                break
            if _CANCELLED["flag"]:
                print("TO-SSH: cancel signal received — terminating "
                      "remote process group")
                try:
                    ok = _kill_remote(client, spec)
                except Exception:
                    ok = False
                print("TO-SSH: cancel-confirmed" if ok
                      else "TO-SSH: cancel-unconfirmed")
                return 0 if ok else 15
            if time.monotonic() > deadline:
                print("TO-SSH: timeout — remote run left for "
                      "reconciliation (not killed)")
                return 2
            time.sleep(POLL_S)
        _tail_remote_log(client, spec)
        _collect(client, spec, local_artifacts)
        print(f"TO-SSH: runner exit_code={exit_code}; artifacts "
              "collected")
        return 0 if exit_code == 0 else 3
    except SystemExit:
        raise
    except Exception as exc:
        print(f"TO-SSH: dispatcher error: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
