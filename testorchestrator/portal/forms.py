from flask_wtf import FlaskForm
from wtforms import (IntegerField, SelectMultipleField, StringField,
                     TextAreaField)
from wtforms.validators import DataRequired, Length, Optional


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


class PlainForm(FlaskForm):
    """CSRF-only submit (sync/cancel/release actions)."""
    pass
