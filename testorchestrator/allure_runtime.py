"""Allure launcher — bundled allure-commandline + OPERATOR-SUPPLIED Java.

Java is a declared prerequisite for Allure reporting: it is detected or
operator-selected, then validated by an ACTUAL report-generation probe
with the pinned bundled generator — never by ``java -version`` alone.
The selected JAVA_HOME is persisted and exported only inside the Allure
child process: no global Java settings are changed, KRE's own runtime is
untouched, and unrelated processes keep their normal environment.

Selection precedence (first non-empty wins):
1. ``allure_java_home`` setting (Admin -> Reporting).
2. ``TO_ALLURE_JAVA_HOME`` environment (installer-persisted, scoped to
   the service units via EnvironmentFile).
3. Auto-detection: ``JAVA_HOME`` env, ``java`` on PATH, then
   platform-specific locations (Linux /usr/lib/jvm + /opt + /usr/java;
   Windows registry JavaSoft entries + Program Files vendor dirs).

Every subprocess runs as an argument list — never a shell string — so
paths containing spaces are safe on both platforms.

If no Java is found or validation fails, status reports not-ready
honestly — Allure is disabled with a clear warning, native KRE reports
and original artifacts remain available independently.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BUNDLE_ROOT = Path(__file__).resolve().parent.parent / "3rdparty"
ENV_JAVA_HOME = "TO_ALLURE_JAVA_HOME"
ENV_RECORD = "TO_ALLURE_JAVA_RECORD"   # installer-written probe record path
PROBE_TIMEOUT = 300


def _allure() -> Path | None:
    dirs = sorted((BUNDLE_ROOT / "allure").glob("allure-*/bin/allure"))
    return dirs[-1] if dirs else None


def version() -> str:
    """Pinned bundled generator version, or '' when not installed."""
    a = _allure()
    return a.parent.parent.name.split("-", 1)[1] if a else ""


def normalize_java_home(path: str | Path) -> Path | None:
    """Accept a JAVA_HOME directory or a path to the java binary itself;
    return the usable JAVA_HOME, or None when no bin/java exists."""
    if not path:
        return None
    p = Path(path).expanduser()
    if p.is_file() or p.name in ("java", "java.exe"):
        # Resolve through PATH shims/symlinks so the recorded JAVA_HOME
        # is the real JDK root, not a link dir like ~/.local/bin.
        p = Path(os.path.realpath(p)).parent.parent
    else:
        p = p.resolve()
    return p if (p / "bin" / JAVA_EXE).exists() else None


JAVA_EXE = "java.exe" if sys.platform == "win32" else "java"


def java_binary(home: Path) -> Path:
    return home / "bin" / JAVA_EXE


def _sanitized(text: str, limit: int = 500) -> str:
    """Error text for operators: control chars stripped, whitespace
    collapsed, truncated — never a raw stack dump."""
    clean = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text or "")
    clean = " ".join(clean.split())
    return clean[-limit:] if len(clean) > limit else clean


def java_version(home: Path, timeout: int = 15) -> tuple[str, str]:
    """(version string, error). A runtime that cannot even start is
    reported distinctly from one that merely reports oddly."""
    try:
        res = subprocess.run([str(java_binary(home)), "-version"],
                             capture_output=True, text=True,
                             timeout=timeout)
    except FileNotFoundError:
        return "", f"java binary not executable: {java_binary(home)}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", f"java -version failed to run: {_sanitized(str(exc), 200)}"
    text = res.stderr or res.stdout
    m = re.search(r'version "([^"]+)"', text)
    if res.returncode != 0:
        return "", (f"java -version exited {res.returncode}: "
                    f"{_sanitized(text, 200)}")
    return (m.group(1) if m else "unknown"), ""


def detect_java_homes() -> list[Path]:
    """Candidate JAVA_HOMEs in discovery order — de-duplicated.

    Order: JAVA_HOME env -> java on PATH -> platform-specific install
    locations (Windows: registry JavaSoft keys + Program Files vendor
    dirs; Linux/macOS: /usr/lib/jvm, /usr/java, /opt, JVM bundles)."""
    cands, seen = [], set()

    def add(p):
        home = normalize_java_home(p) if p else None
        if home and str(home) not in seen:
            seen.add(str(home))
            cands.append(home)

    add(os.environ.get("JAVA_HOME"))
    add(shutil.which(JAVA_EXE))
    for p in _platform_java_homes():
        add(p)
    return cands


def _platform_java_homes() -> list[str]:
    """OS-specific install locations."""
    if sys.platform == "win32":
        return _registry_java_homes() + _windows_dir_homes()
    homes = []
    for root in ("/usr/lib/jvm", "/usr/java", "/opt",
                 "/Library/Java/JavaVirtualMachines"):
        base = Path(root)
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if d.is_dir():
                homes.append(str(d))
                homes.append(str(d / "Contents" / "Home"))  # macOS bundle
    # /opt itself may hold jdk/bin directly or vendor dirs holding JDKs
    opt = Path("/opt")
    if opt.is_dir():
        for d in sorted(opt.iterdir()):
            if (d / "bin" / JAVA_EXE).exists():
                homes.append(str(d))
    return homes


def _windows_dir_homes() -> list[str]:
    """Common Windows vendor install roots under Program Files et al."""
    roots = []
    for var in ("ProgramFiles", "ProgramFiles(x86)",
                "ProgramW6432", "LOCALAPPDATA"):
        val = os.environ.get(var)
        if not val:
            continue
        base = Path(val)
        for vendor in ("Java", "Eclipse Adoptium", "Eclipse Foundation",
                       "Microsoft", "Amazon Corretto", "Zulu", "BellSoft",
                       "OpenJDK"):
            roots.append(base / vendor)
        if var == "LOCALAPPDATA":
            roots.append(base / "Programs")
    homes = []
    for root in roots:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if d.is_dir():
                homes.append(str(d))
    return homes


def _enum_registry_java_homes(winreg) -> list[str]:
    """Enumerate JavaSoft JavaHome values (HKLM + HKCU, 64-bit view
    first then default). ``winreg`` is injected so the logic is
    testable off-Windows."""
    homes = []
    roots = (r"SOFTWARE\JavaSoft\JDK", r"SOFTWARE\JavaSoft\JRE",
             r"SOFTWARE\JavaSoft\Java Development Kit",
             r"SOFTWARE\JavaSoft\Java Runtime Environment")
    views = [winreg.KEY_READ]
    wow = getattr(winreg, "KEY_WOW64_64KEY", 0)
    if wow:
        views.insert(0, winreg.KEY_READ | wow)
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for root in roots:
            for access in views:
                try:
                    base = winreg.OpenKey(hive, root, 0, access)
                except OSError:
                    continue
                i = 0
                while True:
                    try:
                        ver = winreg.EnumKey(base, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        sub = winreg.OpenKey(base, ver)
                        val, _typ = winreg.QueryValueEx(sub, "JavaHome")
                    except OSError:
                        continue
                    if val:
                        homes.append(val)
    return homes


def _registry_java_homes() -> list[str]:
    """Windows registry discovery — silently empty off-Windows or when
    no vendor registered a JavaHome."""
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []
    try:
        return _enum_registry_java_homes(winreg)
    except OSError:
        return []


def _setting(key, default=None):
    """Lazy settings read — None-safe outside an app/DB context."""
    try:
        from .settings import get
        return get(key, default)
    except Exception:
        return default


def _set_setting(key, value) -> None:
    from .extensions import db
    from .settings import set as _s
    _s(key, value)
    db.session.commit()


def selected_java() -> tuple[Path | None, str]:
    """The Java the app will use for Allure, plus its selection source."""
    home = normalize_java_home(_setting("allure_java_home", ""))
    if home:
        return home, "admin"
    home = normalize_java_home(os.environ.get(ENV_JAVA_HOME, ""))
    if home:
        return home, "installer"
    homes = detect_java_homes()
    return (homes[0], "detected") if homes else (None, "none")


def _child_env(home: Path) -> dict:
    """Per-invocation environment — JAVA_HOME/PATH scoped to the Allure
    child process only; os.environ and every other runtime untouched."""
    env = dict(os.environ)
    env["JAVA_HOME"] = str(home)
    env["PATH"] = f"{home / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    return env


def _fixture_results(dest: Path) -> None:
    """Minimal valid allure-results input for the generation probe."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "probe-result.json").write_text(json.dumps({
        "uuid": "probe-1", "historyId": "probe.java",
        "name": "java-runtime-probe", "fullName": "probe.java",
        "status": "passed", "stage": "finished", "labels": []}))


