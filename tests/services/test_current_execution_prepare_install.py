from datetime import datetime, timezone

from sqlalchemy import event

import pytest

from app import models
from app.services.item_ledger.current_execution import (
    CurrentExecutionPreparedBasisConflict,
    install_prepared_current_execution_scope,
    install_prepared_compact_current_execution_views,
    install_prepared_current_obligation_views,
    prepare_current_execution_scope,
    prepare_compact_current_execution_views,
    prepare_current_obligation_views_from_generation,
    prepare_current_production_control_from_payload,
    publish_current_execution_scope,
)


SCOPE = "assembly:prepare-install"
KIND = "assembly_queue"


def _row(identity: str, qty: str, **extra):
    payload = {
        "plan_id": 1,
        "plan_line_id": int(identity.rsplit(":", 1)[-1]),
        "assembly_remaining_qty": qty,
        **extra,
    }
    return {
        "entity_kind": KIND,
        "business_identity": identity,
        "scope_key": SCOPE,
        "payload": payload,
    }


def _prepare(db, revision, rows, *, summary=None):
    return prepare_current_execution_scope(
        db,
        source_revision=revision,
        scope_key=SCOPE,
        entity_kinds=(KIND,),
        rows=rows,
        summary=summary,
    )


def test_prepare_is_read_only_and_install_matches_direct_create(db_session):
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.upper())

    event.listen(db_session.bind, "before_cursor_execute", capture)
    try:
        prepared = _prepare(
            db_session,
            "accepted:prepared:1",
            [_row("queue:1", "2.000")],
            summary={"total_rows": 1},
        )
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture)

    assert not db_session.new
    assert not db_session.dirty
    assert db_session.query(models.CurrentExecutionScope).count() == 0
    assert db_session.query(models.CurrentExecutionRow).count() == 0
    assert all("FOR UPDATE" not in statement for statement in statements)
    assert all(not statement.lstrip().startswith(("INSERT", "UPDATE", "DELETE")) for statement in statements)

    result = install_prepared_current_execution_scope(db_session, prepared)
    row = db_session.query(models.CurrentExecutionRow).one()
    scope = db_session.query(models.CurrentExecutionScope).one()
    assert result.changed_rows == 1
    assert result.closed_rows == 0
    assert row.payload["assembly_remaining_qty"] == "2.000"
    assert row.content_hash == prepared.row_changes[0].content_hash
    assert scope.content_hash == dict(prepared.scope_hashes)[KIND]
    assert scope.summary == {"total_rows": 1}


def test_prepare_install_update_delete_and_semantic_noop_have_exact_audit(db_session):
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:base",
        scope_key=SCOPE,
        entity_kinds=(KIND,),
        rows=[_row("queue:1", "1.000"), _row("queue:2", "2.000")],
    )
    original_ids = {
        row.business_identity: int(row.id)
        for row in db_session.query(models.CurrentExecutionRow).all()
    }
    initial_audits = db_session.query(models.CurrentExecutionChange).count()

    prepared = _prepare(
        db_session,
        "accepted:next",
        [_row("queue:1", "3.000"), _row("queue:3", "4.000")],
    )
    result = install_prepared_current_execution_scope(db_session, prepared)
    assert (result.changed_rows, result.closed_rows) == (2, 1)
    assert db_session.query(models.CurrentExecutionChange).count() == initial_audits + 3
    rows = {row.business_identity: row for row in db_session.query(models.CurrentExecutionRow).all()}
    assert int(rows["queue:1"].id) == original_ids["queue:1"]
    assert rows["queue:2"].result_status == "closed"
    assert rows["queue:3"].result_status == "accepted"

    audit_count = db_session.query(models.CurrentExecutionChange).count()
    row_hashes = {identity: row.content_hash for identity, row in rows.items()}
    noop = _prepare(
        db_session,
        "accepted:metadata-only",
        [_row("queue:1", "3"), _row("queue:3", "4.0")],
    )
    noop_result = install_prepared_current_execution_scope(db_session, noop)
    assert noop_result.idempotent
    assert db_session.query(models.CurrentExecutionChange).count() == audit_count
    current = {row.business_identity: row for row in db_session.query(models.CurrentExecutionRow).all()}
    assert current["queue:1"].content_hash == row_hashes["queue:1"]
    assert current["queue:3"].content_hash == row_hashes["queue:3"]
    assert db_session.query(models.CurrentExecutionScope).one().source_revision == "accepted:metadata-only"


