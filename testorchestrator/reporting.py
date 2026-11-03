"""Local Orchestrator reporting (§3–§5 of consolidated direction).

Native-KRE-report path (selected: KRE's own HTML report + JUnit +
screenshots; Allure deferred — no concrete gap identified). Responsibilities:

- Assess what a run actually produced against the stable run dir
  (``Execution.report_dir`` — evidence already lives under the immutable
  run ID, so artifacts of different runs can never mix).
- Track ``report_status`` INDEPENDENT of execution state/outcome:
  ``pending`` (not yet assessed), ``ready`` (viewable native report),
  ``partial`` (results/evidence but no report entry — labelled, never a
  false ready), ``missing`` (nothing usable — actionable), ``failed``
  (collection/assessment error — the execution outcome is untouched).
- Authenticated view/download/refresh actions are in portal.routes; this
  module supplies the assessment, path safety, and archive builders.

Retry semantics: ``refresh()`` and ``register_missing_artifacts()`` are
idempotent — they re-scan the run's evidence dir, add Artifact rows that
are absent, and never delete, overwrite, or rerun KRE.
"""

import io
import json
import zipfile
from pathlib import Path

from .extensions import db
from .execution import Artifact, Execution
from .results import (build_evidence_manifest, classify_artifact,
                      sha256_file)
from .adapters import kre as kre_adapter

REPORT_STATUSES = {"pending", "ready", "partial", "missing", "failed"}
ENTRY_CANDIDATES = ("report.html", "index.html")


def report_root(ex: Execution) -> Path:
    return Path(ex.report_dir or "").resolve()


def safe_under(root: Path, rel: str) -> Path:
    """Resolve rel under root; rejects traversal outright."""
    target = (root / rel).resolve()
    if not str(target).startswith(str(root) + "/"):
        raise ValueError("path escapes evidence root")
    return target


def _find_entry(root: Path) -> Path | None:
    """The native KRE report entry: prefer report.html/index.html,
    otherwise the first HTML artifact. A bare index.html alone is not
    claimed as a complete report — the zip ships its whole subtree."""
    for name in ENTRY_CANDIDATES:
        hit = list(root.rglob(name))
        if hit:
            return hit[0]
    others = sorted(p for p in root.rglob("*")
                    if p.suffix.lower() in (".html", ".htm"))
    return others[0] if others else None


def assess(ex: Execution) -> dict:
    """Inspect evidence dir; return {status, entry}. 'partial' keeps
    results honest when the producer failed before writing a report."""
    root = report_root(ex)
    if not root.is_dir():
        return {"status": "missing", "entry": ""}
    entry = _find_entry(root)
    if entry:
        return {"status": "ready",
                "entry": str(entry.relative_to(root))}
    has_any = (ex.results or
               Artifact.query.filter_by(execution_id=ex.id).first() or
               any(root.rglob("*")))
    return {"status": "partial" if has_any else "missing", "entry": ""}


def refresh_report_status(ex: Execution) -> str:
    """Re-assess and persist. Never touches state/outcome."""
    a = assess(ex)
    ex.report_status = a["status"]
    ex.report_path = a["entry"]
    db.session.commit()
    return ex.report_status


def refresh_failed(ex: Execution, exc: Exception) -> None:
    ex.report_status = "failed"
    ex.report_path = ""
    db.session.commit()


def register_missing_artifacts(ex: Execution) -> int:
    """Idempotent collection retry: re-scan the evidence dir, add Artifact
    rows for files not yet registered. Nothing is deleted or rerun."""
    root = report_root(ex)
    if not root.is_dir():
        return 0
    junit_files = set(kre_adapter.discover_reports(root))
    manifest = build_evidence_manifest(root, junit_files)
    log_dir = Path(ex.workspace_path or "") / "logs"
    if log_dir.is_dir():
        for log_path in sorted(log_dir.glob("kre-*.log")):
            manifest.append({
                "rel_path": f"logs/{log_path.name}", "kind": "log",
                "sha256": sha256_file(log_path),
                "size": log_path.stat().st_size, "mime": "text/plain",
            })
    known = {a.rel_path for a in
             Artifact.query.filter_by(execution_id=ex.id)}
    added = 0
    for item in manifest:
        if item["rel_path"] in known:
            continue
        db.session.add(Artifact(
            execution_id=ex.id, kind=item["kind"],
            rel_path=item["rel_path"], sha256=item["sha256"],
            size=item["size"], mime=item["mime"],
            mapped=item["kind"] == "junit"))
        added += 1
    db.session.commit()
    return added


def entry_dir(ex: Execution) -> Path:
    """Directory holding the report entry — the report bundle root for
    asset resolution and the report.zip subtree."""
    root = report_root(ex)
    if not ex.report_path:
        return root
    return (root / ex.report_path).resolve().parent


def build_report_zip(ex: Execution) -> io.BytesIO:
    """Everything the entry needs: the whole subtree containing it
    (html + linked css/js/screenshots), plus a manifest. This is the
    usable-report download — not a bare index.html."""
    base = entry_dir(ex)
    buf = io.BytesIO()
    manifest = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(base)
            z.write(p, str(rel))
            manifest.append({"path": str(rel),
                             "sha256": sha256_file(p)})
        z.writestr("manifest.json", json.dumps(
            {"execution_id": ex.id, "entry": ex.report_path,
             "files": manifest}, indent=2))
    buf.seek(0)
    return buf


def build_artifacts_zip(ex: Execution) -> io.BytesIO:
    """Original evidence as collected — every registered artifact plus
    its checksum manifest; never the regenerated report."""
    root = report_root(ex)
    buf = io.BytesIO()
    manifest = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for a in Artifact.query.filter_by(execution_id=ex.id):
            try:
                target = safe_under(root, a.rel_path)
            except ValueError:
                continue
            if target.is_file():
                z.write(target, a.rel_path)
            manifest.append({"rel_path": a.rel_path, "kind": a.kind,
                             "sha256": a.sha256})
        z.writestr("manifest.json",
                   json.dumps({"execution_id": ex.id,
                               "artifacts": manifest}, indent=2))
    buf.seek(0)
    return buf
