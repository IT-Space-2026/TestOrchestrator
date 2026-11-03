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
                         Plan, Project, Release, Suite, SuiteEntry, utcnow)
from ..extensions import db
from ..models import Role, record_audit
from ..security import role_required
from . import portal_bp
from .forms import (BindingForm, PlanForm, PlainForm, ProjectForm, SuiteForm)

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
    all_projects = Project.query.order_by(Project.name).all()
    visible = [p for p in all_projects if current_user.project_allowed(p.id)]
    return render_template("portal/projects.html", projects=visible,
                           form=ProjectForm())


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
    return render_template(
        "portal/project_detail.html", project=project,
        binding_form=BindingForm(), plan_form=PlanForm(),
        suite_form=SuiteForm(), plain_form=PlainForm(),
        can_edit=can_edit, can_run=can_run,
        executions=Execution.query.filter_by(project_id=project_id)
        .order_by(Execution.created_at.desc()).limit(25).all(),
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
        for i, ref in enumerate(form.case_refs.data):
            db.session.add(SuiteEntry(
                suite_id=suite.id, order=i, case_ref=ref))
        db.session.add(suite)
        db.session.flush()
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
    return render_template("portal/suite_detail.html", suite=suite)


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
    ex = dispatch.create_execution(
        current_user.username, suite, binding=binding, source_sha=sha)
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


@portal_bp.route("/executions/<execution_id>/logs/<name>")
@login_required
def view_log(execution_id, name):
    """stdout/stderr served as text/plain — never rendered as HTML."""
    ex = _execution_or_404(execution_id)
    if name not in ("kre-stdout.log", "kre-stderr.log"):
        abort(404)
    path = Path(ex.workspace_path or "") / "logs" / name
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
    for entry in plan.entries:
        dispatch.create_execution(
            current_user.username, entry.suite, binding=binding,
            source_sha=sha)
    record_audit(current_user.username, "plan.queued", plan.id)
    return redirect(url_for("portal.executions"))


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
