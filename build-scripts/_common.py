"""Shared helpers for the one-command release builds.

Used by build_linux.py and build_windows.py — never run directly.
Everything here is stdlib-only: the scripts must work on a bare host
with just a supported Python interpreter.

Conventions:
  REPO          repository root (parent of build-scripts/)
  BUILD_ROOT    <repo>/build/          — transient staging (gitignored)
  RELEASE_DIR   <repo>/dist/release/   — final deliverables (gitignored)
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import venv
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUILD_ROOT = REPO / "build"
RELEASE_DIR = REPO / "dist" / "release"
ASSETS = json.loads((Path(__file__).parent / "assets.json").read_text())


def _requirements_pin(name: str) -> str:
    for line in (REPO / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{name}=="):
            return line.split("==", 1)[1]
    raise ValueError(f"{name} not pinned in requirements.txt")


# The buildbot-venv's paramiko (used by the ssh-dispatcher worker)
# must match the app venv pin — one SSH stack, one version.
PARAMIKO_VERSION = _requirements_pin("paramiko")


class BuildError(RuntimeError):
    """Actionable build failure — message is shown to the user."""


class Log:
    """Tee stdout into a persistent log file under dist/release/logs/."""

    def __init__(self, platform_name: str):
        log_dir = RELEASE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = log_dir / f"build-{platform_name}-{ts}.log"
        self._fh = self.path.open("w", encoding="utf-8")

    def write(self, msg: str = "") -> None:
        print(msg, flush=True)
        self._fh.write(msg + "\n")
        self._fh.flush()

    def step(self, msg: str) -> None:
        self.write(f"\n=== {msg} ===")

    def close(self) -> None:
        self._fh.close()


def run(cmd: list[str], log: Log, cwd: Path | None = None,
        env: dict | None = None, capture: bool = False) -> str:
    """Run a subprocess; log the command; raise BuildError on failure."""
    log.write(f"$ {' '.join(str(c) for c in cmd)}")
    merged_env = {**os.environ, **(env or {})}
    r = subprocess.run([str(c) for c in cmd], cwd=cwd or REPO,
                       env=merged_env, capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    log._fh.write(out)
    log._fh.flush()
    if r.returncode != 0:
        tail = "\n".join(out.strip().splitlines()[-15:])
        raise BuildError(
            f"command failed (exit {r.returncode}): {cmd[0]}\n{tail}")
    return r.stdout if capture else out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, dest: Path, expected_sha256: str, log: Log) -> Path:
    """Download url to dest with integrity verification. Reuses an
    existing dest only when it already verifies — otherwise refetches."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        if sha256_file(dest) == expected_sha256:
            log.write(f"  cached, sha256 ok: {dest.name}")
            return dest
        log.write(f"  stale/incorrect {dest.name} — refetching")
        dest.unlink()
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.write(f"  downloading {url}")
    try:
        with urllib.request.urlopen(url, timeout=300) as resp, \
                tmp.open("wb") as f:
            shutil.copyfileobj(resp, f, 1 << 20)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise BuildError(f"download failed: {url}\n  {e}")
    actual = sha256_file(tmp)
    if actual != expected_sha256:
        tmp.unlink(missing_ok=True)
        raise BuildError(
            f"checksum mismatch for {dest.name}\n"
            f"  expected {expected_sha256}\n  got      {actual}")
    tmp.replace(dest)
    log.write(f"  verified {dest.name} ({dest.stat().st_size:,} B)")
    return dest


def read_pin_file(path: Path) -> dict[str, str]:
    """Parse a `<sha256>  <name>` pin file into {name: sha256}."""
    pins = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 2:
            pins[parts[1]] = parts[0]
        elif len(parts) == 1:  # bare digest — single-file pin
            pins[path.stem] = parts[0]
    return pins




