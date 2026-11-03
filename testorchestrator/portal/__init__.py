from flask import Blueprint

portal_bp = Blueprint("portal", __name__)

from . import routes  # noqa: E402,F401
