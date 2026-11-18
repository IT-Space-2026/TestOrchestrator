"""In-process SSH test server (paramiko) — exercises the real protocol
path for the agentless-SSH target: host-key verification, pubkey
authentication, exec_command, and SFTP rooted at a temp dir.

Not a mock of paramiko's client API — a live Transport on a loopback
socket, so fingerprints, auth failures, and changed-host-key behavior
are all real.
"""
from __future__ import annotations

import os
import queue
import socket
import stat
import subprocess
import threading
from pathlib import Path

import paramiko

ROOT_MARKER = "to-ssh-test"


class _SFTPHandle(paramiko.SFTPHandle):
    pass


class _SFTPIface(paramiko.SFTPServerInterface):
    """SFTP rooted at a fixed directory — the remote workdir."""

    ROOT = Path("/")

    def __init__(self, server, *largs, **kwargs):
        super().__init__(server, *largs, **kwargs)

    def _real(self, path: str) -> str:
        rel = path.lstrip("/")
        return str(self.ROOT / rel)

    def list_folder(self, path):
        real = self._real(path)
        out = []
        for name in os.listdir(real):
            st = os.lstat(os.path.join(real, name))
            attr = paramiko.SFTPAttributes.from_stat(st, name)
            out.append(attr)
        return out

    def stat(self, path):
        return paramiko.SFTPAttributes.from_stat(
            os.stat(self._real(path)))

    def lstat(self, path):
        return paramiko.SFTPAttributes.from_stat(
            os.lstat(self._real(path)))

    def open(self, path, flags, attr):
        real = self._real(path)
        os.makedirs(os.path.dirname(real) or "/", exist_ok=True)
        mode = {
            os.O_RDONLY: "rb",
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC: "wb",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND: "ab",
            os.O_RDWR | os.O_CREAT: "r+b",
        }.get(flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR |
                       os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if mode is None:
            mode = "rb" if flags & os.O_WRONLY == 0 else "wb"
            if flags & os.O_CREAT and "b" in mode:
                mode = mode.replace("r", "w") if "w" in mode \
                    else "a+b"
        f = open(real, mode)
        handle = _SFTPHandle(flags)
        handle.filename = real
        # SFTPHandle.read/write call .read()/.write() on these —
        # they must be the file objects, not flags.
        handle.readfile = f if ("r" in mode or "+" in mode) else None
        handle.writefile = f if ("w" in mode or "a" in mode
                                 or "+" in mode) else None
        handle._file = f
        return handle

    def remove(self, path):
        os.remove(self._real(path))
        return paramiko.SFTP_OK

    def rename(self, oldpath, newpath):
        os.rename(self._real(oldpath), self._real(newpath))
        return paramiko.SFTP_OK

    def mkdir(self, path, attr):
        os.makedirs(self._real(path), exist_ok=True)
        return paramiko.SFTP_OK

    def rmdir(self, path):
        os.rmdir(self._real(path))
        return paramiko.SFTP_OK

    def canonicalize(self, path):
        if not path.startswith("/"):
            return "/" + path
        return path

    def chattr(self, path, attr):
        real = self._real(path)
        if attr._flags & attr.FLAG_PERMISSIONS:
            os.chmod(real, attr.st_mode)
        return paramiko.SFTP_OK


class _Server(paramiko.ServerInterface):
    def __init__(self, pubkey_blob):
        self._blob = pubkey_blob
        # Exec commands are queued and started only AFTER this handler
        # returns True — running them inline races the exec-success
        # reply (a fast command would close the channel first).
        self.exec_queue = queue.Queue()

    def check_channel_request(self, kind, chanid):
        return (paramiko.OPEN_SUCCEEDED if kind == "session"
                else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED)

    def check_auth_publickey(self, username, key):
        return (paramiko.AUTH_SUCCESSFUL
                if key.asbytes() == self._blob
                else paramiko.AUTH_FAILED)

    def get_allowed_auths(self, username):
        return "publickey"

    def check_channel_exec_request(self, channel, command):
        self.exec_queue.put((channel, command))
        return True


def _exec_run(channel: paramiko.Channel, command: bytes):
    """Run the command as a local subprocess — stdin/stdout/stderr and
    exit status flow over the channel like a real exec."""
    try:
        proc = subprocess.Popen(command.decode("utf-8", "replace"),
                                shell=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        out, err = proc.communicate()
        channel.sendall(out)
        channel.sendall_stderr(err)
        channel.send_exit_status(proc.returncode)
    except Exception:
        try:
            channel.send_exit_status(127)
        except Exception:
            pass
    finally:
        channel.close()


class TestSSHServer:
    """Context manager: yields (host, port, host_fingerprint)."""

    def __init__(self, sftp_root: Path):
        self.sftp_root = Path(sftp_root)
        self.host_key = paramiko.RSAKey.generate(2048)
        self._threads = []
        self._sock = None

    @property
    def fingerprint(self) -> str:
        import base64
        import hashlib
        digest = hashlib.sha256(self.host_key.asbytes()).digest()
        return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")

    @property
    def key_alg(self) -> str:
        return self.host_key.get_name()

    def start(self, accepted_pubkey: paramiko.PKey):
        _SFTPIface.ROOT = self.sftp_root
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._blob = accepted_pubkey.asbytes()
        threading.Thread(target=self._accept_loop, daemon=True).start()
        return self

    def _accept_loop(self):
        while True:
            try:
                client, _ = self._sock.accept()
            except OSError:
                return
            t = threading.Thread(target=self._serve, args=(client,),
                                 daemon=True)
            t.start()
            self._threads.append(t)

    def _serve(self, client):
        try:
            transport = paramiko.Transport(client)
            transport.add_server_key(self.host_key)
            transport.set_subsystem_handler(
                "sftp", paramiko.SFTPServer, _SFTPIface)
            server = _Server(self._blob)
            transport.start_server(server=server)
            # Keep every accepted channel referenced — a GC'd Channel
            # sends close and the client sees "Channel closed".
            channels = []
            while transport.is_active():
                chan = transport.accept(0.5)
                if chan is not None:
                    channels.append(chan)
                try:
                    channel, command = server.exec_queue.get_nowait()
                except queue.Empty:
                    continue
                threading.Thread(target=_exec_run,
                                 args=(channel, command),
                                 daemon=True).start()
        except Exception:
            pass

    def stop(self):
        try:
            self._sock.close()
        except Exception:
            pass


def generate_client_key(path: Path) -> paramiko.PKey:
    """Write a PEM private key + return it — what an admin would paste
    into the SSH credential field."""
    path = Path(path)
    key = paramiko.RSAKey.generate(2048)
    path.parent.mkdir(parents=True, exist_ok=True)
    key.write_private_key_file(str(path))
    return key
