"""Node.js browser-test runners: Playwright Test and Cypress (§2.6.0).

These are *runtimes*, not transports — dispatch still flows through
the existing Buildbot worker / agentless-SSH paths. What changes vs
KRE is only the rendered argv/env and where reports land:

  - everything runs inside the staged project root via ``npx`` —
    project-pinned dependencies, never bundled with the package;
  - all output is steered under ``Reports/`` so the existing
    DirectoryUpload / SFTP collect path picks it up unchanged;
  - JUnit XML is produced by the frameworks' own reporters and parsed
    by the shared results.parse_junit normalizer.

Prerequisites live on the TARGET host (Node.js + project deps +
browsers) — documented in the Admin manual; the readiness probe in
this module is what verifies them on the selected target under the
same account the run will use.
"""

KINDS = ("playwright", "cypress")

# Browser names each framework's CLI understands. Anything else is
# rejected before dispatch — never silently substitute (spec §1).
_BROWSERS = {
    "playwright": ("chromium", "firefox", "webkit", "msedge"),
    "cypress": ("chrome", "chromium", "edge", "electron", "firefox"),
}


def validate_browser(kind: str, browser: str) -> tuple:
    """(ok, value): empty means 'framework default' — permitted.
    An unknown name is a config error, not a silent substitution."""
    b = (browser or "").strip().lower()
    if not b:
        return True, ""
    if b in _BROWSERS.get(kind, ()):
        return True, b
    return False, (
        f"browser {browser!r} is not supported by {kind} "
        f"(choose from {', '.join(_BROWSERS[kind])} or leave the "
        "framework default)")


def build_args(kind: str, browser: str = "", spec: str = "",
               headed: bool = False) -> list:
    """argv after the npx executable — every output path inside
    Reports/ so collection finds junit + evidence untouched.

    Playwright writes JUnit only to the path named by
    PLAYWRIGHT_JUNIT_OUTPUT_FILE (env carried separately in the
    spec); traces/screenshots land under --output.
    Cypress' junit reporter emits one file per spec —
    mochaFile=junit-[hash].xml keeps later specs from overwriting
    earlier reports (spec §4).
    """
    # --no-install: a project without the framework installed fails
    # visibly — npx must never silently fetch a CLI (spec §3).
    args = ["--no-install"]
    if kind == "playwright":
        args += ["playwright", "test",
                 "--reporter=junit",
                 "--output=Reports/test-results"]
        if browser:
            # playwright selects browsers via project names
            args += ["--project", browser]
        if spec:
            args += [spec]
    elif kind == "cypress":
        args += ["cypress", "run",
                 "--reporter", "junit",
                 "--reporter-options",
                 "mochaFile=Reports/junit/junit-[hash].xml,"
                 "toConsole=false",
                 "--config",
                 "screenshotsFolder=Reports/screenshots,"
                 "videosFolder=Reports/videos"]
        if not headed:
            args += ["--headless"]
        if browser:
            args += ["--browser", browser]
        if spec:
            args += ["--spec", spec]
    else:
        raise ValueError(f"unknown node runner kind {kind!r}")
    return args


def junit_env(kind: str) -> dict:
    """Extra environment the exec step needs — playwright's junit
    reporter writes nowhere without this."""
    env = {}
    if kind == "playwright":
        env["PLAYWRIGHT_JUNIT_OUTPUT_FILE"] = \
            "Reports/junit/results.xml"
    # A leaked ELECTRON_RUN_AS_NODE in the worker/SSH environment runs
    # Cypress' Electron binary as plain Node and fails with cryptic
    # cached-data errors — empty string is falsy, which disables it.
    env["ELECTRON_RUN_AS_NODE"] = ""
    return env


# ---------- readiness probe -------------------------------------------------
#
# The probe is a tiny script staged AS the run source — same builder,
# same account, same workspace mechanics as a real run. It never
# installs anything: `npx --no-install` fails visibly when the
# framework is absent. Markers TO-PROBE <key>=<value> are collected
# from the step log; Reports/probe.txt carries the same lines as an
# artifact.

PROBE_MARKER = "TO-PROBE"

