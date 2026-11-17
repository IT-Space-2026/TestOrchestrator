"""Repository-result import (2.0.1) — detection, parsing, dedup, commit.

Pipeline (framework-independent; katalon is the first registered
parser):
    staging checkout -> detect_run_dirs -> parse_run_dir
    -> fingerprint -> classify (new|duplicate|supplement|ambiguous)
    -> preview (ImportSession/ImportCandidate bookkeeping only)
    -> confirm (transactional recheck + insert/supplement/link)

Dedup contract: execution.uuid exact match OR identical content_hash
= confirmed duplicate; same suite+timestamp but differing case set =
ambiguous (review); a different timestamp is a different run, even
with identical outcomes. Filenames, commits, case names, and totals
alone are never identity.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from . import git_backend, results as results_mod
from .execution import (Artifact, Execution, ExecutionState,
                        GitBinding, ImportCandidate, ImportSession,
                        Outcome, Project, Suite, TestResult, utcnow)
from .extensions import db
from .results import JunitParseError, parse_junit

PARSER_VERSION = "kre-junit/1"

# Input limits — unsafe/hostile trees are bounded, never trusted.
MAX_DIRS_SCANNED = 20_000
MAX_FILES_PER_RUN = 2_000
MAX_RUN_BYTES = 512 << 20          # per run dir
MAX_XML_BYTES = 64 << 20           # per junit candidate file
MAX_CANDIDATES = 500
STAGING_MAX_AGE_S = 7 * 24 * 3600  # orphaned staging dirs cleaned lazily


class ImportError_(RuntimeError):
    """User-facing import failure — actionable message."""


# ---------- detection + parsing ----------

@dataclass
class ParsedImport:
    run_dir: Path                      # first/primary dir
    rel_dir: str                       # relative to the selected root
    junit_files: list = field(default_factory=list)
    # every evidence file: (absolute src, rel path under the scan root)
    files: list = field(default_factory=list)
    run_dirs: list = field(default_factory=list)
    cases: list = field(default_factory=list)
    suite_keys: list = field(default_factory=list)   # (id|name, timestamp)
    timestamps: list = field(default_factory=list)
    run_uuid: str = ""
    properties: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    malformed: bool = False
    unsupported: bool = False

    @property
    def case_count(self) -> int:
        return len(self.cases)


def _looks_junit(path: Path) -> ParsedRun | None:
    if path.stat().st_size > MAX_XML_BYTES:
        return None
    try:
        return parse_junit(path)
    except (JunitParseError, Exception):
        return None


def _iter_tree(root: Path):
    """Yield (dir, files) without following symlinks or hidden dirs."""
    import os
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        scanned += 1
        if scanned > MAX_DIRS_SCANNED:
            raise ImportError_(
                f"scan limit exceeded ({MAX_DIRS_SCANNED} dirs) — "
                "select a narrower result directory")
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".")
                       and not (Path(dirpath) / d).is_symlink()]
        yield Path(dirpath), filenames


def detect_run_dirs(root: Path) -> tuple[list[Path], list[str]]:
    """A run dir = a directory containing ≥1 parseable JUnit XML.
    Katalon names it JUnit_Report.xml; generic junit accepted."""
    run_dirs, notes = [], []
    for dirpath, filenames in _iter_tree(root):
        xmls = [f for f in filenames if f.lower().endswith(".xml")]
        if not xmls:
            continue
        hit = False
        preferred = [f for f in xmls if f == "JUnit_Report.xml"]
        for name in preferred + [f for f in xmls
                                 if f not in preferred][:20]:
            if _looks_junit(dirpath / name):
                hit = True
                break
        if hit:
            run_dirs.append(dirpath)
            if len(run_dirs) > MAX_CANDIDATES:
                raise ImportError_(
                    f"too many run dirs (>{MAX_CANDIDATES}) — "
                    "select a narrower result directory")
    return sorted(run_dirs), notes


def _read_properties(run_dir: Path, warnings: list) -> dict:
    props = {}
    for name in ("execution.properties",):
        p = run_dir / name
        if not p.is_file():
            continue
        try:
            for line in p.read_text(
                    encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"^\s*([^#=][^=]*)=(.*)$", line)
                if m:
                    props[m.group(1).strip()] = m.group(2).strip()
        except OSError:
            warnings.append(f"{name}: unreadable")
    return props


def parse_run_dir(run_dir: Path, rel_dir: str,
                  scan_root: Path = None) -> ParsedImport:
    """Parse one detected run dir into the common import model."""
    scan_root = scan_root or run_dir
    pi = ParsedImport(run_dir=run_dir, rel_dir=rel_dir)
    pi.run_dirs = [run_dir]
    total = 0
    nfiles = 0
    for f in sorted(run_dir.rglob("*")):
        if not f.is_file() or f.is_symlink():
            continue
        nfiles += 1
        total += f.stat().st_size
        if nfiles > MAX_FILES_PER_RUN or total > MAX_RUN_BYTES:
            pi.warnings.append("run dir exceeds limits — evidence "
                               "truncated")
            break
        try:
            pi.files.append((f, str(f.relative_to(scan_root))))
        except ValueError:
            continue
        if f.suffix.lower() == ".xml" and f.parent == run_dir:
            parsed = _looks_junit(f)
            if parsed is not None:
                pi.junit_files.append((f, parsed))
                for s in parsed.suites:
                    pi.suite_keys.append(
                        (s["id"] or s["name"], s["timestamp"]))
                    if s["timestamp"]:
                        pi.timestamps.append(s["timestamp"])
                pi.cases.extend(parsed.cases)
                if parsed.discrepant:
                    pi.warnings.append(
                        f"{f.name}: producer totals disagree with "
                        "case count (recorded honestly)")
    uuid_file = run_dir / "execution.uuid"
    if uuid_file.is_file():
        try:
            pi.run_uuid = uuid_file.read_text().strip()[:64]
        except OSError:
            pi.warnings.append("execution.uuid unreadable")
    if not pi.run_uuid:
        pi.warnings.append("no execution.uuid — dedup relies on "
                           "content fingerprint")
    pi.properties = _read_properties(run_dir, pi.warnings)
    if not pi.junit_files:
        pi.unsupported = True
        pi.warnings.append("no parseable JUnit XML — HTML/CSV alone "
                           "cannot reconstruct an execution")
    return pi


# ---------- fingerprints ----------

def content_fingerprint(suite_keys, timestamps, cases, totals) -> str:
    """Canonical run fingerprint — identical report content hashes
    identically wherever it was produced (live collect or import)."""
    canon = {
        "suites": sorted(set(suite_keys)),
        "ts": sorted(set(timestamps)),
        "cases": [[c.classname, c.name, c.status,
                   round(c.duration_s or 0, 3)] for c in cases],
        "totals": totals,
    }
    return hashlib.sha256(
        json.dumps(canon, sort_keys=True).encode()).hexdigest()


def fingerprint(pi: ParsedImport) -> tuple[str, str]:
    totals = sum(p.producer_tests for _f, p in pi.junit_files)
    return pi.run_uuid, content_fingerprint(
        pi.suite_keys, pi.timestamps, pi.cases, totals)


def fingerprint_live(ex: Execution) -> tuple[str, str]:
    """Fingerprint an existing execution from its stored report dir —
    lazily computed for rows that predate fingerprint stamping."""
    if ex.run_uuid or ex.content_hash:
        return ex.run_uuid, ex.content_hash
    report_dir = Path(ex.report_dir or "")
    if not report_dir.is_dir():
        return "", ""
    uuid_f = report_dir / "execution.uuid"
    run_uuid = ""
    if uuid_f.is_file():
        try:
            run_uuid = uuid_f.read_text().strip()[:64]
        except OSError:
            pass
    suite_keys, timestamps, cases, totals = [], [], [], 0
    for art in Artifact.query.filter_by(execution_id=ex.id,
                                        kind="junit"):
        f = report_dir / art.rel_path
        if not f.is_file():
            continue
        parsed = _looks_junit(f)
        if parsed is None:
            continue
        for s in parsed.suites:
            suite_keys.append((s["id"] or s["name"], s["timestamp"]))
            if s["timestamp"]:
                timestamps.append(s["timestamp"])
        cases.extend(parsed.cases)
        totals += parsed.producer_tests
    if not cases:
        return run_uuid, ""
    return run_uuid, content_fingerprint(
        suite_keys, timestamps, cases, totals)


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        ts = value.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc)
        except ValueError:
            return None


# ---------- classification (dedup) ----------

DECISION_NEW = "new"
DECISION_DUPLICATE = "duplicate"
DECISION_SUPPLEMENT = "supplement"
DECISION_AMBIGUOUS = "ambiguous"
DECISION_UNSUPPORTED = "unsupported"
DECISION_MALFORMED = "malformed"


def classify(project_id: str, pi: ParsedImport) -> tuple[str, str, list]:
    """(decision, match_execution_id, warnings). Project-scoped only —
    matching never crosses project boundaries."""
    warnings = list(pi.warnings)
    if pi.unsupported:
        return DECISION_UNSUPPORTED, "", warnings
    if not pi.cases:
        return DECISION_MALFORMED, "", warnings + [
            "JUnit parsed but contains no test cases"]
    run_uuid, chash = fingerprint(pi)
    candidates = Execution.query.filter_by(project_id=project_id).all()
    exact = None
    suite_ts = {(k[0], k[1]) for k in pi.suite_keys if k[0]}
    for ex in candidates:
        eu, eh = fingerprint_live(ex)
        if run_uuid and eu and run_uuid == eu:
            exact = ex
            break
        if chash and eh and chash == eh:
            exact = ex
            break
        # Same suite+start time but a different case multiset → cannot
        # distinguish partial copy from distinct run → review.
        ex_keys = set()
        report_dir = Path(ex.report_dir or "")
        if report_dir.is_dir():
            for art in Artifact.query.filter_by(execution_id=ex.id,
                                                kind="junit"):
                f = report_dir / art.rel_path
                parsed = _looks_junit(f) if f.is_file() else None
                if parsed:
                    for s in parsed.suites:
                        ex_keys.add((s["id"] or s["name"],
                                     s["timestamp"]))
        if suite_ts and ex_keys \
                and (suite_ts <= ex_keys or ex_keys <= suite_ts) \
                and not (chash and eh and chash == eh):
            return (DECISION_AMBIGUOUS, ex.id, warnings + [
                "same suite and start time but differing case set — "
                "review required (possible partial copy or distinct "
                "run)"])
    if exact is not None:
        missing = []
        if exact.finished_at is None and _parse_ts(
                min(pi.timestamps) if pi.timestamps else ""):
            missing.append("finished_at")
        exact_dir = Path(exact.report_dir or "")
        if exact_dir.is_dir():
            stored_sha = {a.sha256 for a in Artifact.query.filter_by(
                execution_id=exact.id)}
            new_bytes = {results_mod.sha256_file(p)
                         for p, _rel in pi.files}
            unseen = len(new_bytes - stored_sha)
            if unseen:
                missing.append(f"{unseen} artifact(s) not in stored "
                               "evidence")
        if missing:
            return (DECISION_SUPPLEMENT, exact.id, warnings + [
                f"same run already recorded — import can fill missing "
                f"data: {', '.join(missing)}"])
        return (DECISION_DUPLICATE, exact.id, warnings + [
            "identical run already in history"])
    return DECISION_NEW, "", warnings


# ---------- staging + preview ----------

def staging_root() -> Path:
    from flask import current_app
    return Path(current_app.instance_path) / "import-staging"


def evidence_root() -> Path:
    from flask import current_app
    return Path(current_app.instance_path) / "imported-evidence"


def cleanup_stale_staging() -> None:
    root = staging_root()
    if not root.is_dir():
        return
    live = {s.staging_path for s in ImportSession.query.filter(
        ImportSession.status == "previewed")}
    cutoff = utcnow().timestamp() - STAGING_MAX_AGE_S
    for d in root.iterdir():
        if d.is_dir() and str(d) not in live \
                and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def create_preview(project: Project, binding: GitBinding,
                   revision: str, result_dir: str,
                   user: str) -> ImportSession:
    """Resolve revision -> isolated checkout -> detect + parse +
    classify. Creates bookkeeping rows only — no executions."""
    cleanup_stale_staging()
    if binding.project_id != project.id:
        raise ImportError_("binding does not belong to this project")
    worktree = Path(binding.worktree_path or "")
    if not worktree.is_dir():
        raise ImportError_("binding has no synced checkout — sync "
                           "the binding first")
    try:
        sha = git_backend.run("resolve", worktree,
                              revision or "HEAD")
    except Exception as exc:
        raise ImportError_(f"cannot resolve revision "
                           f"{revision!r}: {exc}")
    session = ImportSession(
        project_id=project.id, binding_id=binding.id,
        revision_input=revision, resolved_sha=sha,
        result_dir=result_dir, created_by=user)
    db.session.add(session)
    db.session.flush()                    # id for staging path
    staging = staging_root() / session.id
    try:
        git_backend.run("checkout", worktree, sha, staging)
    except Exception as exc:
        raise ImportError_(f"cannot materialise {sha[:12]}: {exc}")
    session.staging_path = str(staging)
    base = staging
    if binding.subdirectory:
        base = staging / binding.subdirectory
    root = (base / result_dir).resolve() if result_dir else base
    if not str(root).startswith(str(base.resolve())) \
            or not root.is_dir():
        raise ImportError_(f"result directory {result_dir!r} not found "
                           "inside the checkout")
    run_dirs, _notes = detect_run_dirs(root)
    if not run_dirs:
        session.status = "failed"
        session.error = "no result directories (no parseable JUnit XML)"
        db.session.commit()
        raise ImportError_(session.error)
    parsed = [parse_run_dir(rd, str(rd.relative_to(base)), base)
              for rd in run_dirs]
    # A Katalon COLLECTION run produces one report dir per suite, all
    # sharing the same execution.uuid — one actual run, one candidate.
    groups = {}
    ordered = []
    for pi in parsed:
        key = pi.run_uuid or f"dir:{pi.rel_dir}"
        if key not in groups:
            groups[key] = pi
            ordered.append(key)
        else:
            g = groups[key]
            g.run_dirs.append(pi.run_dir)
            g.files.extend(pi.files)
            g.junit_files.extend(pi.junit_files)
            g.cases.extend(pi.cases)
            g.suite_keys.extend(pi.suite_keys)
            g.timestamps.extend(pi.timestamps)
            g.warnings.extend(pi.warnings)
            g.unsupported = g.unsupported and pi.unsupported
            g.malformed = g.malformed or pi.malformed
    for i, key in enumerate(ordered):
        pi = groups[key]
        if len(pi.run_dirs) > 1:
            pi.warnings.append(
                f"{len(pi.run_dirs)} report dirs share "
                f"execution.uuid — imported as one collection run")
            rels = [str(d.relative_to(base)) for d in pi.run_dirs]
            pi.rel_dir = str(Path(__import__("os").path.commonpath(
                rels))) or "."
        decision, match_id, warnings = classify(project.id, pi)
        ru, ch = fingerprint(pi) if not pi.unsupported else ("", "")
        db.session.add(ImportCandidate(
            session_id=session.id, seq=i, run_dir=pi.rel_dir,
            run_dirs_json=json.dumps(
                [str(d.relative_to(base)) for d in pi.run_dirs]),
            decision=decision, match_execution_id=match_id,
            run_uuid=ru, content_hash=ch,
            suite_name=(pi.suite_keys[0][0] if pi.suite_keys
                        else pi.run_dir.name),
            started_at=_parse_ts(
                min(pi.timestamps) if pi.timestamps else ""),
            case_count=pi.case_count,
            warnings_json=json.dumps(warnings)))
    db.session.commit()
    return session


# ---------- confirm (transactional import) ----------

def _evidence_dir_for(exec_id: str) -> Path:
    return evidence_root() / exec_id


def _copy_evidence(pi: ParsedImport, exec_id: str) -> Path:
    """Copy evidence files into durable instance storage, preserving
    the layout relative to the scan root — evidence must outlive the
    transient checkout. Paths already validated at scan time."""
    dest = _evidence_dir_for(exec_id)
    dest.mkdir(parents=True, exist_ok=True)
    for src, rel in pi.files:
        tgt = dest / rel
        if ".." in Path(rel).parts:
            continue
        tgt.parent.mkdir(parents=True, exist_ok=True)
        if not tgt.exists() or tgt.stat().st_size != src.stat().st_size:
            shutil.copy2(src, tgt)
    return dest


def _best_suite_link(project_id: str, pi: ParsedImport) -> str | None:
    names = {k[0].rsplit("/", 1)[-1] for k in pi.suite_keys}
    for s in Suite.query.filter_by(project_id=project_id):
        if s.name in names:
            return s.id
    return None


def _create_execution(project, session, cand, pi, user) -> Execution:
    ex = Execution(
        project_id=project.id,
        binding_id=session.binding_id,
        suite_id=_best_suite_link(project.id, pi),
        origin="imported",
        framework="katalon",
        state=ExecutionState.COMPLETED,
        outcome=Outcome(results_mod.aggregate_outcome(
            pi.cases, completed=True)),
        created_by=user,
        imported_at=utcnow(),
        imported_by=user,
        run_uuid=pi.run_uuid or "",
        content_hash=cand.content_hash,
        source_repo=(db.session.get(GitBinding, session.binding_id).url
                     if session.binding_id else ""),
        source_commit=session.resolved_sha,
        source_path=str(Path(session.result_dir or ".") / cand.run_dir),
        started_at=_parse_ts(min(pi.timestamps) if pi.timestamps
                             else "") or cand.started_at,
        finished_at=_parse_ts(max(pi.timestamps) if pi.timestamps
                              else "") or cand.started_at,
        exit_status=0,
        report_status="pending",
        snapshot_json=json.dumps({
            "import": {
                "parser": PARSER_VERSION,
                "binding_id": session.binding_id,
                "revision": session.revision_input,
                "resolved_sha": session.resolved_sha,
                "run_dir": cand.run_dir,
                "properties": pi.properties,
                "suite_names": sorted({k[0] for k in pi.suite_keys}),
                "warnings": pi.warnings,
            },
            "suite_name": cand.suite_name,
        }))
    db.session.add(ex)
    db.session.flush()                    # id before evidence copy
    report_dir = _copy_evidence(pi, ex.id)
    ex.report_dir = str(report_dir)
    ex.workspace_path = str(report_dir.parent)
    # map junit source paths to their copied destinations
    rel_of = {f: r for f, r in pi.files}
    junit_dests = {report_dir / rel_of[f] for f, _p in pi.junit_files
                   if f in rel_of}
    manifest = results_mod.build_evidence_manifest(
        report_dir, junit_dests)
    for item in manifest:
        db.session.add(Artifact(
            execution_id=ex.id, kind=item["kind"],
            rel_path=item["rel_path"], sha256=item["sha256"],
            size=item["size"], mime=item["mime"],
            mapped=item["kind"] == "junit"))
    db.session.flush()
    seen = {}
    for case in pi.cases:
        key = (case.classname, case.name)
        seen[key] = seen.get(key, 0) + 1
        db.session.add(TestResult(
            execution_id=ex.id, case_ref=case.classname or case.name,
            case_name=case.name, classname=case.classname,
            iteration=case.iteration, attempt=seen[key],
            status=case.status, duration_s=case.duration_s,
            message=(case.message or "")[:4000],
            unresolved=any(p.discrepant
                           for _f, p in pi.junit_files)))
    db.session.flush()
    try:
        from . import reporting
        reporting.refresh_report_status(ex)
    except Exception:
        pass
    return ex


def _supplement(ex: Execution, pi: ParsedImport,
                session: ImportSession) -> list:
    """Fill only demonstrably-missing data; existing values and their
    provenance are preserved. Returns what was filled."""
    filled = []
    if ex.finished_at is None:
        ts = _parse_ts(max(pi.timestamps) if pi.timestamps else "")
        if ts:
            ex.finished_at = ts
            filled.append("finished_at")
    if ex.started_at is None:
        ts = _parse_ts(min(pi.timestamps) if pi.timestamps else "")
        if ts:
            ex.started_at = ts
            filled.append("started_at")
    report_dir = Path(ex.report_dir) if ex.report_dir else None
    stored_sha = {a.sha256 for a in Artifact.query.filter_by(
        execution_id=ex.id)}
    added = 0
    if report_dir is not None and report_dir.is_dir():
        for src, rel in pi.files:
            if results_mod.sha256_file(src) in stored_sha:
                continue
            if ".." in Path(rel).parts:
                continue
            tgt = report_dir / rel
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tgt)
            db.session.add(Artifact(
                execution_id=ex.id,
                kind=results_mod.classify_artifact(tgt),
                rel_path=rel,
                sha256=results_mod.sha256_file(tgt),
                size=tgt.stat().st_size,
                mime="", mapped=False))
            added += 1
    if added:
        filled.append(f"{added} artifact(s)")
    snap = ex.snapshot()
    snap.setdefault("imports", []).append({
        "parser": PARSER_VERSION, "resolved_sha": session.resolved_sha,
        "run_dir": pi.rel_dir, "at": utcnow().isoformat(),
        "filled": filled})
    ex.snapshot_json = json.dumps(snap)
    return filled


def _reparse_candidate(base: Path, cand: ImportCandidate
                       ) -> ParsedImport:
    """Rebuild the ParsedImport for a candidate at confirm time —
    collection runs span several dirs (cand.run_dir is their parent)."""
    scan_root = base
    dirs = [base / d for d in cand.run_dirs()] or         [base / cand.run_dir]
    parsed = [parse_run_dir(d, str(d.relative_to(base)), base)
              for d in dirs if d.is_dir()]
    if not parsed:
        raise ImportError_("run dir missing from staging")
    pi = parsed[0]
    for extra in parsed[1:]:
        pi.run_dirs.append(extra.run_dir)
        pi.files.extend(extra.files)
        pi.junit_files.extend(extra.junit_files)
        pi.cases.extend(extra.cases)
        pi.suite_keys.extend(extra.suite_keys)
        pi.timestamps.extend(extra.timestamps)
        pi.warnings.extend(extra.warnings)
    return pi


def confirm_import(session: ImportSession, selected_ids: list,
                   force_new_ids: list, user: str) -> dict:
    """Recheck + persist selected candidates, per-candidate transaction.
    Idempotent: imported/skipped candidates are not re-processed."""
    staging = Path(session.staging_path)
    if not staging.is_dir() and any(
            c.status in ("pending", "failed")
            for c in session.candidates):
        raise ImportError_(
            "staging checkout expired — rerun preview to re-materialise "
            "the revision")
    base = staging
    binding = db.session.get(GitBinding, session.binding_id)
    if binding and binding.subdirectory:
        base = staging / binding.subdirectory
    out = {"imported": [], "skipped": [], "supplemented": [],
           "failed": [], "ambiguous": []}
    for cand in session.candidates:
        if cand.id not in selected_ids or cand.status in (
                "imported", "skipped"):
            continue
        try:
            pi = _reparse_candidate(base, cand)
            decision, match_id, warnings = classify(
                session.project_id, pi)
            if decision == DECISION_AMBIGUOUS \
                    and cand.id in force_new_ids:
                decision = DECISION_NEW
            if decision == DECISION_DUPLICATE:
                cand.status = "skipped"
                cand.imported_execution_id = match_id
                out["skipped"].append((cand.id, match_id))
            elif decision == DECISION_SUPPLEMENT:
                ex = db.session.get(Execution, match_id)
                filled = _supplement(ex, pi, session)
                cand.status = "imported"
                cand.imported_execution_id = ex.id
                out["supplemented"].append((cand.id, ex.id, filled))
            elif decision == DECISION_NEW:
                ex = _create_execution(
                    db.session.get(Project, session.project_id),
                    session, cand, pi, user)
                cand.status = "imported"
                cand.imported_execution_id = ex.id
                out["imported"].append((cand.id, ex.id))
            elif decision == DECISION_AMBIGUOUS:
                cand.status = "failed"
                cand.error = ("ambiguous match — requires review "
                              "(select 'import as new' explicitly)")
                out["ambiguous"].append(cand.id)
            else:
                cand.status = "failed"
                cand.error = decision
                out["failed"].append((cand.id, decision))
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            # Race: another import committed the same run between our
            # recheck and insert — resolve to the existing row.
            try:
                pi = _reparse_candidate(base, cand)
                _d, match_id, _w = classify(session.project_id, pi)
                cand.status = "skipped"
                cand.imported_execution_id = match_id
                out["skipped"].append((cand.id, match_id))
                db.session.commit()
            except Exception as exc2:
                db.session.rollback()
                cand.status = "failed"
                cand.error = f"integrity conflict: {exc2}"[:1000]
                out["failed"].append((cand.id, "conflict"))
                db.session.commit()
        except Exception as exc:
            db.session.rollback()
            cand.status = "failed"
            cand.error = f"{type(exc).__name__}: {exc}"[:1000]
            out["failed"].append((cand.id, str(exc)[:200]))
            db.session.commit()
    session.status = "committed"
    session.summary_json = json.dumps(
        {k: len(v) for k, v in out.items()})
    db.session.commit()
    shutil.rmtree(staging, ignore_errors=True)
    return out
