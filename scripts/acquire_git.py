#!/usr/bin/env python3
"""Acquire + verify a distributable Git for an exact Ubuntu target (§13).

Design: resolves `git` and its runtime dependency closure against the
signed apt Release/Packages indexes of a pinned Ubuntu suite+arch,
downloads the .deb set, verifies every package SHA256 against the
index (which is itself verified against the signed InRelease), extracts
into a self-contained runtime layout under 3rdparty/git/<target>/, and
records an acquisition manifest — versions, digests, source URIs,
licences, and honesty fields (acquired/runtime_verified).

Covers the Ubuntu target ONLY. Windows/SUSE get separate acquisition
adapters; this tool does not claim them.

    python3 scripts/acquire_git.py --suite resolute --arch amd64 --plan
    python3 scripts/acquire_git.py --suite resolute --arch amd64 --acquire
    python3 scripts/acquire_git.py --suite resolute --arch amd64 \
        --out 3rdparty/git/ubuntu-26.04-amd64 --acquire --verify-runtime
"""

import argparse
import gzip
import hashlib
import json
import lzma
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

MIRROR = "http://archive.ubuntu.com/ubuntu"
KEYRING = "/usr/share/keyrings/ubuntu-archive-keyring.gpg"
# Packages satisfying these are part of the debootstrap/base image on any
# supported Ubuntu install — recorded as the declared host boundary, not
# bundled. Everything else in the closure IS bundled.
BASE_PRIORITIES = {"required", "important"}


@dataclass
class Pkg:
    name: str
    version: str
    filename: str
    sha256: str
    depends: str = ""
    provides: str = ""
    priority: str = ""
    essential: str = ""


