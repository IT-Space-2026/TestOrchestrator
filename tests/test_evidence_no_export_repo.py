"""Evidence retention/report access does NOT require an export repo.

`git_export_repo` is optional: with it unset, a project still retains
execution evidence, serves artifacts, and hosts the report view —
the local evidence store is authoritative (§11)."""

import hashlib
from pathlib import Path

from testorchestrator import settings as settings_mod
from testorchestrator.dispatch import create_execution
from testorchestrator.execution import (Artifact, ExecutionState, Project,
                                        Suite)
from testorchestrator.extensions import db
from tests.conftest import login


def test_evidence_and_report_without_export_repo(app, client, tmp_path):
    """With git_export_repo unset, evidence + report view still work."""
    with app.app_context():
        settings_mod.set("git_export_repo", "")
        db.session.commit()
        p = Project(name="P"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        ex = create_execution("m", s, source_sha="sha1")
        ex.state = ExecutionState.COMPLETED
        ex.report_status = "ready"

        # report dir doubles as the evidence root for the artifact route
        repdir = tmp_path / "rep"
        repdir.mkdir()
        logf = repdir / "out.log"
        logf.write_text("execution log content")
        (repdir / "index.html").write_text("<html>report</html>")
        a = Artifact(execution_id=ex.id, kind="log",
                     rel_path="out.log",
                     sha256=hashlib.sha256(logf.read_bytes()).hexdigest(),
                     mime="text/plain", size=logf.stat().st_size)
        db.session.add(a)
        ex.report_dir = str(repdir)
        ex.report_path = "index.html"
        db.session.commit()
        exid, aid = ex.id, a.id

    login(client, "admin")
    # evidence artifact downloadable (checksum-verified, attachment)
    ra = client.get(f"/executions/{exid}/artifacts/{aid}")
    assert ra.status_code == 200
    assert ra.get_data() == b"execution log content"
    # hosted report view serves from local evidence — no export repo
    rv = client.get(f"/executions/{exid}/report")
    assert rv.status_code == 200
    assert rv.headers.get("Content-Security-Policy") == "sandbox"
