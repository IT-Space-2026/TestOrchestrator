"""Bundled-Allure launcher — same controlled-process pattern as
git_runtime: vendored JRE + vendored allure-commandline, env scoped to
the generator child process only. If the bundle is absent the adapter
reports not-ready honestly — no silent fallback to a system Java.
"""

import json
import os
import subprocess
from pathlib import Path

BUNDLE_ROOT = Path(__file__).resolve().parent.parent / "3rdparty"


def _jre() -> Path | None:
    for d in (BUNDLE_ROOT / "java-runtime").glob("*/rootfs"):
        hits = list(d.glob("usr/lib/jvm/*/bin/java"))
        if hits:
            return hits[0].parent.parent  # .../java-XX-openjdk-amd64
    return None


def _allure() -> Path | None:
    dirs = sorted((BUNDLE_ROOT / "allure").glob("allure-*/bin/allure"))
    return dirs[-1] if dirs else None


def version() -> str:
    """Pinned bundled version, or '' when not installed."""
    a = _allure()
    return a.parent.parent.name.split("-", 1)[1] if a else ""


def available() -> bool:
    return _jre() is not None and _allure() is not None


def status() -> dict:
    """Readiness detail for the settings page — truthful, checkable."""
    if not available():
        return {"ready": False,
                "reason": "bundled JRE or allure-commandline not installed"}
    return {"ready": True, "version": version(),
            "jre": str(_jre()), "generator": str(_allure())}


def generate(results_dir: Path, out_dir: Path,
             single_file: bool = True, timeout: int = 300) -> dict:
    """Run the bundled generator on an allure-results dir.
    Offline: no network — JRE + CLI + plugins all vendored."""
    if not available():
        return {"ok": False, "error": "allure bundle not installed"}
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(_allure()), "generate", str(results_dir),
           "-o", str(out_dir)]
    if single_file:
        cmd.append("--single-file")
    env = dict(os.environ)
    env["JAVA_HOME"] = str(_jre())
    res = subprocess.run(cmd, env=env, capture_output=True, text=True,
                         timeout=timeout)
    entry = out_dir / "index.html" if single_file \
        else out_dir / "index.html"
    return {"ok": res.returncode == 0 and entry.exists(),
            "error": (res.stderr or res.stdout)[-500:] if res.returncode
                     else "",
            "entry": str(entry) if entry.exists() else ""}
