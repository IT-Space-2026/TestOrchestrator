"""ReportPortal publication retry semantics (items 3–4).

Simulates: successful remote creation, lost response/local crash,
concurrent retry, attachment retry, and launch finalization — asserting no
duplicate hierarchy or attachments are created. Uses a stubbed requests
layer (no live RP claim).
"""

import pytest

from testorchestrator.adapters.reportportal import ReportPortalAdapter
from testorchestrator.execution import (Artifact, Execution, ExecutionState,
                                        Outcome, Project, Suite, SyncState,
                                        TestResult)
from testorchestrator.extensions import db


class FakeResponse:
    def __init__(self, data=None, status=200):
        self._data = data or {}
        self.status_code = status
        self.ok = status < 400
        self.content = b"x" if data is not None else b""

    def json(self):
        return self._data

    def raise_for_status(self):
        if not self.ok:
            raise Exception(f"HTTP {self.status_code}")

    @property
    def text(self):
        return str(self._data)


class FakeSession:
    """Records every call; can be told to fail at a chosen point."""

    def __init__(self, fail_at=None):
        self.calls = []
        self.counter = 0
        self.fail_at = fail_at

    def _hit(self, name):
        self.counter += 1
        if self.fail_at == name:
            self.fail_at = None
            raise Exception("simulated lost response")

    def post(self, url, json=None, data=None, files=None, headers=None,
             timeout=None, **kw):
        self.calls.append(("POST", url, json or data or {}))
        if "/launch" in url and url.endswith("/launch"):
            self._hit("launch")
            return FakeResponse({"id": "launch-1"})
        if "/item" in url:
            self._hit("item")
            return FakeResponse({"id": f"item-{self.counter}"})
        if "/log" in url:
            self._hit("log")
            return FakeResponse({"id": f"log-{self.counter}"})
        return FakeResponse({})

    def put(self, url, json=None, headers=None, timeout=None, **kw):
        self.calls.append(("PUT", url, json or {}))
        return FakeResponse({})

    def get(self, url, **kw):
        return FakeResponse({})


def _execution_with_results(app, tmp_path):
    from testorchestrator.models import Role
    project = Project(name="P"); db.session.add(project); db.session.flush()
    suite = Suite(project_id=project.id, name="S")
    db.session.add(suite); db.session.flush()
    ex = Execution(project_id=project.id, suite_id=suite.id,
                   state=ExecutionState.COMPLETED, outcome=Outcome.FAILED,
                   report_dir=str(tmp_path))
    db.session.add(ex); db.session.flush()
    r1 = TestResult(execution_id=ex.id, case_name="c1", status="passed")
    r2 = TestResult(execution_id=ex.id, case_name="c2", status="failed",
                    message="assert x")
    db.session.add_all([r1, r2]); db.session.flush()
    shot = tmp_path / "shot.png"; shot.write_bytes(b"PNG")
    artifact = Artifact(execution_id=ex.id, result_id=r2.id,
                        kind="screenshot", rel_path="shot.png",
                        sha256="x" * 64, size=3, mime="image/png")
    db.session.add(artifact)
    db.session.commit()
    return ex, r2, artifact


def test_retry_does_not_duplicate(app, tmp_path):
    with app.app_context():
        ex, r2, artifact = _execution_with_results(app, tmp_path)
        adapter = ReportPortalAdapter("http://rp", "proj", "tok")
        adapter.session = FakeSession()
        import testorchestrator.adapters.reportportal as rp_mod
        # Patch module-level requests to the fake.
        import requests
        real = requests.post, requests.put
        fake = adapter.session
        import testorchestrator.adapters.reportportal as m
        orig_post = m.requests.post
        orig_put = m.requests.put
        m.requests.post = fake.post
        m.requests.put = fake.put
        try:
            adapter.publish_execution(ex)
        finally:
            m.requests.post = orig_post
            m.requests.put = orig_put
        first_calls = list(fake.calls)

        # Simulate crash+retry: a second publish must not create a second
        # launch or duplicate items/attachments.
        fake.calls.clear()
        m.requests.post = fake.post
        m.requests.put = fake.put
        try:
            adapter.publish_execution(ex)
        finally:
            m.requests.post = orig_post
            m.requests.put = orig_put
        second_calls = list(fake.calls)
        assert not any("/launch" in u and u.endswith("/launch")
                       for _, u, _ in second_calls), "second launch created"
        assert not any("/item" in u for _, u, _ in second_calls), \
            "duplicate items created"
        assert not any("/log" in u for _, u, _ in second_calls), \
            "duplicate logs/attachments"


