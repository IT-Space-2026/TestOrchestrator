"""Contract tests for the Node.js browser runners (§2.6.0):
argv rendering, browser validation, probe scripts, spec shape."""
from testorchestrator.adapters import nodefw
from testorchestrator import buildbot_cfg


def test_playwright_args_junit_and_project_browser():
    args = nodefw.build_args("playwright", "chromium")
    assert args[:3] == ["playwright", "test", "--reporter=junit"]
    assert "--output=Reports/test-results" in args
    assert "--project" in args
    assert "chromium" in args


def test_cypress_args_unique_junit_per_spec():
    args = nodefw.build_args("cypress", "chrome")
    assert "cypress" in args and "run" in args
    ro = args[args.index("--reporter-options") + 1]
    assert "junit-[hash].xml" in ro  # per-spec names — no overwrite
    cfg = args[args.index("--config") + 1]
    assert "Reports/screenshots" in cfg and "Reports/videos" in cfg
    assert "--browser" in args


def test_browser_validation_no_silent_substitution():
    ok, _ = nodefw.validate_browser("playwright", "webkit")
    assert ok
    ok, _ = nodefw.validate_browser("playwright", "")
    assert ok  # framework default permitted
    ok, msg = nodefw.validate_browser("cypress", "netscape")
    assert not ok and "not supported" in msg
    ok2, _ = nodefw.validate_browser("cypress", "chrome")
    assert ok2


def test_junit_env_only_playwright():
    assert "PLAYWRIGHT_JUNIT_OUTPUT_FILE" in nodefw.junit_env(
        "playwright")
    assert nodefw.junit_env("cypress") == {}


def test_node_exec_spec_shape():
    spec = buildbot_cfg.node_exec_spec(
        None, "npx", ["playwright", "test"],
        env={"PLAYWRIGHT_JUNIT_OUTPUT_FILE": "Reports/junit/results.xml"})
    assert spec["executable"] == "npx"
    assert spec["exec_workdir"] == "build/src"
    assert spec["report_src"] == "src/Reports"
    assert spec["env"]["PLAYWRIGHT_JUNIT_OUTPUT_FILE"].endswith(
        "results.xml")


def test_probe_scripts_markers_no_install():
    sh = nodefw.probe_scripts("playwright", "https://app/")["probe.sh"]
    assert "--no-install" in sh          # never installs
    assert "app_url" in sh and "https://app/" in sh
    cmd = nodefw.probe_scripts("cypress")["probe.cmd"]
    assert "cypress verify" in cmd


def test_probe_argv_platform():
    assert nodefw.probe_argv("windows-amd64") == \
        ["cmd", "/c", "probe.cmd"]
    assert nodefw.probe_argv("linux-x86_64") == ["sh", "probe.sh"]


def test_parse_probe():
    out = nodefw.parse_probe(
        "noise\nTO-PROBE node=v22.1.0\nTO-PROBE app_url=UNREACHABLE"
        " from this target\nTO-PROBE done=1\n")
    assert out["node"] == "v22.1.0"
    assert out["done"] == "1"
    assert out["app_url"].startswith("UNREACHABLE")