def test_install_rejects_concurrent_scope_basis_change_without_overwrite(db_session):
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:base",
        scope_key=SCOPE,
        entity_kinds=(KIND,),
        rows=[_row("queue:1", "1.000")],
    )
    prepared = _prepare(db_session, "accepted:stale", [_row("queue:1", "2.000")])

    manifest = db_session.query(models.CurrentExecutionScope).one()
    manifest.source_revision = "accepted:concurrent"
    row = db_session.query(models.CurrentExecutionRow).one()
    row.payload = {**row.payload, "assembly_remaining_qty": "9.000"}
    db_session.flush()

    with pytest.raises(CurrentExecutionPreparedBasisConflict, match="basis changed"):
        install_prepared_current_execution_scope(db_session, prepared)

    assert db_session.query(models.CurrentExecutionScope).one().source_revision == "accepted:concurrent"
    assert db_session.query(models.CurrentExecutionRow).one().payload["assembly_remaining_qty"] == "9.000"


def test_production_payload_prepare_is_read_only_and_installs_normalized_identity(db_session):
    cutoff = datetime(2026, 10, 30, tzinfo=timezone.utc)
    generation = models.LedgerGeneration(
        generation_key="prepare-production-current",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        source_watermarks={},
        capabilities={},
        physical_import_batch=models.PhysicalImportBatch(
            batch_key="prepare-production-current-batch",
            status="completed",
            cutoff=cutoff,
            source_watermarks={},
        ),
        algorithm_version="tests/prepare-production-current",
    )
    db_session.add(generation)
    db_session.flush()
    payload = {
        "meta": {"row_count": 1},
        "summary": {"total_rows": 1},
        "rows": [{
            "current_identity": "legacy-wrong-identity",
            "source_mrp_requirement_id": 10,
            "source_mrp_allocation_key": "mrp_requirement:10:order:1",
            "product_id": 20,
            "item_id": 20,
            "quantity": "2.000",
            "remaining_qty": "2.000",
            "root_item_ids": [20, 20],
        }],
    }
    db_session.expunge_all()

    prepared = prepare_current_production_control_from_payload(
        db_session, int(generation.id), payload
    )

    assert not db_session.new
    assert not db_session.dirty
    assert db_session.query(models.CurrentExecutionScope).count() == 0
    assert db_session.query(models.CurrentExecutionRow).count() == 0
    change = prepared.row_changes[0]
    assert change.business_identity == (
        "production-mrp-requirement:10:mrp_requirement:10:order:1:product:20"
    )
    assert change.payload["root_item_ids"] == [20]

    result = install_prepared_current_execution_scope(db_session, prepared)
    assert result.changed_rows == 1
    row = db_session.query(models.CurrentExecutionRow).one()
    assert row.business_identity == change.business_identity


def test_obligation_bundle_prepare_is_read_only_and_public_shapes_install(db_session):
    cutoff = datetime(2026, 10, 30, 1, tzinfo=timezone.utc)
    generation = models.LedgerGeneration(
        generation_key="prepare-obligation-current",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        source_watermarks={},
        capabilities={},
        physical_import_batch=models.PhysicalImportBatch(
            batch_key="prepare-obligation-current-batch",
            status="completed",
            cutoff=cutoff,
            source_watermarks={},
        ),
        algorithm_version="tests/prepare-obligation-current",
    )
    db_session.add(generation)
    db_session.flush()
    generation_id = int(generation.id)
    db_session.expunge_all()

    prepared = prepare_current_obligation_views_from_generation(
        db_session,
        generation_id,
        purchase_payload={"rows": [], "meta": {}},
        production_payload={"rows": [], "meta": {"row_count": 0}},
        mrp_payloads={},
        period_payloads={},
        period_run_ids=(),
    )

    assert not db_session.new
    assert not db_session.dirty
    assert db_session.query(models.CurrentExecutionScope).count() == 0
    assert [consumer for consumer, _change in prepared.changes] == [
        "production_control_journal",
        "purchase_control_journal",
        "mrp_result",
        "period_plan_execution",
    ]
    results = install_prepared_current_obligation_views(db_session, prepared)
    assert set(results) == {
        "production_control_journal",
        "purchase_control_journal",
        "mrp_result",
        "period_plan_execution",
    }
    assert all(result.idempotent for result in results.values())
    assert db_session.query(models.CurrentExecutionScope).count() == 4


