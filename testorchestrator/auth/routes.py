from flask import flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..bootstrap import rate_limit_exceeded, reset_attempts
from ..models import User, record_audit
from ..settings import mark_setup_complete
from . import auth_bp
from .forms import ChangePasswordForm, LoginForm


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        username = form.username.data.strip()
        # Rate-limit bootstrap/login attempts (v1.8 §2).
        if rate_limit_exceeded(username):
            record_audit(username, "login.rate_limited")
            form.password.errors = (
                *form.password.errors,
                "Too many attempts. Try again shortly.",
            )
            return render_template("auth/login.html", form=form), 429

        user = User.query.filter_by(username=username).first()
        if user and user.is_active and user.check_password(form.password.data):
            reset_attempts(username)
            login_user(user)
            record_audit(user.username, "login.success")
            # A bootstrap login lands on the forced password change only.
            if user.must_change_password:
                return redirect(url_for("auth.change_password"))
            next_url = request.args.get("next")
            # Only same-origin redirects — never an open redirector.
            if next_url and next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect(url_for("main.dashboard"))
        record_audit(username or "<empty>", "login.failure")
        form.password.errors = (*form.password.errors, "Invalid username or password.")

    return render_template("auth/login.html", form=form)


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    record_audit(current_user.username, "logout")
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    """Mandatory first-login change (bootstrap) and normal self-service.

    While must_change_password is set this is the only reachable endpoint —
    the gate in bootstrap.py guarantees it cannot be bypassed (BOOT02).
    """

    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            form.current_password.errors = (
                *form.current_password.errors, "Current password is incorrect."
            )
        elif form.new_password.data == form.current_password.data:
            form.new_password.errors = (
                *form.new_password.errors,
                "New password must differ from the current password.",
            )
        else:
            bootstrap = current_user.must_change_password
            current_user.set_password(form.new_password.data)
            current_user.must_change_password = False
            from ..extensions import db
            db.session.commit()

            if bootstrap:
                # Invalidate the bootstrap credential, rotate the session,
                # and mark setup complete durably (v1.8 §2).
                mark_setup_complete()
                record_audit(
                    current_user.username, "password.bootstrap_replaced"
                )
            record_audit(current_user.username, "password.changed")

            # Session rotation: drop the old session cookie entirely, then
            # issue a fresh authenticated one.
            username = current_user.username
            logout_user()
            session.clear()
            user = User.query.filter_by(username=username).first()
            login_user(user)

            flash("Password updated.", "info")
            return redirect(url_for("main.dashboard"))

    return render_template(
        "auth/change_password.html", form=form,
        bootstrap=current_user.must_change_password,
    )
