"""Release workflow: scope, launch association, link/unlink, status."""
import json
import pytest
from testorchestrator.extensions import db
from testorchestrator.execution import (
    Execution, Outcome, Release, ReleaseItem, DiscoveredItem,
    GitBinding, utcnow)
from tests.conftest import make_app


def _seed(app, tmp_path):
    with app.app_context():
        from testorchestrator.execution import Project
        p = Project(name="rp"); db.session.add(p); db.session.flush()
        rel = Release(project_id=p.id, name="R1", status="draft")
        db.session.add(rel); db.session.commit()
        return p.id, rel.id


def _rel(rid):
    return db.session.get(Release, rid)


def test_release_scope_add_remove(app, tmp_path):
    pid, rid = _seed(app, tmp_path)
    with app.app_context():
        rel = _rel(rid)
        it = ReleaseItem(release_id=rel.id, kind="suite",
                         path="Test Suites/S.ts")
        db.session.add(it); db.session.commit()
        assert ReleaseItem.query.filter_by(release_id=rel.id).count() == 1
        db.session.delete(it); db.session.commit()
        assert ReleaseItem.query.filter_by(release_id=rel.id).count() == 0


def test_release_execution_link(app, tmp_path):
    pid, rid = _seed(app, tmp_path)
    with app.app_context():
        ex = Execution(project_id=pid, release_id=rid,
                       outcome=Outcome.NONE, created_by="admin")
        db.session.add(ex); db.session.commit()
        assert ex.release_id == rid
        assert ex.release.name == "R1"          # relationship resolves


def test_release_status_released_at(app, tmp_path):
    pid, rid = _seed(app, tmp_path)
    with app.app_context():
        rel = _rel(rid)
        rel.status = "released"; rel.released_at = utcnow()
        db.session.commit()
        first = rel.released_at
        rel.status = "draft"; rel.released_at = None   # reopen clears
        db.session.commit()
        assert rel.released_at is None
        rel.status = "released"; rel.released_at = utcnow()
        db.session.commit()
        assert rel.released_at >= first          # restamps
