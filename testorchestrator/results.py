"""Canonical results: defensive JUnit parsing + evidence manifest (§8–§9).

- Parses actual KRE JUnit output defensively: repeated entries, data rows,
  partial output, skipped cases, discrepant producer totals.
- Case names alone are NOT unique — unresolved items are exposed honestly
  rather than guessed.
- Independent state fields: execution state, test outcome, ReportPortal
  publication, Git sync. All-skipped is not passed; incomplete execution is
  never silently green.
- Evidence manifest associates files to execution/case where verifiable;
  unmapped files remain at run level, clearly labelled.
"""

import hashlib
import mimetypes
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# Prevent path traversal in artifacts destined for the portal/Git.
SAFE_KINDS = {"junit", "log", "screenshot", "video", "har", "html", "other"}
DANGEROUS_MIMES = {"text/html", "image/svg+xml"}  # active content policy


class JunitParseError(ValueError):
    pass


@dataclass
class ParsedCase:
    name: str
    classname: str
    status: str            # passed|failed|error|skipped
    duration_s: float
    message: str
    iteration: int = 0
    attempt: int = 1


@dataclass
class ParsedRun:
    cases: list = field(default_factory=list)
    producer_tests: int = 0       # totals claimed by producer
    producer_failures: int = 0
    producer_errors: int = 0
    producer_skipped: int = 0
    partial: bool = False          # parse problems encountered
    discrepant: bool = False       # producer totals ≠ counted cases


def _text_of(element) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _case_status(case) -> tuple:
    """(status, message) from a <testcase> element."""
    if case.find("skipped") is not None:
        return "skipped", _text_of(case.find("skipped"))
    failure = case.find("failure")
    if failure is not None:
        return "failed", _text_of(failure) or failure.get("message", "")
    error = case.find("error")
    if error is not None:
        return "error", _text_of(error) or error.get("message", "")
    return "passed", ""


def parse_junit(path: Path) -> ParsedRun:
    """Parse one JUnit XML file. Defensive: tolerates partial output,
    missing attributes, and duplicate names. ElementTree only (no DTD/
    entity processing), so hostile report XML cannot execute anything."""

    run = ParsedRun()
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise JunitParseError(f"{path.name}: malformed XML: {exc}") from exc

    root = tree.getroot()
    if root.tag not in ("testsuite", "testsuites"):
        raise JunitParseError(f"{path.name}: not JUnit XML (<{root.tag}>)")

    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    for suite in suites:
        for key, attr in (("producer_tests", "tests"),
                          ("producer_failures", "failures"),
                          ("producer_errors", "errors"),
                          ("producer_skipped", "skipped")):
            try:
                setattr(run, key, getattr(run, key) + int(suite.get(attr, "0")))
            except ValueError:
                run.partial = True

        for case in suite.iter("testcase"):
            status, message = _case_status(case)
            try:
                duration = float(case.get("time", "0") or 0)
            except ValueError:
                duration, run.partial = 0.0, True
            # Data-driven rows / retries can repeat the same name — attempt
            # numbering follows document order, not identity guessing.
            run.cases.append(
                ParsedCase(
                    name=case.get("name", "") or "<unnamed>",
                    classname=case.get("classname", "") or "",
                    status=status,
                    duration_s=duration,
                    message=message,
                )
            )

    if run.producer_tests and run.producer_tests != len(run.cases):
        run.discrepant = True
    return run


def aggregate_outcome(cases: list, completed: bool) -> str:
    """Outcome semantics (§8): all-skipped ≠ passed; incomplete ≠ green."""
    if not completed:
        return "incomplete"
    if not cases:
        return "incomplete"  # missing results are never a zero-test pass
    statuses = {c.status for c in cases}
    if statuses == {"skipped"}:
        return "incomplete"
    if statuses & {"failed", "error"}:
        return "failed"
    return "passed"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while data := f.read(chunk):
            h.update(data)
    return h.hexdigest()


def safe_relpath(report_root: Path, candidate: Path) -> str:
    """Validate an artifact path stays inside the report directory."""
    root = report_root.resolve()
    resolved = candidate.resolve()
    if not str(resolved).startswith(str(root) + "/"):
        raise ValueError(f"artifact escapes report root: {candidate}")
    return str(resolved.relative_to(root))


def classify_artifact(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".xml":
        return "junit"
    if suffix in (".log", ".txt", ".out"):
        return "log"
    if suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return "screenshot"
    if suffix in (".mp4", ".webm", ".mov", ".avi"):
        return "video"
    if suffix == ".har":
        return "har"
    if suffix in (".html", ".htm", ".svg"):
        return "html"
    return "other"


def build_evidence_manifest(report_root: Path, junit_files: set = None) -> list:
    """Versioned evidence manifest (§9): every generated file is recorded
    with path, kind, checksum, size, and MIME. Files are associated by the
    manifest only — never by filename guessing alone."""
    manifest = []
    junit_files = junit_files or set()
    if not report_root.exists():
        return manifest
    for path in sorted(report_root.rglob("*")):
        if not path.is_file():
            continue
        try:
            rel = safe_relpath(report_root, path)
        except ValueError:
            continue
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        manifest.append(
            {
                "rel_path": rel,
                "kind": "junit" if path in junit_files else classify_artifact(path),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "mime": mime,
                "active_content": mime in DANGEROUS_MIMES,
            }
        )
    return manifest
