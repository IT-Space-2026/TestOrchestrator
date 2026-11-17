"""Worker-download assets: bundled into both packages, installed into
the payload, and served by the portal from local files only — never
fetched from GitHub at runtime.

Covers three layers:
- scripts/install.py: packaging/windows worker installer lands in the
  installed payload (and superseded versions are removed on upgrade)
- scripts/validate_package.py: the assembled-package gate rejects
  missing/mismatched/skewed assets
- the two /infrastructure download routes serve fabricated payload
  bytes — proving local-file serving with no remote fetch
"""
import hashlib
import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _worker_exe(dirpath: Path, version: str = "2.27.0") -> Path:
    exe = dirpath / ("testorchestrator-dagu-worker-"
                     f"{version}-setup-UNSIGNED.exe")
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ-fake-worker-installer")
    return exe


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
        _worker_exe(repo / "packaging" / "windows", "2.28.0")
        payload = tmp_path / "payload"
        old = _worker_exe(payload / "packaging" / "windows", "2.27.0")
        out = self._mod().install_worker_installer_asset(repo, payload)
        assert not old.exists(), "stale worker installer must be removed"
        assert "2.28.0" in out.name
        assert len(list(out.parent.glob(
            "testorchestrator-dagu-worker-*-setup-UNSIGNED.exe"))) == 1

    def test_missing_installer_refuses_install(self, tmp_path):
        repo = tmp_path / "pkg"
        (repo / "packaging" / "windows").mkdir(parents=True)
        with pytest.raises(SystemExit):
            self._mod().install_worker_installer_asset(
                repo, tmp_path / "payload")


# ------------------------------------------------------------------
# scripts/validate_package.py — assembled-package gate
# ------------------------------------------------------------------

class TestPackageWorkerAssetsGate:
    def _gate(self):
        return _load("to_validate", "scripts/validate_package.py"
                     )._worker_assets

    def _pkg(self, tmp_path, dagu_ver="2.27.0", skewed=False,
             corrupt=False, drop_archive=False, exes=1):
        pkg = tmp_path / "pkg"
        dagu = pkg / "3rdparty" / "dagu"
        dagu.mkdir(parents=True)
        pins = []
        for i, os_ in enumerate(("linux", "windows")):
            ver = f"2.28.{i}" if skewed else dagu_ver
            name = f"dagu_{ver}_{os_}_amd64.tar.gz"
            data = b"corrupt" if (corrupt and i == 0) else \
                f"archive-{os_}".encode()
            if not (drop_archive and i == 0):
                (dagu / name).write_bytes(data)
            pins.append((name, hashlib.sha256(
                f"archive-{os_}".encode()).hexdigest()))
        (dagu / "dagu.sha256").write_text(
            "".join(f"{h}  {n}\n" for n, h in pins))
        for i in range(exes):
            _worker_exe(pkg / "packaging" / "windows",
                        dagu_ver if i == 0 else "9.9.9")
        return pkg

    def test_complete_package_passes(self, tmp_path):
        self._gate()(self._pkg(tmp_path))

    @pytest.mark.parametrize("fault", ["drop_archive", "corrupt",
                                       "skewed", "no_exe", "two_exes"])
    def test_incomplete_package_rejected(self, tmp_path, fault):
        kw = {"drop_archive": fault == "drop_archive",
              "corrupt": fault == "corrupt",
              "skewed": fault == "skewed",
              "exes": 0 if fault == "no_exe"
                      else 2 if fault == "two_exes" else 1}
        with pytest.raises(AssertionError):
            self._gate()(self._pkg(tmp_path, **kw))


# ------------------------------------------------------------------
# Portal routes — the installed payload serves local files only
# ------------------------------------------------------------------

def _fabricate_payload(app, monkeypatch, tmp_path):
    """Redirect current_app.root_path at a fabricated installed layout:
    root/testorchestrator (package dir) + 3rdparty/dagu + packaging."""
    root = tmp_path / "installed"
    pkg_dir = root / "testorchestrator"
    pkg_dir.mkdir(parents=True)
    dagu = root / "3rdparty" / "dagu"
    dagu.mkdir(parents=True)
    (dagu / "dagu_9.9.9_linux_amd64.tar.gz").write_bytes(
        b"linux-worker-archive")
    (dagu / "dagu_9.9.9_windows_amd64.tar.gz").write_bytes(
        b"windows-worker-archive")
    exe = _worker_exe(root / "packaging" / "windows")
    monkeypatch.setattr(app, "root_path", str(pkg_dir))
    return root, exe


