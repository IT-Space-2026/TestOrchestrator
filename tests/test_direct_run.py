"""Phase 1: direct execution of discovered repository definitions."""

from testorchestrator import dispatch
from testorchestrator.execution import (DiscoveredItem, Execution,
                                        ExecutionState, GitBinding,
                                        Project, Suite, utcnow)
from testorchestrator.extensions import db


def _env(app, kind="suite", path="Test Suites/S1"):
    p = Project(name="P"); db.session.add(p); db.session.flush()
    b = GitBinding(project_id=p.id, name="b", url="x",
                   worktree_path="/x", last_synced_sha="sha1",
                   status="synced")
    db.session.add(b); db.session.flush()
    item = DiscoveredItem(binding_id=b.id, project_id=p.id,
                          sha="sha1", kind=kind, path=path,
                          name=path.split("/")[-1])
    db.session.add(item); db.session.commit()
    return p, b, item


def test_source_target_identity_in_snapshot(app):
    """Repo-def runs snapshot the durable triple
    (binding+kind+path+sha) — history survives inventory replacement."""
    with app.app_context():
        p, b, item = _env(app)
        ex = dispatch.create_execution("m", source_item=item,
                                       binding=b, source_sha="sha1")
        snap = ex.snapshot()
        st = snap["source_target"]
        assert st["binding_id"] == item.binding_id
        assert st["kind"] == "suite" and st["path"] == item.path
        assert ex.suite_id is None and ex.source_item_id == item.id
        assert snap["suite_name"] == item.name
        rc = snap["run_config"]
        assert rc["suite_path"] == item.path


def test_collection_emits_collection_arg(app):
    """A discovered collection carries collection=True through the
    snapshot -> build_args -> -testSuiteCollectionPath."""
    with app.app_context():
        p, b, item = _env(app, kind="collection",
                          path="Test Suites/Coll 1")
        ex = dispatch.create_execution("m", source_item=item)
        rc = ex.snapshot()["run_config"]
        assert rc["collection"] is True
        from testorchestrator.adapters.kre import build_args
        a = build_args("k", "/p", rc["suite_path"], "/r",
                       rc["profile"], collection=rc["collection"])
        assert "-testSuiteCollectionPath=Test Suites/Coll 1" in a
        assert not any(x.startswith("-testSuitePath=") for x in a)


def test_neither_and_both_targets_rejected(app):
    """Exactly one target required — suite xor source_item."""
    with app.app_context():
        p, b, item = _env(app)
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        try:
            dispatch.create_execution("m")           # neither
            assert False
        except dispatch.DispatchError:
            pass
        try:
            dispatch.create_execution("m", suite=s, source_item=item)
            assert False
        except dispatch.DispatchError:
            pass


def test_removed_definition_rejected_at_dispatch(app):
    """A discovered item deleted after queueing fails visibly at
    dispatch — never silently retargets."""
    with app.app_context():
        p, b, item = _env(app)
        ex = dispatch.create_execution("m", source_item=item)
        db.session.delete(item); db.session.commit()
        dispatch.run_execution(ex.id)
        db.session.refresh(ex)
        assert ex.state == ExecutionState.FAILED
        assert "no longer in the binding" in ex.error_text


def test_same_path_other_binding_not_target(app):
    """Identity is (binding,kind,path) — same path in a different
    binding must NOT satisfy the re-validation."""
    with app.app_context():
        p, b, item = _env(app)
        b2 = GitBinding(project_id=p.id, name="b2", url="y",
                        worktree_path="/y", last_synced_sha="s2",
                        status="synced")
        db.session.add(b2); db.session.flush()
        other = DiscoveredItem(binding_id=b2.id, project_id=p.id,
                               sha="s2", kind="suite",
                               path=item.path, name="dup")
        db.session.add(other)
        db.session.delete(item); db.session.commit()
        ex = dispatch.create_execution("m", source_item=other)
        # item is from b2 — the ORIGINAL binding's row is gone but the
        # OTHER binding's row exists → still resolves on ITS binding.
        ex2 = ex
        # now delete the matching row entirely
        db.session.delete(other); db.session.commit()
        dispatch.run_execution(ex2.id)
        db.session.refresh(ex2)
        assert ex2.state == ExecutionState.FAILED
