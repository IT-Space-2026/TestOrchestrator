"""Bundled-Git launcher (§7 consolidated direction).

The application invokes the vendored Git (3rdparty/git/<target>/rootfs)
through a controlled per-process environment: the bundled binary plus
its own library path and exec path, scoped to each git subprocess. No
global LD_LIBRARY_PATH, nothing required of the user, and unrelated
processes keep their normal environment.

If the bundle for this platform is absent, the host's declared Git
prerequisite is used — the fallback is recorded, never silent.
"""

import os
import sys
from pathlib import Path

# Same frozen-layout fix as allure_runtime: installed payload puts
# 3rdparty at <payload>/3rdparty, outside the onedir.
if getattr(sys, "frozen", False):
    BUNDLE_ROOT = (Path(sys.executable).resolve().parent.parent
                   / "3rdparty" / "git")
else:
    BUNDLE_ROOT = (Path(__file__).resolve().parent.parent
                   / "3rdparty" / "git")


def _rootfs() -> Path | None:
    """Bundled runtime for this platform, if shipped. Bundle dirs are
    named ubuntu-<suite>-<deb-arch>; map the kernel machine name to the
    deb architecture (x86_64 -> amd64) — a silent miss must never fall
    back to host git unnoticed."""
    if not BUNDLE_ROOT.is_dir():
        return None
    arch = {"x86_64": "amd64", "aarch64": "arm64",
            "armv7l": "armhf"}.get(
        os.uname().machine if os.name == "posix" else "",
        os.uname().machine if os.name == "posix" else "")
    matches = [d for d in BUNDLE_ROOT.iterdir()
               if (d / "rootfs").is_dir() and d.name.endswith(arch)]
    if not matches:
        return None
    return sorted(matches)[-1] / "rootfs"


def bundled_available() -> bool:
    r = _rootfs()
    return bool(r and (r / "usr" / "bin" / "git").exists())


def executable() -> str:
    """The program the app invokes for EVERY git operation: a generated
    launcher that exports the bundled library/exec paths only inside the
    git child process, then execs the vendored binary. Falls back to the
    documented host-git prerequisite when no bundle is shipped."""
    r = _rootfs()
    if not (r and (r / "usr" / "bin" / "git").exists()):
        return "git"
    import tempfile
    libdirs = [str(p) for p in r.glob("usr/lib/*-linux-gnu")]
    libdirs += [str(r / "lib" / "x86_64-linux-gnu")]
    launcher = Path(tempfile.gettempdir()) / f"to-git-launch-{os.getuid()}.sh"
    content = (
        "#!/bin/sh\n"
        f'export LD_LIBRARY_PATH="{":".join(libdirs)}'
        f'${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}"\n'
        f'export GIT_EXEC_PATH="{r}/usr/lib/git-core"\n'
        f'exec "{r}/usr/bin/git" "$@"\n')
    if not launcher.exists() or launcher.read_text() != content:
        launcher.write_text(content)
        launcher.chmod(0o755)
    return str(launcher)


def configure() -> str:
    """Point GitPython at the controlled launcher (app-process scoped —
    no os.environ changes, no effect on other processes)."""
    import git as _gitpython
    _gitpython.Git.GIT_PYTHON_GIT_EXECUTABLE = executable()
    return _gitpython.Git.GIT_PYTHON_GIT_EXECUTABLE


def env(extra: dict = None) -> dict:
    """Per-invocation environment for git subprocesses — bundled libs
    and exec path scoped to the child only, merged over os.environ."""
    e = dict(os.environ)
    r = _rootfs()
    if r:
        libdirs = [str(p) for p in r.glob("usr/lib/*-linux-gnu")]
        libdirs += [str(r / "lib" / "x86_64-linux-gnu")]
        e["LD_LIBRARY_PATH"] = ":".join(libdirs)
        e["GIT_EXEC_PATH"] = str(r / "usr" / "lib" / "git-core")
        e["PATH"] = f"{r / 'usr' / 'bin'}:{e.get('PATH', '')}"
    e.update(extra or {})
    return e


def configure_gitpython() -> str:
    """Point GitPython at the selected executable (app-process scope only).
    Returns the executable in use — surfaced in diagnostics, not hidden."""
    import git
    exe = executable()
    git.Git.GIT_PYTHON_GIT_EXECUTABLE = exe
    return exe
