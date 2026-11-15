"""Shared Flask extension instances.

Extensions are created here and bound to the application inside the
application factory so that blueprints and modules can import them without
circular dependencies.
"""

import sqlite3

from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    """WAL + busy_timeout so the dispatch worker's continuous writes do
    not starve concurrent web requests on the shared SQLite database."""
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()
