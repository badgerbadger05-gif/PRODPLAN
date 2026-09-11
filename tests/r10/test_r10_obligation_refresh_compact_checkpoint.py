"""R10 RED contracts for compact obligation-refresh checkpoints.

These tests deliberately exercise the accepted retry boundary.  A checkpoint
must prove the publication inputs and current-scope anchors without embedding
the complete read models that are already owned by their current scopes.
"""

from sqlalchemy import event

import pytest

from app import models
from app.services import obligation_refresh_orchestrator as workflow
from tests.services.test_obligation_refresh_orchestrator import _run, _world


HEAVY_CHECKPOINT_FIELDS = {
    "purchase_control_journal_payload",
    "production_control_journal_payload",
    "mrp_result_payloads",
    "period_plan_execution_payloads",
}


def _snapshot_build(db, generation_id: int):
    return db.query(models.LedgerBuildBatch).filter_by(
        ledger_generation_id=int(generation_id), stage="snapshot_build"
    ).one()


def _current_scope_ids(db, generation_id: int) -> dict[tuple[str, str], int]:
    return {
        (str(scope.entity_kind), str(scope.scope_key)): int(scope.id)
        for scope in db.query(models.CurrentExecutionScope).filter_by(
            source_generation_id=int(generation_id)
        ).all()
    }


def test_obligation_refresh_checkpoint_is_compact_and_has_no_read_model_payloads(db_session):
    accepted, plan, _line, _item, _parent, _cutoff = _world(
        db_session, with_parent=False
    )
    result = _run(db_session, accepted, "compact-checkpoint", add=[plan.id])
    metrics = dict(_snapshot_build(db_session, result.target_generation_id).metrics or {})

    assert HEAVY_CHECKPOINT_FIELDS.isdisjoint(metrics)
    assert isinstance(metrics.get("candidate_run_ids"), list)
    assert isinstance(metrics.get("future_supply_capture_batch_id"), int)
    assert isinstance(metrics.get("assembly_queue_materialization"), dict)


def test_published_retry_reuses_current_scopes_without_saved_read_models(db_session, monkeypatch):
    accepted, plan, _line, _item, _parent, _cutoff = _world(
        db_session, with_parent=False
    )
    first = _run(db_session, accepted, "compact-retry", add=[plan.id])
    target_id = int(first.target_generation_id)
    scope_ids_before = _current_scope_ids(db_session, target_id)
    checkpoint = _snapshot_build(db_session, target_id)
    checkpoint.metrics = {
        key: value
        for key, value in dict(checkpoint.metrics or {}).items()
        if key not in HEAVY_CHECKPOINT_FIELDS
    }
    db_session.flush()

    def forbidden_publish(*_args, **_kwargs):
        raise AssertionError("published retry must not invoke the publisher")

    monkeypatch.setattr(workflow, "publish_obligation_refresh_batch", forbidden_publish)

    writes: list[str] = []
    bind = db_session.get_bind()

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(bind, "before_cursor_execute", observe)
    try:
        retry = _run(db_session, accepted, "compact-retry", add=[plan.id])
    finally:
        event.remove(bind, "before_cursor_execute", observe)

    assert retry.target_generation_id == first.target_generation_id
    assert retry.candidate_run_ids == first.candidate_run_ids
    assert _current_scope_ids(db_session, target_id) == scope_ids_before
    assert writes == []


def test_corrupt_compact_checkpoint_and_current_scope_fail_closed_before_dml(db_session):
    accepted, plan, _line, _item, _parent, _cutoff = _world(
        db_session, with_parent=False
    )
    first = _run(db_session, accepted, "compact-corrupt", add=[plan.id])
    target_id = int(first.target_generation_id)
    checkpoint = _snapshot_build(db_session, target_id)
    checkpoint.metrics = {
        key: value
        for key, value in dict(checkpoint.metrics or {}).items()
        if key not in HEAVY_CHECKPOINT_FIELDS
    }
    scope = db_session.query(models.CurrentExecutionScope).filter_by(
        source_generation_id=target_id,
        entity_kind="mrp_result",
    ).one()
    scope.source_revision = "corrupt-current-scope"
    db_session.flush()

    writes: list[str] = []
    bind = db_session.get_bind()

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(bind, "before_cursor_execute", observe)
    try:
        with pytest.raises(
            workflow.ObligationRefreshOrchestratorError,
            match="current scope|checkpoint",
        ):
            _run(db_session, accepted, "compact-corrupt", add=[plan.id])
    finally:
        event.remove(bind, "before_cursor_execute", observe)

    assert writes == []
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == target_id
