"""Robot Framework runner (2.6.0).

Same execution pipeline as the Node runners — per-target builders,
staged project root, ``Reports/`` collection, JUnit normalization.
What differs is only the rendered argv: ``python -m robot`` under the
*configured* target-side interpreter (runtime.executable_path), never
whatever python happens to be first on PATH.

Output layout inside the isolated per-run workspace:

    Reports/junit.xml           xunit for results.parse_junit
    Reports/output.xml          native Robot report (evidence)
    Reports/log.html            native log (evidence)
    Reports/report.html         native summary (evidence)

Robot exit codes are meaningful: 0 all tests passed, 1-249 the number
of failed tests, 250 help/usage, 251 data or source errors, 252
invalid command-line options or data, 253 interrupted
(SIGINT/SIGTERM/user stop), 255 unexpected internal error.
``classify_exit`` maps them so an invalid invocation or a missing
library is surfaced as an execution/configuration problem — never a
passing zero-test run.
"""

KIND = "robot"

_PROBE_MARKER = "TO-PROBE"


def build_args(suite_path: str = "", test: str = "",
               suite: str = "", include=(), exclude=(),
               pabot_processes: int = 0) -> list:
    """argv after the interpreter — every output under Reports/ so the
    shared collect path finds junit + native evidence untouched.

    ``--xunit`` resolves relative to ``--outputdir``, so
    ``--outputdir Reports --xunit junit.xml`` lands exactly at
    Reports/junit.xml next to the native output.xml/log.html/
    report.html. (RF7 removed ``--xunitskipnoncritical``; skipped
    non-critical tests map to xunit <skipped> by default.)
    """
    if pabot_processes > 0:
        # Pabot suite-level parallelism — optional dependency checked
        # by readiness (opt_pabot), never auto-installed. Pabot merges
        # per-suite output.xml into a single Reports/output.xml; the
        # merged --xunit keeps the shared JUnit collect path.
        args = ["-m", "pabot.pabot",
                "--processes", str(pabot_processes),
                "--outputdir", "Reports",
                "--xunit", "junit.xml"]
    else:
        args = ["-m", "robot",
                "--outputdir", "Reports",
                "--xunit", "junit.xml",
                "--log", "log.html",
                "--report", "report.html"]
    if suite:
        args += ["--suite", suite]
    if test:
        args += ["--test", test]
    for t in include:
        args += ["--include", t]
    for t in exclude:
        args += ["--exclude", t]
    # The positional source is the project-relative suite file or
    # directory; empty means "whole staged project".
    if suite_path:
        args.append(suite_path)
    return args


def classify_exit(rc: int) -> tuple:
    """(severity, label) for a Robot process exit status.

    severity is one of: "ok" (tests ran and passed), "tests" (exit code
    IS the failed-test count — junit carries the verdict),
    "config" (usage/data problems), "interrupted", "internal".
    """
    if rc == 0:
        return "ok", "all tests passed"
    if 1 <= rc <= 249:
        return "tests", f"{rc} failed test(s)"
    labels = {
        # RF semantics: 250 = 250+ failed tests (still a test verdict,
        # the xunit carries the detail); 251 = informational output
        # (--help/--version — no tests executed, NOT a pass); 252 =
        # invalid test data or command-line options; 253 = stopped by
        # the user; 255 = unexpected framework error.
        250: "250 or more failed tests",
        251: "informational output only — no tests executed",
        252: "invalid test data or command-line arguments",
        253: "execution stopped by the user",
        255: "unexpected framework error",
    }
    if rc in labels:
        return ("tests" if rc == 250
                else "interrupted" if rc == 253
                else "internal" if rc == 255
                else "config"), labels[rc]
    return "internal", f"unexpected exit status {rc}"


_LOG_MARKERS = [
    # (substring in Robot console output, label) — used when the run
    # produced no usable xunit, to distinguish invocation failures
    # from plain "no result data".
    ("[ ERROR ] Suite", "suite-level error"),
    ("Suite contains no tests", "suite contains no tests"),
    ("Data source does not exist", "data source does not exist"),
    ("Error in file", "invalid test data"),
    ("Importing library", "missing or broken library import"),
    ("No keyword with name", "missing keyword"),
    ("Execution interrupted", "execution interrupted"),
    ("Test execution stopped", "execution stopped"),
]


