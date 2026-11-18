"""Buildbot orchestration adapter (docs/orchestration-replacement.md).

Versioned client for the Buildbot master's REST API, verified against
buildbot/buildbot-worker/buildbot-www 4.3.0. The API is bound to
127.0.0.1 on the controller host — it is never proxied by the frontend
and never reachable from execution targets.

Auth: UserPasswordAuth issues a TWISTED_SESSION cookie via a basic-auth
GET on /auth/login; the cookie is reused until it expires or the master
restarts (the session secret is in-memory), at which point the adapter
re-authenticates once and retries.

Correlation: submissions carry a ``to_execution_id`` build property and
return a buildrequest id (brid). The portal persists the brid before any
further side effects; a lost response is reconciled by querying
buildrequests by property — never blindly resubmitted.

Cancellation: one POST ``cancel`` on the buildrequest covers BOTH the
queued case (brid completes results=6/CANCELLED) and the running case
(the claimed build is stopped and the worker kills the remote command's
process tree) — verified on 4.3.0.
"""

import itertools
import json as _json

import requests
from flask import current_app

from .. import settings as settings_mod
from ..secrets_store import read_secret

# Buildbot result codes (buildbot.process.results)
RESULT_SUCCESS = 0
RESULT_WARNINGS = 1
RESULT_FAILURE = 2
RESULT_SKIPPED = 3
RESULT_EXCEPTION = 4
RESULT_RETRY = 5
RESULT_CANCELLED = 6

EXECUTION_PROP = "to_execution_id"


class NotConfigured(RuntimeError):
    """Actionable status when the Buildbot endpoint/credential is absent."""


class SubmissionLost(RuntimeError):
    """The force response could not be read — the buildrequest may or may
    not exist. The caller reconciles by execution-id property, never by
    blind resubmission."""


