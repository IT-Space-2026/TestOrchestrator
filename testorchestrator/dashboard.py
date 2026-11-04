"""Dashboard aggregation — pure query helpers shared by the page and
the live-update JSON endpoint.

Metric definitions (all counts over real stored rows only):
- projects_total: projects visible to the requesting user
  (current_user.project_allowed — same scope as the Projects page).
- executions_active: state in ACTIVE_STATES (queued..cancel_requested);
  "active" means not yet terminal — it says nothing about test results.
- outcomes_last_days: terminal executions finishing in the period,
  split by Outcome (passed/failed/other). Each execution *attempt* is
  one row — a rerun/retry is a separate attempt, so denominators count
  attempts, not original suites.
- runners_available: enabled and not draining (dispatch-eligible);
  health is reported separately (health == "ok" counts as healthy).
- schedules_upcoming: enabled schedules with a future next_run_at.
- project rows: latest finished execution's outcome/time, active+queued
  counts, pass rate = passed / terminal attempts in the period
  (attempts, not original runs), earliest upcoming schedule run.
- charts: per-day terminal execution counts for the last 14 days.
"""

from __future__ import annotations

from datetime import timedelta

from .execution import (Execution, ExecutionState, Outcome, Project,
                        Runner, Schedule)
from .extensions import db
from .execution import utcnow

ACTIVE_STATES = (
    ExecutionState.QUEUED, ExecutionState.DISPATCHED,
    ExecutionState.PREPARING, ExecutionState.RUNNING,
    ExecutionState.COLLECTING, ExecutionState.NORMALIZING,
    ExecutionState.PUBLISHING, ExecutionState.FINALIZING,
    ExecutionState.CANCEL_REQUESTED,
)
TERMINAL_STATES = (
    ExecutionState.COMPLETED, ExecutionState.FAILED,
    ExecutionState.CANCELED,
)
DEFAULT_PERIOD_DAYS = 14
CHART_DAYS = 14
MAX_DASHBOARD_PROJECTS = 50   # labelled preview; full list is /projects


def _day_key(dt) -> str:
    return dt.strftime("%Y-%m-%d") if dt else ""


def _aware(dt):
    """SQLite stores naive datetimes — normalize to compare with utcnow."""
    return dt.replace(tzinfo=None) if dt else None


def snapshot(user, period_days: int = DEFAULT_PERIOD_DAYS) -> dict:
    now = utcnow()
    since = _aware(now - timedelta(days=period_days))
    now_n = _aware(now)

    projects = [p for p in Project.query.order_by(Project.name).all()
                if user.project_allowed(p.id)]
    project_ids = {p.id for p in projects}

    executions = (Execution.query.filter(
        Execution.project_id.in_(project_ids)).all()
        if project_ids else [])

    terminal_period = [e for e in executions
                       if e.state in TERMINAL_STATES
                       and e.finished_at
                       and _aware(e.finished_at) >= since]
    passed = sum(1 for e in terminal_period if e.outcome == Outcome.PASSED)
    failed = sum(1 for e in terminal_period if e.outcome == Outcome.FAILED)
    other = len(terminal_period) - passed - failed
    active = [e for e in executions if e.state in ACTIVE_STATES]

    runners = Runner.query.all()
    runners_available = [r for r in runners
                         if r.enabled and not r.draining]
    runners_healthy = [r for r in runners_available
                       if r.health == "ok"]

    schedules = Schedule.query.filter_by(enabled=True).all()
    upcoming = [s for s in schedules
                if s.next_run_at and _aware(s.next_run_at) > now_n
                and s.project_id in project_ids]
    upcoming.sort(key=lambda s: s.next_run_at)

    # Per-project rows — all pre-aggregated, no per-row queries.
    by_project = {}
    for e in executions:
        by_project.setdefault(e.project_id, []).append(e)
    sched_by_project = {}
    for s in upcoming:
        sched_by_project.setdefault(s.project_id, []).append(s)

    rows = []
    for p in projects:
        exs = by_project.get(p.id, [])
        term = [e for e in exs if e.state in TERMINAL_STATES
                and e.finished_at]
        term.sort(key=lambda e: e.finished_at, reverse=True)
        term_period = [e for e in term
                       if _aware(e.finished_at) >= since]
        p_passed = sum(1 for e in term_period
                       if e.outcome == Outcome.PASSED)
        p_total = len(term_period)
        act = [e for e in exs if e.state in ACTIVE_STATES]
        rows.append({
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "latest_finished_at":
                term[0].finished_at.isoformat() if term else None,
            "latest_outcome": term[0].outcome.value if term else None,
            "active": len(act),
            "queued": sum(1 for e in act
                          if e.state == ExecutionState.QUEUED),
            "pass_rate": (round(100 * p_passed / p_total)
                          if p_total else None),
            "pass_total": p_total,
            "next_run_at": (sched_by_project[p.id][0].next_run_at
                            .isoformat()
                            if sched_by_project.get(p.id) else None),
        })

    recent = sorted(
        executions,
        key=lambda e: (e.finished_at or e.started_at or e.created_at),
        reverse=True)[:10]

    # Charts: per-day terminal counts for CHART_DAYS.
    days = [(_day_key(now - timedelta(days=CHART_DAYS - 1 - i)), 0, 0, 0)
            for i in range(CHART_DAYS)]
    idx = {d[0]: i for i, d in enumerate(days)}
    series = []
    for e in terminal_period:
        k = _day_key(e.finished_at)
        if k in idx:
            days[idx[k]] = (
                k,
                days[idx[k]][1] + (e.outcome == Outcome.PASSED),
                days[idx[k]][2] + (e.outcome == Outcome.FAILED),
                days[idx[k]][3] + 1)
    for d, p_, f_, t_ in days:
        series.append({"day": d, "passed": p_, "failed": f_,
                       "total": t_})

    return {
        "generated_at": now.isoformat(),
        "period_days": period_days,
        "metrics": {
            "projects_total": len(projects),
            "executions_active": len(active),
            "executions_queued": sum(
                1 for e in active
                if e.state == ExecutionState.QUEUED),
            "passed_period": passed,
            "failed_period": failed,
            "other_period": other,
            "terminal_period": len(terminal_period),
            "runners_available": len(runners_available),
            "runners_total": len(runners),
            "runners_healthy": len(runners_healthy),
            "schedules_upcoming": len(upcoming),
            "next_schedule_at": (upcoming[0].next_run_at.isoformat()
                                 if upcoming else None),
        },
        "projects": rows[:MAX_DASHBOARD_PROJECTS],
        "projects_total_rows": len(rows),
        "projects_capped": len(rows) > MAX_DASHBOARD_PROJECTS,
        "recent": [{
            "id": e.id,
            "suite": e.snapshot().get("suite_name", "") or e.suite_id,
            "project_id": e.project_id,
            "state": e.state.value,
            "outcome": e.outcome.value,
            "created_at": e.created_at.isoformat()
                          if e.created_at else None,
            "finished_at": e.finished_at.isoformat()
                           if e.finished_at else None,
            "created_by": e.created_by,
        } for e in recent],
        "chart_outcomes": series,
    }
