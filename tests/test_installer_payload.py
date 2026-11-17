"""Installer payload completeness — regression for the 2.0.2 defect.

scripts/install.py copied the payload but omitted the vendored
``reporting_labs`` package, so systems installed through the supported
installer could not generate reportingLabs reports
(``ModuleNotFoundError: No module named 'reporting_labs'``). Running the
application from the extracted tarball masked it, because the package
sat next to ``wsgi.py`` at the tarball root. These tests pin the
installer-side behaviour, not the tarball layout.

The vendored source lives at ``3rdparty/reporting_labs`` in both the
checkout and the installed payload; the app's scoped bootstrap finder
(testorchestrator/__init__.py) resolves the ``reporting_labs`` import
name from there.
"""
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import install as inst  # noqa: E402  scripts/install.py


def test_payload_items_all_exist_and_cover_3rdparty():
    """Every declared payload item must exist at the package root —
    the installer aborts on a missing source instead of silently
    producing a half-complete payload. reporting-labs arrives via the
    wholesale 3rdparty copy."""
    assert inst.PAYLOAD_ITEMS, "payload copy list is empty"
    missing = [i for i in inst.PAYLOAD_ITEMS if not (REPO / i).exists()]
    assert not missing, f"payload sources missing: {missing}"
    assert "3rdparty" in inst.PAYLOAD_ITEMS
    assert (REPO / "3rdparty" / "reporting_labs"
            / "__init__.py").is_file()


def test_required_payload_files_cover_reporting():
    """The post-copy completeness gate must include the vendored
    report generator and its licence under 3rdparty/."""
    joined = "\n".join(inst.REQUIRED_PAYLOAD_FILES)
    assert "3rdparty/reporting_labs/__init__.py" in joined
    assert "3rdparty/reporting_labs/LICENSE" in joined


def test_root_copy_is_gone_source_lives_in_3rdparty():
    """The relocation must leave exactly one vendored copy — no stale
    root-level package that could mask a broken payload."""
    assert not (REPO / "reporting_labs").exists(), \
        "stale root-level reporting_labs copy present"


def test_copied_payload_resolves_reporting_labs(tmp_path):
    """Simulate the installed payload (app package + 3rdparty vendored
    dir) in a dir outside the checkout, then resolve reporting_labs
    through the app's own bootstrap — no PYTHONPATH, no manual copy.
    A global site-packages package cannot mask the vendored source
    because the bootstrap pins it, and __file__ proves the location."""
    payload = tmp_path / "payload"
    inst.copy_payload_tree(REPO / "3rdparty",
                           payload / "3rdparty")
    inst.copy_payload_tree(REPO / "testorchestrator",
                           payload / "testorchestrator")
    assert (payload / "3rdparty" / "reporting_labs"
            / "__init__.py").is_file()
    assert (payload / "3rdparty" / "reporting_labs"
            / "LICENSE").is_file()
    r = subprocess.run(
        [sys.executable, "-E", "-s", "-c",
         "import testorchestrator, reporting_labs, sys;"
         "assert reporting_labs.__file__.startswith(sys.argv[1]),"
         " reporting_labs.__file__;"
         "print(reporting_labs.__version__)",
         str(payload)],
        capture_output=True, text=True, cwd=payload)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "0.1.14"


def test_bootstrap_resolves_vendored_source():
    """In the checkout itself the bootstrap must produce the vendored
    module — and only the reporting_labs name, nothing else from
    3rdparty becomes importable."""
    finders = [f for f in sys.meta_path
               if type(f).__name__ == "_VendoredReportingLabs"]
    assert finders, "vendored reporting-labs bootstrap not installed"
    spec = finders[0].find_spec("reporting_labs")
    assert spec is not None
    assert str(REPO / "3rdparty" / "reporting_labs") in \
        spec.submodule_search_locations
    # scoped: other 3rdparty dirs must not be handed out by the finder
    assert finders[0].find_spec("dagu") is None
    assert finders[0].find_spec("caddy") is None
    # and the import resolves for real
    import reporting_labs
    assert "3rdparty" in reporting_labs.__file__


def test_isolated_payload_without_package_fails_import(tmp_path):
    """The defect, as a negative control: a payload copy that lacks
    reporting_labs really cannot import it — neither the repo root nor
    site-packages may supply it (isolated mode + site-packages strip,
    since dev environments have carried a stale namespace copy)."""
    r = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys;"
         "sys.path = [p for p in sys.path if 'site-packages' not in p];"
         "import reporting_labs"],
        capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode != 0
    assert "No module named 'reporting_labs'" in r.stderr
