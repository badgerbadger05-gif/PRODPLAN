"""A physical refresh must not lose the accepted parent's future supply.

``accept_generation_build`` forks the physical prefix only.  It never captured
future supply and never cloned it, while the purchase-journal candidate reads
``ledger_future_supply`` by generation — so every three-hour refresh published a
journal reporting zero ordered and zero in transit.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.future_supply_capture import (
    FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION,
    FutureSupplyCaptureError,
    _carry_forward_rows,
    carry_forward_future_supply,
)
from app.services.item_ledger.generation_lifecycle import accept_generation_build

from tests.services.test_generation_lifecycle import _synthetic


CUTOFF = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)


def _accepted_parent_with_future_supply(
    db, key: str, item, *,
    qty="4",
    realized_qty: str = "0",
    open_qty: str | None = None,
    source_requirement_id: int | None = None,
    supply_kind: str = "wip_order",
):
    physical = models.PhysicalImportBatch(
        batch_key=f"fs-physical-{key}",
        status="completed",
        cutoff=CUTOFF,
        completed_at=CUTOFF,
        source_watermarks={},
    )
    parent = models.LedgerGeneration(
        generation_key=f"fs-parent-{key}",
        status="accepted",
        cutoff=CUTOFF,
        accepted_at=CUTOFF,
        physical_import_batch=physical,
        algorithm_version="test",
        replay_version="test",
        source_watermarks={},
        capabilities={"physical_ledger": True, "future_supply": True},
    )
    db.add(parent)
    db.flush()
    batch = models.LedgerBuildBatch(
        ledger_generation_id=int(parent.id),
        stage="future_supply_capture",
        batch_key=f"fs-capture-{key}",
        status="completed",
        algorithm_version="test",
        metrics={},
        completed_at=CUTOFF,
    )
    db.add(batch)
    db.flush()
    supply = models.LedgerFutureSupply(
        ledger_generation_id=int(parent.id),
        capture_batch_id=int(batch.id),
        supply_kind=supply_kind,
        item_id=item.item_id,
        planning_stock_pool="default",
        destination_warehouse_ref1c="WH",
        source_ref=f"order-{key}",
        source_line_ref="1",
        source_requirement_id=source_requirement_id,
        ordered_qty_at_cutoff=Decimal(qty),
        realized_qty_at_cutoff=Decimal(realized_qty),
        open_qty_at_cutoff=Decimal(open_qty if open_qty is not None else qty),
        eta_date=date(2026, 8, 1),
        source_state_key="ready",
        capture_cutoff=CUTOFF,
        source_content_hash=f"hash-{key}",
        evidence_status="exact",
    )
    db.add(supply)
    db.flush()
    return parent


def _building_child(db, key: str, parent):
    child = models.LedgerGeneration(
        generation_key=f"fs-child-{key}",
        status="building",
        cutoff=datetime(2026, 7, 21, 12, tzinfo=timezone.utc),
        physical_import_batch_id=int(parent.physical_import_batch_id),
        algorithm_version="test",
        replay_version="test",
        source_watermarks={
            "generation_kind": "physical_refresh",
            "parent_generation_id": int(parent.id),
        },
        capabilities={},
    )
    db.add(child)
    db.flush()
    return child


def _item(db, code: str):
    item = models.Item(item_code=code, item_name=code)
    db.add(item)
    db.flush()
    return item


def _supplier_realization_between_cutoffs(
    db_session,
    parent_generation: models.LedgerGeneration,
    item,
    *,
    order_ref: str,
    qty: str = "2",
):
    posted_at = CUTOFF.replace(hour=18).replace(tzinfo=None)
    sle = models.StockLedgerEntry(
        ingest_batch_id=int(parent_generation.physical_import_batch_id),
        source_content_hash=("r" * 64),
        item_id=item.item_id,
        qty=Decimal(qty),
        posting_at=posted_at,
        record_type="Receipt",
        movement_kind="receipt",
        recorder_type="Document_ПоступлениеТоваров",
        recorder_ref=f"receipt-{order_ref}",
        line_no="1",
        ingest_source="pull",
    )
    db_session.add(sle)
    db_session.flush()
    db_session.add(models.StockLedgerSupplierReceiptProvenance(
        ledger_generation_id=int(parent_generation.id),
        stock_ledger_entry_id=sle.id,
        receipt_doc_type=sle.recorder_type,
        receipt_doc_ref=sle.recorder_ref,
        receipt_doc_line_no=sle.line_no,
        supplier_order_ref=order_ref,
        supplier_order_line_no="1",
        operation_kind="supplier_receipt",
        evidence_hash=("p" * 64),
        evidence_payload={},
        match_rule="exact",
        match_status="exact",
        ambiguity_count=0,
    ))
    db_session.flush()


def test_carry_forward_stamps_capture_cutoff_to_target_and_recomputes_open_qty(db_session):
    item = _item(db_session, "FS-CARRY")
    parent = _accepted_parent_with_future_supply(
        db_session,
        "carry",
        item,
        qty="10",
        realized_qty="3",
        open_qty="99",
    )
    child = _building_child(db_session, "carry", parent)

    summary = carry_forward_future_supply(
        db_session,
        parent_generation_id=int(parent.id),
        target_generation_id=int(child.id),
    )

    rows = db_session.query(models.LedgerFutureSupply).filter_by(
        ledger_generation_id=int(child.id)
    ).all()
    assert summary["created"] is True
    assert summary["rows"] == 1
    assert len(rows) == 1
    carried = rows[0]
    assert carried.source_ref == "order-carry"
    assert carried.open_qty_at_cutoff == Decimal("7")
    # The captured fact is re-cutoffted to the target generation and kept
    # in a canonical projected form.
    assert carried.capture_cutoff == child.cutoff.replace(tzinfo=None)
    assert carried.capture_batch_id is not None
    batch = db_session.get(models.LedgerBuildBatch, int(carried.capture_batch_id))
    assert int(batch.ledger_generation_id) == int(child.id)
    assert batch.status == "completed"


def test_carry_forward_keeps_timezone_on_timestamp_written_to_postgres(db_session):
    item = _item(db_session, "FS-CARRY-TZ")
    parent = _accepted_parent_with_future_supply(db_session, "carry-tz", item)
    child = _building_child(db_session, "carry-tz", parent)
    child.cutoff = datetime(2026, 7, 21, 15, tzinfo=timezone(timedelta(hours=3)))
    db_session.flush()

    rows = _carry_forward_rows(db_session, parent=parent, target=child)

    assert rows[0]["capture_cutoff"].tzinfo is not None
    assert rows[0]["capture_cutoff"].astimezone(timezone.utc) == datetime(
        2026, 7, 21, 12, tzinfo=timezone.utc
    )


def test_carry_forward_reduces_open_qty_when_realization_occurs_between_cutoffs(db_session):
    item = _item(db_session, "FS-BETWEEN")
    parent = _accepted_parent_with_future_supply(
        db_session,
        "carry-between",
        item,
        qty="10",
        realized_qty="3",
        open_qty="7",
        supply_kind="supplier_order",
    )
    child = _building_child(db_session, "carry-between", parent)
    _supplier_realization_between_cutoffs(
        db_session,
        parent_generation=parent,
        item=item,
        order_ref="order-carry-between",
        qty="2",
    )

    summary = carry_forward_future_supply(
        db_session,
        parent_generation_id=int(parent.id),
        target_generation_id=int(child.id),
    )

    rows = db_session.query(models.LedgerFutureSupply).filter_by(
        ledger_generation_id=int(child.id)
    ).all()
    assert summary["created"] is True
    assert summary["rows"] == 1
    assert len(rows) == 1
    carried = rows[0]
    assert carried.source_ref == "order-carry-between"
    assert carried.open_qty_at_cutoff == Decimal("5")


def test_carry_forward_copies_source_requirement_id(db_session):
    item = _item(db_session, "FS-SR-ID")
    parent = _accepted_parent_with_future_supply(
        db_session,
        "carry-sr-id",
        item,
        source_requirement_id=123,
    )
    child = _building_child(db_session, "carry-sr-id", parent)

    carry_forward_future_supply(
        db_session,
        parent_generation_id=int(parent.id),
        target_generation_id=int(child.id),
    )

    carried = db_session.query(models.LedgerFutureSupply).filter_by(
        ledger_generation_id=int(child.id)
    ).one()
    assert carried.source_requirement_id == 123


def test_carry_forward_is_idempotent(db_session):
    item = _item(db_session, "FS-IDEM")
    parent = _accepted_parent_with_future_supply(db_session, "idem", item)
    child = _building_child(db_session, "idem", parent)

    first = carry_forward_future_supply(
        db_session,
        parent_generation_id=int(parent.id),
        target_generation_id=int(child.id),
    )
    second = carry_forward_future_supply(
        db_session,
        parent_generation_id=int(parent.id),
        target_generation_id=int(child.id),
    )

    assert first["created"] is True
    assert second["created"] is False
    assert first["content_hash"] == second["content_hash"]
    assert db_session.query(models.LedgerFutureSupply).filter_by(
        ledger_generation_id=int(child.id)
    ).count() == 1


def test_carry_forward_rejects_a_conflicting_existing_capture(db_session):
    item = _item(db_session, "FS-CONFLICT")
    parent = _accepted_parent_with_future_supply(db_session, "conflict", item)
    child = _building_child(db_session, "conflict", parent)
    carry_forward_future_supply(
        db_session,
        parent_generation_id=int(parent.id),
        target_generation_id=int(child.id),
    )
    carried = db_session.query(models.LedgerFutureSupply).filter_by(
        ledger_generation_id=int(child.id)
    ).one()
    carried.open_qty_at_cutoff = Decimal("1")
    db_session.flush()

    with pytest.raises(FutureSupplyCaptureError, match="conflicting future-supply"):
        carry_forward_future_supply(
            db_session,
            parent_generation_id=int(parent.id),
            target_generation_id=int(child.id),
        )


def test_accept_carries_the_parent_capture_and_claims_the_capability(db_session):
    generation, _requirement = _synthetic(db_session, "fs-accept")
    item = db_session.query(models.Item).filter_by(item_code="ITEM-fs-accept").one()
    parent = _accepted_parent_with_future_supply(db_session, "accept", item)
    generation.source_watermarks = {
        **dict(generation.source_watermarks or {}),
        "parent_generation_id": int(parent.id),
    }
    pointer = models.PlanningTruthState(id=1, current_generation_id=int(parent.id))
    db_session.add(pointer)
    db_session.flush()

    result = accept_generation_build(
        db_session, generation.id, replay_from=datetime(2026, 7, 1)
    )

    assert result["capabilities"]["future_supply"] is True
    assert result["future_supply"]["rows"] == 1
    assert db_session.query(models.LedgerFutureSupply).filter_by(
        ledger_generation_id=int(generation.id)
    ).count() == 1
    db_session.refresh(generation)
    assert generation.capabilities["future_supply"] is True


def test_a_generation_with_nothing_to_inherit_still_creates_zero_future_supply_proof(db_session):
    generation, _requirement = _synthetic(db_session, "fs-genesis")

    result = accept_generation_build(
        db_session, generation.id, replay_from=datetime(2026, 7, 1)
    )

    assert result["capabilities"]["future_supply"] is True
    assert result["future_supply"]["rows"] == 0
    assert result["future_supply"]["open_qty"] == Decimal("0")
    db_session.refresh(generation)
    assert generation.capabilities["future_supply"] is True
    batch = db_session.query(models.LedgerBuildBatch).filter_by(
        ledger_generation_id=int(generation.id),
        stage="future_supply_capture",
    ).one()
    assert batch.status == "completed"
    assert batch.algorithm_version == FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION
    assert batch.metrics["rows"] == 0
