"""Synchronous Buildbot stand-in for the dispatch test suite.

Executes ``exec_spec``s as real local subprocesses behind the adapter's
submit/poll/cancel/collect contract (Buildbot results codes included),
so the full portal state machine — assignment, outbox, reconcile,
collection, normalize — is exercised without a live master. Mirrors
the worker semantics of deploy/buildbot/master.cfg: source tar is
untarred into the builddir, argv is rendered from the spec (credential
wrapper supported), the report dir is uploaded to artifacts/<exec_id>.
"""
import base64
import json
import os
import signal
import subprocess
import tarfile
from pathlib import Path

from testorchestrator import buildbot_cfg

# buildbot.results codes
SUCCESS, FAILURE, EXCEPTION, CANCELLED = 0, 2, 4, 6


class LocalBuildbotStub:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.reqs = {}        # brid(str) -> dict
        self._next = 0

    # ----- adapter surface used by dispatch.py -----

    def submit(self, builder_name, execution_id, properties=None):
        self._next += 1
        brid = str(self._next)
        spec = json.loads((properties or {})["exec_spec"])
        basedir = self.home / "runs" / execution_id
        builddir = basedir / "build"
        builddir.mkdir(parents=True, exist_ok=True)
        req = {"buildrequestid": int(brid), "execution_id": execution_id,
               "builder": builder_name, "complete": False,
               "results": None, "log": "", "spec": spec,
               "builddir": builddir, "popen": None, "rc": None}
        tar_path = buildbot_cfg.staging_dir() / f"{execution_id}.tar"
        if tar_path.is_file():
            with tarfile.open(tar_path) as tar:
                tar.extractall(builddir, filter="data")
        argv = [spec["executable"], *spec.get("args", [])]
        if spec.get("credential_wrapper") and spec.get("credential_file"):
            argv = [spec["credential_wrapper"], spec["credential_file"],
                    *argv]
        workdir = basedir / spec.get("exec_workdir", "build")
        log_path = basedir / "run.log"
        req["log_path"] = log_path
        try:
            logf = open(log_path, "wb")
            req["popen"] = subprocess.Popen(
                argv, cwd=str(workdir), stdout=logf,
                stderr=subprocess.STDOUT, start_new_session=True)
            req["logf"] = logf
        except OSError as exc:
            req["log"] = f"exec failed: {exc}"
            log_path.write_text(req["log"])
            req["complete"] = True
            req["results"] = EXCEPTION
            req["rc"] = 127
        self.reqs[brid] = req
        return brid

    def _finish(self, req):
        """First poll after process exit: upload reports to artifacts,
        mirror results code."""
        req["complete"] = True
        rc = req["rc"]
        if rc is None:
            req["results"] = CANCELLED
        elif rc == 0:
            req["results"] = SUCCESS
        else:
            req["results"] = FAILURE
        spec, builddir = req["spec"], req["builddir"]
        report_src = builddir / spec.get("report_src", "Reports")
        dest = buildbot_cfg.artifacts_dir() / req["execution_id"]
        if report_src.is_dir():
            import shutil
            shutil.copytree(report_src, dest / "Reports",
                            dirs_exist_ok=True)

    def buildrequest(self, dispatch_id):
        req = self.reqs.get(str(dispatch_id))
        if req is None:
            return {}
        p = req["popen"]
        if p is not None and p.poll() is None and not req["complete"]:
            return {"buildrequestid": req["buildrequestid"],
                    "complete": False}
        if p is not None and req["rc"] is None:
            req["rc"] = p.returncode
            req["logf"].close()
        if not req["complete"]:
            self._finish(req)
        return {"buildrequestid": req["buildrequestid"],
                "complete": True, "results": req["results"]}

    def find_buildrequests(self, execution_id):
        return [{"buildrequestid": r["buildrequestid"],
                 "complete": r["complete"]}
                for r in self.reqs.values()
                if r["execution_id"] == execution_id]

    def builds_for_request(self, dispatch_id):
        return [{"buildid": 1}]

    def run_log(self, buildid):
        for r in self.reqs.values():
            p = r.get("log_path")
            if p and p.exists():
                return p.read_text(errors="replace")
        return r.get("log", "") if self.reqs else ""

    def cancel(self, dispatch_id):
        req = self.reqs.get(str(dispatch_id))
        if req is None:
            return False
        p = req["popen"]
        if p is not None and p.poll() is None:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            p.wait()
            req["logf"].close()
        req["complete"] = True
        req["results"] = CANCELLED
        req["rc"] = None
        return True

    def workers(self):
        return [{"name": "to-local", "connected": True}]

    def health(self):
        return True
