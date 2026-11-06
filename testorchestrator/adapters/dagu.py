"""Dagu orchestration adapter (§7).

Pin policy: implemented against the Dagu HTTP REST API as a versioned
client. The exact deployed Dagu version/schema must be verified against the
operator's instance before production use — status in
docs/integration-contracts.md is ``implemented, unverified`` (A14: not
claimed proven).

Dagu owns workflow orchestration; the portal owns immutable execution
definitions and authorization. Submissions are correlated to portal
execution IDs, and submission identity is persisted before side effects so
an ambiguous timeout can be reconciled instead of re-dispatched.
"""

import requests

from .. import settings as settings_mod
from ..secrets_store import read_secret
from flask import current_app


class NotConfigured(RuntimeError):
    """Actionable status when the Dagu endpoint/credential is absent."""


class DaguAdapter:
    """Versioned HTTP client for Dagu's REST API."""

    DEFAULT_TIMEOUT = 30

    def __init__(self, endpoint: str, token: str = "", username: str = "",
                 password: str = "", timeout: int = None):
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.username = username
        self.password = password
        self.timeout = timeout or self.DEFAULT_TIMEOUT

    @classmethod
    def from_settings(cls) -> "DaguAdapter":
        endpoint = settings_mod.get("dagu_endpoint") or ""
        if not endpoint:
            raise NotConfigured("dagu_endpoint is not configured")
        username = settings_mod.get("dagu_username") or ""
        secret = ""
        ref = settings_mod.get("dagu_credential_ref") or ""
        if ref:
            secret = read_secret(current_app._get_current_object(), ref)
        if username:
            # auth.mode: basic — verified against bundled Dagu 2.25.8.
            return cls(endpoint, username=username, password=secret)
        # auth.mode: builtin — Bearer API key/JWT issued by Dagu.
        return cls(endpoint, token=secret)

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if not self.username and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _auth(self):
        return (self.username, self.password) if self.username else None

    def _url(self, path: str) -> str:
        return f"{self.endpoint}/api/v1{path}"

    def health(self) -> dict:
        """Health probe — verifies reachability, authentication, version,
        AND that the orchestration workflow is installed (verified paths
        on bundled Dagu 2.25.8)."""
        resp = requests.get(
            self._url("/health"), headers=self._headers(),
            auth=self._auth(), timeout=self.timeout,
        )
        resp.raise_for_status()
        info = resp.json() if resp.content else {}
        dags = requests.get(
            self._url("/dags"), headers=self._headers(),
            auth=self._auth(), timeout=self.timeout,
        )
        dags.raise_for_status()
        names = [
            (d.get("dag") or {}).get("name") or d.get("fileName", "")
            for d in (dags.json().get("dags") or [])
        ]
        installed = "testorchestrator-execution" in names
        return {
            "reachable": True,
            "status": info.get("status", ""),
            "version": str(info.get("version", ""))[:64],
            "workflow_installed": installed,
        }

    def submit(self, payload: dict) -> str:
        """Submit a workflow run; returns the external run identity which the
        caller persists BEFORE doing anything else (§7 correlation)."""
        resp = requests.post(
            self._url("/dags/testorchestrator-execution/start"),
            json={"params": "execution_id=" + str(payload.get("execution_id",""))},
            headers=self._headers(), auth=self._auth(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        return str(data.get("dagRunId") or data.get("requestId") or "submitted")

    def status(self, dispatch_id: str) -> dict:
        resp = requests.get(
            self._url(f"/dags/testorchestrator-execution/dag-runs/{dispatch_id}"),
            headers=self._headers(), auth=self._auth(), timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def logs(self, dispatch_id: str) -> str:
        resp = requests.get(
            self._url(
                f"/dags/testorchestrator-execution/dag-runs/{dispatch_id}/log"
            ),
            headers=self._headers(), auth=self._auth(), timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.text

    def cancel(self, dispatch_id: str) -> bool:
        """Verified mechanism: `dagu stop <dag> --run-id <id>` via the
        bundled binary (HTTP cancel path unresolved on 2.25.8)."""
        import os
        import shutil
        import subprocess
        exe = (shutil.which("dagu")
               or os.environ.get("DAGU_BIN")
               or "3rdparty/dagu/dagu")
        try:
            r = subprocess.run(
                [exe, "stop", "testorchestrator-execution",
                 "--run-id", dispatch_id],
                capture_output=True, timeout=30)
            return r.returncode == 0
        except Exception:
            return False


    def remote_kre_spec(self, *, worker_label: str,
                        project_files: list, executable: str,
                        args: list, remote_workspace: str,
                        report_dir: str, timeout_s: int = 3600) -> dict:
        """A remote-KRE execution DAG — dispatched to the worker whose
        labels include the installation's unique tag (precise routing,
        never a broad label that could reach a different host). The
        label matches the worker's ``--worker.labels kre=<tag>``.

        Steps: create an isolated remote workspace, run the registered
        KRE executable in it, and return the Reports/ dir listing for
        portal-side _collect/_normalize. Cancellation/timeout is the
        DAG-run deadline — process-tree termination is the worker's job
        (verified in 2.25.8 docs, not yet exercised).

        Schema verified via `dagu validate` on bundled 2.25.8: there is
        NO DAG-level file-dependency field — `depends` is step-level
        only. ``project_files`` therefore stays a caller contract for
        the pending wire-up: the worker must reach the project through
        its own mechanism (checkout/shared mount), decided when remote
        dispatch is implemented."""
        return {
            "worker_selector": {"kre": worker_label},
            "timeout_sec": timeout_s,
            "steps": [
                {"id": "workspace",
                 "run": "mkdir -p " + remote_workspace},
                {"id": "run",
                 "action": "exec",
                 "depends": ["workspace"],
                 "working_dir": remote_workspace,
                 "with": {
                     "command": executable,
                     "args": args}},
                {"id": "collect",
                 "action": "file.list",
                 "depends": ["run"],
                 "with": {"path": report_dir}},
            ]}
