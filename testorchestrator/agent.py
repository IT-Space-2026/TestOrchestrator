"""Remote runner agent (§6) — runs on the execution machine.

Polls the portal for assigned executions, invokes the operator-installed
KRE via the local adapter path, heartbeats leases, reports results, and
uploads evidence. Never installs or updates KRE. On reconnect it
re-registers and reconciles claimed tasks rather than duplicating runs.

Usage: flask agent --portal https://host --token toa_... [--once]
"""

import threading
import time
from pathlib import Path

import requests

from .adapters import kre as kre_adapter
from .results import build_evidence_manifest, parse_junit

AGENT_VERSION = "0.1"


class RemoteCancelEvent:
    def __init__(self, client, execution_id):
        self.client = client
        self.execution_id = execution_id

    def is_set(self):
        try:
            resp = self.client.get(f"/api/agent/cancel/{self.execution_id}")
            return resp.ok and resp.json().get("cancel", False)
        except requests.RequestException:
            return False  # uncertainty: keep running, portal will reconcile


class AgentClient:
    def __init__(self, portal: str, token: str, verify_tls: bool = True):
        self.portal = portal.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.verify = verify_tls
        self.runner = None

    def get(self, path, **kw):
        return self.session.get(self.portal + path,
                                verify=self.verify, timeout=30, **kw)

    def post(self, path, **kw):
        return self.session.post(self.portal + path,
                                 verify=self.verify, timeout=30, **kw)

    def register(self):
        resp = self.post("/api/agent/register",
                         json={"agent_version": AGENT_VERSION})
        resp.raise_for_status()
        self.runner = resp.json()
        return self.runner


def _heartbeat_loop(client, execution_id, stop):
    while not stop.wait(30):
        try:
            client.post("/api/agent/heartbeat",
                        json={"executions": [execution_id]})
        except requests.RequestException:
            pass  # lease expiry reconciles on portal side


def run_task(client: AgentClient, task: dict, work_root: Path) -> None:
    """Execute one assigned execution locally, then report back."""
    ex_id = task["execution_id"]
    runtime = task["runtime"]
    attempt_dir = work_root / ex_id
    report_dir = attempt_dir / "Reports"
    log_dir = attempt_dir / "logs"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    stop = threading.Event()
    hb = threading.Thread(target=_heartbeat_loop,
                          args=(client, ex_id, stop), daemon=True)
    hb.start()
    payload = {"execution_id": ex_id, "results": [], "outcome": "incomplete",
               "canceled": False}
    try:
        source_dir = Path(runtime.get("workspace_root") or attempt_dir) / "src"
        source_dir.mkdir(parents=True, exist_ok=True)
        project_dir = kre_adapter.prepare_workspace(
            source_dir, attempt_dir, task.get("source_sha", ""))
        rc = (task.get("snapshot") or {}).get("run_config") or {}
        args = kre_adapter.build_args(
            executable=runtime["executable_path"],
            project_path=str(project_dir),
            suite_path=task["snapshot"]["entries"][0]["case_ref"]
            if task["snapshot"].get("entries") else "",
            report_dir=str(report_dir),
            profile=rc.get("profile", "default"),
            browser=rc.get("browser", "Chrome (headless)"),
        )
        creds = task.get("redact") or []
        if creds:
            args = args + [f"-apiKey={creds[0]}"]
        result = kre_adapter.run_invocation(
            args, log_dir,
            cancel_event=RemoteCancelEvent(client, ex_id),
            redact=creds)
        payload["exit_status"] = result.exit_status
        payload["canceled"] = result.canceled
        payload["error_kind"] = kre_adapter.classify_error(result)

        cases = []
        for xml in kre_adapter.discover_reports(report_dir):
            try:
                parsed = parse_junit(xml)
            except Exception:
                continue
            for c in parsed.cases:
                cases.append({
                    "case_name": c.name, "classname": c.classname,
                    "case_ref": c.classname or c.name,
                    "status": c.status, "duration_s": c.duration_s,
                    "message": c.message[:2000],
                    "unresolved": parsed.discrepant,
                })
        payload["results"] = cases
        if cases:
            statuses = {c["status"] for c in cases}
            payload["outcome"] = (
                "failed" if statuses & {"failed", "error"}
                else "incomplete" if statuses == {"skipped"}
                else "passed")
        # Evidence upload: authenticated artifact transfer (§9).
        uploads = [
            (report_dir / item["rel_path"], item["rel_path"], item["kind"],
             item["mime"])
            for item in build_evidence_manifest(report_dir)
        ]
        # Runner logs (recorded argv + captured stdout/stderr) are evidence.
        uploads += [
            (p, f"logs/{p.name}", "log", "text/plain")
            for p in sorted(log_dir.glob("kre-*.log"))
        ]
        for path, rel_path, kind, mime in uploads:
            try:
                client.post(
                    f"/api/agent/artifact/{ex_id}",
                    files={"file": (Path(rel_path).name,
                                    path.read_bytes())},
                    data={"rel_path": rel_path, "kind": kind, "mime": mime},
                )
            except requests.RequestException:
                pass  # retry on next poll cycle — evidence stays local
    except Exception as exc:
        payload["error_kind"] = "infrastructure"
        payload["error_text"] = f"{type(exc).__name__}: {exc}"[:1500]
    finally:
        stop.set()
        client.post("/api/agent/result", json=payload)


