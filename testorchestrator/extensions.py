"""Shared Flask extension instances.

Extensions are created here and bound to the application inside the
application factory so that blueprints and modules can import them without
circular dependencies.
"""

from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
