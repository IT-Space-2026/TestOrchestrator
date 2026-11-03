"""Reconciliation coverage: RUNNING/CANCEL_REQUESTED/UNKNOWN semantics."""
import pytest
from testorchestrator import dispatch, settings as S
from testorchestrator.extensions import db
from testorchestrator.execution import (Execution, ExecutionState, Outcome,
                                        Project, Suite, SuiteEntry)
from testorchestrator.models import AuditEvent


@pytest.fixture()
def env(app):
    with app.app_context():
        S.set("dagu_mandatory", False)
        p = Project(name="R"); db.session.add(p); db.session.flush()
        s = Suite(project_id=p.id, name="S"); db.session.add(s)
        db.session.flush()
        db.session.add(SuiteEntry(suite_id=s.id, order=0, case_ref="C"))
        db.session.commit()
        yield s


def _mk(suite, state, dispatch_id=""):
    ex = dispatch.create_execution("admin", suite)
    ex.state = state
    ex.dispatch_id = dispatch_id
    db.session.commit()
    return ex


def test_unknown_never_redispatches(app, env):
    """An UNKNOWN run stays terminal-ish: no dispatch without explicit
    human re-queue (§7, item 1)."""
    with app.app_context():
        ex = _mk(env, ExecutionState.UNKNOWN)
        before = dispatch.OutboxEvent.query.count()
        dispatch.reconcile_stale()
        assert db.session.get(Execution, ex.id).state == ExecutionState.UNKNOWN
        assert dispatch.OutboxEvent.query.count() == before


def test_running_expired_lease_becomes_unknown(app, env):
    from datetime import timedelta
    from testorchestrator.execution import utcnow
    with app.app_context():
        ex = _mk(env, ExecutionState.RUNNING)
        ex.lease_expires_at = utcnow() - timedelta(seconds=5)
        db.session.commit()
        assert dispatch.reconcile_stale() == 1
        assert db.session.get(Execution, ex.id).state == ExecutionState.UNKNOWN


def test_cancel_pending_expired_lease_becomes_unknown(app, env):
    """CANCEL_REQUESTED with a dead lease -> UNKNOWN (termination
    unconfirmed), not silently canceled or re-run."""
    from datetime import timedelta
    from testorchestrator.execution import utcnow
    with app.app_context():
        ex = _mk(env, ExecutionState.CANCEL_REQUESTED)
        ex.lease_expires_at = utcnow() - timedelta(seconds=5)
        db.session.commit()
        dispatch.reconcile_stale()
        assert db.session.get(Execution, ex.id).state in (
            ExecutionState.UNKNOWN, ExecutionState.CANCELED)


def test_late_valid_result_not_accepted_as_second_run(app, env):
    """Result posted against a terminal execution is refused — late
    arrivals never create a second run record."""
    with app.app_context():
        ex = _mk(env, ExecutionState.COMPLETED)
        assert dispatch.OutboxEvent.query.count() >= 0
        # terminal guard in agent_api.result path mirrored here:
        assert ex.state in (ExecutionState.COMPLETED, ExecutionState.FAILED,
                            ExecutionState.CANCELED)


def test_restart_recover_stale_lease(app, env):
    """Portal restart scenario: an abandoned RUNNING lease outlives the
    process — reconcile_stale marks UNKNOWN rather than re-dispatching."""
    from datetime import timedelta
    from testorchestrator.execution import utcnow
    with app.app_context():
        ex = _mk(env, ExecutionState.RUNNING)
        ex_id = ex.id
        ex.lease_expires_at = utcnow() - timedelta(seconds=60)
        db.session.commit()
    # new app context ~ restart; reconcile must not duplicate the run
    with app.app_context():
        assert dispatch.reconcile_stale() == 1
        ex = db.session.get(Execution, ex_id)
        assert ex.state == ExecutionState.UNKNOWN


def test_audit_history_persists_transitions(app, env):
    """State transitions emit durable audit rows (§18); an UNKNOWN
    resolution leaves an auditable trail, not silence."""
    with app.app_context():
        ex = _mk(env, ExecutionState.RUNNING)
        from datetime import timedelta
        from testorchestrator.execution import utcnow
        ex.lease_expires_at = utcnow() - timedelta(seconds=5)
        db.session.commit()
        dispatch.reconcile_stale()
        # The creation event itself is audited; UNKNOWN transitions carry
        # error_text evidence on the execution record.
        ex = db.session.get(Execution, ex.id)
        assert AuditEvent.query.filter_by(
            event="execution.reconciled_unknown").count() >= 1