def fetch(url: str, dest: Path = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent":
                                               "to-git-acquire/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    if dest:
        dest.write_bytes(data)
    return data


def verify_inrelease(data: bytes, work: Path) -> str:
    """Verify the clearsigned InRelease against the Ubuntu keyring;
    returns the signed text on success."""
    f = work / "InRelease"
    f.write_bytes(data)
    res = subprocess.run(
        ["gpgv", "--keyring", KEYRING, str(f)],
        capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"InRelease signature verification failed:\n"
                         f"{res.stderr}")
    # Strip the armor, keep the signed body.
    body = data.decode("utf-8", "replace")
    m = re.search(r"-----BEGIN PGP SIGNED MESSAGE-----.*?\n\n(.*?)"
                  r"-----BEGIN PGP SIGNATURE-----", body, re.S)
    if not m:
        raise SystemExit("InRelease parse failure")
    return m.group(1)


def parse_release_digests(body: str) -> dict:
    """Extract sha256 -> path records from the Release/InRelease body."""
    digests = {}
    in_sha = False
    for line in body.splitlines():
        if line.startswith("SHA256:"):
            in_sha = True
            continue
        if in_sha:
            if not line.startswith(" "):
                in_sha = False
                continue
            parts = line.split()
            if len(parts) >= 3:
                digests[parts[2]] = parts[0]
    return digests


def parse_packages(data: bytes) -> dict:
    text = data.decode("utf-8", "replace")
    pkgs = {}
    for stanza in re.split(r"\n\n+", text):
        fields = {}
        last = None
        for line in stanza.splitlines():
            if line.startswith(" "):
                if last:
                    fields[last] += "\n" + line
                continue
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip()
            last = k.strip()
        if "Package" in fields:
            pkgs[fields["Package"]] = Pkg(
                name=fields["Package"],
                version=fields.get("Version", ""),
                filename=fields.get("Filename", ""),
                sha256=fields.get("SHA256", ""),
                depends=fields.get("Depends", ""),
                provides=fields.get("Provides", ""),
                priority=fields.get("Priority", ""),
                essential=fields.get("Essential", ""))
    return pkgs


def resolve_closure(root: str, pkgs: dict,
                    arch: str = "amd64") -> tuple[list, list]:
    """Recursive Depends closure. Returns (bundle_set, base_satisfied).

    Alternatives: first alternative present in the index wins; a
    version-qualified or virtual name resolves via Provides. Missing
    entries are recorded and fail loudly in the manifest.
    Arch-restricted deps (`libx [s390x]`): alternatives whose qualifier
    excludes `arch` are inapplicable — a group with no applicable
    alternative is skipped silently, not reported missing.
    """
    provides = {}
    for p in pkgs.values():
        for alt in p.provides.split(","):
            name = re.split(r"[ (\[]", alt.strip())[0]
            if name:
                provides.setdefault(name, p.name)

    bundle, base, missing = [], [], []

    def dep_names(depends: str) -> list:
        out = []
        for group in depends.split(","):
            group = group.strip()
            if not group:
                continue
            chosen = None
            any_applicable = False
            for alt in group.split("|"):
                alt = alt.strip()
                am = re.search(r"\[([^\]]+)\]", alt)
                if am:  # arch-restricted alternative
                    quals = am.group(1).split()
                    pos = {q for q in quals if not q.startswith("!")}
                    neg = {q[1:] for q in quals if q.startswith("!")}
                    if pos and arch not in pos:
                        continue  # restricted to other arches
                    if arch in neg:
                        continue  # excluded on this arch
                any_applicable = True
                name = re.split(r"[ (\[:]", alt)[0]
                if name in pkgs:
                    chosen = name
                    break
                if name in provides:
                    chosen = provides[name]
                    break
            if chosen:
                out.append(chosen)
            elif any_applicable:
                missing.append(group)
        return out

    seen = {root}
    stack = [root]
    while stack:
        name = stack.pop()
        p = pkgs.get(name)
        if p is None:
            missing.append(name)
            continue
        if (p.essential.lower() == "yes") or (p.priority in BASE_PRIORITIES):
            base.append(name)
        else:
            bundle.append(p)
        for dep in dep_names(p.depends):
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return bundle, base + [f"MISSING:{m}" for m in missing]


def acquire_deb(mirror: str, p: Pkg, dest: Path) -> dict:
    url = f"{mirror}/{p.filename}"
    raw = fetch(url)
    actual = hashlib.sha256(raw).hexdigest()
    ok = actual == p.sha256
    if ok:
        dest.write_bytes(raw)
    return {"package": p.name, "version": p.version, "url": url,
            "sha256_index": p.sha256, "sha256_actual": actual,
            "verified": ok}


def extract_rootfs(deb_paths: list, out: Path) -> None:
    rootfs = out / "rootfs"
    for deb in deb_paths:
        subprocess.check_call(["dpkg-deb", "-x", str(deb), str(rootfs)])


def fixup_rootfs_symlinks(rootfs: Path) -> int:
    """Deb payloads symlink config files to absolute paths (/etc/...) —
    inside a movable bundle those break or silently read HOST files.
    Rewrite every absolute symlink rootfs-relative so the extracted
    runtime is self-contained and never falls through to host config."""
    fixed = 0
    for link in rootfs.rglob("*"):
        if not link.is_symlink():
            continue
        target = link.readlink()
        if not str(target).startswith("/"):
            continue
        real = rootfs / str(target).lstrip("/")
        if not real.exists():
            continue  # genuinely external target — leave + note
        link.unlink()
        link.symlink_to(Path(os.path.relpath(real, link.parent)))
        fixed += 1
    return fixed


def collect_licenses(rootfs: Path, out: Path) -> list:
    licdir = out / "licenses"
    licdir.mkdir(parents=True, exist_ok=True)
    found = []
    for doc in rootfs.glob("usr/share/doc/*/copyright"):
        dest = licdir / f"{doc.parent.name}.copyright"
        shutil.copy2(doc, dest)
        found.append(dest.name)
    return found


def verify_runtime(out: Path, args) -> dict:
    """Best-effort: run the extracted git on THIS host with bundled libs
    first. A resolute build on a noble host may fail on glibc — record
    the failure honestly; clean-target verification remains a gate."""
    git = (out / "rootfs" / args.bin if "/" in args.bin
           else out / "rootfs" / "usr" / "bin" / args.bin)
    if not git.exists():
        return {"verified": False, "error": f"{args.bin} missing"}
    libdirs = [str(p) for p in (out / "rootfs").glob("usr/lib/*-linux-gnu")]
    libdirs += [str(out / "rootfs" / "lib" / "x86_64-linux-gnu")]
    env = {"LD_LIBRARY_PATH": ":".join(libdirs),
           "PATH": "/nonexistent",  # prove no host-git fallback
           "GIT_EXEC_PATH": str(out / "rootfs" / "usr" / "lib" /
                                "git-core")}
    try:
        res = subprocess.run([str(git), "--version"], env=env,
                             capture_output=True, text=True, timeout=15)
        return {"verified": res.returncode == 0,
                "output": (res.stdout or res.stderr).strip()[:200]}
    except Exception as exc:
        return {"verified": False, "error": str(exc)[:200]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="git",
                    help="root package to bundle (e.g. git, "
                         "openjdk-21-jre-headless)")
    ap.add_argument("--suite", default="resolute",
                    help="Ubuntu release codename (resolute = 26.04 LTS)")
    ap.add_argument("--arch", default="amd64")
    ap.add_argument("--mirror", default=MIRROR)
    ap.add_argument("--component", default="main")
    ap.add_argument("--out", default=None)
    ap.add_argument("--plan", action="store_true",
                    help="resolve + manifest only, no downloads")
    ap.add_argument("--acquire", action="store_true",
                    help="download + verify + extract")
    ap.add_argument("--verify-runtime", action="store_true",
                    help="run extracted binary with bundled libs")
    ap.add_argument("--bin", default=None,
                    help="binary for --verify-runtime (default: root)")
    args = ap.parse_args()
    if not args.bin:
        args.bin = args.root.split("-")[0]
    target = f"ubuntu-{args.suite}-{args.arch}"
    out = Path(args.out or f"3rdparty/git/{target}")

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        body = verify_inrelease(
            fetch(f"{args.mirror}/dists/{args.suite}/InRelease"), work)
        digests = parse_release_digests(body)
        rel = f"{args.component}/binary-{args.arch}/Packages.xz"
        if rel not in digests:
            raise SystemExit(f"{rel} not in Release digests")
        pkgs_raw = fetch(f"{args.mirror}/dists/{args.suite}/{rel}")
        if hashlib.sha256(pkgs_raw).hexdigest() != digests[rel]:
            raise SystemExit("Packages index digest mismatch")
        pkgs = parse_packages(lzma.decompress(pkgs_raw))

    if args.root not in pkgs:
        raise SystemExit(f"{args.root} not found in target Packages index")
    bundle, boundary = resolve_closure(args.root, pkgs, arch=args.arch)
    missing = [b for b in boundary if b.startswith("MISSING:")]

    manifest = {
        "tool": "acquire_git.py",
        "target": target,
        "suite": args.suite, "arch": args.arch,
        "mirror": args.mirror,
        "root_package": {"name": args.root,
                         "version": pkgs[args.root].version},
        "release_authenticity": "InRelease gpg-verified vs "
                                "ubuntu-archive-keyring",
        "bundle": [{"name": p.name, "version": p.version,
                    "filename": p.filename, "sha256": p.sha256}
                   for p in bundle],
        "host_boundary": boundary,
        "unresolved": missing,
        "licences": [],
        "acquired": False,
        "runtime_verified": False,
    }

    if args.acquire and not missing:
        debs = out / "debs"
        debs.mkdir(parents=True, exist_ok=True)
        results = [acquire_deb(args.mirror, p,
                               debs / Path(p.filename).name)
                   for p in bundle]
        bad = [r for r in results if not r["verified"]]
        manifest["downloads"] = results
        if bad:
            manifest["status"] = "FAILED: digest mismatches"
        else:
            extract_rootfs(list(debs.iterdir()), out)
            manifest["symlinks_fixed"] = fixup_rootfs_symlinks(
                out / "rootfs")
            manifest["licences"] = collect_licenses(out / "rootfs", out)
            manifest["acquired"] = True
            manifest["status"] = "acquired+verified"
            if args.verify_runtime:
                rv = verify_runtime(out, args)
                manifest["runtime_check"] = rv
                manifest["runtime_verified"] = rv["verified"]
    elif args.acquire and missing:
        manifest["status"] = f"FAILED: unresolved deps {missing}"
    else:
        manifest["status"] = "plan only — not acquired"

    out.mkdir(parents=True, exist_ok=True)
    (out / "acquisition-manifest.json").write_text(
        json.dumps(manifest, indent=2))
    print(json.dumps({k: manifest[k] for k in
                      ("target", "status", "acquired",
                       "runtime_verified")}, indent=2))
    print(f"manifest: {out}/acquisition-manifest.json")


if __name__ == "__main__":
    main()