def probe(java_home=None, timeout: int = PROBE_TIMEOUT) -> dict:
    """Validate a Java runtime by ACTUALLY generating a report with the
    pinned bundled Allure. Returns a persistable record — truthful about
    failures (missing binary, non-zero exit, no report produced)."""
    if java_home:
        home = normalize_java_home(java_home)
        if home is None:
            # An explicit selection that is not a usable JAVA_HOME fails
            # on its own — never silently probe a different runtime.
            return {"ok": False, "java_home": str(java_home),
                    "java_version": "",
                    "error": "not a usable Java home — no bin/java found",
                    "checked_at": _now()}
    else:
        home, _src = selected_java()
        if home is None:
            return {"ok": False, "java_home": "", "java_version": "",
                    "error": "no Java runtime found — install a JRE/JDK "
                             "or select its location",
                    "checked_at": _now()}
    version_str, verr = java_version(home)
    record = {"java_home": str(home), "java_version": version_str,
              "checked_at": _now()}
    if verr:
        return {**record, "ok": False,
                "error": f"version check failed — {verr}; select a "
                         "working JRE/JDK and recheck"}
    generator = _allure()
    if generator is None:
        return {**record, "ok": False,
                "error": "bundled allure-commandline not installed"}
    try:
        with tempfile.TemporaryDirectory(prefix="allure-probe-") as td:
            results_dir, out_dir = Path(td) / "results", Path(td) / "out"
            _fixture_results(results_dir)
            res = subprocess.run(
                [str(generator), "generate", str(results_dir),
                 "-o", str(out_dir), "--single-file"],
                env=_child_env(home), capture_output=True, text=True,
                timeout=timeout)
            entry = out_dir / "index.html"
            ok = res.returncode == 0 and entry.is_file() \
                and entry.stat().st_size > 0
            record["ok"] = ok
            record["error"] = "" if ok else _sanitized(
                res.stderr or res.stdout or
                f"generator exited {res.returncode} without producing "
                "a report")
            if not ok and not record["error"]:
                record["error"] = "report generation produced no output"
    except (OSError, subprocess.TimeoutExpired) as exc:
        record["ok"] = False
        record["error"] = f"probe timed out or failed to start: " \
                          f"{_sanitized(str(exc), 300)}"
    return record


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _installer_record() -> dict | None:
    """Probe record written by the installer (path via TO_* env) — used
    only until the app persists its own probe in settings."""
    path = os.environ.get(ENV_RECORD, "")
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def select_and_validate(java_home: str, timeout: int = PROBE_TIMEOUT) -> dict:
    """Admin action: probe the given path and persist the result. On
    success the selected home is stored; on failure the previous
    selection is kept and the failed probe is still recorded honestly."""
    record = probe(java_home, timeout=timeout)
    if record["ok"]:
        _set_setting("allure_java_home", record["java_home"])
    _set_setting("allure_java_probe", record)
    return record


