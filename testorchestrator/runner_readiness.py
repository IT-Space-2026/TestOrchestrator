"""Target-side runner readiness checks (§2.6.0-3) for Node browser
runtimes (Playwright Test / Cypress).

'Check runner readiness' submits a tiny probe build on the target's
OWN builder — same account, same workspace path, same transport as a
real run. The staged source is only probe scripts; the probe never
installs software (npx --no-install) and never runs the test suite.

Outcome surfaces through the RuntimeConnection's last_diagnostic /
last_check_at fields: pending submissions are recorded there as
{"_probe": {...}} and reconciled on the next poll — durable across
restarts without a separate store. Credentials never appear in the
probe spec, log, or report.
"""
import json
import time
import uuid
from pathlib import Path

from .adapters import nodefw, pytestfw, robotfw
from .adapters.buildbot import BuildbotAdapter
from . import buildbot_cfg
from .extensions import db


def _rid(runtime) -> str:
    return f"rdy-{runtime.id}-{uuid.uuid4().hex[:8]}"


def pending(runtime) -> dict:
    try:
        d = json.loads(runtime.last_diagnostic or "{}")
        return d.get("_probe") or {}
    except (ValueError, TypeError):
        return {}


def submit(runtime, app_url: str = "",
           required_libs: str = "") -> tuple:
    """Stage + submit the probe. Returns (ok, message)."""
    kind = runtime.runtime_kind or "kre"
    pyfw = kind in (robotfw.KIND, pytestfw.KIND)
    kind = kind if pyfw or kind in nodefw.KINDS else "playwright"
    runner = runtime.runner
    rid = _rid(runtime)
    if kind == pytestfw.KIND:
        scripts = pytestfw.probe_scripts(app_url.strip())
    elif kind == robotfw.KIND:
        scripts = robotfw.probe_scripts(app_url.strip())
    else:
        scripts = nodefw.probe_scripts(kind, app_url.strip())
    buildbot_cfg.stage_probe_source(rid, scripts)
    probe_env = {}
    if pyfw:
        probe_env["PYBIN"] = (runtime.executable_path or "").strip()
        probe_env["REQUIRED_LIBS"] = (required_libs or "").strip()
    adapter = BuildbotAdapter.from_settings()
    try:
        if runner.target_type == "ssh":
            buildbot_cfg.materialize_ssh_key(runner)

            class _Ex:  # write_ssh_run_spec only reads .id/.snapshot
                id = rid

                @staticmethod
                def snapshot():
                    return {}

            spec_file = buildbot_cfg.write_ssh_run_spec(
                _Ex(), runner, runtime, ["probe.sh"], executable="sh",
                source_tar=str(buildbot_cfg.staging_dir()
                               / f"{rid}.tar"),
                execution_id=rid,
                extra=dict(
                    {"exec_subdir": "src",
                     "report_rel": "proj/src/Reports",
                     **({"env": probe_env} if probe_env else {})},
                    **({"path_prefix":
                        str(Path(runtime.executable_path).parent)}
                       if runtime.executable_path
                       and Path(runtime.executable_path).is_absolute()
                       else {})))
            import sys as _sys
            spec = buildbot_cfg.ssh_exec_spec(
                None, python=_sys.executable,
                dispatcher_script=str(
                    Path(__file__).resolve().parent.parent / "scripts"
                    / "to_ssh_exec.py"),
                spec_file=str(spec_file))
        else:
            argv = (robotfw.probe_argv(runner.os_arch) if pyfw
                    else nodefw.probe_argv(runner.os_arch))
            spec = {"executable": argv[0], "args": argv[1:],
                    "exec_workdir": "build/src",
                    "report_src": "src/Reports"}
            if probe_env:
                spec["env"] = probe_env
        builder = buildbot_cfg.builder_for(runner)
        brid = adapter.submit(
            builder, rid,
            {"exec_spec": BuildbotAdapter.encode_props(spec)})
    except Exception as exc:
        return False, f"readiness submission failed: {exc}"
    runtime.last_diagnostic = json.dumps(
        {"_probe": {"rid": rid, "brid": int(brid), "kind": kind,
                    "transport": runner.target_type or "",
                    "at": time.time()}})
    from .execution import utcnow
    runtime.last_check_at = utcnow()
    db.session.commit()
    return True, rid


