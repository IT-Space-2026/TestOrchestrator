"""Git acquisition tool tests — index parsing, closure resolution,
digest verification, and manifest honesty. Acquisition itself is
verified separately on the build host (3rdparty/git/*/manifest)."""

import hashlib
import json
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import acquire_git


INDEX = """Package: git
Version: 1:2.53.0-1ubuntu1
Priority: optional
Filename: pool/main/g/git/git_2.53.0_amd64.deb
SHA256: aaa
Depends: libc6 (>= 2.39), perl:any, libcurl4t64 | libcurl3t64-gnutls, libvirt0 [s390x]
Provides: git-completion

Package: libc6
Version: 2.42-0ubuntu3
Priority: required
Filename: pool/main/g/glibc/libc6.deb
SHA256: bbb

Package: perl
Version: 5.40.1-7build1
Priority: standard
Essential: yes
Filename: pool/main/p/perl/perl.deb
SHA256: ccc

Package: libcurl3t64-gnutls
Version: 8.18.0-1ubuntu2
Priority: optional
Filename: pool/main/c/curl/libcurl3t64.deb
SHA256: ddd
"""


def test_parse_packages():
    pkgs = acquire_git.parse_packages(INDEX.encode())
    assert pkgs["git"].version == "1:2.53.0-1ubuntu1"
    assert pkgs["libc6"].priority == "required"
    assert pkgs["perl"].essential == "yes"


def test_closure_resolves_alternatives_any_and_base():
    pkgs = acquire_git.parse_packages(INDEX.encode())
    bundle, boundary = acquire_git.resolve_closure("git", pkgs)
    names = {p.name for p in bundle}
    assert names == {"git", "libcurl3t64-gnutls"}  # | alt picked, [s390x] skipped
    assert "libc6" in boundary and "perl" in boundary  # base-priority
    assert not any(b.startswith("MISSING:") for b in boundary)


def test_closure_reports_missing():
    pkgs = acquire_git.parse_packages(
        "Package: git\nVersion: 1\nDepends: nonexistent-pkg\n".encode())
    bundle, boundary = acquire_git.resolve_closure("git", pkgs)
    assert any(b.startswith("MISSING:nonexistent-pkg") for b in boundary)


def test_release_digest_parsing():
    body = "Origin: Ubuntu\nSHA256:\n " + \
        f"{hashlib.sha256(b'x').hexdigest()}          123 " + \
        "main/binary-amd64/Packages.xz\nMD5Sum:\n ff 1 x"
    d = acquire_git.parse_release_digests(body)
    assert d["main/binary-amd64/Packages.xz"] == \
        hashlib.sha256(b"x").hexdigest()


def test_acquire_deb_verifies_digest(tmp_path, monkeypatch):
    payload = b"fake-deb-bytes"
    p = acquire_git.Pkg("git", "1", "pool/g.deb",
                        hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(acquire_git, "fetch", lambda url: payload)
    r = acquire_git.acquire_deb("m://m", p, tmp_path / "g.deb")
    assert r["verified"] and (tmp_path / "g.deb").exists()
    p.sha256 = "0" * 64
    r = acquire_git.acquire_deb("m://m", p, tmp_path / "g2.deb")
    assert not r["verified"] and not (tmp_path / "g2.deb").exists()


def test_symlink_fixup_stays_inside_rootfs(tmp_path):
    """Absolute deb symlinks (e.g. /etc/java-*/x) must be rewritten
    rootfs-RELATIVE: resolved target inside the bundle, same intended
    file, never a host path."""
    rootfs = tmp_path / "rootfs"
    real = rootfs / "etc" / "java-25" / "java.security"
    real.parent.mkdir(parents=True)
    real.write_text("conf")
    link = rootfs / "usr" / "lib" / "jvm" / "conf"
    link.parent.mkdir(parents=True)
    link.symlink_to("/etc/java-25/java.security")  # absolute, like debs use
    n = acquire_git.fixup_rootfs_symlinks(rootfs)
    assert n == 1
    resolved = link.resolve()
    assert str(resolved).startswith(str(rootfs))          # inside bundle
    assert resolved == real                               # intended target
    assert not link.readlink().is_absolute()              # relative link


def test_symlink_fixup_leaves_external_and_relative(tmp_path):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ext = rootfs / "x"
    ext.symlink_to("/nonexistent-target")              # external -> left
    rel = rootfs / "y"; rel.symlink_to("x")            # relative -> left
    assert acquire_git.fixup_rootfs_symlinks(rootfs) == 0


def test_git_fallback_is_declared():
    """System-git fallback is ONLY a declared host prerequisite —
    permitted when no bundle is shipped for this platform, never a
    silent substitution of the vendored runtime."""
    from testorchestrator import git_runtime
    exe = git_runtime.executable()
    if git_runtime.bundled_available():
        assert "to-git-launch" in exe  # bundle present -> always launcher
        import subprocess
        out = subprocess.run([exe, "--version"], env={
            "PATH": "/nonexistent"}, capture_output=True, text=True)
        assert out.returncode == 0  # works WITHOUT host git on PATH
    else:
        assert exe == "git"          # declared prerequisite, documented


def test_extracted_layout_paths():
    """The extracted rootfs must carry git's binaries, exec path, and
    shared libraries — layout asserted, not merely extracted blindly."""
    m = Path("3rdparty/git/ubuntu-resolute-amd64/"
             "acquisition-manifest.json")
    if not m.exists():
        pytest.skip("no acquired manifest on this host")
    manifest = json.loads(m.read_text())
    if not manifest.get("acquired"):
        pytest.skip("acquisition not performed here")
    rootfs = m.parent / "rootfs"
    assert (rootfs / "usr" / "bin" / "git").exists()
    assert (rootfs / "usr" / "lib" / "git-core").is_dir()
    assert list(rootfs.glob("usr/lib/*-linux-gnu/libcurl*"))
    assert manifest["licences"], "licence notices must be collected"
    assert manifest["release_authenticity"]
    for pkg in manifest["bundle"]:
        assert len(pkg["sha256"]) == 64  # index digests, never invented
