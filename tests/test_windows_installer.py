"""Windows controller packaging — static + provisioning contracts.

The real installer runs on the customer's Windows host (acceptance is
manual there); these tests lock the Linux-side build/provisioning
surface: the NSIS script's component topology, provision_buildbot's
--no-units/entry-point behaviour, the exec-shim master.cfg fallback,
and init_env's secret preservation.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "build-scripts"))

import init_env  # noqa: E402
import provision_buildbot as pb  # noqa: E402

NSI = REPO / "packaging" / "windows" / "app-installer.nsi"
TASKS = ("TestOrchestratorWeb", "TestOrchestratorWorker",
         "TestOrchestratorBuildbotMaster",
         "TestOrchestratorBuildbotLocal",
         "TestOrchestratorBuildbotSSH")


def test_nsi_exists_and_has_no_legacy_components():
    text = NSI.read_text()
    low = text.lower()
    assert "dagu" not in low and "caddy" not in low
    assert "provision_dagu" not in low


def test_nsi_registers_five_tasks_with_buildbot_topology():
    text = NSI.read_text()
    for task in TASKS:
        assert text.count(f'/TN "{task}"') >= 3  # create+run in install,
        # delete in uninstall


def test_nsi_provisions_buildbot_without_systemd():
    text = NSI.read_text()
    assert "provision_buildbot.py" in text
    assert "--setup --no-units" in text
    assert "buildbot-py" in text  # dedicated embedded runtime


def test_nsi_wires_env_and_cert_trust():
    text = NSI.read_text()
    for key in ("TO_INSTANCE_DIR", "TO_BUILDBOT_ENDPOINT",
                "TO_BUILDBOT_HOME", "TO_BUILDBOT_PB_PORT",
                "TO_HTTPS_PORT"):
        assert key in text
    # install-unique secrets come from init_env.py, not the static file
    assert "init_env.py" in text
    # per-install CA trusted via the portal's own generated root —
    # never a shared/distributed key
    assert r"certs\local-ca.pem" in text
    assert "certutil -addstore" in text


def test_provision_no_units_skips_service_files(tmp_path):
    pb.main(["--home", str(tmp_path / "bb"), "--repo-root", str(REPO),
             "--no-units"])
    assert not list((tmp_path / "bb").rglob("*.service"))
    # data files are still provisioned
    assert (tmp_path / "bb" / "secrets.json").is_file()
    assert (tmp_path / "bb" / "targets.json").is_file()
    assert (tmp_path / "bb" / "master.cfg").is_file()


def test_provision_preserves_credentials_on_rerun(tmp_path):
    home = tmp_path / "bb"
    pb.main(["--home", str(home), "--repo-root", str(REPO),
             "--no-units"])
    first = json.loads((home / "secrets.json").read_text())
    pb.main(["--home", str(home), "--repo-root", str(REPO),
             "--no-units"])
    second = json.loads((home / "secrets.json").read_text())
    assert first["api_password"] == second["api_password"]
    assert first["worker_passwords"] == second["worker_passwords"]


def test_link_master_cfg_symlink_and_shim_fallback(tmp_path):
    real = tmp_path / "master.cfg"
    real.write_text("# real cfg\n")
    link = tmp_path / "master" / "master.cfg"
    link.parent.mkdir()
    pb._link_master_cfg(real, link)
    assert link.is_symlink() or "exec(compile" in link.read_text()

    # forced fallback: OSError -> exec-shim loading the real file under
    # its own __file__ (keeps BB_DIR = <home>)
    link2 = tmp_path / "m2" / "master.cfg"
    link2.parent.mkdir()

    def _boom(self, target):
        raise OSError("no privilege")

    orig = Path.symlink_to
    Path.symlink_to = _boom
    try:
        pb._link_master_cfg(real, link2)
    finally:
        Path.symlink_to = orig
    shim = link2.read_text()
    assert "exec(compile" in shim
    assert str(real) in shim


def test_init_env_generates_and_preserves_secrets(tmp_path):
    env = tmp_path / "testorchestrator.env"
    env.write_text("TO_INSTANCE_DIR=X\n")
    added = init_env.ensure(env)
    assert set(added) == {"TO_SECRET_KEY", "TO_INSTALL_ID"}
    text = env.read_text()
    key = [l.split("=", 1)[1] for l in text.splitlines()
           if l.startswith("TO_SECRET_KEY=")][0]
    assert len(key) == 48
    # rerun preserves
    assert init_env.ensure(env) == []
    assert env.read_text() == text


@pytest.mark.parametrize("rel", [
    "build-scripts/build_windows.py",
    "packaging/windows/app-installer.nsi",
    "scripts/init_env.py",
])
def test_windows_build_files_exist(rel):
    assert (REPO / rel).is_file()


def test_build_windows_stages_controller_not_only_worker():
    """Regression: build_windows.py must stage the full controller —
    embedded python, buildbot-py runtime, NSIS compile — not stop at
    the remote-worker installer."""
    src = (REPO / "build-scripts" / "build_windows.py").read_text()
    for token in ("stage_app_payload", "buildbot-py",
                  "app-installer.nsi", "site-packages",
                  "check_payload", "TO_BUILDBOT_GPL_APPROVED"):
        assert token in src


# ---------- Linux worker bundle (offline install asset) ----------

LINUX_BUNDLE_RE = (r"TestOrchestrator-Buildbot-Worker-\d+\.\d+\.\d+"
                   r"-linux-x86_64\.tar\.gz")


def test_linux_worker_bundle_route_and_template():
    """The Admin UI must serve the linux worker bundle beside the .exe
    — strict filename regex, no path traversal."""
    routes = (REPO / "testorchestrator" / "system" / "routes.py")         .read_text()
    assert "buildbot-worker-linux" in routes
    assert "_bb_linux_worker_asset" in routes
    tpl = (REPO / "testorchestrator" / "templates" / "system"
           / "infrastructure.html").read_text()
    assert "buildbot_worker_linux_download" in tpl
    assert "bb_linux_worker" in tpl


def test_linux_worker_script_is_offline_first():
    """install_linux_worker.sh must default to the bundled wheels and
    carry the get-pip fallback — PyPI is opt-in (--online) only."""
    sh = (REPO / "scripts" / "install_linux_worker.sh").read_text()
    assert "--no-index --find-links" in sh
    assert "get-pip.py" in sh
    assert "--without-pip" in sh


def test_build_stages_linux_worker_bundle():
    """Both build pipelines must produce the linux bundle and stage it
    beside the .exe for the Admin UI."""
    common = (REPO / "build-scripts" / "_common.py").read_text()
    assert "def build_linux_worker_bundle" in common
    for script in ("build_linux.py", "build_windows.py"):
        src_ = (REPO / "build-scripts" / script).read_text()
        assert "build_linux_worker_bundle" in src_, script
    # install.py stages it into the served payload dir
    inst = (REPO / "scripts" / "install.py").read_text()
    assert "linux-x86_64.tar.gz" in inst


def test_linux_worker_bundle_contents_if_built():
    """When the bundle artifact exists in the tree it must contain the
    script, the wheel dir, and get-pip."""
    import tarfile
    bundles = sorted((REPO / "packaging" / "windows").glob(
        "TestOrchestrator-Buildbot-Worker-*-linux-x86_64.tar.gz"))
    if not bundles:
        pytest.skip("linux worker bundle not staged in this checkout")
    with tarfile.open(bundles[-1]) as t:
        names = set(t.getnames())
    assert "install_linux_worker.sh" in names
    assert "get-pip.py" in names
    assert any(n.startswith("bb-wheels/buildbot_worker")
               for n in names)

WORKER_NSI = REPO / "packaging" / "windows" / "buildbot-worker-installer.nsi"


def test_worker_nsi_interactive_config_and_logging():
    """Regression: the interactive worker install must collect master/
    name/password itself and persist a real log — a bare default of a
    nonexistent worker-password.txt previously died inside
    setup-worker.py with a raw FileNotFoundError."""
    text = WORKER_NSI.read_text()
    assert "Page custom ConfigPage ConfigPageLeave" in text
    assert "nsDialogs.nsh" in text
    assert "NSD_CreatePassword" in text
    # password persisted to the install-owned file, never a bare path
    assert r"$DATA_DIR\worker-password.txt" in text
    # early validation: missing file aborts with a clear message,
    # not a python traceback
    assert '${IfNot} ${FileExists} "$PASSWD_FILE"' in text
    # persistent log — the details pane is ephemeral
    assert r"$DATA_DIR\setup-worker.log" in text


def test_setup_worker_clean_error_on_missing_passwd():
    src = (REPO / "packaging" / "windows" / "bb-worker-payload-src"
           / "setup-worker.py").read_text()
    assert "password file missing or empty" in src
    assert "except (OSError, IndexError)" in src


def test_linux_worker_validates_nonempty_passwd_and_logs():
    sh = (REPO / "scripts" / "install_linux_worker.sh").read_text()
    assert '[ -s "$PASSWD_FILE" ]' in sh
    assert "worker-install.log" in sh


def test_setup_worker_installs_pywin32_and_registers_dlls():
    """Regression: Twisted on Windows needs win32api to load the tac —
    the wheel was downloaded into wheels/ but never installed, so the
    task died before twistd.log existed ('No module named win32api').
    The install list must carry pywin32 AND run its post-install so the
    pywintypes/pythoncom DLLs resolve under the SYSTEM task."""
    src = (REPO / "packaging" / "windows" / "bb-worker-payload-src"
           / "setup-worker.py").read_text()
    assert '"pywin32"' in src
    assert "pywin32_postinstall.py" in src
    assert "import win32api" in src  # post-install import check
