from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app import models
from app.routers.production_control import get_orders_journal
from app.services.planning_truth import publish_generation
from app.services.production_control_journal_snapshot import (
    ProductionControlJournalSnapshotUnavailable,
    build_candidate_snapshot,
    promote_candidate_snapshot,
    read_snapshot,
)


CAPABILITIES = {
    "physical_ledger": True,
    "reservation_replay": True,
    "execution_allocations": True,
    "planning_snapshots": True,
    "production_control_journal": True,
}


def _building_generation(db, key: str):
    cutoff = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key=f"{key}:physical",
        status="completed",
        cutoff=cutoff,
        completed_at=cutoff,
        source_watermarks={"explicit_empty_prefix": True},
    )
    generation = models.LedgerGeneration(
        generation_key=key,
        status="building",
        cutoff=cutoff,
        source_watermarks={"explicit_empty_prefix": True},
        capabilities={},
        physical_import_batch=physical,
        algorithm_version="test",
        replay_version="test",
    )
    db.add(generation)
    db.flush()
    return generation


def _journal_line(db):
    item = models.Item(
        item_code="SNAP-PROD-1",
        item_name="Snapshot production line",
        item_article="SNAP-ARTICLE",
        unit="шт",
        status="active",
    )
    order = models.ProductionOrder(
        order_number="SNAP-ORDER-1",
        order_date=datetime(2026, 7, 20),
        source="1c",
        deletion_mark=False,
    )
    db.add_all([item, order])
    db.flush()
    product = models.ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=10,
        produced_qty=3,
        remaining_qty=999,
    )
    db.add(product)
    db.flush()
    return item, order, product


def _accept(db, generation, snapshot):
    accepted_at = datetime(2026, 7, 29, 13, tzinfo=timezone.utc)
    generation.status = "accepted"
    generation.accepted_at = accepted_at
    generation.capabilities = dict(CAPABILITIES)
    promoted = promote_candidate_snapshot(
        db,
        generation=generation,
        accepted_at=accepted_at,
    )
    assert promoted is snapshot
    publish_generation(db, generation)
    db.commit()


def test_public_read_is_persisted_paged_and_stable_after_live_mutation(db_session):
    generation = _building_generation(db_session, "production-journal-snapshot")
    item, order, product = _journal_line(db_session)

    snapshot = build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[],
    )
    assert build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[],
    ) is snapshot
    assert snapshot.truth_status == "building"
    assert snapshot.payload["meta"]["row_count"] == 1
    assert len(snapshot.rows) == 1
    assert snapshot.rows[0].payload["remaining_qty"] == 7
    _accept(db_session, generation, snapshot)

    first = read_snapshot(db_session, search="SNAP-ARTICLE", limit=20, offset=0)
    assert first["total"] == 1
    assert first["rows"][0]["product_id"] == product.product_id
    assert first["rows"][0]["remaining_qty"] == 7

    # Public reads are byte-stable for the generation and do not rebuild from
    # live rows, even when an operational writer changes those rows later.
    item.item_name = "MUTATED LIVE NAME"
    order.deletion_mark = True
    product.produced_qty = 10
    product.remaining_qty = 0
    db_session.commit()

    second = read_snapshot(db_session, search="SNAP-ARTICLE", limit=20, offset=0)
    assert second == first


def test_missing_snapshot_fails_closed_and_router_maps_it_to_503(db_session):
    generation = _building_generation(db_session, "production-journal-missing")
    generation.status = "accepted"
    generation.accepted_at = datetime(2026, 7, 29, 13, tzinfo=timezone.utc)
    generation.capabilities = dict(CAPABILITIES)
    publish_generation(db_session, generation)
    db_session.commit()

    with pytest.raises(ProductionControlJournalSnapshotUnavailable) as caught:
        read_snapshot(db_session)
    assert caught.value.as_dict()["status"] == "unavailable"
    assert "missing" in caught.value.as_dict()["reason"]

    with pytest.raises(HTTPException) as router_error:
        get_orders_journal(db=db_session)
    assert router_error.value.status_code == 503
    assert (
        router_error.value.detail["code"]
        == "production_control_journal_snapshot_unavailable"
    )


def test_stale_truth_fails_before_snapshot_lookup(db_session):
    generation = _building_generation(db_session, "production-journal-stale")
    _journal_line(db_session)
    snapshot = build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[],
    )
    _accept(db_session, generation, snapshot)
    generation.status = "stale"
    generation.reason = "refresh overdue"
    db_session.commit()

    with pytest.raises(ProductionControlJournalSnapshotUnavailable) as caught:
        read_snapshot(db_session)
    detail = caught.value.as_dict()
    assert detail["truth_status"] == "stale"
    assert detail["status"] == "unavailable"
