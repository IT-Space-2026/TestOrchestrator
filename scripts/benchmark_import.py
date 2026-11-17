#!/usr/bin/env python3
"""Import-matching benchmark (2.0.2) — measures classify/preview/confirm
cost against increasing project-history sizes.

Generated datasets for scale; real-format KRE fixture for correctness.
Reports: preview+classify duration, confirm duration, DB query count,
evidence-file reads, repeat-import behaviour, concurrent import.

Usage:
    python3 scripts/benchmark_import.py [--sizes 50 200 500] [--out docs/bench-2.0.2.md]
"""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

JUNIT = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="{suite}" id="{suite}" hostname="w1"
             timestamp="{ts}" tests="{n}" failures="0" errors="0"
             skipped="0" time="{t}">
    {cases}
  </testsuite>
</testsuites>
"""
CASE = '<testcase classname="{cls}" name="{nm}" time="1.0"/>'

# --- counters ---
QUERY_COUNT = {"n": 0}
FILE_READS = {"n": 0}


def _count_sql(*_a, **_kw):
    QUERY_COUNT["n"] += 1


def instrument(app):
    from sqlalchemy import event
    event.listen(app.extensions["sqlalchemy"].engine,
                 "before_cursor_execute", _count_sql)
    # count junit parses (evidence reads)
    from testorchestrator import importer
    orig = importer._looks_junit.__wrapped__ if hasattr(
        importer._looks_junit, "__wrapped__") else importer._looks_junit
    def counting(path):
        FILE_READS["n"] += 1
        return orig(path)
    importer._looks_junit = counting


def reset():
    QUERY_COUNT["n"] = 0
    FILE_READS["n"] = 0


def make_app(tmp_path):
    from testorchestrator import create_app
    from testorchestrator.config import Config

    class Cfg(Config):
        TESTING = True
        SECRET_KEY = "bench"
        WTF_CSRF_ENABLED = False
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path}/bench.db"
    return create_app(Cfg)


def kre_files(suite="TS_A", ts="2026-11-10T09:30:00", n_cases=5,
              uuid=""):
    cases = "\n    ".join(
        CASE.format(cls=suite, nm=f"case{i}") for i in range(n_cases))
    out = {
        "JUnit_Report.xml": JUNIT.format(
            suite=suite, ts=ts, n=n_cases, t=n_cases, cases=cases),
        "execution.properties": "os=Linux\n",
        "index.html": "<html/>",
    }
    if uuid:
        out["execution.uuid"] = uuid
    return out


def seed_history(app, project_id, n, stamped_frac=1.0, tmp_path=None):
    """Create n past executions in the project; stamped_frac of them
    carry run_uuid+content_hash, the rest are legacy-unstamped rows
    with real report dirs + junit artifacts."""
    from testorchestrator.execution import (
        Artifact, Execution, ExecutionState, Outcome)
    from testorchestrator.extensions import db
    from testorchestrator import importer, results as results_mod

    for i in range(n):
        rd = tmp_path / f"hist-{i}" / "Reports"
        rd.mkdir(parents=True)
        files = kre_files(suite=f"TS_H{i}", ts=f"2026-11-{(i % 28) + 1:02d}"
                          f"T09:{i % 60:02d}:00",
                          uuid=f"uuid-hist-{i:05d}")
        for name, content in files.items():
            (rd / name).write_text(content)
        stamped = i < int(n * stamped_frac)
        ex = Execution(
            project_id=project_id, origin="live", framework="katalon",
            state=ExecutionState.COMPLETED, outcome=Outcome.PASSED,
            created_by="bench",
            run_uuid=f"uuid-hist-{i:05d}" if stamped else "",
            report_dir=str(rd))
        db.session.add(ex)
        db.session.flush()
        if stamped:
            parsed = results_mod.parse_junit(rd / "JUnit_Report.xml")
            keys = [(s["id"] or s["name"], s["timestamp"])
                    for s in parsed.suites]
            ex.content_hash = importer.content_fingerprint(
                keys, [s["timestamp"] for s in parsed.suites],
                parsed.cases, parsed.producer_tests)
        db.session.add(Artifact(
            execution_id=ex.id, kind="junit",
            rel_path="JUnit_Report.xml",
            sha256=results_mod.sha256_file(rd / "JUnit_Report.xml"),
            size=(rd / "JUnit_Report.xml").stat().st_size,
            mime="", mapped=True))
    db.session.commit()


def preview_one(app, project_id, binding_id, result_dir="Reports"):
    from testorchestrator import importer
    from testorchestrator.execution import GitBinding, Project
    from testorchestrator.extensions import db
    p = db.session.get(Project, project_id)
    b = db.session.get(GitBinding, binding_id)
    s = importer.create_preview(p, b, "HEAD", result_dir, "bench")
    return s


def run_size(app, tmp_path, n_hist, stamped_frac, label):
    """History=n_hist; import a new run, then repeat-import it."""
    from testorchestrator import importer
    from testorchestrator.execution import (
        Execution, GitBinding, ImportSession, Project)
    from testorchestrator.extensions import db
    import tests.git_helpers as gh

    with app.app_context():
        project = Project(name=f"bench-{label}", description="")
        db.session.add(project)
        db.session.flush()
        origin = gh.bare_repo(tmp_path / f"{label}-origin")
        files = {f"Reports/run1/{k}": v for k, v in
                 kre_files(suite="TS_NEW", uuid="uuid-import-1").items()}
        gh.seed_repo(str(origin), tmp_path / f"{label}-wc", files)
        wc = gh.clone(str(origin), tmp_path / f"{label}-binding")
        binding = GitBinding(project_id=project.id, name=label,
                             url=str(origin), ref="master",
                             subdirectory="", worktree_path=str(wc))
        db.session.add(binding)
        db.session.commit()
        pid, bid = project.id, binding.id

        seed_history(app, pid, n_hist, stamped_frac, tmp_path / label)

        # --- import a NEW candidate ---
        reset()
        t0 = time.perf_counter()
        s1 = preview_one(app, pid, bid)
        t_preview = time.perf_counter() - t0
        q_prev, r_prev = QUERY_COUNT["n"], FILE_READS["n"]

        reset()
        t0 = time.perf_counter()
        s = db.session.get(ImportSession, s1.id)
        out = importer.confirm_import(
            s, [c.id for c in s.candidates], [], "bench")
        t_confirm = time.perf_counter() - t0
        q_conf, r_conf = QUERY_COUNT["n"], FILE_READS["n"]
        imported = len(out["imported"])

        # --- repeat import (same content → dedup path) ---
        reset()
        t0 = time.perf_counter()
        s2 = preview_one(app, pid, bid)
        t_repeat = time.perf_counter() - t0
        q_rep, r_rep = QUERY_COUNT["n"], FILE_READS["n"]
        from testorchestrator.extensions import db as _d
        dec = _d.session.get(ImportSession, s2.id).candidates[0].decision

        return {
            "label": label, "history": n_hist,
            "stamped_frac": stamped_frac,
            "imported": imported,
            "preview_s": round(t_preview, 3),
            "preview_queries": q_prev,
            "preview_reads": r_prev,
            "confirm_s": round(t_confirm, 3),
            "confirm_queries": q_conf,
            "confirm_reads": r_conf,
            "repeat_preview_s": round(t_repeat, 3),
            "repeat_queries": q_rep,
            "repeat_reads": r_rep,
            "repeat_decision": dec,
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", nargs="+", type=int,
                    default=[25, 100, 400])
    ap.add_argument("--out", default="")
    ap.add_argument("--unstamped", type=float, default=0.2,
                    help="fraction of history left unstamped (legacy)")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="to-bench-"))
    app = make_app(tmp)
    with app.app_context():
        from testorchestrator.extensions import db
        db.create_all()
        from testorchestrator import settings as _s
        _s.set("dagu_mandatory", False)
        db.session.commit()
    with app.app_context():
        instrument(app)

    rows = []
    env = {"python": sys.version.split()[0],
           "platform": os.uname().sysname + " " + os.uname().machine,
           "cpu": os.cpu_count()}
    for n in args.sizes:
        r = run_size(app, tmp, n, 1.0 - args.unstamped, f"n{n}")
        rows.append(r)
        print(json.dumps(r))

    report = ["# Import-matching benchmark — 2.0.2", "",
              f"Environment: {env}", "",
              "| history | preview_s | preview_q | preview_reads | "
              "confirm_s | confirm_q | confirm_reads | "
              "repeat_preview_s | repeat_q | repeat_reads |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        report.append(
            f"| {r['history']} | {r['preview_s']} | "
            f"{r['preview_queries']} | {r['preview_reads']} | "
            f"{r['confirm_s']} | {r['confirm_queries']} | "
            f"{r['confirm_reads']} | {r['repeat_preview_s']} | "
            f"{r['repeat_queries']} | {r['repeat_reads']} |")
    text = "\n".join(report) + "\n"
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
    print(text)


if __name__ == "__main__":
    main()