def git_info() -> dict:
    def g(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=REPO, capture_output=True,
                              text=True).stdout.strip()
    commit = g("rev-parse", "HEAD") or "unknown"
    dirty = bool(subprocess.run(["git", "status", "--porcelain"],
                                cwd=REPO, capture_output=True,
                                text=True).stdout.strip())
    return {"commit": commit, "short": commit[:12], "dirty": dirty,
            "branch": g("rev-parse", "--abbrev-ref", "HEAD")}


def release_version() -> str:
    """Single authoritative version source: release/release.json."""
    return json.loads(
        (REPO / "release" / "release.json").read_text())["version"]


def venv_python(build_dir: Path, log: Log) -> Path:
    """Isolated build venv — created once per build dir, reused on
    repeat runs. Hosts without ensurepip (Debian python3-venv absent)
    get a pip-less venv; callers needing pip use ensure_pip()."""
    vp = build_dir / "venv"
    py = vp / ("Scripts/python.exe" if os.name == "nt"
               else "bin/python")
    if not py.is_file():
        log.write(f"  creating build venv at {vp}")
        try:
            venv.EnvBuilder(with_pip=True, clear=False).create(vp)
        except (Exception, SystemExit):
            # Debian/Ubuntu without python3-venv: the patched ensurepip
            # calls sys.exit(1) — SystemExit, not Exception.
            log.write("  ensurepip unavailable — venv without pip "
                      "(pip bootstrapped on demand)")
            shutil.rmtree(vp, ignore_errors=True)
            venv.EnvBuilder(with_pip=False, clear=False).create(vp)
    return py


