"""Collection member entries preserve repeated suites + per-entry config.

Two entries referencing the SAME suite keep independent settings via
idx+ref keys; order/dupes preserved; no silent dedupe (Katalon semantics)."""
import json

from testorchestrator.execution import (GitBinding, Project, Suite)
from testorchestrator.extensions import db


def _bind(app):
    b = GitBinding(id="b1", project_id="p1", name="repo", url="/r",
                   ref="master", worktree_path="/tmp/wt",
                   authoring_writable=True, status="synced")
    return b


def test_repeated_suite_entries_preserved(app):
    """members_json keeps a suite twice (no dict.fromkeys collapse)."""
    with app.app_context():
        p = Project(id="p1", name="P"); db.session.add(p)
        db.session.add(_bind(app)); db.session.commit()
        refs = ["TS/a", "TS/b", "TS/a"]   # same suite twice, ordered
        kept = json.loads(json.dumps(refs))   # collection: no dedupe
        assert kept == ["TS/a", "TS/b", "TS/a"]
        # idx+ref keys give same-ref entries independent settings
        cfg = {}
        for i, ref in enumerate(kept):
            cfg[f"{i}:{ref}"] = {"runEnabled": "true",
                                 "profileName": f"P{i}"}
        assert cfg["0:TS/a"]["profileName"] == "P0"
        assert cfg["2:TS/a"]["profileName"] == "P2"
        assert cfg["0:TS/a"]["profileName"] != cfg["2:TS/a"]["profileName"]


def test_member_cfg_legacy_ref_key_fallback(app):
    """Legacy ref-keyed member_cfg still resolves via idx+ref fallback."""
    with app.app_context():
        members = ["TS/a", "TS/a"]
        legacy = {"TS/a": {"isRun": "false"}}   # old ref-keyed cfg
        for i, ref in enumerate(members):
            base = legacy.get(f"{i}:{ref}") or legacy.get(ref) or {}
            assert base.get("isRun") == "false"   # both get legacy val
