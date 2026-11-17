"""Installer payload completeness — regression for the 2.0.2 defect.

scripts/install.py copied the payload but omitted the vendored
``reporting_labs`` package, so systems installed through the supported
installer could not generate reportingLabs reports
(``ModuleNotFoundError: No module named 'reporting_labs'``). Running the
application from the extracted tarball masked it, because the package
sat next to ``wsgi.py`` at the tarball root. These tests pin the
installer-side behaviour, not the tarball layout.
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import install as inst  # noqa: E402  scripts/install.py


def test_payload_items_all_exist_and_include_vendored_package():
    """Every declared payload item must exist at the package root —
    the installer now aborts on a missing source instead of silently
    producing a half-complete payload."""
    assert inst.PAYLOAD_ITEMS, "payload copy list is empty"
    missing = [i for i in inst.PAYLOAD_ITEMS if not (REPO / i).exists()]
    assert not missing, f"payload sources missing: {missing}"
    assert "reporting_labs" in inst.PAYLOAD_ITEMS


def test_required_payload_files_cover_reporting():
    """The post-copy completeness gate must include the report
    generator and its licence."""
    joined = "\n".join(inst.REQUIRED_PAYLOAD_FILES)
    assert "reporting_labs/__init__.py" in joined
    assert "reporting_labs/LICENSE" in joined


def test_copied_payload_resolves_reporting_labs(tmp_path):
    """Copy the package exactly as the installer does, then import it
    from the payload dir the way the running app does (cwd-based
    resolution, no PYTHONPATH). A repository-root copy or a globally
    installed package cannot mask a missing payload file because the
    assertion pins __file__ inside the copied payload."""
    payload = tmp_path / "payload"
    inst.copy_payload_tree(REPO / "reporting_labs",
                           payload / "reporting_labs")
    assert (payload / "reporting_labs" / "__init__.py").is_file()
    assert (payload / "reporting_labs" / "LICENSE").is_file()
    r = subprocess.run(
        [sys.executable, "-E", "-s", "-c",
         "import reporting_labs, sys;"
         "assert reporting_labs.__file__.startswith(sys.argv[1]),"
         " reporting_labs.__file__;"
         "print(reporting_labs.__version__)",
         str(payload)],
        capture_output=True, text=True, cwd=payload)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "0.1.14"


def test_isolated_payload_without_package_fails_import(tmp_path):
    """The defect, as a negative control: a payload copy that lacks
    reporting_labs really cannot import it (isolated mode, so neither
    the repo root nor site-packages can supply it)."""
    r = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys;"
         "sys.path = [p for p in sys.path if 'site-packages' not in p];"
         "import reporting_labs"],
        capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode != 0
    assert "No module named 'reporting_labs'" in r.stderr
