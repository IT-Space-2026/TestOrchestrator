"""Studio/Git-to-app update workflow — upstream content changes then
rediscovery updates inventory; old snapshots unchanged; deleted
definitions become unavailable (never silently retargeted)."""

from testorchestrator import dispatch, gitsync
from testorchestrator.extensions import db
from testorchestrator.execution import (DiscoveredItem, ExecutionState,
                                        Project, Suite)
from tests.test_discovery import _base_files, _binding, _fixture


def test_upstream_edit_updates_inventory_no_duplicates(app, tmp_path):
    """An upstream case/suite edit lands via rediscovery — membership
    updates, no duplicate TO suite rows, new runs use the new rev."""
    with app.app_context():
        files = _base_files()
        wt = _fixture(tmp_path / "wt", files)
        b = _binding(app, wt)
        gitsync.discover_project(b, b.last_synced_sha or 'sha1')
        s1 = DiscoveredItem.query.filter_by(
            binding_id=b.id, kind="suite").first()
        assert s1 is not None
        old_sha = s1.sha
        ex_old = dispatch.create_execution("m", source_item=s1)
        # upstream edits: new case + extended suite membership
        (wt / "Test Cases/API/tc3.tc").write_text(
            "<TestCaseEntity><name>tc3</name></TestCaseEntity>")
        (wt / "Test Suites/S1.ts").write_text(
            (wt / "Test Suites/S1.ts").read_text().replace(
                "</TestSuiteEntity>",
                "<testCaseLink><guid>g3</guid><testCaseId>"
                "Test Cases/API/tc3</testCaseId></testCaseLink>"
                "</TestSuiteEntity>"))
        # rediscovery at a NEW SHA — replaces inventory
        b.last_synced_sha = "sha2"; db.session.commit()
        gitsync.discover_project(b, b.last_synced_sha or 'sha1')
        items = DiscoveredItem.query.filter_by(binding_id=b.id).all()
        cases = [i for i in items if i.kind == "case"]
        suites = [i for i in items if i.kind == "suite"]
        assert len(cases) == 3                      # +tc3
        assert len(suites) == 1                     # no duplicates
        assert "tc3" in " ".join(suites[0].members() or [])
        # a NEW direct run uses the updated revision
        ex_new = dispatch.create_execution("m", source_item=suites[0])
        assert ex_new.snapshot()["source_target"]["sha"] == "sha2"
        # the OLD snapshot is immutable — still references sha1
        # old row deleted by inventory replacement — the SNAPSHOT
        # retains its immutable sha (history doesn't depend on it).
        assert ex_old.snapshot()["source_target"]["sha"] == old_sha


def test_removed_definition_rejected_not_retargeted(app, tmp_path):
    """A definition deleted upstream vanishes from inventory; a queued
    run against it fails visibly — never retargets."""
    with app.app_context():
        wt = _fixture(tmp_path / "wt", _base_files())
        b = _binding(app, wt)
        gitsync.discover_project(b, b.last_synced_sha or 'sha1')
        suite = DiscoveredItem.query.filter_by(
            binding_id=b.id, kind="suite").first()
        ex = dispatch.create_execution("m", source_item=suite)
        (wt / "Test Suites/S1.ts").unlink()
        b.last_synced_sha = "sha2"; db.session.commit()
        gitsync.discover_project(b, b.last_synced_sha or 'sha1')
        assert DiscoveredItem.query.filter_by(
            binding_id=b.id, kind="suite").count() == 0
        dispatch.run_execution(ex.id)
        db.session.refresh(ex)
        assert ex.state == ExecutionState.FAILED
        assert "no longer" in ex.error_text
        assert ex.snapshot()["source_target"]["path"].endswith("S1.ts")


def test_failed_discovery_preserves_prior_inventory(app, tmp_path):
    """A failed sync/discovery keeps the previous inventory marked
    stale — not emptied, not retargeted."""
    with app.app_context():
        wt = _fixture(tmp_path / "wt", _base_files())
        b = _binding(app, wt)
        gitsync.discover_project(b, b.last_synced_sha or 'sha1')
        n1 = DiscoveredItem.query.filter_by(binding_id=b.id).count()
        assert n1 > 0
        # simulate failed rediscovery: the worktree is gone
        b.worktree_path = str(tmp_path / "gone"); db.session.commit()
        gitsync.discover_project(b, "bad")
        n2 = DiscoveredItem.query.filter_by(binding_id=b.id).count()
        assert n2 == n1                          # preserved
        db.session.refresh(b)
        assert b.discovery_status in ("failed", "stale")
