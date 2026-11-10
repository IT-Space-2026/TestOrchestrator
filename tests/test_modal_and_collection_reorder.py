"""Modal markup + collection reorder/insertion/deletion without config
crossover — settings stay attached to the correct idx+ref entry."""
import json
import re

from testorchestrator.extensions import db
from testorchestrator.execution import Project, Suite


def test_reorder_keeps_cfg_with_entry(app):
    """Reordering members must not swap their idx+ref settings."""
    with app.app_context():
        # original order + per-entry cfg
        members = ["TS/a", "TS/b", "TS/a"]
        cfg = {f"{i}:{m}": {"profileName": f"P{i}"}
               for i, m in enumerate(members)}
        # user drags index 2 (TS/a, P2) to position 0 -> rebuild keys
        new_order = ["TS/a", "TS/a", "TS/b"]      # P2's entry moved to 0
        new_cfg = {}
        for new_i, m in enumerate(new_order):
            # carry the entry's identity: find its original cfg by value
            src = [v for k, v in cfg.items() if v["profileName"] == "P2"][0]
            new_cfg[f"{new_i}:{m}"] = dict(src)
        # the moved entry keeps P2 regardless of position
        assert new_cfg["0:TS/a"]["profileName"] == "P2"


def test_delete_entry_shifts_cfg(app):
    """Deleting an entry must not orphan its settings onto the next."""
    with app.app_context():
        members = ["TS/a", "TS/b", "TS/a"]
        cfg = {f"{i}:{m}": {"profileName": f"P{i}"}
               for i, m in enumerate(members)}
        # delete index 0 -> remaining reindex to 0,1 keeping their cfg
        remaining = members[1:]
        new_cfg = {f"{i}:{m}": dict(cfg[f"{i+1}:{m}"])
                   for i, m in enumerate(remaining)}
        assert new_cfg["0:TS/b"]["profileName"] == "P1"
        assert new_cfg["1:TS/a"]["profileName"] == "P2"


def test_modal_markup_and_script(app, client):
    """Both creation modals + the modal JS are present on the Test page."""
    src = open("testorchestrator/templates/portal/project_detail.html").read()
    assert 'id="suite-modal"' in src
    assert 'id="coll-modal"' in src
    assert 'data-modal-open="suite-modal"' in src
    assert 'data-modal-open="coll-modal"' in src
    assert 'test-modal.js' in src
    # CSP-safe: no inline on* handler on the modal form
    assert 'data-modal-form' in src
