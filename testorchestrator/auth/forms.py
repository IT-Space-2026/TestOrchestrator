from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField, ValidationError
from wtforms.validators import DataRequired, EqualTo, Length


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=64)])
    password = PasswordField("Password", validators=[DataRequired()])


def _strong_password(form, field):
    """Bootstrap replacement policy (v1.8 §2): strong, and never 'Admin'."""
    pw = field.data or ""
    errors = []
    if len(pw) < 12:
        errors.append("at least 12 characters")
    if pw.lower() == "admin":
        errors.append("must differ from the bootstrap password")
    if not any(c.isupper() for c in pw):
        errors.append("at least one uppercase letter")
    if not any(c.islower() for c in pw):
        errors.append("at least one lowercase letter")
    if not any(c.isdigit() for c in pw):
        errors.append("at least one digit")
    if errors:
        raise ValidationError("Password must contain " + "; ".join(errors))


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField("Current password",
                                     validators=[DataRequired()])
    new_password = PasswordField(
        "New password",
        validators=[DataRequired(), _strong_password],
    )
    confirm_password = PasswordField(
        "Confirm new password",
        validators=[
            DataRequired(),
            EqualTo("new_password", message="Passwords do not match."),
        ],
    )


class LicenceActivationForm(FlaskForm):
    licence_code = StringField("Licence code",
                               validators=[DataRequired(), Length(max=8192)])
    website = StringField("Website")   # honeypot — must stay empty
