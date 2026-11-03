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
        args = kre_adapter.build_args(
            executable=runtime["executable_path"],
            project_path=str(project_dir),
            suite_path=task["snapshot"]["entries"][0]["case_ref"]
            if task["snapshot"].get("entries") else "",
            report_dir=str(report_dir),
        )
        result = kre_adapter.run_invocation(
            args, log_dir,
            cancel_event=RemoteCancelEvent(client, ex_id))
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
        for item in build_evidence_manifest(report_dir):
            path = report_dir / item["rel_path"]
            try:
                client.post(
                    f"/api/agent/artifact/{ex_id}",
                    files={"file": (Path(item["rel_path"]).name,
                                    path.read_bytes())},
                    data={"rel_path": item["rel_path"], "kind": item["kind"],
                          "mime": item["mime"]},
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