def available() -> bool:
    """A generation attempt is possible: generator bundled AND some
    Java runtime selectable. 'available' is not 'validated' — see
    status()/probe() for the validated-readiness signal."""
    home, _src = selected_java()
    return _allure() is not None and home is not None


def status() -> dict:
    """Readiness detail for Admin -> Reporting — truthful and checkable.
    'ready' requires a successful generation probe against the CURRENTLY
    selected Java (persisted by the UI check or written by installer)."""
    generator = _allure()
    home, source = selected_java()
    record = _setting("allure_java_probe") or _installer_record() or {}
    probed_home = record.get("java_home", "")
    probe_ok = bool(record.get("ok")) and probed_home == (str(home) if home
                                                        else "")
    st = {
        "generator_present": generator is not None,
        "version": version(),
        "java_home": str(home) if home else "",
        "java_source": source,
        "java_version": java_version(home)[0] if home else "",
        "probe": record,
        "probe_current": probe_ok,
        "detected": [str(h) for h in detect_java_homes()],
        "enabled": bool(_setting("allure_enabled", False)),
    }
    if generator is None:
        st.update(ready=False,
                  reason="bundled allure-commandline not installed")
    elif home is None:
        st.update(ready=False,
                  reason="no Java runtime found — install a JRE/JDK or "
                         "enter its location, then run Check")
    elif not record:
        st.update(ready=False,
                  reason="Java not yet validated — run Check to verify "
                         "report generation works")
    elif not probe_ok:
        st.update(ready=False,
                  reason=record.get("error") or
                         "Java changed since last validation — recheck")
    else:
        st.update(ready=True, reason="")
    return st


def generate(results_dir: Path, out_dir: Path,
             single_file: bool = True, timeout: int = 300) -> dict:
    """Run the bundled generator on an allure-results dir using the
    selected Java. Offline: no network — CLI + plugins are vendored."""
    home, _src = selected_java()
    if _allure() is None:
        return {"ok": False,
                "error": "bundled allure-commandline not installed"}
    if home is None:
        return {"ok": False,
                "error": "no Java runtime configured or detected — "
                         "Allure generation unavailable"}
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(_allure()), "generate", str(results_dir),
           "-o", str(out_dir)]
    if single_file:
        cmd.append("--single-file")
    res = subprocess.run(cmd, env=_child_env(home), capture_output=True,
                         text=True, timeout=timeout)
    entry = out_dir / "index.html"
    return {"ok": res.returncode == 0 and entry.exists(),
            "error": _sanitized(res.stderr or res.stdout)
                     if res.returncode else "",
            "entry": str(entry) if entry.exists() else ""}


def main(argv=None) -> int:
    """Probe CLI — used by the installer, possibly via runuser as the
    service account. Pure stdlib: importable standalone (see
    scripts/java_probe.py). Prints a JSON record."""
    import argparse
    ap = argparse.ArgumentParser(prog="allure_runtime")
    ap.add_argument("--java-home", default="")
    ap.add_argument("--detect", action="store_true")
    args = ap.parse_args(argv)
    if args.detect:
        print(json.dumps([str(h) for h in detect_java_homes()]))
        return 0
    record = probe(args.java_home or None)
    print(json.dumps(record))
    return 0 if record.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