def _summarize(probe: dict, kind: str) -> str:
    problems = []
    if probe.get("node", "missing") == "missing":
        problems.append("Node.js not on PATH for this account")
    if probe.get("npx", "missing") == "missing":
        problems.append("npx not on PATH for this account")
    if probe.get(kind, "missing").startswith("missing"):
        problems.append(f"{kind} not resolvable — install it as a "
                        "project dependency (npx) on the target")
    if kind == "cypress" and probe.get("cypress_binary", "ok") != "ok":
        problems.append("Cypress binary not available to the worker "
                        "account")
    if probe.get("workspace_write") not in (None, "ok"):
        problems.append("workspace not writable for the run account")
    if probe.get("app_url", "").startswith("UNREACHABLE"):
        problems.append("target/browser cannot reach the application "
                        "URL")
    if kind in (robotfw.KIND, pytestfw.KIND):
        problems = []
        if probe.get("python", "missing").startswith("missing"):
            problems.append("configured Python interpreter not "
                            "runnable by this account")
        pkg = {"robot": "robotframework",
               "pytest": "pytest"}[kind]
        if probe.get(kind, "missing").startswith("missing"):
            problems.append(f"{kind} not importable — install "
                            f"{pkg} in the configured "
                            "interpreter")
        for k, v in probe.items():
            if k.startswith("lib_") and v == "MISSING":
                problems.append(f"required library {k[4:]} not "
                                "importable")
        if probe.get("workspace_write") not in (None, "ok"):
            problems.append("workspace not writable for the run "
                            "account")
        if probe.get("app_url", "").startswith("UNREACHABLE"):
            problems.append("target cannot reach the application "
                            "URL")
        versions = ", ".join(
            f"{k}={v}" for k, v in probe.items()
            if k in ("python", kind)
            and not v.startswith("missing"))
    else:
        versions = ", ".join(
            f"{k}={v}" for k, v in probe.items()
            if k in ("node", "npx", kind)
            and not v.startswith("missing"))
    verdict = "ready" if not problems else "not ready"
    parts = [f"{kind} readiness: {verdict}"]
    if versions:
        parts.append(versions)
    if problems:
        parts.append("; ".join(problems))
    return " — ".join(parts)


def poll(runtime) -> bool:
    """Reconcile a pending probe. Returns True when a result landed."""
    pend = pending(runtime)
    if not pend:
        return False
    adapter = BuildbotAdapter.from_settings()
    try:
        br = adapter.buildrequest(str(pend["brid"]))
    except Exception:
        return False
    if not br.get("complete"):
        return False
    # Collect markers: step log over the API + the uploaded probe.txt.
    text = ""
    try:
        builds = adapter.builds_for_request(str(pend["brid"]))
        if builds:
            text += adapter.run_log(builds[0]["buildid"]) or ""
    except Exception:
        pass
    # DirectoryUpload lands Reports/ contents at the artifact root —
    # same convention as run collection (probe.txt is the marker log).
    pulled = False
    for probe_txt in (
            buildbot_cfg.artifacts_dir() / pend["rid"] / "probe.txt",
            buildbot_cfg.artifacts_dir() / pend["rid"]
            / "Reports" / "probe.txt",
            buildbot_cfg.artifacts_dir() / pend["rid"]
            / "src" / "Reports" / "probe.txt",
            # agentless SSH collection preserves the remote tree
            buildbot_cfg.artifacts_dir() / pend["rid"]
            / "proj" / "src" / "Reports" / "probe.txt"):
        try:
            if probe_txt.is_file():
                text += "\n" + probe_txt.read_text(errors="replace")
                pulled = True
                break
        except OSError:
            pass
    probe = nodefw.parse_probe(text)
    # For SSH targets the probe file itself arrived over SFTP —
    # retrieving it is the SFTP verification.
    if pulled and pend.get("transport") == "ssh":
        probe["sftp"] = "ok (artifact retrieved over SFTP)"
    if not probe.get("done"):
        runtime.last_diagnostic = (
            f"{pend['kind']} readiness: probe finished but produced "
            "no markers — inspect the build log")
    else:
        runtime.last_diagnostic = _summarize(probe, pend["kind"])
        runtime.probe_json = json.dumps(
            {k: v for k, v in probe.items()})
    from .execution import utcnow
    runtime.last_check_at = utcnow()
    db.session.commit()
    buildbot_cfg.clear_staging(pend["rid"])
    return True


def poll_all_pending() -> int:
    """Called on infrastructure/run-form renders — cheap no-op when
    nothing is pending."""
    from .execution import RuntimeConnection
    done = 0
    for rt in RuntimeConnection.query.all():
        if pending(rt) and poll(rt):
            done += 1
    return done
