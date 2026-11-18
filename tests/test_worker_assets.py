"""Worker-download assets: bundled into the package, installed into
the payload, and served by the portal from local files only — never
fetched remotely at runtime.

Covers three layers:
- scripts/install.py: packaging/windows Buildbot-worker installer
  lands in the installed payload (superseded versions removed on
  upgrade; a dev package without the artifact warns, never crashes)
- scripts/validate_package.py: the assembled-package gate requires the
  worker .exe and the controller-side buildbot wheels
- the /infrastructure/buildbot-worker-installer route serves
  fabricated payload bytes — proving local-file serving with a strict
  filename policy and admin-only access
"""
import hashlib
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
EXE_GLOB = "TestOrchestrator-Buildbot-Worker-Setup-*.exe"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _worker_exe(dirpath: Path, version: str = "4.3.0",
                unsigned=True) -> Path:
    suffix = "-UNSIGNED" if unsigned else ""
    exe = dirpath / ("TestOrchestrator-Buildbot-Worker-Setup-"
                     f"{version}{suffix}.exe")
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ-fake-worker-installer")
    return exe


def _wheels(dirpath: Path, version: str = "4.3.0") -> None:
    d = dirpath / "3rdparty" / "buildbot-wheels"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"buildbot-{version}-py3-none-any.whl").write_bytes(b"w")
    (d / f"buildbot_worker-{version}-py3-none-any.whl").write_bytes(b"w")
    (d / f"buildbot_www-{version}-py3-none-any.whl").write_bytes(b"w")
    (d / "paramiko-3.5.0-py3-none-any.whl").write_bytes(b"w")
    # §3(a) corresponding-source archive beside the exe + licence texts
    (dirpath / "packaging" / "windows").mkdir(parents=True, exist_ok=True)
    (dirpath / "packaging" / "windows" /
     "TestOrchestrator-Buildbot-Sources-4.3.0.tar.gz").write_bytes(b"s")
    lic = dirpath / "3rdparty" / "licenses"
    lic.mkdir(parents=True, exist_ok=True)
    (lic / "buildbot-COPYING").write_bytes(b"l")
    (lic / "paramiko-LICENSE").write_bytes(b"l")


# ------------------------------------------------------------------
# scripts/install.py — installed-payload coverage
# ------------------------------------------------------------------

class TestInstallWorkerInstallerAsset:
    def _mod(self):
        return _load("to_install", "scripts/install.py")

    def test_copies_bundled_installer_into_payload(self, tmp_path):
        repo = tmp_path / "pkg"
        exe = _worker_exe(repo / "packaging" / "windows")
        payload = tmp_path / "payload"
        out = self._mod().install_worker_installer_asset(repo, payload)
        assert out == (payload / "packaging" / "windows" / exe.name)
        assert out.read_bytes() == b"MZ-fake-worker-installer"

    def test_upgrade_replaces_superseded_version(self, tmp_path):
        repo = tmp_path / "pkg"
        _worker_exe(repo / "packaging" / "windows", "4.4.0")
        payload = tmp_path / "payload"
        old = _worker_exe(payload / "packaging" / "windows", "4.3.0")
        out = self._mod().install_worker_installer_asset(repo, payload)
        assert not old.exists(), "stale worker installer must be removed"
        assert "4.4.0" in out.name
        assert len(list(out.parent.glob(EXE_GLOB))) == 1

    def test_missing_installer_warns_not_crashes(self, tmp_path):
        """Dev builds without GPL approval legitimately lack the exe —
        warn and continue (release validation is the hard gate)."""
        repo = tmp_path / "pkg"
        (repo / "packaging" / "windows").mkdir(parents=True)
        out = self._mod().install_worker_installer_asset(
            repo, tmp_path / "payload")
        assert out is None


# ------------------------------------------------------------------
# scripts/validate_package.py — assembled-package gate
# ------------------------------------------------------------------

