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
import mimetypes
from datetime import datetime, timedelta
from pathlib import Path

from flask import (abort, flash, redirect, render_template, request,
                   send_file, url_for)
from flask_login import current_user, login_required

from .. import dispatch, gitsync
from ..execution import (Execution, ExecutionState, GitBinding, OutboxEvent,
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
    schedule_form.target_id.choices = [
        (s.id, f"Suite: {s.name}") for s in project.suites] + [
        (p.id, f"Plan: {p.name}") for p in project.plans]
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
    return render_template(
        "portal/project_detail.html", project=project,
        binding_form=BindingForm(), plan_form=PlanForm(),
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

@portal_bp.route("/projects/<project_id>/bindings", methods=["POST"])
@role_required(*EDIT_ROLES)
def create_binding(project_id):
    project = _project_or_404(project_id)
    form = BindingForm()
    if form.validate_on_submit():
        binding = GitBinding(
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
        "portal/suite_detail.html", suite=suite, can_run=can_run,
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
    return render_template(
        "portal/discovered_detail.html", item=item, runners=runners,
        runtimes=runtimes, profiles=profiles,
        plain_form=PlainForm(),
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


@portal_bp.route("/suites/<suite_id>/run", methods=["POST"])
@role_required(*RUN_ROLES)
def launch_execution(suite_id):
    suite = db.session.get(Suite, suite_id)
    if suite is None:
        abort(404)
    _project_or_404(suite.project_id)
    PlainForm().validate_on_submit()
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
                                  suite_id=suite_id))
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
def view_allure(execution_id):
    """Hosted Allure report — same sandboxed-active-content policy as
    the native report."""
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
    resp.headers["Content-Security-Policy"] = "sandbox"
    return resp


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
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(target, "index.html")
        z.writestr("manifest.json", json.dumps({
            "execution_id": ex.id, "format": "allure-single-file",
            "allure_version": ex.allure_version,
            "view": "open index.html directly in a browser"}, indent=2))
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

@portal_bp.route("/projects/<project_id>/plans", methods=["POST"])
@role_required(*EDIT_ROLES)
def create_plan(project_id):
    project = _project_or_404(project_id)
    form = PlanForm()
    if form.validate_on_submit():
        parallelism = max(1, min(4, form.parallelism.data or 1))
        db.session.add(Plan(
            project_id=project.id, name=form.name.data.strip(),
            description=form.description.data or "",
            parallelism=parallelism))
        db.session.commit()
        record_audit(current_user.username, "plan.created", project.id)
    return redirect(url_for("portal.project_detail", project_id=project_id))


@portal_bp.route("/plans/<plan_id>/run", methods=["POST"])
@role_required(*RUN_ROLES)
def run_plan(plan_id):
    """Sequential execution first (§10): queue one execution per suite
    entry in order; parallelism>1 remains bounded by runner capacity."""
    plan = db.session.get(Plan, plan_id)
    if plan is None:
        abort(404)
    _project_or_404(plan.project_id)
    PlainForm().validate_on_submit()
    binding = GitBinding.query.filter_by(project_id=plan.project_id).first()
    sha = binding.last_synced_sha if binding else ""
    run_id, exs = dispatch.queue_plan_executions(
        current_user.username, plan, binding=binding, source_sha=sha)
    record_audit(current_user.username, "plan.queued",
                 f"{plan.id} run {run_id[:8]} ({len(exs)} entries)")
    flash(f"Plan run {run_id[:8]} queued ({len(exs)} executions).", "info")
    return redirect(url_for("portal.executions"))


@portal_bp.route("/plans/<plan_id>/rerun-failed", methods=["POST"])
@role_required(*RUN_ROLES)
def rerun_plan_failed(plan_id):
    """Re-queue only entries whose LATEST execution in the newest plan
    run ended non-passed — a new plan_run keeps the attempt evidence
    separate instead of overwriting history."""
    plan = db.session.get(Plan, plan_id)
    if plan is None:
        abort(404)
    _project_or_404(plan.project_id)
    PlainForm().validate_on_submit()
    runs = {}
    for ex in Execution.query.filter_by(project_id=plan.project_id).all():
        rid = ex.snapshot().get("plan_run_id")
        if rid:
            runs.setdefault(rid, []).append(ex)
    if not runs:
        flash("No previous plan run — nothing to rerun.", "info")
        return redirect(url_for("portal.plan_detail", plan_id=plan.id))
    latest_run = max(
        runs.values(), key=lambda exs: max(
            e.created_at for e in exs if e.created_at))
    rid = latest_run[0].snapshot().get("plan_run_id")
    latest = {}
    for ex in latest_run:
        sid = ex.snapshot().get("suite_id")
        if sid not in latest or ex.created_at > latest[sid].created_at:
            latest[sid] = ex
    failed_ids = {sid for sid, ex in latest.items()
                  if results_mod.entry_state(ex) != "passed"}
    entries = [e for e in plan.entries if e.suite_id in failed_ids]
    if not entries:
        flash("Latest run has no failed entries.", "info")
        return redirect(url_for("portal.plan_detail", plan_id=plan.id))
    binding = GitBinding.query.filter_by(project_id=plan.project_id).first()
    sha = binding.last_synced_sha if binding else ""
    new_run, exs = dispatch.queue_plan_executions(
        current_user.username, plan, binding=binding, source_sha=sha,
        entries=entries)
    record_audit(current_user.username, "plan.rerun_failed",
                 f"{plan.id} run {new_run[:8]} "
                 f"({len(exs)}/{len(latest)} entries, from {rid[:8]})")
    flash(f"Rerun {new_run[:8]} queued: {len(exs)} failed entries.",
          "info")
    return redirect(url_for("portal.executions"))


@portal_bp.route("/plans/<plan_id>")
@login_required
def plan_detail(plan_id):
    plan = db.session.get(Plan, plan_id)
    if plan is None:
        abort(404)
    _project_or_404(plan.project_id)
    can_edit = current_user.role.value in ("admin", "test_manager")
    can_run = current_user.role.value in ("admin", "test_manager", "tester")
    return render_template(
        "portal/plan_detail.html", plan=plan, can_edit=can_edit,
        can_run=can_run, plain_form=PlainForm(),
        plan_form=PlanForm(obj=plan),
        summary=results_mod.plan_summary(plan),
        entry_states=results_mod.plan_entry_states(plan),
        entry_cfg={e.id: json.loads(e.config_json or "{}")
                   for e in plan.entries},
        suites=Suite.query.filter_by(project_id=plan.project_id).all())


@portal_bp.route("/plans/<plan_id>/edit", methods=["POST"])
@role_required(*EDIT_ROLES)
def edit_plan(plan_id):
    plan = db.session.get(Plan, plan_id)
    if plan is None:
        abort(404)
    _project_or_404(plan.project_id)
    form = PlanForm()
    if form.validate_on_submit():
        plan.name = form.name.data.strip()
        plan.description = form.description.data or ""
        plan.parallelism = max(1, min(4, form.parallelism.data or 1))
        db.session.commit()
        record_audit(current_user.username, "plan.updated", plan.id)
    return redirect(url_for("portal.plan_detail", plan_id=plan.id))


@portal_bp.route("/plan-entries/<entry_id>/move", methods=["POST"])
@role_required(*EDIT_ROLES)
def move_plan_entry(entry_id):
    entry = db.session.get(PlanEntry, entry_id)
    if entry is None:
        abort(404)
    _project_or_404(entry.plan.project_id)
    PlainForm().validate_on_submit()
    ordered = sorted(entry.plan.entries, key=lambda e: e.order)
    idx = ordered.index(entry)
    direction = request.form.get("direction")
    swap = idx - 1 if direction == "up" else \
        idx + 1 if direction == "down" else -1
    if 0 <= swap < len(ordered):
        ordered[idx].order, ordered[swap].order = \
            ordered[swap].order, ordered[idx].order
        db.session.commit()
        record_audit(current_user.username, "plan.entry_moved",
                     entry.plan_id)
    return redirect(url_for("portal.plan_detail",
                          plan_id=entry.plan_id))


@portal_bp.route("/plan-entries/<entry_id>/delete", methods=["POST"])
@role_required(*EDIT_ROLES)
def delete_plan_entry(entry_id):
    entry = db.session.get(PlanEntry, entry_id)
    if entry is None:
        abort(404)
    _project_or_404(entry.plan.project_id)
    PlainForm().validate_on_submit()
    plan_id = entry.plan_id
    db.session.delete(entry)
    db.session.commit()
    record_audit(current_user.username, "plan.entry_removed", plan_id)
    return redirect(url_for("portal.plan_detail", plan_id=plan_id))


@portal_bp.route("/plan-entries/<entry_id>/policy", methods=["POST"])
@role_required(*EDIT_ROLES)
def plan_entry_policy(entry_id):
    """Per-entry failure policy: continue (default) queues the rest of
    the plan regardless; stop cancels later queued entries when this
    entry ends non-passed (enforced by the worker, restart-safe)."""
    entry = db.session.get(PlanEntry, entry_id)
    if entry is None:
        abort(404)
    _project_or_404(entry.plan.project_id)
    PlainForm().validate_on_submit()
    cfg = json.loads(entry.config_json or "{}")
    cfg["on_failure"] = "stop" \
        if request.form.get("on_failure") == "stop" else "continue"
    entry.config_json = json.dumps(cfg)
    db.session.commit()
    record_audit(current_user.username, "plan.entry_policy",
                 f"{entry.plan_id}: order {entry.order} -> "
                 f"{cfg['on_failure']}")
    return redirect(url_for("portal.plan_detail",
                          plan_id=entry.plan_id))


@portal_bp.route("/projects/<project_id>/releases", methods=["POST"])
@role_required(*EDIT_ROLES)
def create_release(project_id):
    project = _project_or_404(project_id)
    form = PlanForm()  # same fields subset: name/description
    if form.validate_on_submit():
        db.session.add(Release(
            project_id=project.id, name=form.name.data.strip(),
            description=form.description.data or "",
            owner=current_user.username))
        db.session.commit()
        record_audit(current_user.username, "release.created", project.id)
    return redirect(url_for("portal.project_detail", project_id=project_id))


@portal_bp.route("/releases/<release_id>/mark", methods=["POST"])
@role_required(*EDIT_ROLES)
def mark_release(release_id):
    """Marking a release released does not deploy or tag Git (§10)."""
    release = db.session.get(Release, release_id)
    if release is None:
        abort(404)
    _project_or_404(release.project_id)
    PlainForm().validate_on_submit()
    release.status = "released"
    release.released_at = utcnow()
    db.session.commit()
    record_audit(current_user.username, "release.released", release.id)
    return redirect(url_for("portal.project_detail",
                          project_id=release.project_id))


# ---------- suite/plan builder endpoints (still marked partial) ----------

@portal_bp.route("/suites/<suite_id>/entries", methods=["POST"])
@role_required(*EDIT_ROLES)
def add_suite_entry(suite_id):
    suite = db.session.get(Suite, suite_id)
    if suite is None:
        abort(404)
    _project_or_404(suite.project_id)
    if suite.readonly:
        abort(409)  # imported suites: duplicate instead of editing
    PlainForm().validate_on_submit()
    case_ref = request.form.get("case_ref", "").strip()
    if case_ref:
        order = (max([e.order for e in suite.entries], default=-1) + 1)
        db.session.add(SuiteEntry(suite_id=suite.id, order=order,
                                  case_ref=case_ref))
        suite.version += 1          # edits version the definition (§10)
        suite.content_hash = _suite_hash(suite)
        db.session.commit()
        record_audit(current_user.username, "suite.entry_added", suite.id)
    return redirect(url_for("portal.suite_detail", suite_id=suite_id))


@portal_bp.route("/suites/<suite_id>/duplicate", methods=["POST"])
@role_required(*EDIT_ROLES)
def duplicate_suite(suite_id):
    suite = db.session.get(Suite, suite_id)
    if suite is None:
        abort(404)
    _project_or_404(suite.project_id)
    PlainForm().validate_on_submit()
    copy = Suite(project_id=suite.project_id,
                 name=f"{suite.name} (copy)", description=suite.description)
    db.session.add(copy)
    db.session.flush()
    for e in suite.entries:
        db.session.add(SuiteEntry(suite_id=copy.id, order=e.order,
                                  case_ref=e.case_ref, enabled=e.enabled,
                                  config_json=e.config_json))
    copy.content_hash = _suite_hash(copy)
    db.session.commit()
    record_audit(current_user.username, "suite.duplicated", suite.id)
    return redirect(url_for("portal.suite_detail", suite_id=copy.id))


@portal_bp.route("/plans/<plan_id>/entries", methods=["POST"])
@role_required(*EDIT_ROLES)
def add_plan_entry(plan_id):
    plan = db.session.get(Plan, plan_id)
    if plan is None:
        abort(404)
    _project_or_404(plan.project_id)
    PlainForm().validate_on_submit()
    suite = db.session.get(Suite, request.form.get("suite_id", ""))
    if suite and suite.project_id == plan.project_id:
        from ..execution import PlanEntry
        order = max([e.order for e in plan.entries], default=-1) + 1
        db.session.add(PlanEntry(plan_id=plan.id, order=order,
                                 suite_id=suite.id))
        db.session.commit()
        record_audit(current_user.username, "plan.entry_added", plan.id)
    return redirect(url_for("portal.project_detail",
                          project_id=plan.project_id))


@portal_bp.route("/releases/<release_id>/run", methods=["POST"])
@role_required(*RUN_ROLES)
def run_release(release_id):
    """Run-from-release: queues the release's linked plan (§10)."""
    release = db.session.get(Release, release_id)
    if release is None or release.plan_id is None:
        abort(404)
    _project_or_404(release.project_id)
    PlainForm().validate_on_submit()
    return run_plan(release.plan_id)


@portal_bp.route("/suite-entries/<entry_id>/toggle", methods=["POST"])
@role_required(*EDIT_ROLES)
def toggle_suite_entry(entry_id):
    entry = db.session.get(SuiteEntry, entry_id)
    if entry is None or entry.suite.readonly:
        abort(404 if entry is None else 409)
    _project_or_404(entry.suite.project_id)
    PlainForm().validate_on_submit()
    entry.enabled = not entry.enabled
    entry.suite.version += 1
    entry.suite.content_hash = _suite_hash(entry.suite)
    db.session.commit()
    return redirect(url_for("portal.suite_detail",
                          suite_id=entry.suite_id))


# ---------- schedules (§10, §16 Test Manager) ----------

def _parse_run_at(text: str):
    """'YYYY-MM-DD HH:MM' (UTC) -> aware datetime, else None."""
    from datetime import datetime, timezone
    text = (text or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M") \
            .replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@portal_bp.route("/projects/<project_id>/schedules", methods=["POST"])
@role_required(*EDIT_ROLES)
def create_schedule(project_id):
    from .forms import ScheduleForm
    project = _project_or_404(project_id)
    form = ScheduleForm()
    targets = ([(s.id, f"Suite: {s.name}") for s in project.suites]
               if form.target_type.data == "suite" else
               [(p.id, f"Plan: {p.name}") for p in project.plans])
    form.target_id.choices = targets
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
        suite_id = form.target_id.data \
            if form.target_type.data == "suite" else None
        plan_id = form.target_id.data \
            if form.target_type.data == "plan" else None
        db.session.add(Schedule(
            project_id=project.id, name=form.name.data.strip(),
            description=form.description.data or "",
            target_type=form.target_type.data,
            suite_id=suite_id, plan_id=plan_id, kind=kind,
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
            plan = db.session.get(Plan, sched.plan_id)
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
