"""pytest runner (2.6.0).

Same execution pipeline as the Robot runner — per-target builders,
staged project root, ``Reports/`` collection, JUnit normalization.
What differs is only the rendered argv: ``python -m pytest`` under the
*configured* target-side interpreter (runtime.executable_path), never
whatever python happens to be first on PATH.

Output layout inside the isolated per-run workspace:

    Reports/junit.xml   xunit for results.parse_junit
    Reports/run.out     console transcript (evidence)

pytest exit codes: 0 all passed, 1 tests failed, 2 interrupted,
3 internal error, 4 usage error, 5 no tests collected.
``classify_exit`` maps them so a usage error, an interrupted run, or
"no tests collected" is surfaced as an execution/configuration
problem — never a passing zero-test run.
"""

KIND = "pytest"

_PROBE_MARKER = "TO-PROBE"


def build_args(suite_path: str = "", keyword: str = "",
               markers: str = "", node_ids=(),
               extra=()) -> list:
    """argv after the interpreter — junit under Reports/ so the shared
    collect path finds it untouched. ``suite_path`` is the
    project-relative file/directory; ``node_ids`` may carry
    ``path::test`` selectors; ``keyword``/``markers`` map to
    ``-k``/``-m``.
    """
    args = ["-m", "pytest", "--junitxml", "Reports/junit.xml", "-q"]
    if keyword:
        args += ["-k", keyword]
    if markers:
        args += ["-m", markers]
    for nid in node_ids:
        args.append(nid)
    for a in extra:
        args.append(a)
    if suite_path and not node_ids:
        args.append(suite_path)
    return args


def classify_exit(rc: int) -> tuple:
    """(severity, label) for a pytest process exit status.

    severity: "ok" (tests ran and passed), "tests" (tests ran, some
    failed — junit carries the verdict), "config" (usage error / no
    tests collected), "interrupted", "internal".
    """
    labels = {
        0: ("ok", "all tests passed"),
        1: ("tests", "tests failed"),
        2: ("interrupted", "execution interrupted"),
        3: ("internal", "internal pytest error"),
        4: ("config", "command-line usage error"),
        5: ("config", "no tests collected"),
    }
    if rc in labels:
        return labels[rc]
    return "internal", f"unexpected exit status {rc}"


_LOG_MARKERS = [
    ("ERROR: file or directory not found", "path not found"),
    ("ERROR: not found:", "node id not found"),
    ("collected 0 items", "no tests collected"),
    ("ModuleNotFoundError", "missing python module"),
    ("ImportError while importing", "missing or broken import"),
    ("usage:", "usage error"),
    ("!!! KeyboardInterrupt !!!", "interrupted"),
]


def classify_log(text: str) -> str:
    """Best-effort label for a pytest run log when no junit exists."""
    low = (text or "")
    for needle, label in _LOG_MARKERS:
        if needle.lower() in low.lower():
            return label
    return ""


# ---------- readiness probe -------------------------------------------------
#
# Same contract as the other runners: TO-PROBE markers, runs as the
# staged source on the same builder/account, installs nothing. The
# interpreter under test is $PYBIN (the runtime's configured
# executable_path); REQUIRED_LIBS is a comma list of import names the
# administrator marked as required.

PROBE_SH = """#!/bin/sh
# TestOrchestrator pytest readiness probe (POSIX).
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
v=$("$PY" -m pytest --version 2>/dev/null | head -1)
[ -n "$v" ] && p "pytest=$v" \\
  || p "pytest=missing ($PY -m pytest --version failed)"
IFS=','
for lib in ${REQUIRED_LIBS:-}; do
  lib=$(echo "$lib" | tr -d ' ')
  [ -z "$lib" ] && continue
  "$PY" -c "import $lib" >/dev/null 2>&1 \\
    && p "lib_$lib=ok" || p "lib_$lib=MISSING"
done
unset IFS
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
rem TestOrchestrator pytest readiness probe (Windows cmd, argv-only).
setlocal enabledelayedexpansion
if not exist Reports mkdir Reports
set L=Reports\probe.txt
type nul > "%L%"
call :p connectivity=ok
if "%PYBIN%"=="" (set PY=python) else (set "PY=%PYBIN%")
"%PY%" --version >nul 2>&1 && (for /f "delims=" %%v in ('"%PY%" --version 2^>nul') do call :p python=%%v) || call :p python=missing (%PY% not runnable)
"%PY%" -m pytest --version >nul 2>&1 && call :p pytest=installed || call :p pytest=missing (%PY% -m pytest --version failed)
if defined REQUIRED_LIBS (
  for %%l in (%REQUIRED_LIBS%) do (
    "%PY%" -c "import %%l" >nul 2>&1 && call :p lib_%%l=ok || call :p lib_%%l=MISSING
  )
)
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
