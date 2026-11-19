"""Contract tests for the Robot Framework runner (2.6.0)."""
from pathlib import Path

from testorchestrator.adapters import robotfw
from testorchestrator.results import parse_junit


def test_build_args_reports_layout():
    a = robotfw.build_args()
    assert a[:4] == ["-m", "robot", "--outputdir", "Reports"]
    assert "--xunit" in a and "junit.xml" in a
    # xunit resolves relative to outputdir -> Reports/junit.xml
    assert a[-1] != "--xunit"


def test_build_args_selection_and_tags():
    a = robotfw.build_args(suite_path="tests/login.robot",
                           test="Valid login", suite="Login",
                           include=["smoke", "ui"], exclude=["slow"])
    assert a[-1] == "tests/login.robot"
    for flag, val in (("--test", "Valid login"),
                      ("--suite", "Login"),
                      ("--include", "smoke"), ("--exclude", "slow")):
        assert flag in a and val in a
    assert a.count("--include") == 2


def test_classify_exit_semantics():
    assert robotfw.classify_exit(0)[0] == "ok"
    assert robotfw.classify_exit(2) == ("tests", "2 failed test(s)")
    assert robotfw.classify_exit(251)[0] == "config"
    assert robotfw.classify_exit(252)[0] == "config"
    assert robotfw.classify_exit(253)[0] == "interrupted"
    assert robotfw.classify_exit(255)[0] == "internal"


def test_classify_log_labels():
    assert "data source" in robotfw.classify_log(
        "[ ERROR ] Data source does not exist: /x")
    assert "invalid test data" in robotfw.classify_log(
        "Error in file '/p/s.robot' on line 3")
    assert "library" in robotfw.classify_log(
        "Importing library 'NoSuch' failed")
    assert robotfw.classify_log("all good 1 test passed") == ""


def test_robot_xunit_normalizes(tmp_path: Path):
    """Real Robot --xunit output shape feeds parse_junit — pass,
    fail and skip must all survive normalization."""
    (tmp_path / "junit.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<testsuite name="Demo" tests="3" failures="1" '
        'errors="0" skipped="1" time="0.012">'
        '<testcase classname="Demo" name="passes" time="0.001"/>'
        '<testcase classname="Demo" name="fails" time="0.010">'
        '<failure message="1 != 2" type="AssertionError"/>'
        '</testcase>'
        '<testcase classname="Demo" name="skips" time="0.001">'
        '<skipped message="skipped via tag"/>'
        '</testcase>'
        '</testsuite>')
    p = parse_junit(tmp_path / "junit.xml")
    st = {c.name: c.status for c in p.cases}
    assert st == {"passes": "passed", "fails": "failed",
                  "skips": "skipped"}


def test_probe_scripts_present():
    s = robotfw.probe_scripts("http://x")
    assert "probe.sh" in s and "probe.cmd" in s
    assert "PYBIN" in s["probe.sh"] and "robot" in s["probe.cmd"]
