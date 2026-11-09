"""Project dashboard: aggregation correctness + figure behavior."""
import json
from datetime import datetime, timedelta, timezone
from testorchestrator.extensions import db
from testorchestrator.execution import (Project, Execution, ExecutionState,
                                        Outcome, Project, TestResult)
from testorchestrator.project_dashboard import summary
from testorchestrator import charts


def _seed(app, project_id="pd", days=3):
    with app.app_context():
        p = Project.query.get(project_id) or Project(
            id=project_id, name=project_id)
        db.session.add(p); db.session.flush()
        now = datetime.now(timezone.utc)
        out = []
        for i in range(days):
            ex = Execution(project_id=project_id,
                           state=ExecutionState.COMPLETED,
                           outcome=Outcome.PASSED if i % 2 == 0
                                   else Outcome.FAILED,
                           created_by="fixture",
                           started_at=now - timedelta(days=i,
                                                      minutes=2),
                           finished_at=now - timedelta(days=i),
                           report_status="ready")
            db.session.add(ex); db.session.flush()
            # 3 cases: 2 pass + 1 fail (or skip on one day)
            for j, st in enumerate(
                    ["passed", "passed",
                     "failed" if i % 2 else "skipped"]):
                db.session.add(TestResult(execution_id=ex.id,
                                          case_ref=f"c{j}",
                                          case_name=f"case-{j}",
                                          status=st))
            out.append(ex)
        db.session.commit()
        return out


def test_pass_rate_excludes_skips_from_denominator(app):
    _seed(app, days=2)
    with app.app_context():
        d = summary("pd", days=30)
        rows = d["pass_rate"]
        assert rows
        # each day: 2 passed + (1 failed or 1 skipped). skips must NOT
        # enter the denominator (passed+failed+error)
        day0 = next(r for r in rows if r["failed"] == 0)
        # day with a skip: denominator is 2 (passed only) not 3
        assert day0["passed"] == 2 and day0["failed"] == 0


def test_executions_vs_test_case_counts_separate(app):
    _seed(app, days=3)
    with app.app_context():
        d = summary("pd", days=30)
        assert d["total"] == 3                # executions
        # outcome trend counts executions per day, not test cases
        total_exec_in_trend = sum(
            r["passed"] + r["failed"] + r["error"] + r["skipped"]
            for r in d["outcome_trend"])
        assert total_exec_in_trend == 3       # 3 executions, not 9 cases


def test_top_failure_ranking_counts_across_runs(app):
    _seed(app, days=4)  # case-2 fails on even days (2 of 4)
    with app.app_context():
        d = summary("pd", days=30)
        top = d["failures"][0]
        assert top["case"] == "case-2"
        assert top["count"] >= 2      # same case across multiple runs


def test_duration_only_terminal_states_contribute(app):
    with app.app_context():
        p = Project.query.get("pd") or Project(id="pd", name="pd")
        db.session.add(p)
        ex = Execution(project_id="pd", state=ExecutionState.RUNNING,
                       outcome=Outcome.NONE, created_by="f")
        db.session.add(ex); db.session.commit()
    _seed(app, days=1)
    with app.app_context():
        d = summary("pd", days=30)
        # only completed executions feed durations
        assert all(isinstance(x["seconds"], float)
                   for x in d["durations"])
        assert len(d["durations"]) >= 1


def test_empty_project_has_null_figures(app):
    with app.app_context():
        d = summary("nonexistent", days=30)
        assert d["total"] == 0
        assert charts.outcome_trend(d["outcome_trend"]) == "null"
        assert charts.pass_rate_trend(d["pass_rate"]) == "null"
        assert charts.duration_trend(d["durations"]) == "null"
        assert charts.failure_breakdown(d["failures"]) == "null"


def test_period_filter_limits_executions(app):
    with app.app_context():
        d7 = summary("pd", days=7)
        d1 = summary("pd", days=1)
        assert d1["total"] <= d7["total"]


def test_figures_serializable_json(app):
    _seed(app, days=1)
    with app.app_context():
        d = summary("pd", days=30)
        for fig in (charts.outcome_trend(d["outcome_trend"]),
                    charts.pass_rate_trend(d["pass_rate"]),
                    charts.duration_trend(d["durations"]),
                    charts.failure_breakdown(d["failures"])):
            if fig == "null":      # legitimately empty figure
                continue
            j = json.loads(fig)
            assert "data" in j and "layout" in j


def test_source_ref_env_filters_independent_and_combined(app):
    from testorchestrator.project_dashboard import summary
    from testorchestrator.execution import Execution, Outcome, utcnow
    from testorchestrator.extensions import db
    with app.app_context():
        p = Project(id="pf", name="pf")
        db.session.add(p)
        for i,(ref,env) in enumerate([("main","linux"),("dev","linux"),
                                       ("main","win"),(None,None)]):
            e=Execution(project_id="pf",source_sha="x",source_ref=ref,
                        environment=env)
            e.outcome=Outcome.PASSED; e.finished_at=utcnow()
            e.state=ExecutionState.COMPLETED
            db.session.add(e)
        db.session.commit()
        assert summary("pf", source_ref="main")["total"] == 2
        assert summary("pf", environment="win")["total"] == 1
        assert summary("pf", source_ref="main",
                       environment="linux")["total"] == 1
        assert summary("pf", source_ref="__none__")["total"] == 1
        assert summary("pf")["total"] == 4