def test_compact_bundle_defers_only_new_numeric_queue_owner_id(db_session):
    cutoff = datetime(2026, 10, 30, 2, tzinfo=timezone.utc)
    generation = models.LedgerGeneration(
        generation_key="prepare-compact-current",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        source_watermarks={},
        capabilities={},
        physical_import_batch=models.PhysicalImportBatch(
            batch_key="prepare-compact-current-batch",
            status="completed",
            cutoff=cutoff,
            source_watermarks={},
        ),
        algorithm_version="tests/prepare-compact-current",
    )
    db_session.add(generation)
    db_session.flush()
    generation_id = int(generation.id)
    queue = {
        "entity_kind": "assembly_queue",
        "business_identity": "plan-line:1",
        "scope_key": "assembly:all-live-plans",
        "payload": {
            "plan_id": 1,
            "plan_line_id": 1,
            "assembly_remaining_qty": "2.000",
        },
    }
    readiness = {
        "entity_kind": "assembly_readiness",
        "business_identity": "plan-line:1",
        "scope_key": "assembly:all-live-plans",
        "payload": {"plan_line_id": 1, "status": "ready", "open_qty": "2.000"},
    }
    drum_schedule = {
        "entity_kind": "drum_schedule",
        "business_identity": "drum:all-live-plans",
        "scope_key": "drum:all-live-plans",
        "payload": {"schedule_from": "2026-10-30", "schedule_to": "2026-10-30"},
    }
    drum_slot = {
        "entity_kind": "drum_slot",
        "business_identity": "slot:plan-line:1:ordinal:0",
        "scope_key": "drum:all-live-plans",
        "payload": {
            "queue_owner_identity": "plan-line:1",
            "plan_line_id": 1,
            "slot_ordinal": 0,
            "slot_qty": "2.000",
        },
    }
    db_session.expunge_all()

    prepared = prepare_compact_current_execution_views(
        db_session,
        generation_id=generation_id,
        source_revision="physical:test",
        assembly_payload={
            "queue_rows": [queue],
            "readiness_rows": [readiness],
            "readiness_metrics": {},
        },
        drum_payload={"rows": [drum_schedule, drum_slot], "metrics": {}},
        shelf_payload={"rows": [], "metrics": {}},
    )

    assert not db_session.new
    assert not db_session.dirty
    assert len(prepared.deferred_queue_owners) == 2
    assert prepared.readiness.normalized_rows[0]["payload"]["queue_line_id"] == 0
    results = install_prepared_compact_current_execution_views(db_session, prepared)
    assert set(results) == {"assembly_queue", "assembly_readiness", "drum", "shelf"}
    queue_owner = db_session.query(models.CurrentExecutionRow).filter_by(
        entity_kind="assembly_queue"
    ).one()
    readiness_owner = db_session.query(models.CurrentExecutionRow).filter_by(
        entity_kind="assembly_readiness"
    ).one()
    slot_owner = db_session.query(models.CurrentExecutionRow).filter_by(
        entity_kind="drum_slot"
    ).one()
    assert readiness_owner.payload["queue_line_id"] == int(queue_owner.id)
    assert slot_owner.payload["queue_line_id"] == int(queue_owner.id)
    assert readiness_owner.content_hash != prepared.readiness.row_changes[0].content_hash

