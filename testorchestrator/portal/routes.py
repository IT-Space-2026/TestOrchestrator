"""Portal blueprint: projects, bindings, suites, plans, executions (§10).

Role model (§16), enforced server-side:
  Admin + Test Manager — definitions, bindings, schedules, execution
  Tester               — launch + cancel own executions, view reports
  PO                   — read-only results/evidence, no mutation/execution
Project scope is checked on every endpoint including artifact downloads
and log streams.
"""

import hashlib
import json
import re
import mimetypes
from datetime import datetime, timedelta
from pathlib import Path

from flask import (abort, flash, redirect, render_template, request,
                   send_file, url_for)
from flask_login import current_user, login_required

from .. import dispatch, gitsync
from ..execution import (DiscoveredItem, Execution, ExecutionState,
                         ReleaseItem,
                       GitBinding, OutboxEvent,
                         Outcome, Plan, PlanEntry, Project, Release,
                         Schedule, Suite, SuiteEntry, utcnow)
from ..extensions import db
from ..models import Role, record_audit
from ..security import role_required
from . import portal_bp
from .. import results as results_mod
from .forms import (BindingForm, PlanForm, PlainForm, ProjectForm,
                    ScheduleForm, SuiteForm)

EDIT_ROLES = (Role.ADMIN, Role.TEST_MANAGER)
RUN_ROLES = (Role.ADMIN, Role.TEST_MANAGER, Role.TESTER)


def _dash_trend(qs, days: int = 14):
    """Daily passed/failed counts — bar-chart data like the main
    dashboard's chart_outcomes."""
    from collections import Counter
    from datetime import date, timedelta
    cp, cf = Counter(), Counter()
    for e in qs.all():
        if not e.created_at:
            continue
        d = e.created_at.date().isoformat()
        if e.outcome.value == "passed":
            cp[d] += 1
        elif e.outcome.value in ("failed", "incomplete"):
            cf[d] += 1
    out = []
    for i in range(days):
        day = (date.today() - timedelta(days=days - 1 - i)).isoformat()
        out.append({"day": day, "passed": cp.get(day, 0),
                    "failed": cf.get(day, 0)})
    return out


def _project_or_404(project_id) -> Project:
    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)
    if not current_user.project_allowed(project_id):
        abort(403)
    return project


def _execution_or_404(execution_id) -> Execution:
    ex = db.session.get(Execution, execution_id)
    if ex is None:
        abort(404)
    if not current_user.project_allowed(ex.project_id):
        abort(403)
    return ex


