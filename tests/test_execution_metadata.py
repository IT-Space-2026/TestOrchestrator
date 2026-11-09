"""Execution source_ref/resolved_sha/environment snapshot semantics."""
from testorchestrator.extensions import db
from testorchestrator.execution import (Execution, Project, GitBinding,
                                        Suite)
from testorchestrator import dispatch


def _mk():
    p = Project.query.get("p1") or Project(id="p1", name="p1")
    b = GitBinding.query.get("b1") or GitBinding(
        id="b1", project_id="p1", name="b1", url="/tmp/r",
        ref="feature-x")
    s = Suite.query.get("s1") or Suite(id="s1", project_id="p1", name="s1")
    db.session.add_all([p, b, s]); db.session.commit()
    return b, s


def test_source_ref_and_env_snapshotted(app):
    with app.app_context():
        b, s = _mk()
        ex = dispatch.create_execution(
            "u", suite=s, binding=b, source_sha="abc123",
            run_config={"environment": "linux-x64"})
        assert ex.source_ref == "feature-x"   # selector, not the sha
        assert ex.source_sha == "abc123"      # resolved commit
        assert ex.environment == "linux-x64"
        # later binding changes must NOT rewrite the snapshot
        b.ref = "main"; db.session.commit()
        assert ex.source_ref == "feature-x"


def test_no_invented_sha_or_env(app):
    with app.app_context():
        b, s = _mk()
        ex = dispatch.create_execution("u", suite=s, binding=b)
        assert ex.source_sha == ""            # unresolved -> empty, not guessed
        assert ex.environment is None
        assert ex.source_ref == "feature-x"


def test_nullable_historical(app):
    """Records lacking the captured value stay NULL — no backfill."""
    with app.app_context():
        _mk()
        ex = Execution(project_id="p1", suite_id="s1", source_sha="")
        db.session.add(ex); db.session.commit()
        assert ex.source_ref is None and ex.environment is None
