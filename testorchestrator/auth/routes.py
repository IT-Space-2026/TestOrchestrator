from flask import redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db
from ..models import User, record_audit
from . import auth_bp
from .forms import LoginForm


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data.strip()).first()
        if user and user.is_active and user.check_password(form.password.data):
            login_user(user)
            record_audit(user.username, "login.success")
            next_url = request.args.get("next")
            # Only same-origin redirects — never an open redirector.
            if next_url and next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect(url_for("main.dashboard"))
        record_audit(
            form.username.data.strip() or "<empty>", "login.failure"
        )
        form.password.errors = (*form.password.errors, "Invalid username or password.")

    return render_template("auth/login.html", form=form)


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    record_audit(current_user.username, "logout")
    logout_user()
    return redirect(url_for("auth.login"))
