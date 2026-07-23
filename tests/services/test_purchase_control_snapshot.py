from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app import models
from app.routers.purchase_control import get_filters, get_order
from app.services.planning_truth import publish_generation
from app.services.purchase_control_journal import (
    get_order_card,
    list_filters,
    list_journal,
)
from app.services.purchase_control_snapshot import (
    PurchaseJournalSnapshotUnavailable,
    build_candidate_snapshot,
)


CAPABILITIES = {
    "physical_ledger": True,
    "reservation_replay": True,
    "planning_snapshots": True,
    "purchase_control_journal": True,
}


def _context(db):
    cutoff = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="purchase-snapshot-physical",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="purchase-snapshot-generation",
        status="building",
        cutoff=cutoff,
        source_watermarks={},
        capabilities={},
        physical_import_batch=physical,
        algorithm_version="test",
    )
    supplier = models.Supplier(supplier_ref1c="SUP-1", supplier_name="Альфа")
    items = [
        models.Item(item_code="MAT-B", item_name="Материал Б", unit="шт"),
        models.Item(item_code="MAT-A", item_name="Материал А", unit="кг"),
    ]
    db.add_all([generation, supplier, *items])
    db.flush()
    order = models.SupplierOrder(
        order_number="ЗП-100",
        order_date=datetime(2026, 7, 1),
        order_ref1c="ORDER-1",
        supplier_id=supplier.supplier_id,
        order_state_name="В закупку",
    )
    db.add(order)
    db.flush()
    legacy_lines = [
        models.SupplierOrderItem(
            order_id=order.order_id, item_id_ref=item.item_id, line_number=index,
            quantity=Decimal("999"), received_qty=Decimal("998"),
            remaining_qty=Decimal("1"), delivery_date=datetime(2026, 8, index),
        )
        for index, item in enumerate(items, 1)
    ]
    batch = models.LedgerBuildBatch(
        ledger_generation_id=generation.id,
        stage="snapshot_build",
        batch_key="purchase-snapshot-build",
        status="building",
        algorithm_version="test",
        metrics={},
    )
    db.add_all([*legacy_lines, batch])
    db.flush()
    supplies = [
        models.LedgerFutureSupply(
            ledger_generation_id=generation.id,
            supply_kind="supplier_order",
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            planning_stock_pool="main",
            destination_warehouse_ref1c="WH-1",
            source_ref=order.order_ref1c,
            source_line_ref=str(index),
            source_local_id=str(line.item_id),
            ordered_qty_at_cutoff=Decimal(str(10 * index)),
            realized_qty_at_cutoff=Decimal(str(index)),
            open_qty_at_cutoff=Decimal(str(10 * index - index)),
            eta_date=date(2026, 8, index),
            source_state_key="В закупку",
            capture_cutoff=cutoff,
            source_content_hash=f"hash-{index}",
            capture_batch_id=batch.id,
            evidence_status="exact",
        )
        for index, (item, line) in enumerate(zip(items, legacy_lines), 1)
    ]
    db.add_all(supplies)
    db.flush()
    return generation, order, legacy_lines, supplies


def _accept(db, generation):
    snapshot = build_candidate_snapshot(db, generation.id)
    accepted_at = datetime(2026, 7, 23, 13, tzinfo=timezone.utc)
    generation.status = "accepted"
    generation.accepted_at = accepted_at
    generation.capabilities = dict(CAPABILITIES)
    snapshot.truth_status = "accepted"
    snapshot.reason = None
    snapshot.published_at = accepted_at
    publish_generation(db, generation)
    db.flush()
    return snapshot


def test_candidate_is_idempotent_and_groups_multiple_lines(db_session):
    generation, order, _legacy, _supplies = _context(db_session)

    first = build_candidate_snapshot(db_session, generation.id)
    second = build_candidate_snapshot(db_session, generation.id)

    assert second.id == first.id
    assert second.payload == first.payload
    assert [line["item_code"] for line in first.payload["cards"][str(order.order_id)]["lines"]] == [
        "MAT-A", "MAT-B",
    ]
    assert [(row["quantity"], row["remaining_qty"], row["received_qty"]) for row in first.payload["rows"]] == [
        (20.0, 18.0, None),
        (10.0, 9.0, None),
    ]


