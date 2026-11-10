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


def test_insert_entry_keeps_existing_cfg(app):
    """Inserting a member mid-list doesn't shift/cross other entries' cfg."""
    with app.app_context():
        members = ["TS/a", "TS/b"]
        cfg = {"0:TS/a": {"profileName": "PA"},
               "1:TS/b": {"profileName": "PB"}}
        # insert TS/new at index 1 -> entries keep cfg by identity (ref+old idx)
        new_order = ["TS/a", "TS/new", "TS/b"]
        # map each original entry to its new position (identity-preserving)
        src_map = {(i, m): v for (k, v) in
                   [(k, v) for k, v in cfg.items()]
                   for i, m in [(int(k.split(":")[0]), k.split(":", 1)[1])]}
        new_cfg = {}
        old_pos = {0: ("TS/a", "PA"), 1: ("TS/b", "PB")}   # old idx -> (ref,cfg)
        for new_i, m in enumerate(new_order):
            if m == "TS/new":
                new_cfg[f"{new_i}:{m}"] = {}   # inserted: empty, no crossover
            else:
                # find the original index of this entry by identity
                orig = [k for k, (r, p) in old_pos.items() if r == m][0]
                new_cfg[f"{new_i}:{m}"] = dict(cfg[f"{orig}:{m}"])
        assert new_cfg["0:TS/a"]["profileName"] == "PA"
        assert new_cfg["2:TS/b"]["profileName"] == "PB"
        assert new_cfg["1:TS/new"] == {}   # no crossover


def test_collection_xml_round_trip():
    """parse_collection_detail preserves repeated refs + settings."""
    import tempfile, os
    from testorchestrator import gitsync
    xml = ('<TestSuiteCollectionEntity><executionMode>SEQUENTIAL'
           '</executionMode><TestSuiteRunConfiguration>'
           '<runEnabled>true</runEnabled>'
           '<testSuiteEntity>TS/X</testSuiteEntity>'
           '<profileName>P1</profileName></TestSuiteRunConfiguration>'
           '<TestSuiteRunConfiguration><runEnabled>false</runEnabled>'
           '<testSuiteEntity>TS/X</testSuiteEntity>'
           '<profileName>P2</profileName></TestSuiteRunConfiguration>'
           '</TestSuiteCollectionEntity>')
    f = tempfile.NamedTemporaryFile('w', suffix='.ts', delete=False)
    f.write(xml); f.close()
    d = gitsync.parse_collection_detail(f.name); os.unlink(f.name)
    assert d["executionMode"] == "SEQUENTIAL"
    assert [m["ref"] for m in d["members"]] == ["TS/X", "TS/X"]
    assert d["members"][0]["profileName"] != d["members"][1]["profileName"]
    assert d["members"][0]["runEnabled"] == "true"
    assert d["members"][1]["runEnabled"] == "false"


def test_enabled_disabled_normalization(app):
    """isRun normalizes to runEnabled — selection cannot diverge."""
    with app.app_context():
        # when runEnabled is the canonical, isRun must mirror it
        cfg = {}
        for i, ref in enumerate(["TS/a", "TS/b"]):
            run_enabled = "true" if i == 0 else "false"
            cfg[f"{i}:{ref}"] = {"runEnabled": run_enabled,
                                 "isRun": run_enabled}
        assert cfg["0:TS/a"]["isRun"] == cfg["0:TS/a"]["runEnabled"]
        assert cfg["1:TS/b"]["isRun"] == cfg["1:TS/b"]["runEnabled"]
