"""Main blueprint: dashboard, About (v1.7), notices, admin System pages."""

from datetime import datetime, timedelta
from pathlib import Path

from flask import current_app, render_template, request, send_from_directory
from flask_login import current_user, login_required

from .. import settings as settings_mod
from ..config import BASE_DIR
from ..extensions import db
from ..models import AuditEvent, Role, record_audit
from ..network import access_links
from ..security import role_required
from . import main_bp

# Owner-provided, authoritative company text (v1.7). Do not add registration
# numbers, addresses, legal claims, or copyright wording without approval.
COMPANY_NAME = "IT-Space Kft."
COMPANY_HOMEPAGE = "https://it-space.info"
APP_NAME = "TestOrchestrator"

# assets/branding/README.md tracks approval status. The official
# owner-supplied logo (iT-space-logo-400x400.png, RGBA, transparency
# preserved) replaces the development placeholder (AB07 resolved).
LOGO_APPROVED = True
LOGO_STATIC_PATH = "branding/iT-space-logo.png"


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
    # Approved navigation links are visible to every role; internal bind
    # details (Waitress host/port) are not exposed here (v1.8 §6).
    from .. import dashboard as dash
    return render_template(
        "main/dashboard.html",
        links=access_links(settings_mod.all_settings()),
        data=dash.snapshot(current_user),
    )


@main_bp.route("/dashboard/data")
@login_required
def dashboard_data():
    """JSON snapshot for the dashboard's ~5 s near-real-time poll.

    Same visibility scope as the page — project_allowed() applies."""
    from flask import jsonify
    from .. import dashboard as dash
    return jsonify(dash.snapshot(current_user))


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
    return render_template("system/changelog.html",
                           changelog_html=_md_to_html(changelog_text))


def _md_to_html(text: str) -> str:
    """Minimal escape-then-convert renderer for the bundled CHANGELOG:
    headings, bullets, code spans, bold, links, horizontal rules.
    Content is escaped FIRST so raw HTML in the file can't inject."""
    import re
    from markupsafe import escape
    if not text:
        return ""
    out, in_list = [], False
    def _inline(line: str) -> str:
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        line = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", line)
        line = re.sub(
            r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
            r'<a href="\2" rel="noopener noreferrer">\1</a>', line)
        return line
    for raw in text.splitlines():
        line = escape(raw)
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            if in_list:
                out.append("</ul>"); in_list = False
            lvl = len(m.group(1)) + 1
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
        elif re.match(r"^---+\s*$", line):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append("<hr>")
        elif re.match(r"^\s*[-*]\s+(.*)", line):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{_inline(re.match(r'^\s*[-*]\s+(.*)', line).group(1))}</li>")
        elif not line.strip():
            if in_list:
                out.append("</ul>"); in_list = False
        else:
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<p>{_inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


AUDIT_PAGE_SIZE = 50


@main_bp.route("/system/audit")
@role_required(Role.ADMIN)
def audit():
    """Admin-only audit page — server-side search/filter/pagination over
    the stored safe fields (username, event, detail, occurred_at).
    Free-text search never touches stored secrets (none are stored in
    audit rows by design)."""

    record_audit(current_user.username, "page.audit", request.path)
    q = AuditEvent.query
    filters = {
        "q": request.args.get("q", "").strip(),
        "event": request.args.get("event", "").strip(),
        "actor": request.args.get("actor", "").strip(),
        "related": request.args.get("related", "").strip(),
        "from": request.args.get("from", "").strip(),
        "to": request.args.get("to", "").strip(),
    }
    if filters["q"]:
        like = f"%{filters['q']}%"
        q = q.filter(db.or_(AuditEvent.event.like(like),
                            AuditEvent.detail.like(like),
                            AuditEvent.username.like(like)))
    if filters["event"]:
        q = q.filter(AuditEvent.event == filters["event"])
    if filters["actor"]:
        q = q.filter(AuditEvent.username == filters["actor"])
    if filters["related"]:
        q = q.filter(AuditEvent.detail.like(f"%{filters['related']}%"))
    for bound in ("from", "to"):
        raw = filters[bound]
        if raw:
            try:
                dt = datetime.fromisoformat(raw)
                if bound == "to" and len(raw) == 10:
                    dt = dt + timedelta(days=1)
                q = q.filter(AuditEvent.occurred_at >= dt
                             if bound == "from"
                             else AuditEvent.occurred_at <= dt)
            except ValueError:
                pass
    page = max(1, request.args.get("page", 1, type=int) or 1)
    total = q.count()
    # Deterministic ordering: time then id tiebreak.
    events = (q.order_by(AuditEvent.occurred_at.desc(),
                         AuditEvent.id.desc())
               .offset((page - 1) * AUDIT_PAGE_SIZE)
               .limit(AUDIT_PAGE_SIZE).all())
    pages = max(1, -(-total // AUDIT_PAGE_SIZE))
    event_types = [r[0] for r in db.session.query(
        AuditEvent.event).distinct().order_by(AuditEvent.event).all()]
    actors = [r[0] for r in db.session.query(
        AuditEvent.username).distinct().order_by(
        AuditEvent.username).all()]
    return render_template(
        "system/audit.html", events=events, filters=filters,
        page=page, pages=pages, total=total,
        event_types=event_types, actors=actors)


@main_bp.app_errorhandler(403)
def forbidden(_error):
    return render_template("error.html", code=403,
                           message="You do not have access to this page."), 403


@main_bp.app_errorhandler(404)
def not_found(_error):
    return render_template("error.html", code=404,
                           message="Page not found."), 404
