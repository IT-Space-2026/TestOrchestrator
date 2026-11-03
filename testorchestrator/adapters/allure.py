"""Allure adapter — canonical results -> allure-results -> bundled
generator (§1/§4 consolidated direction).

- One execution produces ONE results conversion + ONE generation; the
  report is regenerated from stored results/artifacts — KRE is never
  re-run to rebuild a report (safe retry).
- Status mapping is honest: PASSED/FAILED/ERROR/SKIPPED map to
  passed/failed/broken/skipped; unresolved/discrepant cases map to
  ``unknown``, never to passed.
- Attachment mapping: an artifact attaches to a case when its filename
  contains the case name (documented KRE naming heuristic). Unmatched
  evidence lands in a labelled run-level container — preserved, not
  silently dropped or misattributed.
- ``allure_status`` is independent of execution state/outcome and of
  report_status/RP state.
"""

import json
import shutil
import uuid
from pathlib import Path

from ..allure_runtime import generate as run_generator, version
from ..execution import Artifact, Execution, ExecutionState
from ..results import sha256_file

STATUS_MAP = {"passed": "passed", "failed": "failed",
              "error": "broken", "skipped": "skipped"}


def _allure_status(result) -> str:
    if getattr(result, "unresolved", False):
        return "unknown"
    return STATUS_MAP.get(result.status.value.lower(), "unknown")


def _case_attachments(ex: Execution, root: Path) -> dict:
    """Artifact -> test_result mapping. result_id wins when populated;
    otherwise the filename-contains-case-name heuristic; unmatched stay
    at run level."""
    by_name = {}
    for a in Artifact.query.filter_by(execution_id=ex.id).all():
        if a.result_id:
            by_name.setdefault(a.result_id, []).append(a)
            continue
        stem = Path(a.rel_path).stem.lower()
        for r in ex.results:
            name = (r.case_name or "").lower()
            cls = (r.classname or "").lower()
            if a.kind in ("screenshot", "video", "har") and \
                    (name and name in stem or cls and cls in stem):
                by_name.setdefault(r.id, []).append(a)
                break
    return by_name


def _write_results(ex: Execution, dest: Path) -> dict:
    """Emit allure-results JSON + attachment payloads. Returns stats."""
    root = Path(ex.report_dir or "")
    dest.mkdir(parents=True, exist_ok=True)
    attached, unmapped, cases = 0, [], 0
    case_attach = _case_attachments(ex, root)
    used = set()
    for r in ex.results:
        rid = uuid.uuid4().hex
        body = {
            "uuid": rid, "historyId": f"{r.classname}.{r.case_name}",
            "name": r.case_name or r.case_ref,
            "fullName": f"{r.classname}.{r.case_name}",
            "status": _allure_status(r),
            "stage": "finished",
            "labels": [
                {"name": "suite", "value": ex.suite.name if ex.suite else ""},
                {"name": "package",
                 "value": r.classname or (ex.project.name if ex.project else "")},
                {"name": "testClass", "value": r.classname or ""},
            ],
        }
        if r.duration_s:
            body["start"] = 0
            body["stop"] = int(r.duration_s * 1000)
        if r.message:
            body["statusDetails"] = {"message": r.message[:8000]}
        files = []
        for a in case_attach.get(r.id, []):
            src = (root / a.rel_path)
            if not src.is_file():
                continue
            ext = Path(a.rel_path).suffix or ".bin"
            fname = f"{uuid.uuid4().hex}-attachment{ext}"
            shutil.copy2(src, dest / fname)
            files.append({"name": Path(a.rel_path).name,
                          "source": fname,
                          "type": a.mime or "application/octet-stream"})
            used.add(a.id)
            attached += 1
        if files:
            body["attachments"] = files
        cases += 1
        (dest / f"{rid}-result.json").write_text(json.dumps(body))
    # Unmapped run-level evidence -> labelled container attachment.
    for a in Artifact.query.filter_by(execution_id=ex.id).all():
        if a.id in used or a.kind in ("junit", "html", "log"):
            continue
        src = root / a.rel_path
        if not src.is_file():
            continue
        ext = Path(a.rel_path).suffix or ".bin"
        fname = f"{uuid.uuid4().hex}-attachment{ext}"
        shutil.copy2(src, dest / fname)
        unmapped.append({"name": f"[run-level] {a.rel_path}",
                         "source": fname})
    if unmapped:
        cid = uuid.uuid4().hex
        (dest / f"{cid}-container.json").write_text(json.dumps({
            "uuid": cid, "name": f"run {ex.id[:8]} evidence",
            "children": [],
            "afters": [{"name": "unmapped artifacts",
                        "attachments": unmapped, "status": "unknown",
                        "stage": "finished", "start": 0, "stop": 0}],
        }))
    # Provenance + run identification — no secrets, config snapshot is
    # non-sensitive fields only.
    (dest / "executor.json").write_text(json.dumps({
        "name": "TestOrchestrator",
        "type": "orchestrator-run",
        "buildName": f"execution {ex.id[:8]}",
        "reportName": f"{ex.project.name if ex.project else ''} / "
                      f"{ex.suite.name if ex.suite else ''}",
        "reportUrl": "",
    }))
    if ex.state in (ExecutionState.CANCELED, ExecutionState.UNKNOWN):
        (dest / "categories.json").write_text(json.dumps([{
            "name": f"interrupted run ({ex.state.value})",
            "matchedStatuses": ["unknown", "skipped"],
        }]))
    return {"cases": cases, "attached": attached,
            "unmapped": len(unmapped)}


def generate_for(ex: Execution) -> dict:
    """Convert + generate + persist allure_status/path/version.
    Idempotent: regenerating overwrites only the allure-results + allure/
    dirs of THIS run; original artifacts are untouched."""
    root = Path(ex.report_dir or "")
    if not root.is_dir() or not ex.results:
        ex.allure_status = "missing"
        from ..extensions import db
        db.session.commit()
        return {"status": "missing"}
    results_dir = root / "allure-results"
    if results_dir.exists():
        shutil.rmtree(results_dir)
    stats = _write_results(ex, results_dir)
    out_dir = root / "allure"
    if out_dir.exists():
        shutil.rmtree(out_dir)  # generated output only — safe to rebuild
    res = run_generator(results_dir, out_dir)
    from ..extensions import db
    ex.allure_version = version()
    if res["ok"] and (out_dir / "index.html").exists():
        ex.allure_status = "ready"
        ex.report_path = ex.report_path  # unchanged
        ex.allure_path = str((out_dir / "index.html").relative_to(root))
    elif not res["ok"] and ex.results:
        ex.allure_status = "failed"
    else:
        ex.allure_status = "missing"
    db.session.commit()
    return {"status": ex.allure_status, **stats,
            "error": res.get("error", "")}
