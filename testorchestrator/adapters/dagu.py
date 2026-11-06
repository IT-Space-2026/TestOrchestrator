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

    # ---------- remote-worker dispatch (verified vs bundled 2.25.8) ----------

    def workers(self) -> list:
        """Connected workers with their labels (coordinator service).
        Raises requests.HTTPError 503 when the coordinator is not
        configured — the caller treats that as remote-incomplete."""
        resp = requests.get(
            self._url("/workers"), headers=self._headers(),
            auth=self._auth(), timeout=self.timeout,
        )
        resp.raise_for_status()
        return (resp.json() or {}).get("workers") or []

    def enqueue(self, *, spec_yaml: str, name: str, dag_run_id: str,
                worker_selector: dict, params: str = "",
                singleton: bool = True) -> str:
        """Ad-hoc DAG submission: the spec travels with the run — no
        pre-registered workflow needed on the coordinator. ``dag_run_id``
        pins the run identity to our execution id (idempotent:
        re-submitting the same id cannot duplicate work), ``singleton``
        rejects a second concurrent run for the same DAG name (409),
        and ``worker_selector`` routes to exactly the labeled worker."""
        resp = requests.post(
            self._url("/dag-runs/enqueue"),
            json={
                "spec": spec_yaml,
                "name": name,
                "dagRunId": dag_run_id,
                "workerSelector": dict(worker_selector),
                "params": params,
                "singleton": singleton,
            },
            headers=self._headers(), auth=self._auth(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        return str(data.get("dagRunId") or dag_run_id)

    def dag_run(self, name: str, dag_run_id: str) -> dict:
        """Run details for an enqueue-submitted run (these live under
        /dag-runs/{name}/{id}, not /dags/{fileName})."""
        resp = requests.get(
            self._url(f"/dag-runs/{name}/{dag_run_id}"),
            headers=self._headers(), auth=self._auth(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json() or {}

    def list_artifacts(self, name: str, dag_run_id: str) -> list:
        """Recursive artifact tree for a run — workers upload run
        artifacts to the coordinator (verified 2.25.8)."""
        resp = requests.get(
            self._url(f"/dag-runs/{name}/{dag_run_id}/artifacts"),
            params={"recursive": "true"},
            headers=self._headers(), auth=self._auth(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return (resp.json() or {}).get("items") or []

    def download_artifact(self, name: str, dag_run_id: str,
                          path: str) -> bytes:
        resp = requests.get(
            self._url(f"/dag-runs/{name}/{dag_run_id}/artifacts/download"),
            params={"path": path},
            headers=self._headers(), auth=self._auth(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.content

    def stop(self, name: str, dag_run_id: str) -> bool:
        """HTTP stop for a submitted run (verified on 2.25.8 —
        POST /dag-runs/{name}/{id}/stop). The coordinator signals the
        worker; process-tree termination is the worker's job."""
        try:
            resp = requests.post(
                self._url(f"/dag-runs/{name}/{dag_run_id}/stop"),
                headers=self._headers(), auth=self._auth(),
                timeout=self.timeout,
            )
            return resp.ok
        except requests.RequestException:
            return False

    def remote_worker_names(self, label_key: str, label_value: str) -> list:
        """Names/ids of workers whose labels include key=value — used for
        unambiguous matching before a remote dispatch."""
        matches = []
        for w in self.workers():
            labels = w.get("labels") or {}
            if isinstance(labels, list):
                labels = dict(
                    (l.split("=", 1) if "=" in l else (l, ""))
                    for l in labels)
            if labels.get(label_key) == label_value:
                matches.append(str(w.get("id") or w.get("name") or w))
        return matches

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

    def run_log(self, name: str, dag_run_id: str) -> str:
        resp = requests.get(
            self._url(f"/dag-runs/{name}/{dag_run_id}/log"),
            headers=self._headers(), auth=self._auth(),
            timeout=self.timeout,
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


    # Name of the ad-hoc remote execution DAG — the run lives under
    # /dag-runs/{name}/{id} since it is enqueued by spec, not from a file.
    REMOTE_DAG_NAME = "to-remote-execution"

    def remote_kre_spec(self, *, worker_label: str, repository: str,
                        ref: str, git_auth: dict, remote_workspace: str,
                        executable: str, args: list,
                        timeout_s: int = 3600) -> dict:
        """A remote-KRE execution DAG — dispatched to the worker whose
        labels include the installation's unique tag (precise routing,
        never a broad label that could reach a different host). The
        label matches the worker's ``--worker.labels kre=<tag>``.

        Mechanism verified end-to-end against bundled 2.25.8 (local run,
        fixture repo + fixture KRE, artifacts listed/downloaded via the
        REST surface):

        - ``git.checkout`` pulls ``repository`` at ``ref`` into the
          per-execution workspace on the WORKER — source delivery needs
          no portal-side file push and no custom agent. ``git_auth``
          takes the action's own fields (username/password/token or
          worker-side ssh_key_path/ssh_passphrase; an ssh key path never
          leaves the worker host, https credentials are embedded in the
          submitted spec — keep them off shared Dagu installs).
        - ``exec`` runs the registered executable with portal-built KRE
          argv; ``-reportFolder`` points into the run's artifact dir so
          reports are uploaded to the coordinator even when the step
          fails (verified: failed step → artifacts still downloadable).
        - ``handler_on.exit`` removes the per-execution workspace —
          artifacts survive on the coordinator, the checkout does not.
        - ``timeout_sec`` bounds the whole run; cancellation/stop is the
          coordinator signaling the worker's process tree."""
        steps = [
            {"id": "prepare",
             "run": "mkdir -p ${context.paths.artifacts_dir}/Reports"},
            {"id": "checkout",
             "action": "git.checkout",
             "depends": ["prepare"],
             "with": {
                 "repository": repository,
                 "path": remote_workspace + "/proj",
                 "ref": ref,
                 **(git_auth or {})}},
            {"id": "run_kre",
             "action": "exec",
             "depends": ["checkout"],
             "working_dir": remote_workspace + "/proj",
             "with": {
                 "command": executable,
                 "args": [
                     *args,
                     "-reportFolder=${context.paths.artifacts_dir}"
                                  "/Reports"]}},
        ]
        return {
            "worker_selector": {"kre": worker_label},
            "timeout_sec": timeout_s,
            "handler_on": {
                "exit": {"run": "rm -rf " + remote_workspace}},
            "steps": steps,
        }
