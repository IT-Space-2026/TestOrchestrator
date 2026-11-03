"""ReportPortal adapter (§9).

Direct-API publication strategy (option 1 in §9): launch -> items -> logs/
attachments -> finish, with persisted local-to-remote IDs and
per-artifact publication state. JUnit-import is NOT used — it cannot map
screenshots to case attempts.

Status: implemented against the ReportPortal v1 REST API shape,
``implemented, unverified`` — requires a live operator instance to prove
(docs/integration-contracts.md, A14). Local canonical results remain
authoritative during outages (A03).
"""

import requests
from flask import current_app

from .. import settings as settings_mod
from ..secrets_store import read_secret


class NotConfigured(RuntimeError):
    pass


class ReportPortalAdapter:
    DEFAULT_TIMEOUT = 30

    def __init__(self, endpoint: str, project: str, token: str,
                 timeout: int = None):
        self.endpoint = endpoint.rstrip("/")
        self.project = project
        self.token = token
        self.timeout = timeout or self.DEFAULT_TIMEOUT

    @classmethod
    def from_settings(cls) -> "ReportPortalAdapter":
        endpoint = settings_mod.get("reportportal_endpoint") or ""
        project = settings_mod.get("reportportal_project") or ""
        if not endpoint or not project:
            raise NotConfigured(
                "reportportal_endpoint/project is not configured"
            )
        ref = settings_mod.get("reportportal_credential_ref") or ""
        token = read_secret(current_app._get_current_object(), ref) if ref else ""
        return cls(endpoint, project, token)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.endpoint}/api/v1/{self.project}{path}"

    def status(self) -> dict:
        """Reachability + auth probe: GET the configured project resource.
        Unverified against a live instance — surfaces actionable status,
        never a fabricated success."""
        resp = requests.get(self._url(""), headers=self._headers(),
                            timeout=self.timeout)
        resp.raise_for_status()
        return {"reachable": True, "project": self.project}

    def launch(self, name: str, attributes: dict = None) -> str:
        resp = requests.post(
            self._url("/launch"),
            json={"name": name, "attributes": attributes or {},
                  "startTime": _now_ms()},
            headers=self._headers(), timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["id"]

    def start_item(self, launch_id: str, name: str, item_type: str,
                   parent_id: str = None) -> str:
        body = {
            "name": name, "type": item_type, "startTime": _now_ms(),
            "launchUuid": launch_id,
        }
        url = self._url("/item") + (f"/{parent_id}" if parent_id else "")
        resp = requests.post(url, json=body, headers=self._headers(),
                             timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()["id"]

    def log(self, launch_id: str, item_id: str, message: str,
            level: str = "INFO", attachment=None) -> str:
        """Attachment upload carries checksum + MIME (§9 artifact safety)."""
        files = None
        data = {
            "itemUuid": item_id, "launchUuid": launch_id,
            "time": _now_ms(), "message": message, "level": level,
        }
        if attachment is not None:
            files = {"file": (attachment["name"], attachment["data"],
                              attachment["mime"])}
            data["file"] = {"name": attachment["name"]}
        resp = requests.post(
            self._url("/log/entry"), data=data, files=files,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["id"]

    def finish_item(self, item_id: str, status: str) -> None:
        requests.put(
            self._url(f"/item/{item_id}"),
            json={"status": status, "endTime": _now_ms()},
            headers=self._headers(), timeout=self.timeout,
        ).raise_for_status()

    def finish_launch(self, launch_id: str) -> None:
        requests.put(
            self._url(f"/launch/{launch_id}/finish"),
            json={"endTime": _now_ms()},
            headers=self._headers(), timeout=self.timeout,
        ).raise_for_status()

    def publish_execution(self, execution) -> None:
        """Hierarchy + evidence publication with per-artifact state (§9).

        Retries reconcile against persisted remote IDs — a remote timeout
        may mean the request was accepted, so nothing is blindly recreated.
        """
        # Destination pinning (§5): a run that already published (or
        # partially published) to one ReportPortal must never be retried
        # against a different endpoint by a later settings change —
        # that would silently split the run across two instances.
        if execution.rp_endpoint_used and \
                execution.rp_endpoint_used != self.endpoint:
            raise NotConfigured(
                "ReportPortal endpoint changed since first publication "
                f"({execution.rp_endpoint_used} -> {self.endpoint}); "
                "retrying would split the run across instances. Resolve "
                "explicitly — no silent redirect.")
        # Resume-safe: an existing launch ID means a previous attempt may
        # have created hierarchy — skip item creation for results that
        # already carry remote IDs (§9; no duplicate trees, no reruns).
        launch_id = execution.rp_launch_id or self.launch(
            f"TO {execution.id}",
            {"suite": execution.snapshot().get("suite_name", "")},
        )
        if not execution.rp_launch_id:
            execution.rp_launch_id = launch_id
            execution.rp_endpoint_used = self.endpoint
            from ..extensions import db as _db
            _db.session.commit()
        artifacts_by_result = {}
        for artifact in execution.artifacts:
            artifacts_by_result.setdefault(
                artifact.result_id, []).append(artifact)

        from pathlib import Path
        from ..execution import SyncState
        for result in execution.results:
            already_published = bool(result.rp_remote_id)
            item_id = result.rp_remote_id or self.start_item(
                launch_id, result.case_name, "STEP")
            if not result.rp_remote_id:
                result.rp_remote_id = item_id
            status = {
                "passed": "PASSED", "failed": "FAILED",
                "error": "FAILED", "skipped": "SKIPPED",
            }[result.status.value]
            # On retry, skip the result's own log only if the item existed
            # before — a remote timeout may have accepted creation but lost
            # the response, so attachments keyed on artifact.rp_state are
            # deduplicated individually below.
            if result.message and not already_published:
                self.log(launch_id, item_id, result.message[:4000],
                         "ERROR" if status == "FAILED" else "INFO")
            # Attach evidence files mapped to this case attempt (§9);
            # per-artifact dedup: only items not yet confirmed remotely.
            for artifact in artifacts_by_result.get(result.id, []):
                if artifact.rp_state == SyncState.DONE:
                    continue
                path = Path(execution.report_dir) / artifact.rel_path
                if path.exists() and artifact.size <= 20 * 1024 * 1024:
                    self.log(
                        launch_id, item_id, artifact.rel_path,
                        attachment={
                            "name": path.name,
                            "data": path.read_bytes(),
                            "mime": artifact.mime,
                        },
                    )
                    artifact.rp_state = SyncState.DONE
                    artifact.rp_remote_id = item_id
            if not already_published:
                self.finish_item(item_id, status)
        self.finish_launch(launch_id)


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)
