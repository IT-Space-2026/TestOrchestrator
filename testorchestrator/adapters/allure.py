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
import os
import shutil
import uuid
from pathlib import Path

from ..allure_runtime import generate as run_generator, version
from ..execution import Artifact, Execution, ExecutionState

STATUS_MAP = {"passed": "passed", "failed": "failed",
              "error": "broken", "skipped": "skipped"}


def _allure_status(result) -> str:
    if getattr(result, "unresolved", False):
        return "unknown"
    return STATUS_MAP.get(result.status.value.lower(), "unknown")


def _stem_tokens(stem: str) -> set:
    import re
    return {t for t in re.split(r"[^a-z0-9]+", stem.lower()) if t}


def _case_attachments(ex: Execution, root: Path) -> dict:
    """Artifact -> test_result mapping. Authoritative `result_id` wins
    when populated. The filename heuristic is an explicitly LIMITED
    fallback: attach only when the artifact stem contains the case name
    as a whole token AND that match is unambiguous (exactly one artifact
    candidate and exactly one case). Duplicate case names, similar
    filenames, or multiply-matching stems are NEVER guessed — they stay
    unmatched and labelled."""
    by_id = {}
    ambiguous = set()
    results = [r for r in ex.results]
    for a in Artifact.query.filter_by(execution_id=ex.id).all():
        if a.result_id:
            by_id.setdefault(a.result_id, []).append(a)
            continue
        if a.kind not in ("screenshot", "video", "har"):
            continue
        stem = Path(a.rel_path).stem.lower()
        tokens = _stem_tokens(stem)
        names = [(r.case_name or "").lower() for r in results]
        hits = {r.id for r in results
                if (r.case_name or "").lower() in tokens
                or (r.case_name or "").lower() == stem}
        # Unambiguous only: one stem -> one case AND that case's name is
        # unique among results (duplicate names can never be resolved).
        dupes = len([n for n in names if n]) != len(set(n for n in names if n))
        if len(hits) == 1 and not dupes:
            by_id.setdefault(hits.pop(), []).append(a)
        else:
            ambiguous.add(a.id)
    return by_id


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
    # Unmatched/ambiguous evidence -> a VISIBLE synthetic result entry
    # labelled plainly (a bare container can render empty/invisible).
    for a in Artifact.query.filter_by(execution_id=ex.id).all():
        if a.id in used or a.kind in ("junit", "html", "log"):
            continue
        src = root / a.rel_path
        if not src.is_file():
            continue
        ext = Path(a.rel_path).suffix or ".bin"
        fname = f"{uuid.uuid4().hex}-attachment{ext}"
        shutil.copy2(src, dest / fname)
        unmapped.append({"name": a.rel_path, "source": fname,
                         "type": a.mime or "application/octet-stream"})
    if unmapped:
        rid = uuid.uuid4().hex
        (dest / f"{rid}-result.json").write_text(json.dumps({
            "uuid": rid, "historyId": "unmatched-evidence",
            "name": "[unmatched run-level artifacts — no case mapping "
                    "could be proven]",
            "fullName": "run.unmatched-artifacts",
            "status": "unknown", "stage": "finished",
            "statusDetails": {"message": "Evidence whose owning test case "
                              "could not be established unambiguously."},
            "labels": [{"name": "suite",
                        "value": ex.suite.name if ex.suite else ""}],
            "attachments": unmapped,
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
    tmp_dir = root / "allure.tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    # Atomic publish: generate into .tmp, swap in only on success — a
    # failed or concurrent regenerate never serves a half-written report
    # and never destroys the last usable one.
    res = run_generator(results_dir, tmp_dir)
    if res["ok"] and (tmp_dir / "index.html").exists():
        if out_dir.exists():
            shutil.rmtree(out_dir)
        os.replace(tmp_dir, out_dir)
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
