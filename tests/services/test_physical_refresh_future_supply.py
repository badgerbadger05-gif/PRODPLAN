"""A physical refresh must not lose the accepted parent's future supply.

``accept_generation_build`` forks the physical prefix only.  It never captured
future supply and never cloned it, while the purchase-journal candidate reads
``ledger_future_supply`` by generation — so every three-hour refresh published a
journal reporting zero ordered and zero in transit.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.future_supply_capture import (
    FutureSupplyCaptureError,
    carry_forward_future_supply,
)
from app.services.item_ledger.generation_lifecycle import accept_generation_build

from tests.services.test_generation_lifecycle import _synthetic


CUTOFF = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)


def _accepted_parent_with_future_supply(db, key: str, item, *, qty="4"):
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
        stage="snapshot_build",
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
        supply_kind="wip_order",
        item_id=item.item_id,
        planning_stock_pool="default",
        destination_warehouse_ref1c="WH",
        source_ref=f"order-{key}",
        source_line_ref="1",
        ordered_qty_at_cutoff=Decimal(qty),
        realized_qty_at_cutoff=Decimal("0"),
        open_qty_at_cutoff=Decimal(qty),
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


def test_carry_forward_copies_the_parent_capture_verbatim(db_session):
    item = _item(db_session, "FS-CARRY")
    parent = _accepted_parent_with_future_supply(db_session, "carry", item)
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
    assert carried.open_qty_at_cutoff == Decimal("4")
    # The evidence was observed at the parent's cutoff and nothing re-read it,
    # so the capture instant travels with the row rather than being restamped.
    assert carried.capture_cutoff.replace(tzinfo=None) == CUTOFF.replace(tzinfo=None)
    assert carried.capture_batch_id is not None
    batch = db_session.get(models.LedgerBuildBatch, int(carried.capture_batch_id))
    assert int(batch.ledger_generation_id) == int(child.id)
    assert batch.status == "completed"


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


def test_a_generation_with_nothing_to_inherit_does_not_claim_future_supply(db_session):
    """Fail closed: no capture means unavailable, never a fabricated zero."""
    generation, _requirement = _synthetic(db_session, "fs-genesis")

    result = accept_generation_build(
        db_session, generation.id, replay_from=datetime(2026, 7, 1)
    )

    assert result["capabilities"]["future_supply"] is False
    assert result["future_supply"] is None
    db_session.refresh(generation)
    assert generation.capabilities["future_supply"] is False
