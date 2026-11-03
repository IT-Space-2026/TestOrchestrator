from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired
from wtforms import (BooleanField, IntegerField, RadioField, SelectMultipleField,
                     StringField)
from wtforms.validators import DataRequired, NumberRange, Optional


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
