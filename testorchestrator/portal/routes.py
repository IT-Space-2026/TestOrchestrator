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
from pathlib import Path

from flask import (abort, flash, redirect, render_template, request,
                   send_file, url_for)
from flask_login import current_user, login_required

from .. import dispatch, gitsync
from ..execution import (Execution, ExecutionState, GitBinding, OutboxEvent,
                         Plan, PlanEntry, Project, Release, Schedule, Suite,
                         SuiteEntry, utcnow)
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
    return render_template(
        "portal/project_detail.html", project=project,
        binding_form=BindingForm(), plan_form=PlanForm(),
        suite_form=suite_form, plain_form=PlainForm(),
        schedule_form=schedule_form, can_edit=can_edit, can_run=can_run,
        executions=Execution.query.filter_by(project_id=project_id)
        .order_by(Execution.created_at.desc()).limit(25).all(),
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
               if form.binding_id.data else None)
    cases = gitsync.list_test_cases(binding) if binding else []
    form.case_refs.choices = [(c, c) for c in cases]
    if form.validate_on_submit():
        suite = Suite(project_id=project.id, name=form.name.data.strip(),
                      description=form.description.data or "",
                      imported=bool(binding))
        db.session.add(suite)
        db.session.flush()  # id assigned before entries reference it
        for i, ref in enumerate(form.case_refs.data):
            db.session.add(SuiteEntry(
                suite_id=suite.id, order=i, case_ref=ref))
        suite.content_hash = _suite_hash(suite)
        db.session.commit()
        record_audit(current_user.username, "suite.created", suite.id)
    return redirect(url_for("portal.project_detail", project_id=project_id))


@portal_bp.route("/suites/<suite_id>")
@login_required
def suite_detail(suite_id):
    suite = db.session.get(Suite, suite_id)
    if suite is None:
        abort(404)
    _project_or_404(suite.project_id)
    can_run = current_user.role.value in ("admin", "test_manager", "tester")
    binding = GitBinding.query.filter_by(project_id=suite.project_id).first()
    return render_template(
        "portal/suite_detail.html", suite=suite, can_run=can_run,
        binding=binding, plain_form=PlainForm())


# ---------- executions ----------

@portal_bp.route("/executions")
@login_required
def executions():
    query = Execution.query.order_by(Execution.created_at.desc()).limit(100)
    visible = [e for e in query.all()
               if current_user.project_allowed(e.project_id)]
    return render_template("portal/executions.html", executions=visible)


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
        "kre_options": kre_options,
    }
    ex = dispatch.create_execution(
        current_user.username, suite, binding=binding, source_sha=sha,
        run_config=run_config)
    record_audit(current_user.username, "execution.queued", ex.id)
    flash(f"Execution {ex.id[:8]} queued.", "info")
    return redirect(url_for("portal.execution_detail", execution_id=ex.id))


@portal_bp.route("/executions/<execution_id>")
@login_required
def execution_detail(execution_id):
    ex = _execution_or_404(execution_id)
    counts = {}
    for r in ex.results:
        counts[r.status.value] = counts.get(r.status.value, 0) + 1
    return render_template(
        "portal/execution_detail.html", ex=ex, counts=counts,
        plain_form=PlainForm())


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
            run_config_json=json.dumps({
                "profile": form.execution_profile.data or "default",
                "browser": form.browser_type.data or "Chrome (headless)",
                "remote_webdriver_url":
                    (form.remote_webdriver_url.data or "").strip(),
                "remote_webdriver_type":
                    (form.remote_webdriver_type.data or "").strip(),
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
