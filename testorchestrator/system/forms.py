from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired
from wtforms import (BooleanField, IntegerField, PasswordField, RadioField,
                     SelectMultipleField, StringField, TextAreaField)
from wtforms.validators import (DataRequired, Length, NumberRange,
                                Optional)


class AddressForm(FlaskForm):
    """External/LAN address settings (v1.8 §3)."""

    external_enabled = BooleanField("Enable external/LAN access")
    external_host = StringField(
        "External/LAN address",
        validators=[Optional()],
        description=(
            "IP address or DNS hostname (no scheme, path, or port). "
            "A private LAN IP is fine — it does not make the site "
            "Internet-accessible; DNS must resolve and any NAT/firewall "
            "forwarding is configured separately."
        ),
    )
    candidate = RadioField("Detected candidates", validators=[Optional()])


class PortsForm(FlaskForm):
    https_port = IntegerField(
        "Public HTTPS port", validators=[DataRequired(), NumberRange(1, 65535)]
    )
    http_port = IntegerField(
        "Public HTTP port (redirect-only)",
        validators=[DataRequired(), NumberRange(1, 65535)],
    )
    internal_app_port = IntegerField(
        "Internal app port (Waitress, loopback)",
        validators=[DataRequired(), NumberRange(1, 65535)],
    )
    http_mode = RadioField(
        "HTTP listener mode",
        choices=[
            ("redirect", "Redirect-only to HTTPS"),
            ("disabled", "Disabled — no HTTP listener"),
            (
                "development-insecure",
                "Development insecure (explicit approval required)",
            ),
        ],
        default="redirect",
    )


class GenerateCertForm(FlaskForm):
    san_hosts = SelectMultipleField(
        "Hostnames/IPs to include as SANs",
        validators=[DataRequired()],
    )


class ImportCertForm(FlaskForm):
    certificate = FileField(
        "Certificate / full chain (PEM)",
        validators=[FileRequired(), FileAllowed(["pem", "crt", "cer", "txt"])],
    )
    private_key = FileField(
        "Matching private key (PEM)",
        validators=[FileRequired(), FileAllowed(["pem", "key", "txt"])],
    )
    intermediates = FileField(
        "Intermediate certificates (PEM, optional)",
        validators=[Optional(), FileAllowed(["pem", "crt", "cer", "txt"])],
    )


class ApplyForm(FlaskForm):
    """CSRF-only submit for staged apply/rollback actions."""
    pass


class UserForm(FlaskForm):
    """Admin user management (§16): create accounts and reset temporary
    passwords; mandatory first change applies to both."""
    username = StringField("Username",
                           validators=[DataRequired(), Length(max=64)])
    display_name = StringField("Display name", validators=[Optional()])
    role = RadioField(
        "Role",
        choices=[("admin", "Admin"), ("test_manager", "Test Manager"),
                 ("tester", "Tester"), ("po", "PO")],
        default="tester",
    )
    temporary_password = PasswordField(
        "Temporary password",
        validators=[DataRequired(), Length(min=12)],
    )


class ResetForm(FlaskForm):
    temporary_password = PasswordField(
        "New temporary password",
        validators=[DataRequired(), Length(min=12)],
    )


class SecretForm(FlaskForm):
    name = StringField("Reference name",
                       validators=[DataRequired(), Length(max=64)])
    kind = RadioField(
        "Kind",
        choices=[("git", "Git — repository authentication"),
                 ("runtime", "KRE license / invocation secret")],
        default="git",
    )
    value = PasswordField("Credential value",
                          validators=[DataRequired()],
                          description="Git: HTTPS access token "
                          "(e.g. ghp_…) or SSH private key. KRE: the "
                          "license/invocation value supplied with your "
                          "Katalon licence.")
    description = StringField("Description", validators=[Optional()])


class RunnerForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=128)])
    description = TextAreaField("Description", validators=[Optional()])
    os_arch = StringField("OS / architecture", validators=[Optional()])
    connection_mode = RadioField(
        "Connection mode",
        choices=[("local", "Local (same machine)"),
                 ("remote", "Remote (pending agent transport)")],
        default="local",
    )
    workspace_root = StringField("Workspace root", validators=[Optional()])
    max_slots = IntegerField("Max concurrent slots", default=1)
    allowed_projects = SelectMultipleField("Allowed projects",
                                           validators=[Optional()])


class RuntimeForm(FlaskForm):
    name = StringField("Connection name",
                       validators=[DataRequired(), Length(max=128)])
    description = TextAreaField("Description", validators=[Optional()])
    runner_id = StringField("Worker", validators=[DataRequired()])
    executable_path = StringField(
        "KRE executable path (absolute, existing installation)",
        validators=[DataRequired(), Length(max=512)],
    )
    workspace_root = StringField("Workspace root", validators=[Optional()])
    credential_ref = StringField("Credential reference", validators=[Optional()])
    worker_label = StringField("Dagu worker label", validators=[Optional()])
    concurrency = IntegerField("Concurrency", default=1)