class TestWorkerDownloads:
    URL_A = "/system/infrastructure/dagu-worker/{}"
    URL_I = "/system/infrastructure/dagu-worker-installer/{}"

    def test_archives_served_from_installed_payload(
            self, app, client, login_as, monkeypatch, tmp_path):
        login_as("admin")
        _fabricate_payload(app, monkeypatch, tmp_path)
        for os_, body in (("linux", b"linux-worker-archive"),
                          ("windows", b"windows-worker-archive")):
            r = client.get(self.URL_A.format(
                f"dagu_9.9.9_{os_}_amd64.tar.gz"))
            assert r.status_code == 200
            assert r.get_data() == body   # local bytes — no remote fetch
            assert "attachment" in r.headers["Content-Disposition"]

    def test_installer_served_from_installed_payload(
            self, app, client, login_as, monkeypatch, tmp_path):
        login_as("admin")
        _, exe = _fabricate_payload(app, monkeypatch, tmp_path)
        r = client.get(self.URL_I.format(exe.name))
        assert r.status_code == 200
        assert r.get_data() == b"MZ-fake-worker-installer"

    @pytest.mark.parametrize("name", [
        "../dagu_9.9.9_linux_amd64.tar.gz",
        "..%2F..%2Fetc%2Fpasswd",
        "dagu_9.9.9_freebsd_amd64.tar.gz",
        "dagu_9.9.9_linux_amd64.zip",
        "evil.tar.gz",
    ])
    def test_archive_name_outside_policy_rejected(
            self, app, client, login_as, monkeypatch, tmp_path, name):
        login_as("admin")
        _fabricate_payload(app, monkeypatch, tmp_path)
        assert client.get(self.URL_A.format(name)).status_code == 404

    @pytest.mark.parametrize("name", [
        "testorchestrator-dagu-worker-9.9.9-setup.exe",
        "testorchestrator-dagu-worker-9.9.9-setup-UNSIGNED.msi",
        "../testorchestrator-dagu-worker-9.9.9-setup-UNSIGNED.exe",
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
        assert client.get(self.URL_A.format(
            "dagu_8.8.8_linux_amd64.tar.gz")).status_code == 404
        assert client.get(self.URL_I.format(
            "testorchestrator-dagu-worker-8.8.8-setup-UNSIGNED.exe"
        )).status_code == 404

    def test_admin_only(self, app, client, login_as,
                        monkeypatch, tmp_path):
        login_as("tester")
        _, exe = _fabricate_payload(app, monkeypatch, tmp_path)
        assert client.get(self.URL_A.format(
            "dagu_9.9.9_linux_amd64.tar.gz")).status_code in (302, 403)
        assert client.get(self.URL_I.format(exe.name)
                          ).status_code in (302, 403)


class TestWorkerAssetListing:
    """The infrastructure page's asset inventory reads the same local
    payload dirs — checksums come from the shipped pin manifest."""

    def test_installer_listing_metadata(
            self, app, monkeypatch, tmp_path):
        _, exe = _fabricate_payload(app, monkeypatch, tmp_path)
        from testorchestrator.system.routes import \
            _worker_installer_asset
        with app.app_context():
            a = _worker_installer_asset()
        assert a["file"] == exe.name
        assert a["version"] == "2.27.0"
        assert a["sha256"] == hashlib.sha256(
            b"MZ-fake-worker-installer").hexdigest()
        assert a["size"] == len(b"MZ-fake-worker-installer")

    def test_archive_listing_uses_pin_manifest(
            self, app, monkeypatch, tmp_path):
        root, _ = _fabricate_payload(app, monkeypatch, tmp_path)
        dagu = root / "3rdparty" / "dagu"
        (dagu / "dagu.sha256").write_text(
            f"{hashlib.sha256(b'linux-worker-archive').hexdigest()}  "
            "dagu_9.9.9_linux_amd64.tar.gz\n")
        from testorchestrator.system.routes import _dagu_worker_assets
        with app.app_context():
            assets = _dagu_worker_assets()
        by_name = {a["file"]: a for a in assets}
        lin = by_name["dagu_9.9.9_linux_amd64.tar.gz"]
        assert lin["os"] == "linux" and lin["version"] == "9.9.9"
        assert lin["sha256"] == hashlib.sha256(
            b"linux-worker-archive").hexdigest()
        # unpinned archive still listed, but without a manifest checksum
        assert by_name["dagu_9.9.9_windows_amd64.tar.gz"
                       ]["sha256"] == ""
