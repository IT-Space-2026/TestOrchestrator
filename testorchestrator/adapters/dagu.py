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

    def __init__(self, endpoint: str, token: str = "", timeout: int = None):
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.timeout = timeout or self.DEFAULT_TIMEOUT

    @classmethod
    def from_settings(cls) -> "DaguAdapter":
        endpoint = settings_mod.get("dagu_endpoint") or ""
        if not endpoint:
            raise NotConfigured("dagu_endpoint is not configured")
        token = ""
        ref = settings_mod.get("dagu_credential_ref") or ""
        if ref:
            token = read_secret(current_app._get_current_object(), ref)
        return cls(endpoint, token)

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _url(self, path: str) -> str:
        return f"{self.endpoint}/api/v1{path}"

    def health(self) -> dict:
        """Version/health probe — verifies reachability + schema era."""
        resp = requests.get(
            self._url("/version"), headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return {"reachable": True, "version": resp.text.strip()[:64]}

    def submit(self, payload: dict) -> str:
        """Submit a workflow run; returns the external run identity which the
        caller persists BEFORE doing anything else (§7 correlation)."""
        resp = requests.post(
            self._url("/dags/testorchestrator-execution/start"),
            json={"params": payload},
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        return str(data.get("dagRunId") or data.get("requestId") or "submitted")

    def status(self, dispatch_id: str) -> dict:
        resp = requests.get(
            self._url(f"/dags/testorchestrator-execution/dag-runs/{dispatch_id}"),
            headers=self._headers(), timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def logs(self, dispatch_id: str) -> str:
        resp = requests.get(
            self._url(
                f"/dags/testorchestrator-execution/dag-runs/{dispatch_id}/log"
            ),
            headers=self._headers(), timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.text

    def cancel(self, dispatch_id: str) -> bool:
        resp = requests.post(
            self._url(
                f"/dags/testorchestrator-execution/dag-runs/{dispatch_id}/stop"
            ),
            headers=self._headers(), timeout=self.timeout,
        )
        return resp.ok
