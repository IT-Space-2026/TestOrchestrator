"""Bundled Dagu assets exist and match the recorded checksums."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "3rdparty" / "dagu"
EXPECTED = {
    "dagu_2.25.2_linux_amd64.tar.gz":
        "1708a12b0d88bdc2a895b2c1e59667498faace887fe45ba8fe91d5178f16f4e9",
    "dagu_2.25.2_windows_amd64.tar.gz":
        "d5954c509df6639901fb1fb707287d64610294d31c557f250dc9a6f2931a69da",
}


def test_bundled_dagu_archives_checksum():
    """Both the Linux and Windows archives are vendored and match the
    official release checksums — a remote Dagu worker needs a real
    binary on the target OS, not a download link."""
    for name, want in EXPECTED.items():
        path = ROOT / name
        assert path.exists(), f"missing bundled asset {name}"
        got = hashlib.sha256(path.read_bytes()).hexdigest()
        assert got == want, f"{name} checksum mismatch"


def test_sha256_manifest_covers_both():
    """dagu.sha256 records both archives so downstream verification can
    compare — a missing entry is a missing asset."""
    manifest = (ROOT / "dagu.sha256").read_text()
    for name in EXPECTED:
        assert name in manifest, f"{name} not in dagu.sha256"


def test_remote_installation_never_local():
    """A worker_label-bound installation must NOT run locally —
    selecting it raises, not silently falls back to the portal host."""
    from tests.conftest import make_app
    import tempfile
    from pathlib import Path
    app = make_app(Path(tempfile.mkdtemp()), "remote.db")
    with app.app_context():
        from testorchestrator.execution import (Runner,
                                                RuntimeConnection,
                                                DiscoveredItem,
                                                Project)
        from testorchestrator.extensions import db
        from testorchestrator import dispatch as d
        r = Runner(name="local", connection_mode="local",
                   os_arch="linux", max_slots=2)
        db.session.add(r); db.session.flush()
        rt = RuntimeConnection(name="remote-kre", runner_id=r.id,
                               executable_path="/opt/kre",
                               worker_label="kre-remote-1",
                               enabled=True)
        db.session.add(rt); db.session.flush()
        p = Project(name="p"); db.session.add(p); db.session.flush()
        di = DiscoveredItem(project_id=p.id, binding_id="b",
                            kind="suite", path="T/x.ts", name="x")
        db.session.add(di); db.session.commit()
        import pytest
        with pytest.raises(d.DispatchError,
                           match="not configured/available"):
            d.create_execution("u", None,
                run_config={"runtime_id": rt.id}, source_item=di)
