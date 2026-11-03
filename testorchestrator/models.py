"""Database models."""

import enum
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from flask_login import UserMixin

from .extensions import db

_ph = PasswordHasher()  # Argon2id with library default parameters.


class Role(str, enum.Enum):
    """Authenticated human roles (spec v1.7: all four may open About)."""

    ADMIN = "admin"
    TEST_MANAGER = "test_manager"
    TESTER = "tester"
    PO = "po"

    @property
    def label(self) -> str:
        return {
            Role.ADMIN: "Admin",
            Role.TEST_MANAGER: "Test Manager",
            Role.TESTER: "Tester",
            Role.PO: "PO",
        }[self]


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(128), nullable=False, default="")
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.Enum(Role), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    # v1.8: bootstrap Admin is provisioned with must_change_password=True;
    # the flag gates every request until a strong password is set (BOOT02).
    must_change_password = db.Column(
        db.Boolean, nullable=False, default=False
    )
    # Bumped on Admin reset/deactivate — sessions carrying an older version
    # are invalidated at load time (reset revokes sessions).
    session_version = db.Column(db.Integer, nullable=False, default=1)
    # Server-side project scope (§16): JSON list of project IDs; empty = all.
    allowed_projects = db.Column(db.Text, nullable=False, default="[]")
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def set_password(self, password: str) -> None:
        self.password_hash = _ph.hash(password)

    def check_password(self, password: str) -> bool:
        try:
            return _ph.verify(self.password_hash, password)
        except VerifyMismatchError:
            return False

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    def project_allowed(self, project_id: str) -> bool:
        import json as _json
        allowed = _json.loads(self.allowed_projects or "[]")
        return not allowed or project_id in allowed


class AuditEvent(db.Model):
    """Minimal page-access/security audit seam.

    v1.7 requires that About never leaks audit/changelog/admin data to
    non-admin roles; audit details themselves remain an Admin concern.
    """

    __tablename__ = "audit_events"

    id = db.Column(db.Integer, primary_key=True)
    occurred_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    username = db.Column(db.String(64), nullable=False)
    event = db.Column(db.String(64), nullable=False)
    detail = db.Column(db.String(256), nullable=False, default="")


def record_audit(username: str, event: str, detail: str = "") -> None:
    db.session.add(AuditEvent(username=username, event=event, detail=detail))
    db.session.commit()