class BuildbotAdapter:
    """Versioned client for the localhost Buildbot REST API (v2)."""

    DEFAULT_TIMEOUT = 30
    _ids = itertools.count(1)

    def __init__(self, endpoint: str, username: str = "",
                 password: str = "", timeout: int = None):
        self.endpoint = endpoint.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout or self.DEFAULT_TIMEOUT
        self.session = requests.Session()

    @classmethod
    def from_settings(cls) -> "BuildbotAdapter":
        endpoint = settings_mod.get("buildbot_endpoint") or ""
        if not endpoint:
            raise NotConfigured("buildbot_endpoint is not configured")
        username = settings_mod.get("buildbot_username") or "to-api"
        secret = ""
        ref = settings_mod.get("buildbot_credential_ref") or ""
        if ref:
            secret = read_secret(current_app._get_current_object(), ref)
        return cls(endpoint, username=username, password=secret)

    # ---------- transport ----------

    def _api(self, path: str) -> str:
        return f"{self.endpoint}/api/v2{path}"

    def _authenticate(self) -> None:
        resp = self.session.get(
            f"{self.endpoint}/auth/login",
            auth=(self.username, self.password),
            timeout=self.timeout, allow_redirects=False)
        if resp.status_code not in (200, 302):
            raise NotConfigured(
                f"buildbot API auth failed (HTTP {resp.status_code})")
        self._authed = True

    def _request(self, method: str, path: str, _retry=True,
                 **kwargs) -> requests.Response:
        if not getattr(self, "_authed", False):
            self._authenticate()
        resp = self.session.request(
            method, self._api(path), timeout=self.timeout, **kwargs)
        # Expired session (restart wipes in-memory sessions): re-auth once.
        if resp.status_code in (401, 403) and _retry:
            self._authed = False
            self._authenticate()
            resp = self.session.request(
                method, self._api(path), timeout=self.timeout, **kwargs)
        resp.raise_for_status()
        return resp

    def _get(self, path: str, **params) -> dict:
        return self._request("GET", path, params=params).json()

    def _control(self, path: str, method: str, params: dict = None):
        """JSON-RPC control action on an item endpoint (POST)."""
        data = self._request("POST", path, json={
            "jsonrpc": "2.0", "method": method,
            "id": next(self._ids), "params": params or {},
        }).json()
        if "error" in data:
            raise RuntimeError(
                f"buildbot control {method} on {path}: "
                f"{data['error'].get('message', data['error'])}")
        return data.get("result")

    # ---------- contract ----------

    def health(self) -> dict:
        """Reachability + expected-builder presence, for the targets
        page (parallel to DaguAdapter.health)."""
        builders = self._get("/builders").get("builders") or []
        return {
            "reachable": True,
            "builders": sorted(b.get("name", "") for b in builders),
        }

    def workers(self) -> list:
        """Registered workers: ``connected_to`` distinguishes a
        provisioned-but-offline worker from an online one."""
        return self._get("/workers").get("workers") or []

    def worker_connected(self, name: str) -> bool:
        for w in self.workers():
            if w.get("name") == name:
                return bool(w.get("connected_to"))
        return False

    def submit(self, builder_name: str, execution_id: str,
               properties: dict = None) -> str:
        """Force a buildrequest on exactly one target builder; returns
        the brid the caller persists immediately. ``builderNames`` is
        name-keyed so it survives builderid renumbering on reconfig."""
        params = {"builderNames": [builder_name],
                  EXECUTION_PROP: execution_id,
                  "reason": f"execution {execution_id}",
                  "username": self.username}
        params.update(properties or {})
        result = self._control("/forceschedulers/to-force", "force", params)
        # result: [buildsetid, {builderid: brid}] — exactly one entry
        brids = (result[1] if isinstance(result, (list, tuple))
                 and len(result) > 1 else {})
        if len(brids) != 1:
            raise RuntimeError(
                f"expected one buildrequest, got {list(brids)}")
        return str(next(iter(brids.values())))

    def cancel(self, dispatch_id: str) -> bool:
        """Cancel the buildrequest — covers queued (results CANCELLED)
        AND running (claimed build stopped, remote process tree killed)
        in one call (verified 4.3.0)."""
        try:
            self._control(f"/buildrequests/{dispatch_id}", "cancel",
                          {"reason": "portal cancel request"})
            return True
        except Exception:
            return False

    def buildrequest(self, dispatch_id: str) -> dict:
        """Buildrequest record: claimed / complete / results."""
        reqs = self._get(f"/buildrequests/{dispatch_id}").get(
            "buildrequests") or []
        return reqs[0] if reqs else {}

    def builds_for_request(self, dispatch_id: str) -> list:
        return self._get(f"/buildrequests/{dispatch_id}/builds").get(
            "builds") or []

    def find_buildrequests(self, execution_id: str) -> list:
        """Lost-submission reconcile: buildrequests carrying this
        execution id. The property filter selects on presence; the
        value is matched here — the count is always tiny."""
        out = []
        for br in self._get(
                "/buildrequests",
                property=EXECUTION_PROP).get("buildrequests") or []:
            prop = (br.get("properties") or {}).get(EXECUTION_PROP)
            if prop and prop[0] == execution_id:
                out.append(br)
        return out

    def steps(self, buildid) -> list:
        return self._get(f"/builds/{buildid}/steps").get("steps") or []

    def step_logs(self, stepid) -> list:
        return self._get(f"/steps/{stepid}/logs").get("logs") or []

    def log_contents(self, logid) -> str:
        """Raw step log text — lines arrive prefixed h(eader)/o(utput)/
        e(rror); the stream prefix is stripped on return."""
        chunks = self._get(f"/logs/{logid}/contents").get(
            "logchunks") or []
        lines = []
        for chunk in chunks:
            for line in (chunk.get("content") or "").splitlines():
                lines.append(line[2:] if len(line) > 1
                             and line[0] in "hoe" and line[1] == " "
                             else line)
        return "\n".join(lines)

    def run_log(self, buildid, step_names=("run", "kre", "shell")) -> str:
        """Best-effort stdout/stderr for the execution step."""
        for step in self.steps(buildid):
            if step.get("name") in step_names or any(
                    step.get("name", "").startswith(p) for p in
                    ("shell", "run_kre", "kre")):
                for lg in self.step_logs(step["stepid"]):
                    if lg.get("name") == "stdio":
                        return self.log_contents(lg["logid"])
        return ""

    # ---------- master config ----------

    def reconfig_pidfile(self, pidfile) -> bool:
        """Signal the master to reload master.cfg (SIGHUP via its pid
        file). The installer/provisioning writes the pid path; the UI
        calls this after target changes so saved settings take effect
        with no manual controller command."""
        import os
        import signal
        try:
            pid = int(open(pidfile).read().strip())
            os.kill(pid, signal.SIGHUP)
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def builder_name(target_id: str) -> str:
        return f"to-target-{target_id}"

    @staticmethod
    def dispatcher_builder_name(target_id: str) -> str:
        return f"to-ssh-{target_id}"

    @staticmethod
    def encode_props(spec: dict) -> str:
        """Per-run spec travels as one JSON property — declared on the
        force scheduler, never carrying secrets (properties are
        readable via the API)."""
        return _json.dumps(spec, separators=(",", ":"))
