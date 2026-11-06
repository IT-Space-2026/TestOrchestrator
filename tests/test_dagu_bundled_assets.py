"""Bundled Dagu assets exist and match the recorded checksums."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "3rdparty" / "dagu"
EXPECTED = {
    "dagu_2.25.8_linux_amd64.tar.gz":
        "f5ab106c186c698fb203191c0b041e3fac9e1e491bf2e5047e521fada7dc4593",
    "dagu_2.25.8_windows_amd64.tar.gz":
        "aa6ce7d1b4190bd3289f15d3b7a8bf2fe2fa0554a62b722fe505220f3cd083ea",
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
        db.create_all()
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
        from testorchestrator.execution import GitBinding
        b = GitBinding(project_id=p.id, name="b", url="/x")
        db.session.add(b); db.session.flush()
        di = DiscoveredItem(project_id=p.id, binding_id=b.id,
                            kind="suite", path="T/x.ts", name="x",
                            sha="s", order_idx=0)
        db.session.add(di); db.session.commit()
        import pytest
        with pytest.raises(d.DispatchError,
                           match="not configured/available"):
            d.create_execution("u", None,
                run_config={"runtime_id": rt.id}, source_item=di)


def test_explicit_db_uri_survives_decoy_env(monkeypatch, tmp_path):
    """Regression: an explicit test database URI must win over decoy
    TO_INSTANCE_DIR/TO_DATABASE_URI environment variables. The engine
    used for create_all() and INSERTs is the file the fixture named —
    a stale real database must never answer in its place (previously
    suspected when an INSERT hit an old-schema runners table)."""
    import sqlite3
    import tempfile
    from pathlib import Path
    from tests.conftest import make_app
    decoy_dir = Path(tempfile.mkdtemp())
    decoy_db = decoy_dir / "decoy.db"
    monkeypatch.setenv("TO_INSTANCE_DIR", str(decoy_dir))
    monkeypatch.setenv("TO_DATABASE_URI", f"sqlite:///{decoy_db}")
    app = make_app(tmp_path, "isolated.db")
    intended = tmp_path / "isolated.db"
    with app.app_context():
        from testorchestrator.extensions import db
        from testorchestrator.execution import Runner
        # Engine identity is fixed by the passed config, not the env.
        assert db.engine.url.database == str(intended)
        db.create_all()
        db.session.add(Runner(name="r", connection_mode="local",
                              os_arch="linux", max_slots=1))
        db.session.commit()
    # The INSERT landed in the intended file...
    with sqlite3.connect(intended) as con:
        assert con.execute("SELECT COUNT(*) FROM runners").fetchone()[0] == 1
    # ...and the decoy was never even opened.
    assert not decoy_db.exists()
    assert not any(decoy_dir.iterdir())
