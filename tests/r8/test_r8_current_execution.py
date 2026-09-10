from datetime import date
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    drum_slot_identity,
    get_current_execution_scope,
    invalidate_current_execution_scope,
    load_current_execution_rows,
    order_execution_queue,
    publish_current_execution_scope,
)


def _queue(identity: str, *, period: str, plan_id: int, line_id: int, qty: str = "1"):
    return {
        "entity_kind": "assembly_queue",
        "business_identity": identity,
        "scope_key": "assembly:all-live-plans",
        "payload": {
            "plan_id": plan_id,
            "plan_line_id": line_id,
            "period_from": period,
            "period_to": period,
            "assembly_remaining_qty": qty,
            "original_priority": [period, period, plan_id, line_id],
        },
    }


def test_r8_current_owner_is_stable_across_100_noop_recalculations(db_session):
    rows = [_queue("queue:1", period="2026-09-10", plan_id=1, line_id=1)]
    first = publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="assembly:all-live-plans",
        rows=rows,
    )
    current = db_session.query(models.CurrentExecutionRow).one()
    row_id = int(current.id)
    updated_at = current.updated_at
    change_count = db_session.query(models.CurrentExecutionChange).count()

    for index in range(100):
        result = publish_current_execution_scope(
            db_session,
            source_revision=f"accepted:g{index + 2}",
            scope_key="assembly:all-live-plans",
            rows=rows,
        )
        assert result.idempotent is True

    current = db_session.query(models.CurrentExecutionRow).one()
    assert int(current.id) == row_id
    assert current.updated_at == updated_at
    assert db_session.query(models.CurrentExecutionRow).count() == 1
    assert db_session.query(models.CurrentExecutionChange).count() == change_count
    assert first.changed_rows == 1


def test_r8_queue_order_is_oldest_first_with_decimal_and_equal_date_tiebreak():
    rows = [
        _queue("queue:2", period="2026-09-10", plan_id=2, line_id=1, qty="1.000"),
        _queue("queue:1", period="2026-09-10", plan_id=1, line_id=9, qty="1.000"),
        _queue("queue:0", period="2026-09-09", plan_id=9, line_id=1, qty="0.001"),
    ]
    ordered = order_execution_queue(rows)
    assert [row["business_identity"] for row in ordered] == [
        "queue:0", "queue:1", "queue:2"
    ]
    assert all(Decimal(str(row["payload"]["assembly_remaining_qty"])) > 0 for row in ordered)


def test_r8_stale_or_not_ready_publication_fails_closed(db_session):
    with pytest.raises(CurrentExecutionUnavailable, match="ready"):
        publish_current_execution_scope(
            db_session,
            source_revision="accepted:g1",
            scope_key="assembly:all-live-plans",
            rows=[_queue("queue:1", period="2026-09-10", plan_id=1, line_id=1)],
            result_ready=False,
        )


def test_r8_complete_scope_cannot_clear_a_foreign_mrp_shelf(db_session):
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="shelf:mrp:1",
        rows=[{
            "entity_kind": "shelf_projection",
            "business_identity": "shelf:item:10:warehouse:W1",
            "scope_key": "shelf:mrp:1",
            "payload": {"item_id": 10, "gap_qty": "2"},
        }],
    )
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="shelf:mrp:2",
        rows=[{
            "entity_kind": "shelf_projection",
            "business_identity": "shelf:item:10:warehouse:W1",
            "scope_key": "shelf:mrp:2",
            "payload": {"item_id": 10, "gap_qty": "3"},
        }],
    )
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g2",
        scope_key="shelf:mrp:1",
        rows=[],
        complete_scope=True,
        entity_kinds=("shelf_projection",),
    )
    current = load_current_execution_rows(
        db_session, entity_kind="shelf_projection", scope_key="shelf:mrp:2"
    )
    assert [row.business_identity for row in current] == ["shelf:item:10:warehouse:W1"]
    assert current[0].scope_key == "shelf:mrp:2"
    assert current[0].payload["gap_qty"] == "3"


def test_r8_manual_input_is_part_of_current_business_state(db_session):
    row = _queue("queue:1", period="2026-09-10", plan_id=1, line_id=1)
    row["manual_input"] = {"slot_date": date(2026, 9, 14).isoformat(), "moved_by": "master"}
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="assembly:all-live-plans",
        rows=[row],
    )
    current = load_current_execution_rows(db_session, entity_kind="assembly_queue")
    assert current[0].manual_input["moved_by"] == "master"


def test_r8_closure_hides_current_row_but_preserves_bounded_change_history(db_session):
    row = _queue("queue:closed", period="2026-09-10", plan_id=1, line_id=1)
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="assembly:all-live-plans",
        rows=[row],
    )
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g2",
        scope_key="assembly:all-live-plans",
        rows=[],
        complete_scope=True,
        entity_kinds=("assembly_queue",),
    )
    assert load_current_execution_rows(db_session, entity_kind="assembly_queue") == []
    current = db_session.query(models.CurrentExecutionRow).one()
    assert current.result_status == "closed"
    assert db_session.query(models.CurrentExecutionChange).count() == 2


def test_r8_empty_readiness_scope_does_not_close_queue_scope(db_session):
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="assembly:all-live-plans",
        entity_kinds=("assembly_queue",),
        rows=[_queue("queue:1", period="2026-09-10", plan_id=1, line_id=1)],
    )
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="assembly:all-live-plans",
        entity_kinds=("assembly_readiness",),
        rows=[],
        complete_scope=True,
    )
    assert [row.business_identity for row in load_current_execution_rows(
        db_session, entity_kind="assembly_queue"
    )] == ["queue:1"]


def test_r8_manual_tile_survives_new_generation_queue_ids(db_session):
    identity = drum_slot_identity(77, 0)
    first = {
        "entity_kind": "drum_slot",
        "business_identity": identity,
        "scope_key": "drum:all-live-plans",
        "payload": {
            "plan_line_id": 77,
            "queue_line_id": 101,
            "slot_ordinal": 0,
            "resource_id": 5,
            "slot_date": "2026-09-10",
        },
        "manual_input": {
            "slot_date": "2026-09-12",
            "resource_id": 5,
            "moved_by": "master",
        },
    }
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="drum:all-live-plans",
        rows=[first],
        entity_kinds=("drum_slot",),
    )
    current_id = int(db_session.query(models.CurrentExecutionRow).one().id)
    second = {
        **first,
        "payload": {**first["payload"], "queue_line_id": 202, "slot_date": "2026-09-11"},
    }
    second.pop("manual_input")
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g2",
        scope_key="drum:all-live-plans",
        rows=[second],
        entity_kinds=("drum_slot",),
    )
    current = db_session.query(models.CurrentExecutionRow).one()
    assert int(current.id) == current_id
    assert current.payload["queue_line_id"] == 202
    assert current.payload["slot_date"] == "2026-09-11"
    assert current.manual_input["slot_date"] == "2026-09-12"


def test_r8_dependency_invalidation_makes_current_read_fail_closed(db_session):
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="assembly:all-live-plans",
        rows=[_queue("queue:1", period="2026-09-10", plan_id=1, line_id=1)],
    )
    assert invalidate_current_execution_scope(
        db_session,
        entity_kind="assembly_queue",
        scope_key="assembly:all-live-plans",
        source_revision="dependency:capacity:v2",
        reason="capacity_changed",
    ) is True
    assert get_current_execution_scope(
        db_session,
        entity_kind="assembly_queue",
        scope_key="assembly:all-live-plans",
    ).result_ready is False
    assert load_current_execution_rows(db_session, entity_kind="assembly_queue") == []
