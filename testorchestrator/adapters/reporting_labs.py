"""reporting-labs adapter — canonical results -> single-file HTML report
(2.0 migration backend; Allure 3 remains the shipping default for 1.0.1).

Mirrors ``adapters/allure.py`` semantics so behaviour is comparable:
- Same status mapping (passed/failed/error->failed/skipped; unresolved
  -> skipped with an explanatory message — reporting-labs has no
  'unknown' status, so unresolved cases surface as skipped+labelled).
- Same attachment policy: result_id wins, else the documented
  unambiguous filename heuristic; unmatched evidence lands in a labelled
  run-level record — preserved, never dropped.
- Same atomic publish: build into a unique .tmp dir, swap in only on
  success — a failed or concurrent regenerate never serves a
  half-written report and never destroys the last usable one.
- No Node/bundled runtime needed — the report is a single self-contained
  index.html built entirely in Python.

Concurrency: each generation uses its OWN tmp dir (mkdtemp), so two
concurrent regenerations can never clobber each other's in-progress
output; the final swap is atomic (last writer wins with a complete
report, never a corrupt one).
"""
import shutil
import tempfile
from pathlib import Path

from ..execution import Artifact, Execution, ExecutionState
from .allure import _case_attachments

STATUS_MAP = {"passed": "passed", "failed": "failed",
              "error": "failed", "skipped": "skipped"}
OUT_DIRNAME = "reporting-labs"


def version() -> str:
    try:
        import reporting_labs
        return getattr(reporting_labs, "__version__", "") or \
            getattr(reporting_labs, "_version", "")
    except Exception:
        return ""


def _options(out_dir: Path):
    from reporting_labs.core.options import DEFAULTS, Options
    values = dict(DEFAULTS)
    values["outputFolder"] = str(out_dir)
    values["pdf"] = False            # single HTML artifact — no chrome dep
    values["open"] = "never"
    values["announce"] = False       # no stdout noise in the service log
    values["history"] = {"enabled": False}  # no stray history file
    return Options(values, base=out_dir, root=out_dir)