def ensure_pip(vpy: Path, log: Log) -> None:
    """Guarantee pip inside the build venv — bootstrap via the pinned
    get-pip.py when ensurepip was unavailable."""
    r = subprocess.run([str(vpy), "-m", "pip", "--version"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return
    spec = ASSETS.get("get_pip")
    if not spec:
        raise BuildError(
            "venv has no pip and no get-pip pin in assets.json")
    gp = fetch(spec["url"], BUILD_ROOT / "downloads" / "get-pip.py",
               spec["sha256"], log)
    run([str(vpy), str(gp)], log)


_LOCAL_NSIS = BUILD_ROOT / "tools" / "nsis"


def _extracted_makensis() -> str | None:
    """makensis previously extracted into build/tools (no-sudo path)."""
    p = _LOCAL_NSIS / "usr" / "bin" / "makensis"
    return str(p) if p.is_file() else None


def _local_makensis() -> str | None:
    """Extracted copy or a Windows default-install location choco
    doesn't PATH-export."""
    p = _extracted_makensis()
    if p:
        return p
    for d in ("C:/Program Files (x86)/NSIS", "C:/Program Files/NSIS"):
        q = Path(d) / "makensis.exe"
        if q.is_file():
            return str(q)
    return None


def ensure_makensis(log: Log) -> str:
    """Locate makensis. Order: PATH → local build/tools copy → package
    manager (apt via sudo, choco) → user-space deb extraction
    (apt-get download, no root) → actionable failure."""
    found = shutil.which("makensis") or _local_makensis()
    if found:
        if found == _extracted_makensis():
            os.environ["NSISDIR"] = str(
                _LOCAL_NSIS / "usr" / "share" / "nsis")
        return found
    log.write("  makensis not on PATH — attempting system install")
    if sys.platform.startswith("linux") and shutil.which("apt-get"):
        try:
            run(["sudo", "-n", "apt-get", "install", "-y",
                 "--no-install-recommends", "nsis"], log)
        except BuildError:
            log.write("  sudo install unavailable — extracting NSIS "
                      "into build/tools (no root needed)")
            _extract_nsis_debs(log)
    elif sys.platform == "win32" and shutil.which("choco"):
        run(["choco", "install", "nsis", "-y", "--no-progress"], log)
    found = shutil.which("makensis") or _local_makensis()
    if not found:
        raise BuildError(
            "makensis (NSIS) is required and could not be installed.\n"
            "  Debian/Ubuntu: sudo apt-get install nsis\n"
            "  Windows:       choco install nsis   (or the NSIS "
            "installer from nsis.sourceforge.io)")
    if found == _extracted_makensis():
        os.environ["NSISDIR"] = str(_LOCAL_NSIS / "usr" / "share" / "nsis")
    return found


def _extract_nsis_debs(log: Log) -> None:
    """Fetch nsis + nsis-common .debs and extract into build/tools/nsis.
    Works without root — Debian/Ubuntu only; no-op failure leaves the
    actionable error in ensure_makensis."""
    if not (shutil.which("apt-get") and shutil.which("dpkg")):
        return
    dl = BUILD_ROOT / "tools" / "debs"
    dl.mkdir(parents=True, exist_ok=True)
    try:
        run(["apt-get", "download", "nsis", "nsis-common"], log, cwd=dl)
    except BuildError:
        log.write("  apt-get download failed — cannot provision NSIS")
        return
    _LOCAL_NSIS.mkdir(parents=True, exist_ok=True)
    for deb in sorted(dl.glob("*.deb")):
        run(["dpkg", "-x", str(deb), str(_LOCAL_NSIS)], log)
    if _local_makensis():
        log.write(f"  user-space makensis ready: {_local_makensis()}")


def stage_buildbot_worker_payload(build_dir: Path, log: Log) -> Path:
    """Stage packaging/windows/bb-worker-payload/ — embed Python +
    pinned buildbot-worker wheels + bootstrap script, the offline
    payload for the NSIS Buildbot-worker installer."""
    out = REPO / "packaging" / "windows" / "bb-worker-payload"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    spec = ASSETS["python_embed_windows"]
    unzip_safely(fetch(spec["url"],
                       REPO / "3rdparty" / "python" / "python-embed-amd64.zip",
                       spec["sha256"], log),
                 out / "python-embed")
    fetch(ASSETS["get_pip"]["url"], out / "get-pip.py",
          ASSETS["get_pip"]["sha256"], log)
    shutil.copy2(REPO / "packaging" / "windows" / "bb-worker-payload-src"
                 / "setup-worker.py", out / "setup-worker.py")
    wheels = out / "wheels"
    wheels.mkdir()
    vpy = venv_python(build_dir, log)
    ensure_pip(vpy, log)
    bbver = ASSETS["buildbot_worker"]["version"]
    # Windows-amd64 wheels for the embedded py3.12 — downloaded for the
    # target platform, never executed on the build host. Pure-python
    # wheels satisfy the platform filter, so one resolve covers both.
    run([str(vpy), "-m", "pip", "download", "--only-binary=:all:",
         "--platform", "win_amd64", "--python-version", "312",
         "--implementation", "cp", "--abi", "cp312",
         "-d", str(wheels), f"buildbot-worker=={bbver}",
         "pywin32"], log)
    # GPL/LGPL notices travel inside the .exe payload too
    lic = out / "licenses"
    lic.mkdir(exist_ok=True)
    shutil.copy2(REPO / "3rdparty" / "CORRESPONDING-SOURCES.txt",
                 lic / "CORRESPONDING-SOURCES.txt")
    import zipfile
    for whl in wheels.glob("*.whl"):
        try:
            with zipfile.ZipFile(whl) as z:
                for name in z.namelist():
                    base = name.rsplit("/", 1)[-1].upper()
                    if ".dist-info/" in name and (
                            base.startswith("LICEN") or
                            base.startswith("COPYING") or
                            base == "NOTICE"):
                        (lic / (whl.name.split("-")[0] + "-" +
                                name.rsplit("/", 1)[-1])
                         ).write_bytes(z.read(name))
        except zipfile.BadZipFile:
            pass
    log.write(f"  bb worker payload staged: {sorted(p.name for p in out.iterdir())}")
    return out


def fetch_buildbot_linux_wheels(build_dir: Path, log: Log) -> Path:
    """Stage 3rdparty/buildbot-wheels/ — linux amd64 wheels for the
    controller-side Buildbot stack (master + workers + paramiko).
    install.py builds payload/buildbot-venv from them offline.
    Also extracts each wheel's license into 3rdparty/licenses/."""
    out = REPO / "3rdparty" / "buildbot-wheels"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    vpy = venv_python(build_dir, log)
    ensure_pip(vpy, log)
    bbver = ASSETS["buildbot_worker"]["version"]
    run([str(vpy), "-m", "pip", "download",
         "-d", str(out), f"buildbot[bundled]=={bbver}",
         f"buildbot-worker=={bbver}", f"buildbot-www=={bbver}",
         f"paramiko=={PARAMIKO_VERSION}"], log)
    log.write(f"  linux bb wheels staged: {len(list(out.iterdir()))} wheels")
    extract_wheel_licenses(out, log)
    return out


def extract_wheel_licenses(wheels_dir: Path, log: Log) -> int:
    """Copy every staged wheel's dist-info license file(s) into
    3rdparty/licenses/ — GPL/LGPL notices must travel with binaries."""
    import zipfile
    lic_dir = REPO / "3rdparty" / "licenses"
    lic_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for whl in sorted(wheels_dir.glob("*.whl")):
        try:
            with zipfile.ZipFile(whl) as z:
                for name in z.namelist():
                    base = name.rsplit("/", 1)[-1].upper()
                    if ".dist-info/" in name and (
                            base.startswith("LICEN") or
                            base.startswith("COPYING") or
                            base == "NOTICE"):
                        dest = lic_dir / (
                            whl.name.split("-")[0] + "-" +
                            name.rsplit("/", 1)[-1])
                        dest.write_bytes(z.read(name))
                        n += 1
        except zipfile.BadZipFile:
            log.write(f"  WARN unreadable wheel for license: {whl.name}")
    log.write(f"  extracted {n} license files -> 3rdparty/licenses/")
    return n


_GPL_SOURCE_PKGS = ("buildbot", "buildbot-worker", "buildbot-www",
                    "paramiko")


def build_linux_worker_bundle(build_dir: Path, log: Log) -> Path:
    """Self-contained Linux worker bundle: install_linux_worker.sh +
    bb-wheels/ holding the pinned buildbot-worker wheel closure for
    linux/x86_64 across Python 3.10-3.12 — the worker host needs no
    network and no pip access at all. Tarred beside the Windows .exe
    so the Admin UI offers all worker installs from one place."""
    bbver = ASSETS["buildbot_worker"]["version"]
    staging = build_dir / "bb-linux-worker-pkg"
    if staging.exists():
        shutil.rmtree(staging)
    wheels = staging / "bb-wheels"
    wheels.mkdir(parents=True)
    dl = build_dir / "downloads"
    dl.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "scripts" / "install_linux_worker.sh",
                 staging / "install_linux_worker.sh")
    # get-pip embeds the pip wheel itself — offline bootstrap when the
    # worker host's python lacks ensurepip (install script falls back
    # to it automatically).
    shutil.copy2(fetch(ASSETS["get_pip"]["url"], dl / "get-pip.py",
                       ASSETS["get_pip"]["sha256"], log),
                 staging / "get-pip.py")
    vpy = venv_python(build_dir, log)
    ensure_pip(vpy, log)
    # no --platform: resolves for the linux/amd64 build host; the
    # --python-version sweep covers the compiled wheels (autobahn,
    # cffi, msgpack, ...) for every supported host interpreter.
    for pyver in ("3.10", "3.11", "3.12"):
        per = build_dir / f"bbw-dl-{pyver.replace('.', '')}"
        run([str(vpy), "-m", "pip", "download",
             "--only-binary=:all:", "--python-version", pyver,
             "-d", str(per), f"buildbot-worker=={bbver}"], log)
        for w in per.glob("*.whl"):
            shutil.move(str(w), wheels / w.name)
        shutil.rmtree(per)
    log.write(f"  linux worker wheels: "
              f"{len(list(wheels.iterdir()))} wheels "
              f"(py3.10-3.12)")
    name = f"TestOrchestrator-Buildbot-Worker-{bbver}-linux-x86_64.tar.gz"
    out = build_dir / name
    if out.exists():
        out.unlink()
    import tarfile
    with tarfile.open(out, "w:gz") as t:
        t.add(staging / "install_linux_worker.sh",
              arcname="install_linux_worker.sh")
        t.add(wheels, arcname="bb-wheels")
        t.add(staging / "get-pip.py", arcname="get-pip.py")
    # beside the Windows .exe — the Admin UI serves every worker
    # install asset from this directory.
    shutil.copy2(out, REPO / "packaging" / "windows" / name)
    log.write(f"  linux worker bundle: {out.name} "
              f"({out.stat().st_size:,} B) + packaging/windows/ copy")
    return out


def build_corresponding_sources(build_dir: Path, log: Log) -> Path:
    """Corresponding-source archive for the GPL/LGPL-covered components
    actually shipped: source distributions + the build/install scripts
    that turn them into the shipped artifacts. Emitted beside the
    controller package AND inside packaging/windows/ so the Admin UI
    can offer it next to the worker .exe download."""
    bbver = ASSETS["buildbot_worker"]["version"]
    staging = build_dir / "bb-corresponding-sources"
    if staging.exists():
        shutil.rmtree(staging)
    sdist_dir = staging / "sdists"
    sdist_dir.mkdir(parents=True)
    # Fetch the sdists verbatim from PyPI's JSON API — pip download
    # --no-binary tries to BUILD them (buildbot-www's sdist requires
    # buildbot_pkg to even read its metadata).
    import json as _json
    import urllib.request
    for pkg in _GPL_SOURCE_PKGS:
        if pkg == "paramiko":
            # match the version actually staged in buildbot-wheels
            whl = next((REPO / "3rdparty" / "buildbot-wheels").glob(
                "paramiko-*.whl"), None)
            ver = whl.name.split("-")[1] if whl else None
        else:
            ver = bbver
        meta_url = (f"https://pypi.org/pypi/{pkg}/{ver}/json" if ver
                    else f"https://pypi.org/pypi/{pkg}/json")
        with urllib.request.urlopen(meta_url, timeout=30) as r:
            meta = _json.loads(r.read())
        files = meta["urls"]
        sdist = next(f for f in files if f["packagetype"] == "sdist")
        log.write(f"  sdist {pkg}: {sdist['filename']} "
                  f"({sdist['url']})")
        run(["curl", "-fSL", "-o",
             str(sdist_dir / sdist["filename"]), sdist["url"]], log)
    scripts = staging / "build-and-install"
    scripts.mkdir()
    for src, dst in [
            ("scripts/provision_buildbot.py", "provision_buildbot.py"),
            ("deploy/buildbot/master.cfg", "master.cfg"),
            ("scripts/install_linux_worker.sh", "install_linux_worker.sh"),
            ("packaging/windows/bb-worker-payload-src/setup-worker.py",
             "setup-worker.py"),
            ("packaging/windows/buildbot-worker-installer.nsi",
             "buildbot-worker-installer.nsi"),
            ("packaging/windows/app-installer.nsi",
             "app-installer.nsi"),
            ("scripts/init_env.py", "init_env.py")]:
        shutil.copy2(REPO / src, scripts / dst)
    name = f"TestOrchestrator-Buildbot-Sources-{bbver}.tar.gz"
    out = build_dir / name
    if out.exists():
        out.unlink()
    import tarfile
    with tarfile.open(out, "w:gz") as t:
        t.add(sdist_dir, arcname="sdists")
        t.add(scripts, arcname="build-and-install")
        t.add(REPO / "3rdparty" / "CORRESPONDING-SOURCES.txt",
              arcname="CORRESPONDING-SOURCES.txt")
    # companion copy beside the worker .exe — the Admin UI serves it
    # from packaging/windows/ next to the installer download.
    shutil.copy2(out, REPO / "packaging" / "windows" / name)
    log.write(f"  corresponding sources: {out.name} "
              f"({out.stat().st_size:,} B) + packaging/windows/ copy")
    return out


def build_buildbot_worker_installer(build_dir: Path, log: Log) -> Path:
    """makensis the buildbot-worker NSIS installer. Output:
    packaging/windows/testorchestrator-buildbot-worker-<v>-setup-UNSIGNED.exe"""
    makensis = ensure_makensis(log)
    stage_buildbot_worker_payload(build_dir, log)
    win_dir = REPO / "packaging" / "windows"
    bbver = ASSETS["buildbot_worker"]["version"]
    for stale in win_dir.glob("TestOrchestrator-Buildbot-Worker-*.exe"):
        stale.unlink()
    run([makensis, "-V2", f"-DVERSION={bbver}",
         "buildbot-worker-installer.nsi"], log, cwd=win_dir)
    exes = sorted(win_dir.glob(
        f"TestOrchestrator-Buildbot-Worker-Setup-{bbver}-UNSIGNED.exe"))
    if not exes:
        raise BuildError("makensis produced no buildbot worker installer")
    log.write(f"  built {exes[0].name} ({exes[0].stat().st_size:,} B, unsigned)")
    return exes[0]



def safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract with traversal protection (py3.12 'data' filter)."""
    tar.extractall(dest, filter="data")


def unzip_safely(archive: Path, dest: Path) -> None:
    """zipfile extract with explicit traversal checks."""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise BuildError(f"unsafe path in {archive.name}: "
                                 f"{info.filename}")
        z.extractall(dest)


def write_checksums(artifacts: list[Path], dest: Path, log: Log) -> dict:
    """SHA256SUMS.txt + {name: (sha256,size)} map for the manifest."""
    sums = {}
    lines = []
    for p in artifacts:
        h = sha256_file(p)
        sums[p.name] = {"sha256": h, "size": p.stat().st_size}
        lines.append(f"{h}  {p.name}")
    dest.write_text("\n".join(lines) + "\n")
    log.write(f"  wrote {dest.name} ({len(artifacts)} artifacts)")
    return sums


def clean_release_dir(platform_name: str) -> None:
    """Repeat-safe: drop previous deliverables for this platform only —
    never the source tree, never other platforms, never the logs."""
    out = RELEASE_DIR / platform_name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)


def finalise(log: Log, platform_name: str, gi: dict,
             artifacts: list[Path], extra_meta: dict,
             validations: list[dict]) -> Path:
    """Copy deliverables into dist/release/, emit SHA256SUMS +
    build-manifest.json, print the summary."""
    out = RELEASE_DIR / f"{platform_name}"
    out.mkdir(parents=True, exist_ok=True)   # created by clean_release_dir
    placed = []
    for a in artifacts:
        tgt = out / a.name
        if a.resolve() != tgt.resolve():
            shutil.copy2(a, tgt)
        placed.append(tgt)
    sums = write_checksums(placed, out / "SHA256SUMS.txt", log)
    manifest = {
        "version": release_version(),
        "platform": platform_name,
        "source": {**gi,
                   "note": "built from the current checkout; dirty "
                           "tree means uncommitted content is included"},
        "built_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
        "tools": tool_versions(),
        "artifacts": sums,
        "validations": validations,
        **extra_meta,
    }
    (out / "build-manifest.json").write_text(json.dumps(manifest,
                                                      indent=2))
    log.step("release output")
    for name, s in sums.items():
        log.write(f"  {s['size']:>13,} B  {name}\n"
                  f"                  sha256 {s['sha256']}")
    log.write(f"\n  output dir: {out}\n  log: {log.path}")
    return out


def tool_versions() -> dict:
    def _v(cmd: list[str]) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=30).stdout.strip().splitlines()[0]
        except Exception:
            return "unavailable"
    mk = shutil.which("makensis") or _local_makensis()
    return {
        "python": sys.version.split()[0],
        "makensis": _v([mk, "-VERSION"]) if mk else "absent",
        "buildbot_worker": ASSETS.get("buildbot_worker", {}).get(
            "version", "unpinned"),
        "python_embed_windows": ASSETS["python_embed_windows"]["version"],
    }


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise BuildError(msg)