def classify_log(text: str) -> str:
    """Best-effort label for a Robot run log when no xunit exists."""
    low = (text or "")
    for needle, label in _LOG_MARKERS:
        if needle.lower() in low.lower():
            return label
    return ""


# ---------- readiness probe -------------------------------------------------
#
# Same contract as the node runners: TO-PROBE markers, runs as the
# staged source on the same builder/account, installs nothing. The
# interpreter under test is $PYBIN (the runtime's configured
# executable_path); REQUIRED_LIBS is a comma list of import names the
# administrator marked as required (each is probed with
# "$PYBIN" -c "import <lib>").

PROBE_SH = """#!/bin/sh
# TestOrchestrator Robot readiness probe (POSIX).
set +e
R=Reports
mkdir -p "$R"
L="$R/probe.txt"; : > "$L"
p() { echo "TO-PROBE $1"; echo "TO-PROBE $1" >> "$L"; }
p "connectivity=ok"
PY="${PYBIN:-python3}"
"$PY" --version >/dev/null 2>&1 \\
  && p "python=$("$PY" --version 2>&1)" \\
  || p "python=missing ($PY not runnable)"
v=$("$PY" -m robot --version 2>/dev/null)
[ -n "$v" ] && p "robot=$v" \\
  || p "robot=missing ($PY -m robot --version failed)"
IFS=','
for lib in ${REQUIRED_LIBS:-}; do
  lib=$(echo "$lib" | tr -d ' ')
  [ -z "$lib" ] && continue
  "$PY" -c "import $lib" >/dev/null 2>&1 \\
    && p "lib_$lib=ok" || p "lib_$lib=MISSING"
done
unset IFS
"$PY" -c "import pabot" >/dev/null 2>&1 \
  && p "opt_pabot=ok" || p "opt_pabot=absent"
mkdir -p Reports/.wtest && rmdir Reports/.wtest \\
  && p "workspace_write=ok" || p "workspace_write=denied"
if [ -n "%APP_URL%" ]; then
  curl -sf -m 10 -o /dev/null "%APP_URL%" \\
    && p "app_url=reachable" || p "app_url=UNREACHABLE from this target"
else
  p "app_url=not-configured (no application URL to check)"
fi
p "done=1"
"""


PROBE_CMD = r"""@echo off
rem TestOrchestrator Robot readiness probe (Windows cmd, argv-only).
setlocal enabledelayedexpansion
if not exist Reports mkdir Reports
set L=Reports\probe.txt
type nul > "%L%"
call :p connectivity=ok
if "%PYBIN%"=="" (set PY=python) else (set "PY=%PYBIN%")
"%PY%" --version >nul 2>&1 && (for /f "delims=" %%v in ('"%PY%" --version 2^>nul') do call :p python=%%v) || call :p python=missing (%PY% not runnable)
"%PY%" -m robot --version >nul 2>&1 && (for /f "delims=" %%v in ('"%PY%" -m robot --version 2^>nul') do call :p robot=%%v) || call :p robot=missing (%PY% -m robot --version failed)
if defined REQUIRED_LIBS (
  for %%l in (%REQUIRED_LIBS%) do (
    "%PY%" -c "import %%l" >nul 2>&1 && call :p lib_%%l=ok || call :p lib_%%l=MISSING
  )
)
"%PY%" -c "import pabot" >nul 2>&1 && call :p opt_pabot=ok || call :p opt_pabot=absent
mkdir Reports\.wtest 2>nul & rmdir Reports\.wtest 2>nul && call :p workspace_write=ok || call :p workspace_write=denied
if not "%APP_URL%"=="" (
  curl -sf -m 10 -o nul "%APP_URL%" >nul 2>&1 && call :p app_url=reachable || call :p app_url=UNREACHABLE from this target
) else (
  call :p app_url=not-configured
)
call :p done=1
exit /b 0
:p
echo TO-PROBE %*>>"%L%"
echo TO-PROBE %*
exit /b 0
"""


def probe_scripts(app_url: str = "") -> dict:
    return {
        "probe.sh": PROBE_SH.replace("%APP_URL%", app_url or ""),
        "probe.cmd": PROBE_CMD.replace("%APP_URL%", app_url or ""),
    }


def probe_argv(os_arch: str) -> list:
    """argv for the probe on the worker — batch file on Windows (no
    Bash dependency), POSIX sh elsewhere."""
    win = (os_arch or "").lower().startswith("win")
    return ["cmd", "/c", "probe.cmd"] if win else ["sh", "probe.sh"]
