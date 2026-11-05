from flask_wtf import FlaskForm
from wtforms import (IntegerField, SelectField, SelectMultipleField,
                     StringField, TextAreaField)
from wtforms.validators import DataRequired, Length, Optional

from ..adapters.kre import SUPPORTED_BROWSERS

BROWSER_CHOICES = [(m["label"], m["label"])
                   for m in SUPPORTED_BROWSERS.values()]


class ProjectForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=128)])
    description = TextAreaField("Description", validators=[Optional()])


class BindingForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=128)])
    url = StringField("Repository URL or path",
                      validators=[DataRequired(), Length(max=512)])
    ref = StringField("Ref (branch/tag)", validators=[Optional(), Length(max=128)])
    subdirectory = StringField("Subdirectory", validators=[Optional()])
    prj_path = StringField(".prj path", validators=[Optional()])
    credential_ref = StringField("Credential reference", validators=[Optional()])


class SuiteForm(FlaskForm):
    name = StringField("Suite name", validators=[DataRequired(), Length(max=128)])
    description = TextAreaField("Description", validators=[Optional()])
    binding_id = StringField("Source binding", validators=[Optional()])
    case_refs = SelectMultipleField("Test cases", validators=[Optional()])


class PlanForm(FlaskForm):
    name = StringField("Plan name", validators=[DataRequired(), Length(max=128)])
    description = TextAreaField("Description", validators=[Optional()])
    parallelism = IntegerField("Max parallel executions", default=1)


class ScheduleForm(FlaskForm):
    name = StringField("Schedule name",
                       validators=[DataRequired(), Length(max=128)])
    description = TextAreaField("Description", validators=[Optional()])
    target_type = SelectField("Target", choices=[("suite", "Suite")])
    target_id = SelectField("Runs", validators=[DataRequired()])
    kind = SelectField("Kind", choices=[("once", "Once"),
                                        ("interval", "Repeating")])
    run_at = StringField("First run (UTC, YYYY-MM-DD HH:MM)",
                         validators=[Optional()])
    interval_minutes = IntegerField("Every N minutes", default=60)
    execution_profile = StringField("Execution profile",
                                    default="default")
    browser_type = SelectField("Browser", choices=BROWSER_CHOICES,
                               default="Chrome (headless)")
    remote_webdriver_url = StringField(
        "Remote WebDriver URL", validators=[Optional(), Length(max=512)])
    remote_webdriver_type = StringField(
        "WebDriver driver type", validators=[Optional(), Length(max=64)])
    timeout_seconds = IntegerField(
        "Timeout (seconds)", validators=[Optional()],
        default=3600)
    runner_id = SelectField("Runner", validators=[Optional()],
                            choices=[])
    runtime_id = SelectField("KRE installation", validators=[Optional()],
                             choices=[])
    overlap_policy = SelectField(
        "Overlap", choices=[("allow", "Allow overlap"),
                            ("skip", "Skip if previous run active")],
        default="allow")


class PlainForm(FlaskForm):
    """CSRF-only submit (sync/cancel/release actions)."""
    pass