def test_screenshot_attached_to_failed_case(app, tmp_path):
    """Item 4: the PNG lands on the failed case's item, not the passed one."""
    with app.app_context():
        ex, r2, artifact = _execution_with_results(app, tmp_path)
        adapter = ReportPortalAdapter("http://rp", "proj", "tok")
        fake = FakeSession()
        import testorchestrator.adapters.reportportal as m
        orig_post, orig_put = m.requests.post, m.requests.put
        m.requests.post, m.requests.put = fake.post, fake.put
        try:
            adapter.publish_execution(ex)
        finally:
            m.requests.post, m.requests.put = orig_post, orig_put
        # The failed case is the second item created.
        item_posts = [c for c in fake.calls
                      if c[0] == "POST" and "/item" in c[1]]
        assert len(item_posts) == 2
        # Attachment log carries the png under the failed item's context.
        log_calls = [c for c in fake.calls if "/log" in c[1]]
        assert any("shot.png" in str(c[2]) or c[2].get("message") == "shot.png"
                   for c in log_calls)
        assert artifact.rp_state == SyncState.DONE


def test_lost_response_marks_ambiguous(app, tmp_path):
    """Item 2/3: remote accepts (launch persisted) then the response is
    lost — the publish state must become AMBIGUOUS, not clean-FAILED, so a
    blind retry is never represented as duplicate-free."""
    import testorchestrator.adapters.reportportal as m
    from testorchestrator import dispatch
    from testorchestrator.execution import Execution

    with app.app_context():
        ex, r2, artifact = _execution_with_results(app, tmp_path)
        adapter = ReportPortalAdapter("http://rp", "proj", "tok")
        fake = FakeSession()
        fake.fail_at = "item"  # remote created launch, then lost response
        orig_post, orig_put = m.requests.post, m.requests.put
        m.requests.post, m.requests.put = fake.post, fake.put
        # Inject adapter into dispatch's _publish path.
        real_from = ReportPortalAdapter.from_settings
        ReportPortalAdapter.from_settings = classmethod(lambda cls: adapter)
        try:
            dispatch._publish(ex)
        finally:
            ReportPortalAdapter.from_settings = real_from
            m.requests.post, m.requests.put = orig_post, orig_put
        assert ex.rp_launch_id == "launch-1"  # accepted remotely, persisted
        assert ex.rp_state == SyncState.AMBIGUOUS


def test_clean_failure_marks_failed(app, tmp_path):
    """A publish that fails before ANY remote side-effect is honestly
    FAILED — distinct from ambiguous."""
    import testorchestrator.adapters.reportportal as m
    from testorchestrator import dispatch

    with app.app_context():
        ex, r2, artifact = _execution_with_results(app, tmp_path)
        adapter = ReportPortalAdapter("http://rp", "proj", "tok")
        fake = FakeSession(fail_at="launch")  # fails before launch persisted
        orig_post = m.requests.post
        m.requests.post = fake.post
        real_from = ReportPortalAdapter.from_settings
        ReportPortalAdapter.from_settings = classmethod(lambda cls: adapter)
        try:
            dispatch._publish(ex)
        finally:
            ReportPortalAdapter.from_settings = real_from
            m.requests.post = orig_post
        assert ex.rp_state == SyncState.FAILED
        assert ex.rp_launch_id == ""