def _emit(ex: Execution, out_dir: Path) -> dict:
    """Build the reporting-labs report for one execution. Returns stats."""
    from reporting_labs.core.collector import Run
    from reporting_labs.core.model import Attempt, Attachment, ErrorInfo
    from reporting_labs.core.writer import write

    root = Path(ex.report_dir or "")
    opts = _options(out_dir)
    run = Run(opts, framework="katalon",
              environ={"TO_EXECUTION": ex.id[:8]})
    run.rerun = None
    case_attach = _case_attachments(ex, root)
    used, attached, cases = set(), 0, 0
    for r in ex.results:
        tid = f"{r.classname}.{r.case_name}" if r.classname else \
              (r.case_name or r.case_ref)
        unresolved = getattr(r, "unresolved", False)
        t = run.test(tid, r.case_name or r.case_ref,
                     path=[ex.suite.name if ex.suite else "",
                           r.classname or ""],
                     file=r.classname or "", line=0,
                     project=ex.project.name if ex.project else "")
        t.meta = {"execution": ex.id[:8]}
        a = Attempt(tid)
        a.start = 0
        a.end = int((r.duration_s or 0) * 1000)
        a.status = "skipped" if unresolved else \
            STATUS_MAP.get(r.status.value.lower(), "skipped")
        if unresolved:
            a.add_error(ErrorInfo(
                message="unresolved/discrepant case — recorded as "
                        "skipped, never passed"))
        elif a.status == "failed" and r.message:
            a.add_error(ErrorInfo(message=r.message[:8000]))
        for art in case_attach.get(r.id, []):
            src = root / art.rel_path
            if not src.is_file():
                continue
            a.attach(art.rel_path, path=str(src),
                     content_type=art.mime or "application/octet-stream")
            used.add(art.id)
            attached += 1
        run.add_attempt(tid, a)
        cases += 1
    # unmatched/ambiguous evidence -> labelled run-level record
    unmapped = []
    for art in Artifact.query.filter_by(execution_id=ex.id).all():
        if art.id in used or art.kind in ("junit", "html", "log"):
            continue
        src = root / art.rel_path
        if src.is_file():
            unmapped.append(art)
    if unmapped:
        tid = "run.unmatched-artifacts"
        run.test(tid, "[unmatched run-level artifacts — no case mapping "
                      "could be proven]",
                 path=[ex.suite.name if ex.suite else ""], file="",
                 line=0, project=ex.project.name if ex.project else "")
        a = Attempt(tid)
        a.start = a.end = 0
        a.status = "skipped"
        a.add_error(ErrorInfo(
            message="Evidence whose owning test case could not be "
                    "established unambiguously."))
        for art in unmapped:
            a.attach(art.rel_path, path=str(root / art.rel_path),
                     content_type=art.mime or "application/octet-stream")
        run.add_attempt(tid, a)
    # Run-level logs (kre-*.log etc.): representable ONLY at run level —
    # never attributed to a case. Text surfaces as labelled global
    # output; the files are preserved as attachments on a separate
    # run-level record distinct from "unmatched case evidence".
    log_arts = [a for a in
                Artifact.query.filter_by(execution_id=ex.id)
                if a.kind == "log" and (root / a.rel_path).is_file()]
    for art in log_arts:
        try:
            text = (root / art.rel_path).read_text(
                encoding="utf-8", errors="replace")[:20000]
        except OSError:
            continue
        run.output("log", f"--- {art.rel_path} ---\n{text}")
    if log_arts:
        tid = "run.logs"
        run.test(tid, "[run-level logs — captured for the whole "
                      "execution, not case evidence]",
                 path=[ex.suite.name if ex.suite else ""], file="",
                 line=0, project=ex.project.name if ex.project else "")
        a = Attempt(tid)
        a.start = a.end = 0
        a.status = "skipped"
        a.add_error(ErrorInfo(
            message="Run logs apply to the whole execution; step/case "
                    "attribution is not present in the evidence."))
        for art in log_arts:
            a.attach(art.rel_path, path=str(root / art.rel_path),
                     content_type=art.mime or "text/plain")
        run.add_attempt(tid, a)
    if ex.state in (ExecutionState.CANCELED, ExecutionState.UNKNOWN):
        run.global_error(ErrorInfo(
            message=f"interrupted run ({ex.state.value})"))
    run.run_status = "failed" if ex.state is ExecutionState.FAILED \
        else run.run_status
    data = run.build()
    html = write(data, opts)
    return {"cases": cases, "attached": attached,
            "unmapped": len(unmapped), "html": str(html)}


def generate_for(ex: Execution) -> dict:
    """Convert + generate + persist rl_status/path/version. Idempotent
    like the Allure path: regenerates only this run's reporting-labs
    output dir; original artifacts and Allure reports are untouched."""
    root = Path(ex.report_dir or "")
    if not root.is_dir() or not ex.results:
        ex.rl_status = "missing"
        from ..extensions import db
        db.session.commit()
        return {"status": "missing"}
    out_dir = root / OUT_DIRNAME
    # Unique tmp per generation — concurrent regenerations never share
    # an in-progress dir (the fixed-.tmp defect this fixes).
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"{OUT_DIRNAME}.tmp.",
                                    dir=root))
    try:
        stats = _emit(ex, tmp_dir)
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        ex.rl_version = version()
        ex.rl_status = "failed"
        from ..extensions import db
        db.session.commit()
        return {"status": "failed", "error": str(exc)}
    ok = (tmp_dir / "index.html").exists()
    from ..extensions import db
    ex.rl_version = version()
    if ok:
        # Gap-free publish: reporting-labs emits FIXED filenames, so
        # publish per file with os.replace — a reader mid-swap always
        # gets a complete (old or new) file, never a missing/partial
        # one. Better than rmtree+swap, which leaves a gone-dir window.
        out_dir.mkdir(parents=True, exist_ok=True)
        for f in tmp_dir.rglob("*"):
            if f.is_file():
                dest = out_dir / f.relative_to(tmp_dir)
                dest.parent.mkdir(parents=True, exist_ok=True)
                f.replace(dest)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        ex.rl_status = "ready"
        ex.rl_path = str((out_dir / "index.html").relative_to(root))
    else:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        ex.rl_status = "failed"
    db.session.commit()
    return {"status": ex.rl_status, **stats}
