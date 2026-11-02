"""Main blueprint: dashboard, About (v1.7), notices, admin System pages."""

from pathlib import Path

from flask import current_app, render_template, request, send_from_directory
from flask_login import current_user, login_required

from ..config import BASE_DIR
from ..models import AuditEvent, Role, record_audit
from ..security import role_required
from . import main_bp

# Owner-provided, authoritative company text (v1.7). Do not add registration
# numbers, addresses, legal claims, or copyright wording without approval.
COMPANY_NAME = "IT-Space Kft."
COMPANY_HOMEPAGE = "https://it-space.info"
APP_NAME = "TestOrchestrator"

# assets/branding/README.md tracks approval status. The shipped asset is a
# clearly labelled development placeholder (AB07: production branding stays
# blocked until the owner-approved logo replaces it).
LOGO_APPROVED = False
LOGO_STATIC_PATH = "branding/it-space-logo.placeholder.svg"


@main_bp.app_context_processor
def inject_about_context():
    """Make About content available in every authenticated template so the
    in-app About dialog renders server-side with no extra request (AB03)."""

    if not current_user.is_authenticated:
        return {}
    return {"about": about_context()}


def about_context():
    return {
        "app_name": APP_NAME,
        "company_name": COMPANY_NAME,
        "company_homepage": COMPANY_HOMEPAGE,
        "logo_path": LOGO_STATIC_PATH,
        "logo_approved": LOGO_APPROVED,
        "release": current_app.config["RELEASE_METADATA"],
        "is_admin": current_user.role == Role.ADMIN,
    }


@main_bp.route("/")
@login_required
def dashboard():
    return render_template("main/dashboard.html")


@main_bp.route("/about")
@login_required
def about():
    """Dedicated About page — same content as the in-app About dialog.

    Reachable by every authenticated role (AB01). Rendering it performs no
    network access of its own (AB03).
    """

    return render_template("main/about.html")


@main_bp.route("/about/notices")
@login_required
def notices():
    """Locally bundled third-party notices/licences (AB05)."""

    licenses_dir = Path(BASE_DIR) / "3rdparty" / "licenses"
    license_files = sorted(p.name for p in licenses_dir.glob("*") if p.is_file())
    return render_template(
        "main/notices.html", license_files=license_files
    )


@main_bp.route("/3rdparty/<path:filename>")
@login_required
def thirdparty_file(filename):
    """Serve a single bundled third-party notice/licence file locally."""

    return send_from_directory(
        Path(BASE_DIR) / "3rdparty" / "licenses", filename
    )


@main_bp.route("/system/changelog")
@role_required(Role.ADMIN)
def changelog():
    """Admin-only: System -> Changelog (v1.7: may be linked from About for
    Admin, must not be exposed to other roles)."""

    record_audit(current_user.username, "page.changelog", request.path)
    changelog_path = Path(BASE_DIR) / "CHANGELOG.md"
    try:
        changelog_text = changelog_path.read_text(encoding="utf-8")
    except OSError:
        changelog_text = ""
    return render_template("system/changelog.html", changelog_text=changelog_text)


@main_bp.route("/system/audit")
@role_required(Role.ADMIN)
def audit():
    """Admin-only audit page-access log."""

    record_audit(current_user.username, "page.audit", request.path)
    events = (
        AuditEvent.query.order_by(AuditEvent.occurred_at.desc()).limit(200).all()
    )
    return render_template("system/audit.html", events=events)


@main_bp.app_errorhandler(403)
def forbidden(_error):
    return render_template("error.html", code=403,
                           message="You do not have access to this page."), 403


@main_bp.app_errorhandler(404)
def not_found(_error):
    return render_template("error.html", code=404,
                           message="Page not found."), 404