def _suite_hash(suite: Suite) -> str:
    content = json.dumps([
        {"case_ref": e.case_ref, "order": e.order, "enabled": e.enabled,
         "config": e.config_json}
        for e in suite.entries
    ], sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()


# ---------- projects ----------

@portal_bp.route("/projects")
@login_required
def projects():
    from ..dashboard import ACTIVE_STATES, TERMINAL_STATES
    from ..execution import Execution
    q = request.args.get("q", "").strip().lower()
    sort = request.args.get("sort", "name")
    all_projects = [p for p in Project.query.all()
                    if current_user.project_allowed(p.id)]
    if q:
        all_projects = [p for p in all_projects
                        if q in p.name.lower()
                        or q in (p.description or "").lower()]
    exs = Execution.query.all() if all_projects else []
    by_p = {}
    for e in exs:
        by_p.setdefault(e.project_id, []).append(e)
    cards = []
    for p in all_projects:
        pes = by_p.get(p.id, [])
        term = [e for e in pes if e.state in TERMINAL_STATES
                and e.finished_at]
        term.sort(key=lambda e: e.finished_at, reverse=True)
        active = sum(1 for e in pes if e.state in ACTIVE_STATES)
        cards.append({
            "project": p,
            "latest": term[0] if term else None,
            "active": active,
            "total": len(pes),
        })
    if sort == "recent":
        cards.sort(key=lambda c: (c["latest"].finished_at
                                  if c["latest"] else ""),
                   reverse=True)
    else:
        cards.sort(key=lambda c: c["project"].name.lower())
    return render_template("portal/projects.html", cards=cards,
                           form=ProjectForm(), q=q, sort=sort)


@portal_bp.route("/projects", methods=["POST"])
@role_required(*EDIT_ROLES)
def create_project():
    form = ProjectForm()
    if form.validate_on_submit():
        project = Project(name=form.name.data.strip(),
                          description=form.description.data or "")
        db.session.add(project)
        db.session.commit()
        record_audit(current_user.username, "project.created", project.id)
    return redirect(url_for("portal.projects"))


@portal_bp.route("/projects/<project_id>/edit", methods=["POST"])
@role_required(*EDIT_ROLES)
def edit_project(project_id):
    project = _project_or_404(project_id)
    form = ProjectForm()
    if form.validate_on_submit():
        project.name = form.name.data.strip()
        project.description = form.description.data or ""
        db.session.commit()
        record_audit(current_user.username, "project.edited", project.id)
    return redirect(url_for("portal.projects"))


@portal_bp.route("/projects/<project_id>")
@login_required
def project_detail(project_id):
    project = _project_or_404(project_id)
    can_edit = current_user.role.value in ("admin", "test_manager")
    can_run = current_user.role.value in ("admin", "test_manager", "tester")
    suite_form = SuiteForm()
    # Case choices mirror create_suite's selection: the project's first
    # binding's synced worktree (read-only listing, never executed).
    first_binding = GitBinding.query.filter_by(project_id=project.id).first()
    cases = gitsync.list_test_cases(first_binding) if first_binding else []
    suite_form.case_refs.choices = [(c, c) for c in cases]
    schedule_form = ScheduleForm()
    # repository definitions first (binding · kind · path), then
    # legacy App suites explicitly labelled.
    repo_defs = DiscoveredItem.query.join(GitBinding).filter(
        GitBinding.project_id == project.id,
        DiscoveredItem.kind.in_(("suite", "collection"))).all()
    bind_names = {b.id: b.name for b in GitBinding.query.filter_by(
        project_id=project.id)}
    repo_targets = [
        {"v": f"repo:{d.id}",
         "l": f"{d.name or d.path.split('/')[-1].rsplit('.', 1)[0]} "
             f"({d.kind}) · {bind_names.get(d.binding_id, '?')} · "
             f"{d.path}"} for d in repo_defs]
    schedule_form.target_id.choices = (
        [(f"repo:{d.id}", f"{bind_names.get(d.binding_id, '?')} · "
          f"{d.kind} · {d.path}") for d in repo_defs] +
        [(f"app:{s.id}", f"app-side suite · {s.name}")
         for s in project.suites])
    from ..execution import Runner, RuntimeConnection
    _runners = [x for x in Runner.query.filter_by(enabled=True).all()
                if not x.draining]
    schedule_form.runner_id.choices = [
        ("", "Auto — first eligible")] + [
        (x.id, f"{x.name} ({x.os_arch or 'unknown os'})")
        for x in _runners]
    schedule_form.runtime_id.choices = [
        ("", "Auto — compatible on runner")] + [
        (x.id, f"{x.name} ({x.detected_version or 'unknown ver'})")
        for x in RuntimeConnection.query.filter_by(enabled=True).all()]
    _qs = Execution.query.filter_by(project_id=project.id)
    _since = request.args.get("since", "")
    if _since:
        from datetime import datetime as _dt, timezone as _tz
        _qs = _qs.filter(Execution.created_at >= _dt.fromisoformat(
            _since).replace(tzinfo=_tz.utc))
    _n = _qs.count()
    _done = _qs.filter(Execution.finished_at.isnot(None)).all()
    _pass = sum(1 for e in _done if e.outcome.value == "passed")
    _rate = round(100 * _pass / len(_done)) if _done else 0
    dash = {"count": _n, "passed": _pass,
            "failed": sum(1 for e in _done
                          if e.outcome.value == "failed"),
            "running": _qs.filter(Execution.started_at.isnot(None),
                                  Execution.finished_at.is_(None)).count(),
            "queued": _qs.filter(Execution.started_at.is_(None)).count(),
            "rate": _rate, "since": _since,
            "trend": _dash_trend(_qs),
            "pub_pending": OutboxEvent.query.filter_by(
                status="pending").count(),
            "pub_failed": OutboxEvent.query.filter_by(
                status="failed").count(),
            "recent": _qs.order_by(
                Execution.created_at.desc()).limit(8).all()}
    # per-release testing progress: latest outcome per scoped target
    # (path+kind+binding+config), not all history.
    progress = {}
    for rel in project.releases:
        items = ReleaseItem.query.filter_by(
            release_id=rel.id).all()
        counts = {"passed": 0, "failed": 0, "running": 0,
                  "queued": 0, "cancelled": 0, "unexecuted": 0,
                  "unavailable": 0}
        latest = {}
        for e in Execution.query.filter_by(release_id=rel.id).order_by(
                Execution.created_at.desc()):
            st = e.snapshot().get("source_target", {})
            key = (st.get("path", ""), st.get("kind", ""),
                   st.get("binding_id", ""))
            if key not in latest:
                latest[key] = e
        for it in items:
            src = (db.session.get(DiscoveredItem, it.source_item_id)
                   if it.source_item_id else None)
            key = (it.path, it.kind,
                   src.binding_id if src else "")
            if src is None and it.source_item_id:
                counts["unavailable"] += 1
                continue
            e = latest.get(key) or next(
                (e for (p, _k, _b), e in latest.items()
                 if p == it.path), None)
            if e is None:
                counts["unexecuted"] += 1
            elif e.finished_at:
                counts["passed" if e.outcome.value == "passed"
                        else "failed"] += 1
            elif e.started_at:
                counts["running"] += 1
            else:
                counts["queued"] += 1
        progress[rel.id] = counts
    # Dashboard release overview: active releases (draft+in_progress),
    # dated timeline + undated list — "current state", not
    # date-filtered.
    active = [rel for rel in project.releases
              if rel.status in ("draft", "in_progress")]
    dated_active = [rel for rel in active if rel.start_date]
    undated_active = [rel for rel in active if not rel.start_date]
    from datetime import timezone as _tz, datetime as _dt
    _today = _dt.now(_tz.utc).date()
    overdue_active = [rel for rel in active
                      if rel.end_date and rel.end_date < _today]
    tlo = (min((rel.start_date for rel in dated_active))
           if dated_active else None)
    thi = (max((rel.end_date or _today for rel in dated_active))
           if dated_active else None)
    return render_template(
        "portal/project_detail.html", project=project,
        rel_progress=progress,
        repo_targets=repo_targets,
        sched_targets=[],
        active_releases=active, dated_active=dated_active,
        undated_active=undated_active,
        overdue_active=overdue_active,
        tlo=tlo, thi=thi, dash=dash,
        binding_form=BindingForm(),
        can_author=GitBinding.query.filter_by(
            project_id=project.id, authoring_writable=True).count() > 0,
        repo_cases=cases,
        repo_suites=[i.path for i in
                     DiscoveredItem.query.filter_by(
                         binding_id=first_binding.id if first_binding else '',
                         kind='suite').all()],
        suite_form=suite_form, plain_form=PlainForm(),
        schedule_form=schedule_form, can_edit=can_edit, can_run=can_run,
        executions=Execution.query.filter_by(project_id=project_id)
        .order_by(Execution.created_at.desc()).limit(25).all(),
        discovered=_discovery_summary(project_id),
        plan_summaries={p.id: results_mod.plan_summary(p)
                        for p in project.plans},
        state_labels=results_mod.STATE_LABELS,
    )


# ---------- git bindings ----------

@portal_bp.route("/projects/<project_id>/dashboard")
@login_required
def project_dashboard(project_id):
    """Project reporting dashboard — trends, failures, drill-down.
    Aggregation in project_dashboard.py; figures in charts.py."""
    from ..charts import (outcome_trend, pass_rate_trend,
                          duration_trend, failure_breakdown)
    from ..project_dashboard import summary
    project = _project_or_404(project_id)
    days = request.args.get("period", 30, type=int) or 30
    suite = request.args.get("suite", "")
    state = request.args.get("state", "")
    data = summary(project.id, days=days, suite=suite, state=state)
    return render_template(
        "portal/project_dashboard.html",
        project=project, data=data, days=days,
        fig_outcomes=outcome_trend(data["outcome_trend"]),
        fig_passrate=pass_rate_trend(data["pass_rate"]),
        fig_duration=duration_trend(data["durations"]),
        fig_failures=failure_breakdown(data["failures"]),
        suites=Suite.query.filter_by(project_id=project.id).all(),
        sel_suite=suite, sel_state=state)


@portal_bp.route("/projects/<project_id>/bindings", methods=["POST"])
@role_required(*EDIT_ROLES)
def create_binding(project_id):
    project = _project_or_404(project_id)
    form = BindingForm()
    if form.validate_on_submit():
        binding = GitBinding(
            authoring_writable=bool(
                request.form.get("authoring_writable", "1")),
            project_id=project.id,
            name=form.name.data.strip(),
            url=form.url.data.strip(),
            ref=form.ref.data.strip() or "main",
            subdirectory=form.subdirectory.data.strip(),
            prj_path=form.prj_path.data.strip(),
            credential_ref=form.credential_ref.data.strip(),
        )
        db.session.add(binding)
        db.session.flush()
        db.session.add(OutboxEvent(
            kind="git-sync",
            payload_json=json.dumps({"binding_id": binding.id})))
        db.session.commit()
        record_audit(current_user.username, "binding.created", binding.id)
    return redirect(url_for("portal.project_detail", project_id=project_id))


@portal_bp.route("/bindings/<binding_id>/edit", methods=["POST"])
@role_required(*EDIT_ROLES)
def edit_binding(binding_id):
    binding = db.session.get(GitBinding, binding_id)
    if binding is None:
        abort(404)
    _project_or_404(binding.project_id)
    form = BindingForm()
    if form.validate_on_submit():
        binding.name = form.name.data.strip()
        binding.url = form.url.data.strip()
        binding.ref = form.ref.data.strip() or "main"
        binding.subdirectory = form.subdirectory.data.strip()
        binding.prj_path = form.prj_path.data.strip()
        binding.credential_ref = form.credential_ref.data.strip()
        binding.authoring_writable = bool(
            request.form.get("authoring_writable"))
        db.session.commit()
        record_audit(current_user.username, "binding.edited", binding.id)
        flash("Binding updated.", "info")
    return redirect(url_for("portal.project_detail",
                          project_id=binding.project_id))


@portal_bp.route("/bindings/<binding_id>/sync", methods=["POST"])
@role_required(*EDIT_ROLES)
def sync_binding(binding_id):
    binding = db.session.get(GitBinding, binding_id)
    if binding is None:
        abort(404)
    _project_or_404(binding.project_id)
    PlainForm().validate_on_submit()
    db.session.add(OutboxEvent(
        kind="git-sync",
        payload_json=json.dumps({"binding_id": binding.id})))
    db.session.commit()
    record_audit(current_user.username, "binding.sync_queued", binding.id)
    return redirect(url_for("portal.project_detail",
                          project_id=binding.project_id))


# ---------- suites ----------

@portal_bp.route("/projects/<project_id>/suites", methods=["POST"])
@role_required(*EDIT_ROLES)
def create_suite(project_id):
    project = _project_or_404(project_id)
    form = SuiteForm()
    binding = (db.session.get(GitBinding, form.binding_id.data)
               if form.binding_id.data else
               GitBinding.query.filter_by(
                   project_id=project.id).first())
    cases = gitsync.list_test_cases(binding) if binding else []
    form.case_refs.choices = [(c, c) for c in cases]
    if form.validate_on_submit():
        # posted refs — validate explicitly (field validation drops
        # non-choices silently, which would create a partial suite)
        refs = [x.strip() for x in
                request.form.getlist("case_refs") if x.strip()]
        if binding and refs:
            valid = set(gitsync.list_test_cases(binding))
            bad = [x for x in refs if x not in valid]
            if bad:
                flash("Some cases are not in the selected binding's "
                      f"source: {', '.join(bad[:3])}", "error")
                return redirect(url_for(
                    "portal.project_detail", project_id=project.id))
        suite = Suite(project_id=project.id, name=form.name.data.strip(),
                      description=form.description.data or "",
                      imported=bool(binding))
        db.session.add(suite)
        db.session.flush()  # id assigned before entries reference it
        for i, ref in enumerate(refs):
            db.session.add(SuiteEntry(
                suite_id=suite.id, order=i, case_ref=ref))
        suite.content_hash = _suite_hash(suite)
        db.session.commit()
        record_audit(current_user.username, "suite.created", suite.id)
    else:
        errs = "; ".join(
            f"{k}: {', '.join(v[:2])}"
            for k, v in form.errors.items())
        if errs:
            flash(f"Suite not created — {errs}", "error")
    return redirect(url_for("portal.project_detail", project_id=project_id))


@portal_bp.route("/projects/<project_id>/suites/new")
@role_required(*EDIT_ROLES)
def suite_new(project_id):
    project = _project_or_404(project_id)
    bindings = GitBinding.query.filter_by(project_id=project.id).all()
    binding = bindings[0] if bindings else None
    cases = (gitsync.list_test_cases(binding)
             if binding else [])
    return render_template(
        "portal/suite_edit.html", project=project, suite=None,
        binding=binding, bindings=bindings, cases=cases, selected=[],
        suite_form=SuiteForm(), can_edit=True)


@portal_bp.route("/suites/<suite_id>/edit")
@role_required(*EDIT_ROLES)
def suite_edit(suite_id):
    suite = db.session.get(Suite, suite_id)
    if suite is None:
        abort(404)
    _project_or_404(suite.project_id)
    if suite.readonly:
        abort(409)
    project = suite.project
    bindings = GitBinding.query.filter_by(project_id=project.id).all()
    binding = bindings[0] if bindings else None
    cases = (gitsync.list_test_cases(binding)
             if binding else [])
    selected = [e.case_ref for e in sorted(
        suite.entries, key=lambda e: e.order) if e.enabled]
    # keep selected-but-absent refs visible in the selector
    for c in selected:
        if c not in cases:
            cases.append(c)
    return render_template(
        "portal/suite_edit.html", project=project, suite=suite,
        binding=binding, bindings=bindings, cases=cases,
        selected=selected,
        suite_form=SuiteForm(), can_edit=True)


@portal_bp.route("/suites/<suite_id>/save", methods=["POST"])
@role_required(*EDIT_ROLES)
def save_suite(suite_id):
    suite = db.session.get(Suite, suite_id)
    if suite is None:
        abort(404)
    _project_or_404(suite.project_id)
    if suite.readonly:
        abort(409)
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    refs = [r.strip() for r in
            request.form.getlist("case_refs") if r.strip()]
    bindings = GitBinding.query.filter_by(
        project_id=suite.project_id).all()
    binding = bindings[0] if bindings else None
    if binding and refs:
        valid = set(gitsync.list_test_cases(binding))
        bad = [r for r in refs if r not in valid]
        if bad:
            # re-render with the user's work preserved
            return render_template(
                "portal/suite_edit.html", project=suite.project,
                suite=suite, binding=binding, bindings=bindings,
                cases=gitsync.list_test_cases(binding),
                selected=refs,
                suite_form=SuiteForm(), can_edit=True,
                posted_name=name,
                error=("Some cases are not in the binding's source: "
                       + ", ".join(bad[:3])))
    if not name:
        return render_template(
            "portal/suite_edit.html", project=suite.project,
            suite=suite, binding=binding, bindings=bindings,
            cases=(gitsync.list_test_cases(binding)
                   if binding else []),
            selected=refs, suite_form=SuiteForm(), can_edit=True,
            posted_name=name,
            error="Suite name is required.")
    # wholesale ordered replacement — positions come from the selector
    for e in suite.entries:
        db.session.delete(e)
    db.session.flush()
    for i, ref in enumerate(dict.fromkeys(refs)):   # dedupe, keep order
        db.session.add(SuiteEntry(suite_id=suite.id, order=i,
                                  case_ref=ref))
    suite.name = name
    suite.description = description
    suite.version += 1
    suite.content_hash = _suite_hash(suite)
    db.session.commit()
    record_audit(current_user.username, "suite.saved", suite.id)
    flash("Suite saved.", "info")
    return redirect(url_for("portal.suite_detail", suite_id=suite.id))


@portal_bp.route("/suites/<suite_id>")
@login_required
def suite_detail(suite_id):
    from ..execution import DiscoveredItem
    suite = db.session.get(Suite, suite_id)
    if suite is None:
        abort(404)
    _project_or_404(suite.project_id)
    can_run = current_user.role.value in ("admin", "test_manager", "tester")
    binding = GitBinding.query.filter_by(project_id=suite.project_id).first()
    from ..execution import Runner, RuntimeConnection
    runners = [x for x in Runner.query.filter_by(enabled=True).all()
               if not x.draining]
    runtimes = [x for x in
                RuntimeConnection.query.filter_by(enabled=True).all()
                if (not json.loads(x.allowed_projects or "[]")
                    or suite.project_id in json.loads(
                        x.allowed_projects or "[]"))]
    return render_template(
        "portal/suite_detail.html", suite=suite,
        can_author=GitBinding.query.filter_by(
            project_id=suite.project_id,
            authoring_writable=True).count() > 0, can_run=can_run,
        source_items=[i for i in DiscoveredItem.query.filter_by(
            project_id=suite.project_id).all()
            if i.kind in ("suite", "collection")],
        profiles=[i.name for i in DiscoveredItem.query.filter_by(
            project_id=suite.project_id, kind="profile")],
        binding=binding, plain_form=PlainForm(),
        runners=runners, runtimes=runtimes)


# ---------- executions ----------

@portal_bp.route("/discovered/<item_id>")
@login_required
def discovered_detail(item_id):
    from ..execution import DiscoveredItem
    item = db.session.get(DiscoveredItem, item_id)
    if item is None:
        abort(404)
    _project_or_404(item.project_id)
    from ..execution import Runner, RuntimeConnection
    runners = [x for x in Runner.query.filter_by(enabled=True).all()
               if not x.draining]
    runtimes = RuntimeConnection.query.filter_by(enabled=True).all()
    profiles = [i.name for i in DiscoveredItem.query.filter_by(
        binding_id=item.binding_id, kind="profile")]
    b2 = db.session.get(GitBinding, item.binding_id)
    return render_template(
        "portal/discovered_detail.html", item=item, runners=runners,
        runtimes=runtimes, profiles=profiles,
        plain_form=PlainForm(),
        can_author=bool(b2 and b2.authoring_writable and
                        current_user.role.value in
                        ("admin", "test_manager")),
        binding=b2,
        can_run=current_user.role.value in
        ("admin", "test_manager", "tester"))


@portal_bp.route("/discovered/<item_id>/run", methods=["POST"])
@role_required(*RUN_ROLES)
def run_discovered(item_id):
    from ..execution import DiscoveredItem
    item = db.session.get(DiscoveredItem, item_id)
    if item is None or item.kind not in ("suite", "collection"):
        abort(404)
    _project_or_404(item.project_id)
    PlainForm().validate_on_submit()
    binding = db.session.get(GitBinding, item.binding_id)
    run_config = {
        "profile": request.form.get("execution_profile") or "default",
        "runner_id": request.form.get("runner_id", "").strip(),
        "runtime_id": request.form.get("runtime_id", "").strip(),
        "timeout_seconds":
            int(request.form.get("timeout_seconds") or 0),
        "suite_path": item.path,
        "collection": item.kind == "collection",
    }
    ex = dispatch.create_execution(
        current_user.username, binding=binding,
        release_id=(request.form.get("release_id") or None),
        source_sha=binding.last_synced_sha if binding else "",
        run_config=run_config, source_item=item)
    record_audit(current_user.username, "execution.queued", ex.id)
    flash(f"Execution {ex.id[:8]} queued for "
          f"{item.kind} {item.path!r}.", "info")
    return redirect(url_for("portal.execution_detail", execution_id=ex.id))


@portal_bp.route("/executions/<execution_id>/delete",
             methods=["POST"])
@role_required(*EDIT_ROLES)
def delete_execution(execution_id):
    ex = _execution_or_404(execution_id)
    PlainForm().validate_on_submit()
    ex.deletion_status = "pending"
    db.session.add(OutboxEvent(
        kind="report-delete",
        payload_json=json.dumps({"execution_id": ex.id})))
    db.session.commit()
    record_audit(current_user.username, "execution.delete_queued",
                 ex.id)
    flash("Deletion queued — the repository report will be removed "
          "first, then the execution record.", "info")
    return redirect(url_for("portal.execution_detail",
                          execution_id=ex.id))


@portal_bp.route("/executions/<execution_id>/publish",
             methods=["POST"])
@role_required(*EDIT_ROLES)
def publish_execution_report(execution_id):
    ex = _execution_or_404(execution_id)
    PlainForm().validate_on_submit()
    ex.report_pub_status = "pending"
    db.session.add(OutboxEvent(
        kind="report-publish",
        payload_json=json.dumps({"execution_id": ex.id})))
    db.session.commit()
    record_audit(current_user.username, "execution.publish_queued",
                 ex.id)
    return redirect(url_for("portal.execution_detail",
                          execution_id=ex.id))


@portal_bp.route("/suites/<suite_id>/sync-to-repo", methods=["POST"])
@role_required(*EDIT_ROLES)
def sync_suite_to_repo(suite_id):
    """Export an App suite into a native-authoring draft — same
    draft/diff/publish flow as repo-created definitions."""
    from .. import authoring
    suite = db.session.get(Suite, suite_id)
    if suite is None:
        abort(404)
    _project_or_404(suite.project_id)
    binding = GitBinding.query.filter_by(
        project_id=suite.project_id, authoring_writable=True).first()
    if binding is None:
        flash("Enable Source authoring on a binding first.", "error")
        return redirect(url_for("portal.suite_detail",
                              suite_id=suite.id))
    PlainForm().validate_on_submit()
    name = suite.name.replace(" ", "_")
    d = authoring.open_new_draft(
        current_user.username, binding, "suite",
        f"Test Suites/{name}.ts", suite.name)
    refs = [e.case_ref for e in
            sorted(suite.entries, key=lambda e: e.order)]
    d.members_json = json.dumps(refs)
    db.session.commit()
    record_audit(current_user.username, "suite.sync_to_repo", suite.id)
    flash("Draft created — review the diff and publish.", "info")
    return redirect(url_for("portal.author_item",
                            item_id="", draft_id=d.id))


@portal_bp.route("/bindings/<binding_id>/author-new", methods=["POST"])
@role_required(*EDIT_ROLES)
def author_new(binding_id):
    from .. import authoring
    binding = db.session.get(GitBinding, binding_id)
    if binding is None:
        abort(404)
    _project_or_404(binding.project_id)
    if not binding.authoring_writable:
        abort(403)
    PlainForm().validate_on_submit()
    kind = request.form.get("kind", "suite")
    name = request.form.get("name", "").strip() or "New"
    path = request.form.get("path", "").strip()
    if not path or ".." in path or path.startswith("/"):
        flash("A safe repository-relative path is required.", "error")
        return redirect(url_for("portal.project_detail",
                              project_id=binding.project_id))
    refs = [x.strip() for x in
            request.form.getlist("case_refs") if x.strip()]
    d = authoring.open_new_draft(current_user.username, binding,
                                 kind, path, name)
    if refs:
        d.members_json = json.dumps(list(dict.fromkeys(refs)))
        db.session.commit()
    item = _draft_item(d)
    return redirect(url_for("portal.author_item",
                            item_id=item.id if item else "",
                            draft_id=d.id))


@portal_bp.route("/discovered/<item_id>/author")
@role_required(*EDIT_ROLES)
def author_item(item_id):
    from .. import authoring
    from ..execution import AuthoringDraft, DiscoveredItem
    draft = db.session.get(AuthoringDraft,
                           request.args.get("draft_id", ""))
    if draft is None:
        item = db.session.get(DiscoveredItem, item_id)
        if item is None or item.kind not in ("suite", "collection"):
            abort(404)
        _project_or_404(item.project_id)
        b = db.session.get(GitBinding, item.binding_id)
        if not (b and b.authoring_writable):
            abort(403)
        draft = AuthoringDraft.query.filter_by(
            binding_id=item.binding_id, path=item.path,
            owner=current_user.username, status="draft").first()
        if draft is None:
            draft = authoring.open_draft(current_user.username, item)
    else:
        _project_or_404(draft.project_id)
        if draft.owner != current_user.username:
            abort(403)
        b = db.session.get(GitBinding, draft.binding_id)
        item = db.session.get(DiscoveredItem, item_id)
    avail = (gitsync.list_test_cases(b) if draft.kind == "suite"
             else [i.path for i in DiscoveredItem.query.filter_by(
                 binding_id=b.id, kind="suite")])
    return render_template(
        "portal/author_edit.html", item=item, draft=draft, binding=b,
        cases=avail, diff=authoring.draft_diff(draft),
        review_sig=authoring.draft_signature(draft),
        disabled=[m for m in draft.members()
                  if draft.member_cfg().get(m, {}).get("isRun")
                  == "false"],
        plain_form=PlainForm())


def _draft_item(draft):
    from ..execution import DiscoveredItem
    return DiscoveredItem.query.filter_by(
        binding_id=draft.binding_id, path=draft.path).first()


@portal_bp.route("/author/<draft_id>/save", methods=["POST"])
@role_required(*EDIT_ROLES)
def author_save(draft_id):
    from ..execution import AuthoringDraft
    draft = db.session.get(AuthoringDraft, draft_id)
    if draft is None or draft.owner != current_user.username:
        abort(404)
    _project_or_404(draft.project_id)
    PlainForm().validate_on_submit()
    refs = [x.strip() for x in request.form.getlist("case_refs")
            if x.strip()]
    draft.members_json = json.dumps(list(dict.fromkeys(refs)))
    name = request.form.get("name", "").strip()
    if name:
        draft.name = name
    # per-member enabled state: enabled[] mirrors case_refs order
    enabled = set(request.form.getlist("member_enabled"))
    cfg = draft.member_cfg()
    for ref in draft.members():
        cfg.setdefault(ref, {})["isRun"] = (
            "true" if ref in enabled else "false")
    # collection per-member config: runEnabled/profileName/
    # runConfigurationId are posted as membercfg_<idx>_<field> for
    # each selected ref in order.
    if draft.kind == "collection":
        for i, ref in enumerate(draft.members()):
            cfg.setdefault(ref, {})
            cfg[ref]["runEnabled"] = (
                "true" if request.form.get(
                    f"membercfg_{i}_runEnabled") else "false")
            for f in ("profileName", "runConfigurationId"):
                v = request.form.get(f"membercfg_{i}_{f}")
                if v:
                    cfg[ref][f] = v
    draft.member_cfg_json = json.dumps(cfg)
    # suite-level editable settings (verified .ts fields)
    st = draft.settings()
    for k in ("description", "tag", "isRerun", "numberOfRerun",
              "rerunFailedTestCasesOnly", "rerunImmediately",
              "pageLoadTimeout", "pageLoadTimeoutDefault",
              "delayBetweenInstances", "executionMode",
              "maxConcurrentInstances"):
        v = request.form.get(f"setting_{k}")
        if v is not None and v != "":
            st[k] = v
    for k in ("isRerun", "rerunFailedTestCasesOnly",
              "rerunImmediately", "pageLoadTimeoutDefault"):
        if request.form.get(f"setting_{k}") is None:
            st[k] = "false"
    draft.settings_json = json.dumps(st)
    db.session.commit()
    record_audit(current_user.username, "author.draft_saved", draft.id)
    item = _draft_item(draft)
    return redirect(url_for("portal.author_item",
                            item_id=item.id if item else "",
                            draft_id=draft.id))


@portal_bp.route("/author/<draft_id>/publish", methods=["POST"])
@role_required(*EDIT_ROLES)
def author_publish(draft_id):
    from .. import authoring
    from ..execution import AuthoringDraft
    draft = db.session.get(AuthoringDraft, draft_id)
    if draft is None or draft.owner != current_user.username:
        abort(404)
    _project_or_404(draft.project_id)
    PlainForm().validate_on_submit()
    res = authoring.publish_draft(
        draft, request.form.get("review_sig", ""))
    if res.get("error"):
        flash(f"Publish failed: {res['error']}", "error")
    else:
        flash("Published to the authoring branch — syncing "
              "inventory.", "info")
        record_audit(current_user.username, "author.published",
                     draft.id)
        # publish -> auto-sync: the new/edited definition appears
        # under Repository Suites without a manual Sync click.
        db.session.add(OutboxEvent(
            kind="git-sync",
            payload_json=json.dumps(
                {"binding_id": draft.binding_id})))
        db.session.commit()
    item = _draft_item(draft)
    return redirect(url_for("portal.author_item",
                            item_id=item.id if item else "",
                            draft_id=draft.id))


@portal_bp.route("/author/<draft_id>/abandon", methods=["POST"])
@role_required(*EDIT_ROLES)
def author_abandon(draft_id):
    from ..execution import AuthoringDraft
    draft = db.session.get(AuthoringDraft, draft_id)
    if draft is None or draft.owner != current_user.username:
        abort(404)
    _project_or_404(draft.project_id)
    PlainForm().validate_on_submit()
    draft.status = "abandoned"
    db.session.commit()
    record_audit(current_user.username, "author.abandoned", draft.id)
    return redirect(url_for("portal.project_detail",
                          project_id=draft.project_id))


@portal_bp.route("/executions")
@login_required
def executions():
    """Execution history — server-side filters + bounded pagination.

    Filters: id-prefix, project (visibility-scoped), created date range,
    lifecycle state, outcome, runner, schedule linkage. Stable ordering
    (created_at,id desc); filters survive page turns via query args."""
    q = Execution.query
    # visibility scoping at the QUERY level (not post-page filtering) —
    # the page count and totals stay accurate for scoped users.
    allowed_projects = json.loads(current_user.allowed_projects or "[]")
    if allowed_projects:
        q = q.filter(Execution.project_id.in_(allowed_projects))
    args = request.args
    if args.get("q", "").strip():
        q = q.filter(Execution.id.startswith(args["q"].strip()[:32]))
    if args.get("project_id", ""):
        q = q.filter(Execution.project_id == args["project_id"])
    if args.get("state", ""):
        try:
            q = q.filter(Execution.state ==
                         ExecutionState(args["state"]))
        except ValueError:
            pass
    if args.get("outcome", ""):
        try:
            q = q.filter(Execution.outcome == Outcome(args["outcome"]))
        except ValueError:
            pass
    if args.get("runner_id", ""):
        q = q.filter(Execution.runner_id == args["runner_id"])
    if args.get("schedule_id", ""):
        q = q.filter(Execution.schedule_id == args["schedule_id"])
    if args.get("from_date", ""):
        try:
            q = q.filter(Execution.created_at >=
                         datetime.strptime(args["from_date"],
                                           "%Y-%m-%d"))
        except ValueError:
            pass
    if args.get("to_date", ""):
        try:
            q = q.filter(Execution.created_at <
                         datetime.strptime(args["to_date"],
                                           "%Y-%m-%d")
                         + timedelta(days=1))
        except ValueError:
            pass
    page = max(1, int(args.get("page", 1) or 1))
    per = 50
    total = q.count()
    rows = (q.order_by(Execution.created_at.desc(), Execution.id.desc())
            .offset((page - 1) * per).limit(per).all())
    visible = [e for e in rows
               if current_user.project_allowed(e.project_id)]
    page_args = {k: v for k, v in args.items()
                 if k != "page" and v != ""}
    from ..execution import Runner as _R
    return render_template(
        "portal/executions.html", executions=visible,
        filters=args, total=total, page=page, per=per,
        page_args=page_args,
        states=[s.value for s in ExecutionState],
        outcomes=[o.value for o in Outcome],
        projects=Project.query.order_by(Project.name).all(),
        runners=_R.query.order_by(_R.name).all(),
        schedules=Schedule.query.order_by(Schedule.name).all(),
        plain_form=PlainForm(),
        can_run=current_user.role.value in
        ("admin", "test_manager", "tester"))


@portal_bp.route("/executions/bulk-cancel", methods=["POST"])
@role_required(*RUN_ROLES)
def bulk_cancel_executions():
    """Bulk cancellation of explicitly selected executions via the
    existing request_cancel path — per-item auth + eligibility,
    per-item honest results. No 'all matching pages' scope in v1."""
    PlainForm().validate_on_submit()
    ids = list(dict.fromkeys(request.form.getlist("ex_ids")))
    if not ids:
        flash("No executions selected.", "error")
        return redirect(url_for("portal.executions"))
    results = {"canceled": [], "skipped": [], "denied": [], "failed": []}
    for eid in ids[:50]:                        # bounded selection
        ex = db.session.get(Execution, eid)
        if ex is None:
            results["failed"].append(eid[:8]); continue
        if not current_user.project_allowed(ex.project_id):
            results["denied"].append(eid[:8]); continue
        if (current_user.role == Role.TESTER
                and ex.created_by != current_user.username):
            results["denied"].append(eid[:8]); continue
        try:
            ok = dispatch.request_cancel(ex.id, current_user.username,
                                         "bulk-cancel")
            results["canceled" if ok else "skipped"].append(eid[:8])
        except Exception:
            results["failed"].append(eid[:8])
    record_audit(current_user.username, "execution.bulk_cancel",
                 f"{len(ids)} selected: "
                 f"{len(results['canceled'])} accepted")
    parts = [f"{k}: {len(v)}" for k, v in results.items() if v]
    flash("Bulk cancel — " + "; ".join(parts) +
          ". A request is queued — it does NOT confirm a remote "
          "process has stopped.", "info")
    # Return to the SAME filtered view the user selected from —
    # caller-controlled ret is whitelisted to known filter keys and
    # string values only; anything else falls back to the plain page.
    ret = {}
    try:
        _r = json.loads(request.form.get("ret", "{}") or "{}")
        ret = {k: str(v)[:128] for k, v in _r.items()
               if k in ("q", "project_id", "state", "outcome",
                        "runner_id", "schedule_id", "from_date",
                        "to_date", "page") and isinstance(v, str)}
    except Exception:
        pass
    return redirect(url_for("portal.executions", **ret))


@portal_bp.route("/projects/<project_id>/releases", methods=["POST"])
@role_required(*EDIT_ROLES)
def create_release(project_id):
    project = _project_or_404(project_id)
    name = request.form.get("name", "").strip()
    if name:
        rel = Release(project_id=project.id, name=name,
                      description=request.form.get(
                          "description", ""),
                      start_date=_parse_date(
                          request.form.get("start_date")),
                      end_date=_parse_date(
                          request.form.get("end_date")))
        if (rel.start_date and rel.end_date and
                rel.end_date < rel.start_date):
            flash("Target end date must be on or after the "
                  "start date.", "error")
            return redirect(url_for("portal.project_detail",
                                  project_id=project.id))
        db.session.add(rel)
        db.session.commit()
        record_audit(current_user.username, "release.created",
                     rel.id)
        return redirect(url_for("portal.release_detail",
                              release_id=rel.id))
    return redirect(url_for("portal.project_detail",
                          project_id=project_id))


@portal_bp.route("/releases/<release_id>")
@login_required
def release_detail(release_id):
    release = db.session.get(Release, release_id)
    if release is None:
        abort(404)
    _project_or_404(release.project_id)
    items = ReleaseItem.query.filter_by(release_id=release.id).all()
    runs = Execution.query.filter_by(release_id=release.id).order_by(
        Execution.created_at.desc()).all()
    # scope summary vs historical totals — separate counts.
    # latest keyed by target + execution configuration — a pass under
    # one profile/runtime must not mask a failure under another.
    def _cfg(e):
        c = e.snapshot().get("run_config", {})
        return (c.get("profile", ""), c.get("browser", ""),
                c.get("runner_id", ""), c.get("runtime_id", ""))
    latest_by_path = {}
    for e in runs:
        st = e.snapshot().get("source_target", {})
        tgt = st.get("path", "")
        key = (tgt, st.get("kind", ""), st.get("binding_id", ""),
               _cfg(e))
        if tgt and key not in latest_by_path:
            latest_by_path[key] = e
    scope_rows = []
    unexecuted = 0
    unavailable = 0
    for it in items:
        src = (db.session.get(DiscoveredItem, it.source_item_id)
               if it.source_item_id else None)
        missing = src is None and it.source_item_id
        latest = next((e for (t, _k, _b, _c), e in
                       latest_by_path.items() if t == it.path), None)
        if missing:
            unavailable += 1
            state = "unavailable — definition removed from repository"
        elif latest is None:
            unexecuted += 1
            state = "not yet executed"
        else:
            state = latest.outcome.value
        scope_rows.append({"item": it, "state": state,
                           "missing": bool(missing),
                           "latest": latest})
    avail = DiscoveredItem.query.filter_by(
        binding_id=GitBinding.query.filter_by(
            project_id=release.project_id).first().id if
        GitBinding.query.filter_by(project_id=release.project_id).first()
        else "").all()
    return render_template(
        "portal/release_detail.html", release=release, items=items,
        runs=runs, scope_rows=scope_rows,
        unexecuted=unexecuted, unavailable=unavailable,
        avail=avail, plain_form=PlainForm())


def _parse_date(s):
    from datetime import date as _d
    if not s:
        return None
    try:
        return _d.fromisoformat(s.strip())
    except ValueError:
        return None


def _release_export(release, items, binding=None):
    """Portable release definition — repo paths + binding name, no
    DB ids, no secrets or machine paths. Per-run configuration is
    NOT exported — it is a launch-time choice, not part of the
    release definition."""
    return {
        "type": "testorchestrator-release",
        "version": 1,
        "name": release.name,
        "description": release.description,
        "status": release.status,
        "start_date": (release.start_date.isoformat()
                       if release.start_date else None),
        "end_date": (release.end_date.isoformat()
                     if release.end_date else None),
        "binding": binding.name if binding else "",
        "scope": [{"kind": i.kind, "path": i.path} for i in items]}


def _release_sync_sig(release, items, binding, path, base=""):
    h = hashlib.sha256()
    h.update(json.dumps(
        _release_export(release, items, binding),
        sort_keys=True).encode())
    h.update(path.encode())
    h.update(binding.id.encode())
    h.update(base.encode())
    return h.hexdigest()


@portal_bp.route("/releases/<release_id>/sync-preview")
@role_required(*EDIT_ROLES)
def release_sync_preview(release_id):
    from .. import git_backend
    release = db.session.get(Release, release_id)
    if release is None:
        abort(404)
    _project_or_404(release.project_id)
    items = ReleaseItem.query.filter_by(release_id=release.id).all()
    binding = GitBinding.query.filter_by(
        project_id=release.project_id, authoring_writable=True).first()
    # cross-binding: scope items may reference defs on other bindings
    # — their paths don't resolve in the destination repo.
    bindings = {db.session.get(DiscoveredItem, i.source_item_id).binding_id
                for i in items
                if i.source_item_id and
                db.session.get(DiscoveredItem, i.source_item_id)}
    if len(bindings) > 1:
        flash("Release scope spans multiple bindings — sync exports "
              "only to the single binding the scope targets.", "error")
        return redirect(url_for("portal.release_detail",
                              release_id=release.id))
    export = _release_export(release, items, binding)
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", release.name)
    path = (release.sync_path or
            f"TestOrchestration/release/{slug}.json")
    base = ""
    if binding and binding.worktree_path:
        try:
            base = git_backend.run(
                "resolve", Path(binding.worktree_path), "HEAD") or ""
        except Exception:
            pass
    sig = (_release_sync_sig(release, items, binding, path, base)
           if binding else "")
    return render_template(
        "portal/release_sync.html", release=release, export=export,
        path=path, binding=binding, sig=sig, base=base,
        plain_form=PlainForm())


@portal_bp.route("/releases/<release_id>/sync", methods=["POST"])
@role_required(*EDIT_ROLES)
def release_sync(release_id):
    """Write the release definition to TestOrchestration/release/ in
    the authoring branch — gated by authoring_writable, remote-conflict
    checked, and commits only on change."""
    release = db.session.get(Release, release_id)
    if release is None:
        abort(404)
    _project_or_404(release.project_id)
    PlainForm().validate_on_submit()
    binding = db.session.get(GitBinding,
                             request.form.get("binding_id"))
    if binding is None or binding.project_id != release.project_id:
        abort(404)
    if not binding.authoring_writable:
        flash("Source authoring is not enabled on that binding.",
              "error")
        return redirect(url_for("portal.release_detail",
                              release_id=release.id))
    from .. import authoring, git_backend
    items = ReleaseItem.query.filter_by(release_id=release.id).all()
    export = _release_export(release, items, binding)
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", release.name)
    path = (release.sync_path or
            f"TestOrchestration/release/{slug}.json")
    if request.form.get("sig") != _release_sync_sig(
            release, items, binding, path,
            request.form.get("base", "")):
        flash("Release changed since preview — review again.",
              "error")
        return redirect(url_for("portal.release_sync_preview",
                              release_id=release.id))
    wt = authoring._authoring_wt(binding.id)
    f = wt / path
    content = json.dumps(export, indent=2, sort_keys=True) + "\n"
    if f.exists() and f.read_text() == content:
        flash("Already in sync — no change.", "info")
        return redirect(url_for("portal.release_detail",
                              release_id=release.id))
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    git_backend.run("commit", wt, path,
                    message=f"Release {release.name}: sync")
    release.sync_binding_id = binding.id
    release.sync_path = path
    release.synced_sha = git_backend.run(
        "resolve", wt, "HEAD") or ""
    release.sync_dirty = False
    db.session.commit()
    record_audit(current_user.username, "release.synced", release.id)
    flash(f"Synced {release.name} -> {path}", "info")
    return redirect(url_for("portal.release_detail",
                          release_id=release.id))


@portal_bp.route("/releases/<release_id>/scope", methods=["POST"])
@role_required(*EDIT_ROLES)
def release_scope_add(release_id):
    release = db.session.get(Release, release_id)
    if release is None:
        abort(404)
    _project_or_404(release.project_id)
    PlainForm().validate_on_submit()
    item_id = request.form.get("item_id", "")
    item = db.session.get(DiscoveredItem, item_id)
    if item and item.kind in ("suite", "collection"):
        if not ReleaseItem.query.filter_by(
                release_id=release.id, path=item.path).first():
            db.session.add(ReleaseItem(
                release_id=release.id, kind=item.kind,
                path=item.path, source_item_id=item.id))
            release.sync_dirty = True
            db.session.commit()
    return redirect(url_for("portal.release_detail",
                          release_id=release.id))


@portal_bp.route("/releases/<release_id>/scope/<item_id>/remove",
                 methods=["POST"])
@role_required(*EDIT_ROLES)
def release_scope_remove(release_id, item_id):
    release = db.session.get(Release, release_id)
    if release is None:
        abort(404)
    _project_or_404(release.project_id)
    PlainForm().validate_on_submit()
    it = db.session.get(ReleaseItem, item_id)
    if it and it.release_id == release.id:
        db.session.delete(it)
        release.sync_dirty = True
        db.session.commit()
    return redirect(url_for("portal.release_detail",
                          release_id=release.id))


@portal_bp.route("/executions/<execution_id>/link-release",
                 methods=["POST"])
@role_required(*EDIT_ROLES)
def link_execution_release(execution_id):
    """Link or unlink an existing project execution to a release."""
    ex = db.session.get(Execution, execution_id)
    if ex is None:
        abort(404)
    _project_or_404(ex.project_id)
    PlainForm().validate_on_submit()
    rel = request.form.get("release_id") or None
    if rel:
        release = db.session.get(Release, rel)
        if release is None or release.project_id != ex.project_id:
            abort(404)
    ex.release_id = rel
    db.session.commit()
    record_audit(current_user.username, "execution.link_release",
                 ex.id)
    return redirect(url_for("portal.execution_detail",
                          execution_id=ex.id))


@portal_bp.route("/releases/<release_id>/status", methods=["POST"])
@role_required(*EDIT_ROLES)
def release_status(release_id):
    release = db.session.get(Release, release_id)
    if release is None:
        abort(404)
    _project_or_404(release.project_id)
    PlainForm().validate_on_submit()
    st = request.form.get("status", "")
    if st in ("draft", "in_progress", "released", "archived"):
        release.status = st
        release.sync_dirty = True
        if st == "released":
            release.released_at = utcnow()
        elif release.released_at:
            release.released_at = None   # reopened — re-release restamps
        db.session.commit()
        record_audit(current_user.username, "release.status", release.id)
    return redirect(url_for("portal.release_detail",
                          release_id=release.id))


@portal_bp.route("/releases/<release_id>/run", methods=["POST"])
@role_required(*RUN_ROLES)
def run_release(release_id):
    """Queue executions for every in-scope definition, linked to the
    release — same profile/worker/runtime flow as a direct run."""
    release = db.session.get(Release, release_id)
    if release is None:
        abort(404)
    _project_or_404(release.project_id)
    PlainForm().validate_on_submit()
    items = ReleaseItem.query.filter_by(release_id=release.id).all()
    n = 0
    for it in items:
        src = db.session.get(DiscoveredItem, it.source_item_id) \
            if it.source_item_id else None
        path = src.path if src else it.path
        ex = Execution(
            project_id=release.project_id,
            release_id=release.id,
            suite_id=None, source_item_id=it.source_item_id,
            source_sha=(src.sha if src else ""),
            outcome=Outcome.NONE,
            snapshot_json=json.dumps({
                "source_target": {"kind": it.kind, "path": path,
                                  "binding_id": (src.binding_id
                                                 if src else "")}}),
            config_json=json.dumps({
                "profile": request.form.get("profile", "default"),
                "browser": request.form.get("browser",
                                           "Chrome (headless)"),
                "runner_id": request.form.get("runner_id", ""),
                "runtime_id": request.form.get("runtime_id", ""),
                "timeout_seconds": 3600}),
            created_by=current_user.username)
        db.session.add(ex); n += 1
        db.session.flush()
        db.session.add(OutboxEvent(
            kind="local-dispatch",
            payload_json=json.dumps({"execution_id": ex.id})))
    db.session.commit()
    record_audit(current_user.username, "release.run", release.id)
    flash(f"Queued {n} execution{'s' if n != 1 else ''} for "
          f"{release.name}.", "info")
    return redirect(url_for("portal.release_detail",
                          release_id=release.id))


@portal_bp.route("/releases/<release_id>/edit", methods=["POST"])
@role_required(*EDIT_ROLES)
def edit_release(release_id):
    release = db.session.get(Release, release_id)
    if release is None:
        abort(404)
    _project_or_404(release.project_id)
    name = request.form.get("name", "").strip()
    if name:
        release.name = name
        release.description = request.form.get("description", "")
        sd = _parse_date(request.form.get("start_date"))
        ed = _parse_date(request.form.get("end_date"))
        if sd and ed and ed < sd:
            flash("Target end date must be on or after the "
                  "start date.", "error")
            return redirect(url_for("portal.release_detail",
                                  release_id=release.id))
        release.start_date = sd
        release.end_date = ed
        release.sync_dirty = True
        db.session.commit()
        record_audit(current_user.username, "release.edited",
                     release.id)
    return redirect(url_for("portal.project_detail",
                          project_id=release.project_id))


@portal_bp.route("/suites/<suite_id>/run", methods=["POST"])
@role_required(*RUN_ROLES)
def launch_execution(suite_id):
    suite = db.session.get(Suite, suite_id)
    if suite is None:
        abort(404)
    _project_or_404(suite.project_id)
    PlainForm().validate_on_submit()
    # Submission revalidation: the required subsystem must be
    # operational now — a verified setup does not imply the worker is
    # still connected. Names the missing component (409 semantics are
    # flash + redirect here, consistent with other form rejects).
    from .. import readiness
    missing = readiness.operational_check()
    if missing:
        flash(f"Setup incomplete: {missing}", "error")
        return redirect(url_for("setup.index"))
    binding = GitBinding.query.filter_by(
        project_id=suite.project_id).first()
    sha = binding.last_synced_sha if binding else ""
    kre_options = {}
    try:
        retry_n = int(request.form.get("kre_retry", "0") or 0)
    except ValueError:
        retry_n = 0
    if retry_n > 0:
        kre_options["retry"] = min(3, retry_n)
    run_config = {
        "profile": request.form.get("execution_profile") or "default",
        "browser": request.form.get("browser_type") or "Chrome (headless)",
        "remote_webdriver_url":
            request.form.get("remote_webdriver_url", "").strip(),
        "remote_webdriver_type":
            request.form.get("remote_webdriver_type", "").strip(),
        "timeout_seconds":
            int(request.form.get("timeout_seconds") or 0),
        # Explicit execution target ("" = auto). Re-validated at
        # dispatch — never falls back to another target.
        "runner_id": request.form.get("runner_id", "").strip(),
        "source_item": request.form.get("source_item", "").strip(),
        "runtime_id": request.form.get("runtime_id", "").strip(),
        "kre_options": kre_options,
    }
    # Resolve the submitted discovered item SERVER-SIDE: kind comes
    # from the record — never trust a submitted type flag.
    si = run_config.pop("source_item", "")
    if si:
        from ..execution import DiscoveredItem
        item = db.session.get(DiscoveredItem, si)
        if (item is None or item.project_id != suite.project_id
                or item.kind not in ("suite", "collection")):
            flash("Selected source definition is invalid for this "
                  "project.", "error")
            return redirect(url_for("portal.suite_detail",
                                  suite_id=suite_id,
                                  release_id=(request.form.get(
                                      "release_id") or None)))
        run_config["suite_path"] = item.path
        run_config["collection"] = (item.kind == "collection")
    ex = dispatch.create_execution(
        current_user.username, suite, binding=binding, source_sha=sha,
        run_config=run_config)
    record_audit(current_user.username, "execution.queued", ex.id)
    flash(f"Execution {ex.id[:8]} queued.", "info")
    return redirect(url_for("portal.execution_detail", execution_id=ex.id))


@portal_bp.route("/executions/<execution_id>")
@login_required
def execution_detail(execution_id):
    can_edit = current_user.role.value in ("admin", "test_manager")
    ex = _execution_or_404(execution_id)
    counts = {}
    for r in ex.results:
        counts[r.status.value] = counts.get(r.status.value, 0) + 1
    return render_template(
        "portal/execution_detail.html", ex=ex, counts=counts,
        plain_form=PlainForm(), can_edit=can_edit)


@portal_bp.route("/executions/<execution_id>/cancel", methods=["POST"])
@login_required
def cancel_execution(execution_id):
    ex = _execution_or_404(execution_id)
    PlainForm().validate_on_submit()
    # Tester cancels only their own; Admin/TM cancel any (§16).
    if current_user.role == Role.TESTER and ex.created_by != current_user.username:
        abort(403)
    if current_user.role == Role.PO:
        abort(403)
    if dispatch.request_cancel(ex.id, current_user.username,
                               request.form.get("reason", "")):
        flash("Cancellation requested — pending until the process tree "
              "confirms termination.", "info")
    else:
        flash("Execution is already in a terminal state.", "info")
    return redirect(url_for("portal.execution_detail",
                          execution_id=execution_id))


@portal_bp.route("/executions/<execution_id>/resolve-unknown",
                 methods=["POST"])
@role_required(Role.ADMIN)
def resolve_unknown(execution_id):
    """Operator-confirmed resolution of an UNKNOWN execution.

    UNKNOWN means 'uncertain — original process may still be running'
    and the execution HOLDS its runner slot. This action is the
    operator's recorded confirmation that the original is verified
    stopped — it marks the execution FAILED and releases capacity.
    It never marks a run successful and never auto-retries."""
    ex = _execution_or_404(execution_id)
    PlainForm().validate_on_submit()
    confirm = request.form.get("confirm_stopped") == "yes"
    reason = (request.form.get("reason") or "").strip()
    if not confirm:
        flash("Confirmation required — resolving UNKNOWN does NOT stop "
              "a process; verify the original process and its children "
              "have stopped on the execution host before releasing its "
              "slot.", "error")
        return redirect(url_for("portal.execution_detail",
                              execution_id=execution_id))
    if not reason:
        flash("A confirmation note is required — record how you "
              "verified the original process stopped.", "error")
        return redirect(url_for("portal.execution_detail",
                              execution_id=execution_id))
    # CAS: only an execution still UNKNOWN can be resolved — a
    # concurrent terminal transition or double-resolve cannot
    # overwrite a newer state.
    n = db.session.query(Execution).filter(
        Execution.id == ex.id,
        Execution.state == ExecutionState.UNKNOWN,
    ).update({"state": ExecutionState.FAILED,
              "outcome": Outcome.INCOMPLETE,
              "finished_at": utcnow(),
              "error_text": (db.func.substr(Execution.error_text, 1, 1400) +
                             " | ADMIN-RESOLVED by " +
                             current_user.username + ": " + reason[:500])},
             synchronize_session=False)
    db.session.commit()
    if not n:
        db.session.rollback()
        flash("Execution is no longer UNKNOWN — its state already "
              "resolved elsewhere.", "info")
        return redirect(url_for("portal.execution_detail",
                              execution_id=execution_id))
    record_audit(current_user.username, "execution.unknown_resolved",
                 f"{ex.id}: {reason[:400]}")
    flash("Execution resolved — capacity released. Retry only when "
          "safe.", "info")
    return redirect(url_for("portal.execution_detail",
                          execution_id=execution_id))


@portal_bp.route("/executions/<execution_id>/retry", methods=["POST"])
@role_required(*RUN_ROLES)
def retry_execution(execution_id):
    """Queue the next attempt — a NEW execution linked to this one
    (attempt evidence chain, never a rerun/mutation of the original)."""
    ex = _execution_or_404(execution_id)
    PlainForm().validate_on_submit()
    try:
        new_ex = dispatch.retry_execution(current_user.username, ex)
    except dispatch.DispatchError as exc:
        flash(str(exc), "error")
        return redirect(url_for("portal.execution_detail",
                                execution_id=execution_id))
    return redirect(url_for("portal.execution_detail",
                          execution_id=new_ex.id))


@portal_bp.route("/executions/<execution_id>/artifacts/<artifact_id>")
@login_required
def download_artifact(execution_id, artifact_id):
    """Authorized evidence download (§9). Active content is never served
    inline in the authenticated portal origin — it downloads only."""
    from ..execution import Artifact
    ex = _execution_or_404(execution_id)
    artifact = db.session.get(Artifact, artifact_id)
    if artifact is None or artifact.execution_id != execution_id:
        abort(404)
    root = Path(ex.report_dir or "")
    target = (root / artifact.rel_path).resolve()
    if not str(target).startswith(str(root.resolve()) + "/") or not target.is_file():
        abort(404)
    # Checksum re-verified on every download — stale evidence is refused.
    from ..results import sha256_file
    if sha256_file(target) != artifact.sha256:
        abort(409)
    record_audit(current_user.username, "artifact.download", artifact.id)
    mime = artifact.mime or "application/octet-stream"
    if artifact.mime in ("text/html", "image/svg+xml"):
        return send_file(target, as_attachment=True,
                         download_name=target.name)
    return send_file(target, mimetype=mime,
                     download_name=target.name, as_attachment=True)


@portal_bp.route("/executions/<execution_id>/report")
@login_required
def view_report(execution_id):
    """Hosted native-KRE report view. The entry HTML is active content:
    it is served inside a CSP sandbox (scripts disabled) so evidence
    can never execute in the authenticated portal origin."""
    ex = _execution_or_404(execution_id)
    from .. import reporting
    if ex.report_status in ("pending", "missing", "partial"):
        reporting.refresh_report_status(ex)
    if ex.report_status != "ready" or not ex.report_path:
        flash(f"No viewable report for this run "
              f"(status: {ex.report_status}).", "info")
        return redirect(url_for("portal.execution_detail",
                                execution_id=execution_id))
    try:
        target = reporting.safe_under(reporting.report_root(ex),
                                      ex.report_path)
    except ValueError:
        abort(404)
    if not target.is_file():
        abort(404)
    record_audit(current_user.username, "report.view", execution_id)
    resp = send_file(target, mimetype="text/html")
    resp.headers["Content-Security-Policy"] = "sandbox"
    return resp


@portal_bp.route("/executions/<execution_id>/report/files/<path:rel>")
@login_required
def report_asset(execution_id, rel):
    """Report assets (css/js/images) — resolved strictly inside the
    entry's own subtree of THIS run's evidence dir."""
    ex = _execution_or_404(execution_id)
    from .. import reporting
    try:
        target = reporting.safe_under(reporting.entry_dir(ex), rel)
    except ValueError:
        abort(404)
    if not target.is_file():
        abort(404)
    mime, _ = mimetypes.guess_type(target.name)
    if mime in ("text/html", "image/svg+xml"):  # active content policy
        return send_file(target, as_attachment=True,
                         download_name=target.name)
    return send_file(target, mimetype=mime or "application/octet-stream")


@portal_bp.route("/executions/<execution_id>/report.zip")
@login_required
def download_report(execution_id):
    """Usable report download: the entry subtree (html + assets) with a
    checksum manifest — not a bare index.html."""
    ex = _execution_or_404(execution_id)
    from .. import reporting
    if ex.report_status != "ready":
        reporting.refresh_report_status(ex)
    if ex.report_status != "ready":
        abort(404)
    record_audit(current_user.username, "report.download", execution_id)
    return send_file(reporting.build_report_zip(ex),
                     mimetype="application/zip", as_attachment=True,
                     download_name=f"report-{execution_id[:8]}.zip")


@portal_bp.route("/executions/<execution_id>/artifacts.zip")
@login_required
def download_artifacts(execution_id):
    """Original evidence download — every recorded artifact as collected."""
    ex = _execution_or_404(execution_id)
    from .. import reporting
    if not ex.artifacts:
        abort(404)
    record_audit(current_user.username, "artifacts.download",
                 execution_id)
    return send_file(reporting.build_artifacts_zip(ex),
                     mimetype="application/zip", as_attachment=True,
                     download_name=f"artifacts-{execution_id[:8]}.zip")


@portal_bp.route("/executions/<execution_id>/allure")
@login_required
def allure_redirect(execution_id):
    # Trailing slash so ./app-*.js ./data/… resolve under /allure/
    return redirect(
        url_for("portal.view_allure", execution_id=execution_id))


@portal_bp.route("/executions/<execution_id>/allure/")
@login_required
def view_allure(execution_id):
    """Hosted Allure 3 report — sandboxed multi-file app."""
    ex = _execution_or_404(execution_id)
    if ex.allure_status != "ready" or not ex.allure_path:
        flash(f"No Allure report for this run "
              f"(status: {ex.allure_status}).", "info")
        return redirect(url_for("portal.execution_detail",
                                execution_id=execution_id))
    from .. import reporting
    try:
        target = reporting.safe_under(reporting.report_root(ex),
                                      ex.allure_path)
    except ValueError:
        abort(404)
    if not target.is_file():
        abort(404)
    record_audit(current_user.username, "allure.view", execution_id)
    resp = send_file(target, mimetype="text/html")
    # Allure 3 is a multi-file interactive app — sandbox it (no portal
    # session/API reach) but allow its scripts + same-origin asset
    # fetches. Bare 'sandbox' made the page an inert loading screen.
    resp.headers["Content-Security-Policy"] =         "sandbox allow-scripts allow-same-origin"
    return resp


@portal_bp.route("/executions/<execution_id>/allure/<path:rel>")
@login_required
def allure_asset(execution_id, rel):
    """Allure 3 report assets — js/css/data/plugins under the allure
    subtree. Served inline so the hosted report actually renders."""
    ex = _execution_or_404(execution_id)
    if ex.allure_status != "ready" or not ex.allure_path:
        abort(404)
    from .. import reporting
    try:
        allure_dir = (reporting.report_root(ex)
                      / Path(ex.allure_path).parent)
        target = reporting.safe_under(allure_dir.resolve(), rel)
    except ValueError:
        abort(404)
    if not target.is_file():
        abort(404)
    mime, _ = mimetypes.guess_type(target.name)
    return send_file(target, mimetype=mime or "application/octet-stream")


@portal_bp.route("/executions/<execution_id>/allure.zip")
@login_required
def download_allure(execution_id):
    """Usable Allure download — the self-contained single-file report
    (index.html embeds assets) plus a provenance manifest."""
    import io
    import zipfile
    ex = _execution_or_404(execution_id)
    from .. import reporting
    if ex.allure_status != "ready":
        abort(404)
    target = reporting.safe_under(reporting.report_root(ex),
                                  ex.allure_path)
    if not target.is_file():
        abort(404)
    allure_dir = reporting.safe_under(reporting.report_root(ex),
                                      str(Path(ex.allure_path).parent))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(allure_dir.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(allure_dir))
        z.writestr("manifest.json", json.dumps({
            "execution_id": ex.id, "format": "allure-multi-file",
            "allure_version": ex.allure_version,
            "view": "extract, then open index.html — assets are "
                    "separate files"}, indent=2))
    buf.seek(0)
    record_audit(current_user.username, "allure.download", execution_id)
    return send_file(buf, mimetype="application/zip",
                     as_attachment=True,
                     download_name=f"allure-{execution_id[:8]}.zip")


@portal_bp.route("/executions/<execution_id>/allure/generate",
                 methods=["POST"])
@login_required
def generate_allure(execution_id):
    """Regenerate Allure from STORED results — never reruns KRE, never
    duplicates the run. Independent of RP and native-report status."""
    ex = _execution_or_404(execution_id)
    PlainForm().validate_on_submit()
    from ..adapters import allure as allure_adapter
    res = allure_adapter.generate_for(ex)
    flash(f"Allure generation: {res['status']}"
          + (f" — {res.get('error')}" if res.get("error") else ""),
          "info" if res["status"] == "ready" else "error")
    return redirect(url_for("portal.execution_detail",
                            execution_id=execution_id))


@portal_bp.route("/executions/<execution_id>/report/refresh",
                 methods=["POST"])
@login_required
def refresh_report(execution_id):
    """Safe retry: re-scan the run's evidence dir, register any artifacts
    not yet recorded, re-assess report status. Never reruns KRE, never
    duplicates or overwrites existing rows."""
    ex = _execution_or_404(execution_id)
    PlainForm().validate_on_submit()
    from .. import reporting
    try:
        added = reporting.register_missing_artifacts(ex)
        status = reporting.refresh_report_status(ex)
        flash(f"Report collection refreshed: {added} artifact(s) "
              f"registered, status {status}.", "info")
    except Exception as exc:
        reporting.refresh_failed(ex, exc)
        flash(f"Report collection failed: {exc}", "error")
    return redirect(url_for("portal.execution_detail",
                            execution_id=execution_id))


@portal_bp.route("/executions/<execution_id>/logs/<name>")
@login_required
def view_log(execution_id, name):
    """stdout/stderr served as text/plain — never rendered as HTML."""
    ex = _execution_or_404(execution_id)
    if name not in ("kre-stdout.log", "kre-stderr.log",
                    "kre-invocation.log"):
        abort(404)
    path = Path(ex.workspace_path or "") / "logs" / name
    if not path.is_file():
        # Remote executions: logs arrive as uploaded evidence artifacts.
        path = Path(ex.report_dir or "") / "logs" / name
    if not path.is_file():
        abort(404)
    record_audit(current_user.username, "log.view", f"{execution_id}/{name}")
    return send_file(path, mimetype="text/plain")


# ---------- plans & releases (definition level, §10) ----------



@portal_bp.route("/projects/<project_id>/schedules", methods=["POST"])
@role_required(*EDIT_ROLES)
def create_schedule(project_id):
    from .forms import ScheduleForm
    project = _project_or_404(project_id)
    form = ScheduleForm()
    form.target_id.choices = [
        (s.id, f"Suite: {s.name}") for s in project.suites]
    if form.validate_on_submit():
        kind = form.kind.data
        run_at = _parse_run_at(form.run_at.data)
        interval = max(5, form.interval_minutes.data or 60)
        if kind == "interval" and run_at is None:
            run_at = utcnow()
        if run_at is None:
            flash("First run time is required (UTC, YYYY-MM-DD HH:MM).",
                  "error")
            return redirect(url_for("portal.project_detail",
                                    project_id=project_id))
        db.session.add(Schedule(
            project_id=project.id, name=form.name.data.strip(),
            description=form.description.data or "",
            target_type="suite", suite_id=form.target_id.data,
            plan_id=None,
            targets_json=json.dumps(targets), kind=kind,
            interval_minutes=interval if kind == "interval" else None,
            next_run_at=run_at, enabled=True,
            overlap_policy=form.overlap_policy.data or "allow",
            run_config_json=json.dumps({
                "profile": form.execution_profile.data or "default",
                "browser": form.browser_type.data or "Chrome (headless)",
                "remote_webdriver_url":
                    (form.remote_webdriver_url.data or "").strip(),
                "remote_webdriver_type":
                    (form.remote_webdriver_type.data or "").strip(),
                "timeout_seconds": form.timeout_seconds.data or 3600,
                "runner_id": form.runner_id.data or "",
                "runtime_id": form.runtime_id.data or "",
            }),
            created_by=current_user.username))
        db.session.commit()
        record_audit(current_user.username, "schedule.created", project.id)
    return redirect(url_for("portal.project_detail",
                          project_id=project_id))


@portal_bp.route("/schedules/<schedule_id>/toggle", methods=["POST"])
@role_required(*EDIT_ROLES)
def toggle_schedule(schedule_id):
    sched = db.session.get(Schedule, schedule_id)
    if sched is None:
        abort(404)
    _project_or_404(sched.project_id)
    PlainForm().validate_on_submit()
    sched.enabled = not sched.enabled
    if sched.enabled and sched.next_run_at is None:
        sched.next_run_at = utcnow()
    db.session.commit()
    record_audit(current_user.username, "schedule.toggled", sched.id)
    return redirect(url_for("portal.project_detail",
                          project_id=sched.project_id))


@portal_bp.route("/schedules/<schedule_id>/edit", methods=["POST"])
@role_required(*EDIT_ROLES)
def edit_schedule(schedule_id):
    """Update timing/config + repository targets — preserves history."""
    sched = db.session.get(Schedule, schedule_id)
    if sched is None:
        abort(404)
    _project_or_404(sched.project_id)
    form = ScheduleForm()
    form.validate_on_submit()
    if request.form.get("name"):
        sched.name = request.form["name"].strip()
    for f in ("run_at", "interval_minutes", "kind",
              "execution_profile", "browser_type",
              "remote_webdriver_url", "remote_webdriver_type",
              "runner_id", "runtime_id", "timeout_seconds"):
        v = request.form.get(f)
        if v is not None and v != "":
            setattr(sched, f if f != "kind" else "kind", v)
    refs = [x for x in request.form.getlist("case_refs")
            if x.startswith("repo:")]
    if refs:
        targets = []
        for ref in refs:
            d = db.session.get(DiscoveredItem, ref[5:])
            if d and d.binding.project_id == sched.project_id:
                targets.append({"kind": d.kind, "path": d.path,
                                "source_item_id": d.id,
                                "binding_id": d.binding_id})
        if targets:
            sched.targets_json = json.dumps(targets)
            sched.target_type = "suite"   # off legacy plan
            sched.plan_id = None
    db.session.commit()
    record_audit(current_user.username, "schedule.edited", sched.id)
    return redirect(url_for("portal.project_detail",
                          project_id=sched.project_id))


@portal_bp.route("/schedules/<schedule_id>/run-now", methods=["POST"])
@role_required(*RUN_ROLES)
def run_schedule_now(schedule_id):
    """Manual trigger of a saved schedule — same config as a scheduled
    firing, new immutable snapshot, actor recorded. Disabled schedules
    cannot be run-now'd (enable first); overlap 'skip' still applies
    when the previous run is active. Does NOT advance next_run_at,
    consume a 'once' schedule, or alter recurrence — the next due fire
    still happens as scheduled."""
    sched = db.session.get(Schedule, schedule_id)
    if sched is None:
        abort(404)
    _project_or_404(sched.project_id)
    PlainForm().validate_on_submit()
    if not sched.enabled:
        flash("Schedule is disabled — enable it before triggering "
              "manually.", "error")
        return redirect(url_for("portal.project_detail",
                              project_id=sched.project_id))
    if (sched.overlap_policy == "skip"
            and db.session.query(Execution).filter(
                Execution.schedule_id == sched.id,
                Execution.state.in_(dispatch._ACTIVE_STATES)).count()):
        flash("A previous run from this schedule is still active — "
              "overlap policy 'skip' refused the manual trigger.",
              "error")
        return redirect(url_for("portal.project_detail",
                              project_id=sched.project_id))
    # Submission idempotency: each form render carries a unique token;
    # a repeated submission of the SAME token maps back to the SAME
    # execution — never a second launch. The unique constraint is the
    # only race-safe barrier (concurrent requests serialize on it).
    from ..execution import SubmitToken
    tok = (request.form.get("submit_token") or "").strip()
    if tok:
        prior = SubmitToken.query.filter_by(
            schedule_id=sched.id, username=current_user.username,
            token=tok).first()
        if prior and prior.execution_id:
            flash("This submission was already processed — showing the "
                  "same execution.", "info")
            return redirect(url_for("portal.execution_detail",
                                  execution_id=prior.execution_id))
    cfg = sched.run_config()
    binding = GitBinding.query.filter_by(
        project_id=sched.project_id).first()
    sha = binding.last_synced_sha if binding else ""
    try:
        if sched.target_type == "suite" and sched.suite_id:
            suite = db.session.get(Suite, sched.suite_id)
            if suite is None:
                raise dispatch.DispatchError("scheduled suite no longer exists")
            ex = dispatch.create_execution(
                current_user.username, suite, binding=binding,
                source_sha=sha, run_config=cfg, schedule_id=sched.id)
        elif sched.target_type == "plan" and sched.plan_id:
            plan = None
            if plan is None:
                raise dispatch.DispatchError("scheduled plan no longer exists")
            _, exs = dispatch.queue_plan_executions(
                current_user.username, plan, binding=binding,
                source_sha=sha, schedule_id=sched.id)
            ex = exs[0] if exs else None
        else:
            raise dispatch.DispatchError(
                f"invalid schedule target {sched.target_type!r}")
    except dispatch.DispatchError as exc:
        flash(str(exc), "error")
        return redirect(url_for("portal.project_detail",
                              project_id=sched.project_id))
    if tok and ex:
        try:
            db.session.add(SubmitToken(
                schedule_id=sched.id, username=current_user.username,
                token=tok, execution_id=ex.id))
            db.session.commit()
        except Exception:
            db.session.rollback()
            # Concurrent identical submission won the unique key — its
            # token row already points at the same new execution.
            prior = SubmitToken.query.filter_by(
                schedule_id=sched.id,
                username=current_user.username, token=tok).first()
            if prior and prior.execution_id != ex.id:
                # Lost to a DIFFERENT execution — report the winner.
                return redirect(url_for(
                    "portal.execution_detail",
                    execution_id=prior.execution_id))
    record_audit(current_user.username, "schedule.run_now", sched.id)
    flash("Schedule triggered manually — execution queued. The next "
          "scheduled fire is unchanged.", "info")
    return redirect(url_for("portal.execution_detail",
                          execution_id=ex.id if ex else ""))


@portal_bp.route("/schedules/<schedule_id>/delete", methods=["POST"])
@role_required(*EDIT_ROLES)
def delete_schedule(schedule_id):
    sched = db.session.get(Schedule, schedule_id)
    if sched is None:
        abort(404)
    _project_or_404(sched.project_id)
    PlainForm().validate_on_submit()
    project_id = sched.project_id
    db.session.delete(sched)
    db.session.commit()
    record_audit(current_user.username, "schedule.deleted", schedule_id)
    return redirect(url_for("portal.project_detail",
                          project_id=project_id))


def _discovery_summary(project_id: str) -> dict:
    """Typed discovered-inventory summary for the project page."""
    from ..execution import DiscoveredItem, GitBinding
    b = (GitBinding.query.filter_by(project_id=project_id)
         .order_by(GitBinding.id.desc()).first())
    if b is None:
        return {}
    items = (DiscoveredItem.query.filter_by(binding_id=b.id)
             .order_by(DiscoveredItem.parent, DiscoveredItem.path).all())
    counts = {}
    for i in items:
        counts[i.kind] = counts.get(i.kind, 0) + 1
    warns = [i for i in items if i.warnings]
    return {"binding": b, "counts": counts, "rows": items,
            "warnings": warns}