PROBE_SH = """#!/bin/sh
# TestOrchestrator runner-readiness probe (POSIX).
# Prints TO-PROBE markers; exits 0 always — the markers carry the verdict.
set +e
R=Reports
mkdir -p "$R"
L="$R/probe.txt"; : > "$L"
p() { echo "TO-PROBE $1"; echo "TO-PROBE $1" >> "$L"; }
p "connectivity=ok"
node --version >/dev/null 2>&1 \\
  && p "node=$(node --version)" \\
  || p "node=missing"
npx --version >/dev/null 2>&1 \\
  && p "npx=$(npx --version)" \\
  || p "npx=missing"
npm --version >/dev/null 2>&1 \\
  && p "npm=$(npm --version)" \\
  || p "npm=missing (needed for pinned project deps)"
case "%KIND%" in
  playwright)
    v=$(npx --no-install playwright --version 2>/dev/null)
    [ -n "$v" ] && p "playwright=$v" \\
      || p "playwright=missing (npx --no-install playwright --version failed)"
    ;;
  cypress)
    v=$(npx --no-install cypress version 2>/dev/null | head -1)
    [ -n "$v" ] && p "cypress=$v" \\
      || p "cypress=missing (npx --no-install cypress version failed)"
    npx --no-install cypress verify >/dev/null 2>&1 \\
      && p "cypress_binary=ok" \\
      || p "cypress_binary=unavailable to this account"
    ;;
esac
for b in google-chrome chromium chromium-browser microsoft-edge firefox; do
  command -v $b >/dev/null 2>&1 && p "browser_$b=present"
done
[ -d "$HOME/.cache/ms-playwright" ] \\
  && p "playwright_browsers=present" \\
  || p "playwright_browsers=absent (~/.cache/ms-playwright)"
mkdir -p Reports/.wtest && rmdir Reports/.wtest \\
  && p "workspace_write=ok" || p "workspace_write=denied"
if [ -n "%APP_URL%" ]; then
  curl -sf -m 10 -o /dev/null "%APP_URL%" \\
    && p "app_url=reachable" || p "app_url=UNREACHABLE from this target"
else
  p "app_url=not-configured (no application URL to check)"
fi
command -v sftp-server >/dev/null 2>&1 || command -v /usr/lib/sftp-server >/dev/null 2>&1
[ $? -eq 0 ] && p "sftp=ok" || p "sftp=not verified"
p "done=1"
"""

PROBE_CMD = r"""@echo off
rem TestOrchestrator runner-readiness probe (Windows cmd).
setlocal enabledelayedexpansion
if not exist Reports mkdir Reports
set L=Reports\probe.txt
type nul > "%L%"
call :p connectivity=ok
where node >nul 2>&1 && (for /f "delims=" %%v in ('node --version 2^>nul') do call :p node=%%v) || call :p node=missing
where npx >nul 2>&1 && (for /f "delims=" %%v in ('npx --version 2^>nul') do call :p npx=%%v) || call :p npx=missing
where npm >nul 2>&1 && (for /f "delims=" %%v in ('npm --version 2^>nul') do call :p npm=%%v) || call :p npm=missing (needed for pinned project deps)
if "%KIND%"=="playwright" (
  npx --no-install playwright --version >nul 2>&1 && (for /f "delims=" %%v in ('npx --no-install playwright --version 2^>nul') do call :p playwright=%%v) || call :p playwright=missing
)
if "%KIND%"=="cypress" (
  npx --no-install cypress version >nul 2>&1 && call :p cypress=installed || call :p cypress=missing
  npx --no-install cypress verify >nul 2>&1 && call :p cypress_binary=ok || call :p cypress_binary=unavailable to this account
)
for %%b in (chrome.exe msedge.exe firefox.exe) do (
  where %%b >nul 2>&1 && call :p browser_%%b=present
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


def probe_scripts(kind: str, app_url: str = "") -> dict:
    """Filename -> content for the staged readiness source."""
    return {
        "probe.sh": PROBE_SH.replace("%KIND%", kind)
                            .replace("%APP_URL%", app_url or ""),
        "probe.cmd": PROBE_CMD.replace("%KIND%", kind)
                              .replace("%APP_URL%", app_url or ""),
    }


def probe_argv(os_arch: str) -> list:
    """Entry argv for the probe on the worker — batch on Windows,
    POSIX sh everywhere else."""
    win = (os_arch or "").lower().startswith("win")
    return ["cmd", "/c", "probe.cmd"] if win else ["sh", "probe.sh"]


def parse_probe(text: str) -> dict:
    """TO-PROBE marker lines -> {key: value} for the readiness UI."""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith(PROBE_MARKER + " "):
            continue
        kv = line[len(PROBE_MARKER) + 1:]
        if "=" in kv:
            k, _, v = kv.partition("=")
            out[k.strip()] = v.strip()
    return out