class TestPackageWorkerAssetsGate:
    def _gate(self):
        return _load("to_validate", "scripts/validate_package.py"
                     )._worker_assets

    def _pkg(self, tmp_path, version="4.3.0", exes=1, wheels=True):
        pkg = tmp_path / "pkg"
        for i in range(exes):
            _worker_exe(pkg / "packaging" / "windows",
                        version if i == 0 else "9.9.9")
        if wheels:
            _wheels(pkg, version)
        return pkg

    def test_complete_package_passes(self, tmp_path):
        self._gate()(self._pkg(tmp_path))

    @pytest.mark.parametrize("fault", ["no_exe", "two_exes",
                                       "no_wheels"])
    def test_incomplete_package_rejected(self, tmp_path, fault):
        kw = {"exes": 0 if fault == "no_exe"
                      else 2 if fault == "two_exes" else 1,
              "wheels": fault != "no_wheels"}
        with pytest.raises(AssertionError):
            self._gate()(self._pkg(tmp_path, **kw))


# ------------------------------------------------------------------
# Portal routes — the installed payload serves local files only
# ------------------------------------------------------------------

def _fabricate_payload(app, monkeypatch, tmp_path):
    """Redirect current_app.root_path at a fabricated installed layout:
    root/testorchestrator (package dir) + packaging/windows exe."""
    root = tmp_path / "installed"
    pkg_dir = root / "testorchestrator"
    pkg_dir.mkdir(parents=True)
    exe = _worker_exe(root / "packaging" / "windows")
    monkeypatch.setattr(app, "root_path", str(pkg_dir))
    return root, exe


class TestWorkerDownloads:
    URL_I = "/system/infrastructure/buildbot-worker-installer/{}"

    def test_installer_served_from_installed_payload(
            self, app, client, login_as, monkeypatch, tmp_path):
        login_as("admin")
        _, exe = _fabricate_payload(app, monkeypatch, tmp_path)
        r = client.get(self.URL_I.format(exe.name))
        assert r.status_code == 200
        assert r.get_data() == b"MZ-fake-worker-installer"
        assert "attachment" in r.headers["Content-Disposition"]

    @pytest.mark.parametrize("name", [
        "TestOrchestrator-Buildbot-Worker-Setup-4.3.0.exe",
        "TestOrchestrator-Buildbot-Worker-Setup-4.3.0-UNSIGNED.msi",
        "../TestOrchestrator-Buildbot-Worker-Setup-4.3.0-UNSIGNED.exe",
        "testorchestrator-legacy-worker-4.3.0-setup-UNSIGNED.exe",
        "evil.exe",
    ])
    def test_installer_name_outside_policy_rejected(
            self, app, client, login_as, monkeypatch, tmp_path, name):
        login_as("admin")
        _fabricate_payload(app, monkeypatch, tmp_path)
        assert client.get(self.URL_I.format(name)).status_code == 404

    def test_absent_asset_404s(
            self, app, client, login_as, monkeypatch, tmp_path):
        login_as("admin")
        _fabricate_payload(app, monkeypatch, tmp_path)
        assert client.get(self.URL_I.format(
            "TestOrchestrator-Buildbot-Worker-Setup-8.8.8-UNSIGNED.exe"
        )).status_code == 404

    def test_admin_only(self, app, client, login_as,
                        monkeypatch, tmp_path):
        login_as("tester")
        _, exe = _fabricate_payload(app, monkeypatch, tmp_path)
        assert client.get(self.URL_I.format(exe.name)
                          ).status_code in (302, 403)


class TestWorkerAssetListing:
    """The infrastructure page's asset metadata reads the local payload
    — version from the filename, checksum computed from shipped bytes."""

    def test_installer_listing_metadata(
            self, app, monkeypatch, tmp_path):
        _, exe = _fabricate_payload(app, monkeypatch, tmp_path)
        from testorchestrator.system.routes import \
            _bb_worker_installer_asset
        with app.app_context():
            a = _bb_worker_installer_asset()
        assert a["file"] == exe.name
        assert a["version"] == "4.3.0"
        assert a["sha256"] == hashlib.sha256(
            b"MZ-fake-worker-installer").hexdigest()
        assert a["size"] == len(b"MZ-fake-worker-installer")

    def test_listing_absent_when_unbundled(
            self, app, monkeypatch, tmp_path):
        root = tmp_path / "installed"
        (root / "testorchestrator").mkdir(parents=True)
        monkeypatch.setattr(app, "root_path",
                            str(root / "testorchestrator"))
        from testorchestrator.system.routes import \
            _bb_worker_installer_asset
        with app.app_context():
            assert _bb_worker_installer_asset() is None
