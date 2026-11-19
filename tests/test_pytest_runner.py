"""Contract tests for the pytest runner (2.6.0)."""
from pathlib import Path

from testorchestrator.adapters import pytestfw
from testorchestrator.results import parse_junit


def test_build_args_junit_layout():
    a = pytestfw.build_args()
    assert a[:2] == ["-m", "pytest"]
    assert "--junitxml" in a and "Reports/junit.xml" in a


def test_build_args_selection():
    a = pytestfw.build_args(suite_path="tests/test_login.py",
                            keyword="login and not slow",
                            markers="smoke")
    assert a[-1] == "tests/test_login.py"
    assert "-k" in a and "login and not slow" in a
    assert "-m" in a and "smoke" in a


def test_build_args_node_ids_override_path():
    a = pytestfw.build_args(suite_path="tests/",
                            node_ids=["tests/test_a.py::test_x"])
    assert "tests/test_a.py::test_x" in a
    assert "tests/" not in a


def test_classify_exit_semantics():
    assert pytestfw.classify_exit(0)[0] == "ok"
    assert pytestfw.classify_exit(1)[0] == "tests"
    assert pytestfw.classify_exit(2)[0] == "interrupted"
    assert pytestfw.classify_exit(3)[0] == "internal"
    # usage error + no-tests-collected are config problems, never a
    # passing zero-test run
    assert pytestfw.classify_exit(4)[0] == "config"
    assert pytestfw.classify_exit(5)[0] == "config"
    assert pytestfw.classify_exit(137)[0] == "internal"


def test_classify_log_labels():
    assert "path not found" in pytestfw.classify_log(
        "ERROR: file or directory not found: tests/nope.py")
    assert "no tests collected" in pytestfw.classify_log(
        "collected 0 items")
    assert "module" in pytestfw.classify_log(
        "ModuleNotFoundError: No module named 'xdist'")
    assert "interrupted" in pytestfw.classify_log(
        "!!! KeyboardInterrupt !!!")
    assert pytestfw.classify_log("3 passed in 0.5s") == ""


def test_pytest_junit_normalizes(tmp_path: Path):
    """pytest --junitxml output feeds parse_junit — pass, fail, skip,
    and parametrized cases must all survive normalization."""
    (tmp_path / "junit.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<testsuite name="pytest" tests="4" failures="1" '
        'errors="0" skipped="1" time="0.012">'
        '<testcase classname="tests.test_demo" name="test_passes" '
        'time="0.001"/>'
        '<testcase classname="tests.test_demo" name="test_fails" '
        'time="0.002"><failure message="assert 1 == 2">x</failure>'
        '</testcase>'
        '<testcase classname="tests.test_demo" name="test_skips" '
        'time="0.001"><skipped message="skipif">y</skipped></testcase>'
        '<testcase classname="tests.test_demo" '
        'name="test_param[1-2]" time="0.001"/>'
        '</testsuite>')
    parsed = parse_junit(tmp_path / "junit.xml")
    names = {c.name: c.status for c in parsed.cases}
    assert names["test_passes"].upper() == "PASSED"
    assert names["test_fails"].upper() == "FAILED"
    assert names["test_skips"].upper() in ("SKIPPED", "PASSED")
    assert "test_param[1-2]" in names


def test_probe_scripts_no_secrets_and_done_marker():
    scripts = pytestfw.probe_scripts("https://app.example")
    assert set(scripts) == {"probe.sh", "probe.cmd"}
    for text in scripts.values():
        assert "TO-PROBE" in text and "done=1" in text
        assert "password" not in text.lower()


def test_probe_argv():
    assert pytestfw.probe_argv("linux-x86_64") == ["sh", "probe.sh"]
    assert pytestfw.probe_argv("windows-x86_64") == \
        ["cmd", "/c", "probe.cmd"]