def test_candidate_conflict_is_rejected(db_session):
    generation, _order, _legacy, supplies = _context(db_session)
    build_candidate_snapshot(db_session, generation.id)
    supplies[0].open_qty_at_cutoff = Decimal("8")

    with pytest.raises(ValueError, match="candidate conflict"):
        build_candidate_snapshot(db_session, generation.id)


def test_candidate_rejects_open_greater_than_ordered(db_session):
    generation, _order, _legacy, supplies = _context(db_session)
    supplies[0].open_qty_at_cutoff = Decimal("11")

    with pytest.raises(ValueError, match="ordered/open invariant"):
        build_candidate_snapshot(db_session, generation.id)


def test_public_reads_are_byte_stable_after_legacy_line_mutation(db_session):
    generation, order, legacy, _supplies = _context(db_session)
    _accept(db_session, generation)
    before_list = deepcopy(list_journal(db_session, active_only=False))
    before_card = deepcopy(get_order_card(db_session, order.order_id))

    legacy[0].quantity = Decimal("1")
    legacy[0].received_qty = Decimal("1")
    legacy[0].remaining_qty = Decimal("0")
    legacy[1].quantity = Decimal("5000")
    legacy[1].remaining_qty = Decimal("5000")
    db_session.flush()

    assert list_journal(db_session, active_only=False) == before_list
    assert get_order_card(db_session, order.order_id) == before_card


def test_filters_sort_pagination_and_summary_use_only_snapshot(db_session):
    generation, _order, _legacy, _supplies = _context(db_session)
    _accept(db_session, generation)

    result = list_journal(
        db_session, search="материал", supplier_id=1, sort_by="remaining_qty",
        sort_dir="desc", limit=1, offset=1, active_only=True,
    )

    assert result["total"] == 2
    assert result["rows"][0]["remaining_qty"] == 9.0
    assert result["summary"] == {
        "total_rows": 2,
        "by_status": {"unavailable": 2},
        "by_phase": {"unavailable": 2},
        "to_order": 0,
        "overdue": 0,
        "expected_7d": 0,
        "in_transit_amount": 0.0,
        "fact_status": "unavailable",
    }
    assert list_filters(db_session) == {
        "suppliers": [{"supplier_id": 1, "supplier_name": "Альфа"}],
        "states": ["В закупку"],
    }


def test_missing_or_stale_snapshot_fails_closed(db_session):
    with pytest.raises(PurchaseJournalSnapshotUnavailable) as missing:
        list_journal(db_session)
    assert missing.value.as_dict()["code"] == "purchase_control_snapshot_unavailable"

    generation, _order, _legacy, _supplies = _context(db_session)
    _accept(db_session, generation)
    generation.capabilities = {**generation.capabilities, "purchase_control_journal": False}
    db_session.flush()
    with pytest.raises(PurchaseJournalSnapshotUnavailable) as stale:
        list_journal(db_session)
    assert stale.value.as_dict()["status"] == "unavailable"


def test_unknown_order_detail_is_not_fabricated(db_session):
    generation, _order, _legacy, _supplies = _context(db_session)
    _accept(db_session, generation)

    with pytest.raises(ValueError, match="not found"):
        get_order_card(db_session, 999999)

    with pytest.raises(HTTPException) as response:
        get_order(999999, db=db_session)
    assert response.value.status_code == 404


def test_router_returns_structured_503_when_snapshot_is_missing(db_session):
    with pytest.raises(HTTPException) as response:
        get_filters(db=db_session)

    assert response.value.status_code == 503
    assert response.value.detail["code"] == "purchase_control_snapshot_unavailable"