def poll_once(client: AgentClient, work_root: Path,
              execution_id: str = "") -> int:
    resp = client.get("/api/agent/tasks")
    resp.raise_for_status()
    tasks = resp.json().get("tasks", [])
    if execution_id:
        tasks = [t for t in tasks if t["execution_id"] == execution_id]
    for task in tasks:
        run_task(client, task, work_root)
    return len(tasks)


def run(portal: str, token: str, work_root: Path, once: bool = False,
        interval: float = 5.0, verify_tls: bool = True,
        execution_id: str = "") -> None:
    client = AgentClient(portal, token, verify_tls=verify_tls)
    client.register()
    while True:
        poll_once(client, work_root, execution_id)
        if once:
            return
        time.sleep(interval)


TERMINAL_STATES = {"completed", "failed", "canceled"}


def monitor(portal: str, token: str, execution_id: str,
            verify_tls: bool = True, interval: float = 10.0,
            timeout: float = 7200.0) -> int:
    """Dagu monitor step (Option B remote targeting): poll the portal-side
    execution state until a terminal outcome or explicit uncertainty —
    the dagu run must NOT finish after mere dispatch. Returns a process
    exit code: 0 terminal, 2 unknown/timeout (dagu run fails honestly)."""
    import sys
    client = AgentClient(portal, token, verify_tls=verify_tls)
    client.register()
    deadline = time.monotonic() + timeout
    while True:
        try:
            resp = client.get(f"/api/agent/state/{execution_id}")
            if resp.status_code == 404:
                print(f"monitor: execution {execution_id} not visible to "
                      "this runner credential", file=sys.stderr)
                return 2
            resp.raise_for_status()
            state = resp.json().get("state", "")
            print(f"monitor: execution {execution_id} state={state}",
                  flush=True)
            if state in TERMINAL_STATES:
                return 0
            if state == "unknown":
                return 2  # explicit uncertainty — never a silent success
        except requests.RequestException:
            pass  # portal unreachable: uncertainty, keep polling
        if time.monotonic() > deadline:
            print("monitor: timeout — execution state unresolved",
                  file=sys.stderr)
            return 2
        time.sleep(interval)


def load_agent_config(path: str):
    """Protected token delivery: agent reads portal URL + token from a
    0600 config file provisioned at install time — not from CLI args,
    workflow params, or environment dumps."""
    import tomllib
    cfg = tomllib.loads(Path(path).read_text())
    return cfg["portal"], cfg["token"], cfg.get("work_root")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/testorchestrator/agent.toml")
    ap.add_argument("--portal"); ap.add_argument("--token")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--monitor", action="store_true",
                    help="after polling once, keep watching --execution-id "
                         "until terminal/unknown (Dagu step mode)")
    ap.add_argument("--execution-id", default="")
    ap.add_argument("--work-root", default=None)
    a = ap.parse_args(argv)
    portal, token, work = a.portal, a.token, a.work_root
    if Path(a.config).exists():
        c_portal, c_token, c_work = load_agent_config(a.config)
        portal, token = portal or c_portal, token or c_token
        work = work or c_work
    if not portal or not token:
        ap.error("portal/token required (config file or explicit args)")
    if a.monitor:
        # The step first gives THIS host a chance to execute the task
        # (local mode), then monitors whatever runner owns it (remote).
        client = AgentClient(portal, token)
        try:
            poll_once(client, Path(work or "/var/lib/testorchestrator/agent"),
                      a.execution_id)
        except Exception:
            pass  # task may belong to a remote runner — monitor decides
        raise SystemExit(monitor(portal, token, a.execution_id))
    run(portal, token, Path(work or "/var/lib/testorchestrator/agent"),
        once=a.once, execution_id=a.execution_id)


if __name__ == "__main__":
    main()
