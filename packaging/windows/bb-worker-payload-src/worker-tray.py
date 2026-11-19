#!/usr/bin/env pythonw
"""TestOrchestrator Buildbot worker — system-tray controller.

Launched at user logon via a Startup-folder shortcut (the worker task
itself runs as SYSTEM at boot — a tray icon can only live in an
interactive session). Right-click menu: Start / Stop / Quit, plus a
live status line. Control goes through schtasks — the same interface
the installer uses, no extra service or listener.
"""
from pathlib import Path
import subprocess

import pystray
from PIL import Image

HERE = Path(__file__).resolve().parent
TASK = "TestOrchestratorBuildbotWorker"
TITLE = "TestOrchestrator Buildbot Worker"


def _schtasks(*args):
    return subprocess.run(
        ["schtasks", *args], capture_output=True, text=True,
        timeout=30, creationflags=getattr(
            subprocess, "CREATE_NO_WINDOW", 0))


def is_running() -> bool:
    try:
        r = _schtasks("/Query", "/TN", TASK, "/FO", "LIST")
        return " Running" in r.stdout
    except Exception:
        return False


def _start(icon=None, item=None):
    _schtasks("/Run", "/TN", TASK)
    icon.update_menu() if icon else None


def _stop(icon=None, item=None):
    _schtasks("/End", "/TN", TASK)
    icon.update_menu() if icon else None


def _status_text(icon=None, item=None):
    return f"Worker: {'running' if is_running() else 'stopped'}"


def _configure(icon=None, item=None):
    # console window running the interactive reconfigure prompts
    subprocess.Popen(
        ["cmd", "/c", "start", "",
         str(HERE / "reconfigure-worker.cmd")],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _noop(*_):
    pass


def _quit(icon, item=None):
    icon.stop()


def main() -> None:
    try:
        image = Image.open(HERE / "it.ico")
    except Exception:
        image = Image.new("RGB", (64, 64), (30, 90, 160))
    menu = pystray.Menu(
        pystray.MenuItem(_status_text, _noop),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start", _start),
        pystray.MenuItem("Stop", _stop),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Configure…", _configure),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _quit),
    )
    icon = pystray.Icon("to-bb-worker", image, TITLE, menu)
    icon.title = TITLE
    icon.run()


if __name__ == "__main__":
    main()
