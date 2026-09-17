from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.future_supply_read import future_supply_model
from app.services.item_ledger.physical_refresh_provenance import (
    PhysicalRefreshProvenanceUnavailable,
    apply_bounded_current_material_custody_events,
    handoff_current_future_supply_provenance,
    handoff_current_material_custody_provenance,
)
from app.services.production_material_custody_projection import (
    _event_high_watermark_id_at_cutoff,
    _same_1c_timestamp,
    _require_manifest_cutoff,
    _resolve_projection_baseline,
    load_compact_current_material_custody,
)


def _world(db):
    cutoff = datetime(2026, 9, 15, tzinfo=timezone.utc)
    parent_batch = models.PhysicalImportBatch(
        batch_key="handoff-parent-batch", status="completed",
        source_watermarks={}, cutoff=cutoff,
    )
    target_batch = models.PhysicalImportBatch(
        batch_key="handoff-target-batch", status="completed",
        source_watermarks={}, cutoff=cutoff,
    )
    parent = models.LedgerGeneration(
        generation_key="handoff-parent", status="accepted", cutoff=cutoff,
        accepted_at=cutoff, source_watermarks={}, capabilities={},
        physical_import_batch=parent_batch, algorithm_version="test",
    )
    target = models.LedgerGeneration(
        generation_key="handoff-target", status="building", cutoff=cutoff,
        source_watermarks={"parent_generation_id": 1}, capabilities={},
        physical_import_batch=target_batch, algorithm_version="test",
    )
    item = models.Item(item_code="HANDOFF-ITEM", item_name="handoff")
    order = models.ProductionOrder(
        order_number="HANDOFF-ORDER", order_date=cutoff, order_ref1c="handoff-order",
    )
    db.add_all([parent_batch, target_batch, parent, target, item, order])
    db.flush()
    batch = models.LedgerBuildBatch(
        ledger_generation_id=parent.id, stage="future_supply_capture",
        batch_key="handoff-capture", status="completed", algorithm_version="test",
        metrics={},
    )
    product = models.ProductionProduct(
        order_id=order.order_id, item_id=item.item_id, quantity=Decimal("1"),
        remaining_qty=Decimal("1"), produced_qty=Decimal("0"),
    )
    db.add_all([batch, product])
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    current = models.LedgerFutureSupplyCurrent(
        current_identity="handoff:supply",
        source_generation_id=parent.id, source_capture_batch_id=batch.id,
        supply_kind="wip_order", item_id=item.item_id,
        planning_stock_pool="main", destination_warehouse_ref1c="WH",
        ordered_qty_at_cutoff=Decimal("4"), realized_qty_at_cutoff=Decimal("1"),
        open_qty_at_cutoff=Decimal("3"), source_state_key="open",
        capture_cutoff=cutoff, source_content_hash="a" * 64, evidence_status="exact",
    )
    custody = models.ProductionMaterialCustodyProjection(
        ledger_generation_id=parent.id, product_id=product.product_id,
        component_item_id=item.item_id, location_kind="workshop",
        warehouse_ref1c="WH", reserved_qty=Decimal("2"),
        source_event_high_watermark_id=0, is_current=True,
    )
    manifest = models.ProductionMaterialCustodyProjectionManifest(
        ledger_generation_id=parent.id, baseline_generation_id=parent.id,
        cutoff=cutoff, status="complete", source_event_high_watermark_id=0,
    )
    db.add_all([current, custody, manifest])
    db.flush()
    return parent, target, current, custody


def _building_generation(db, *, key: str, cutoff: datetime) -> models.LedgerGeneration:
    """One more BUILDING physical candidate for a consecutive bounded refresh."""
    batch = models.PhysicalImportBatch(
        batch_key=f"{key}-batch", status="completed",
        source_watermarks={}, cutoff=cutoff,
    )
    generation = models.LedgerGeneration(
        generation_key=key, status="building", cutoff=cutoff,
        source_watermarks={}, capabilities={},
        physical_import_batch=batch, algorithm_version="test",
    )
    db.add_all([batch, generation])
    db.flush()
    return generation


def _accept(db, generation: models.LedgerGeneration) -> None:
    generation.status = "accepted"
    generation.accepted_at = generation.cutoff
    db.get(models.PlanningTruthState, 1).current_generation_id = generation.id
    db.flush()


def test_custody_handoff_stamps_target_cutoff_for_the_next_bounded_generation(db_session):
    """Two consecutive bounded publications must leave a resolvable baseline."""
    parent, first, _current, _custody = _world(db_session)
    handoff_current_material_custody_provenance(
        db_session, parent_generation_id=parent.id, target_generation_id=first.id,
    )
    _accept(db_session, first)

    second = _building_generation(
        db_session, key="handoff-second", cutoff=first.cutoff + timedelta(hours=13),
    )
    handoff_current_material_custody_provenance(
        db_session, parent_generation_id=first.id, target_generation_id=second.id,
    )
    _accept(db_session, second)

    manifest = db_session.get(
        models.ProductionMaterialCustodyProjectionManifest, second.id
    )
    assert manifest is not None
    # The canonical gate every later candidate runs on its parent manifest.
    _require_manifest_cutoff(manifest, second)
    assert _same_1c_timestamp(manifest.cutoff, second.cutoff)
    assert not _same_1c_timestamp(manifest.cutoff, first.cutoff)

    third = _building_generation(
        db_session, key="handoff-third", cutoff=second.cutoff + timedelta(hours=1),
    )
    baseline_generation_id, baseline_manifest, baseline_generation = (
        _resolve_projection_baseline(
            db_session,
            generation=third,
            target_high_watermark_id=_event_high_watermark_id_at_cutoff(
                db_session, cutoff=third.cutoff,
            ),
        )
    )
    assert baseline_generation_id == int(second.id)
    assert int(baseline_manifest.ledger_generation_id) == int(second.id)
    assert _same_1c_timestamp(baseline_generation.cutoff, second.cutoff)


def test_custody_handoff_keeps_the_folded_watermark_on_the_target_manifest(db_session):
    parent, target, _current, custody = _world(db_session)
    target.cutoff = parent.cutoff + timedelta(hours=6)
    target.physical_import_batch.cutoff = target.cutoff
    db_session.flush()
    sle = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="watermark-tail-sle",
        business_identity="watermark-tail-sle",
        item_id=int(custody.component_item_id),
        characteristic_ref="",
        organization_ref="org",
        warehouse_ref1c="WH",
        qty=Decimal("1"),
        posting_at=parent.cutoff + timedelta(hours=1),
        record_type="Receipt",
        movement_kind="transfer_in",
        recorder_type="Document_Transfer",
        recorder_ref="watermark-tail",
        line_no="1",
        ingest_source="test",
    )
    db_session.add(sle)
    db_session.flush()
    event = models.ProductionMaterialCustodyEvent(
        product_id=int(custody.product_id),
        component_item_id=int(custody.component_item_id),
        source_kind="transfer_posted",
        source_sle_id=int(sle.id),
        effective_at=parent.cutoff + timedelta(hours=1),
        location_kind="workshop",
        warehouse_ref1c="WH",
        delta_qty=Decimal("1"),
        idempotency_key="watermark-tail-event",
    )
    db_session.add(event)
    db_session.flush()

    apply_bounded_current_material_custody_events(
        db_session,
        parent_generation_id=parent.id,
        target_generation_id=target.id,
        source_sle_ids=(int(sle.id),),
    )
    result = handoff_current_material_custody_provenance(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
    )
    _accept(db_session, target)

    manifest = db_session.get(
        models.ProductionMaterialCustodyProjectionManifest, target.id
    )
    _require_manifest_cutoff(manifest, target)
    expected = _event_high_watermark_id_at_cutoff(db_session, cutoff=target.cutoff)
    assert expected == int(event.id)
    assert int(manifest.source_event_high_watermark_id) == expected
    assert int(result.custody_event_watermark) == expected
    assert int(custody.source_event_high_watermark_id) == expected


def test_custody_handoff_refuses_stale_cell_watermark_and_backwards_cutoff(db_session):
    parent, target, _current, custody = _world(db_session)
    custody.source_event_high_watermark_id = 7
    db_session.flush()
    with pytest.raises(PhysicalRefreshProvenanceUnavailable, match="watermark is mixed or stale"):
        handoff_current_material_custody_provenance(
            db_session, parent_generation_id=parent.id, target_generation_id=target.id,
        )
    db_session.rollback()

    parent, target, _current, custody = _world(db_session)
    target.cutoff = parent.cutoff - timedelta(hours=1)
    db_session.flush()
    with pytest.raises(PhysicalRefreshProvenanceUnavailable, match="backwards"):
        handoff_current_material_custody_provenance(
            db_session, parent_generation_id=parent.id, target_generation_id=target.id,
        )


def test_future_supply_handoff_keeps_row_and_emits_no_change_audit(db_session):
    parent, target, current, _custody = _world(db_session)
    row_id = int(current.id)
    result = handoff_current_future_supply_provenance(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
    )
    assert result.future_supply_rows == 1
    assert int(current.id) == row_id
    assert int(current.source_generation_id) == int(target.id)
    assert db_session.query(models.LedgerFutureSupplyCurrentChange).count() == 0
    assert db_session.query(models.LedgerFutureSupply).count() == 0
    target.status = "accepted"
    target.accepted_at = target.cutoff
    db_session.get(models.PlanningTruthState, 1).current_generation_id = target.id
    db_session.flush()
    repeated = handoff_current_future_supply_provenance(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
    )
    assert repeated.future_supply_idempotent is True


def test_custody_handoff_keeps_projection_id_and_current_loader_after_pointer(db_session):
    parent, target, _current, custody = _world(db_session)
    row_id = int(custody.id)
    result = handoff_current_material_custody_provenance(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
    )
    assert result.custody_rows == 1
    assert int(custody.id) == row_id
    assert int(custody.ledger_generation_id) == int(target.id)
    assert db_session.get(models.ProductionMaterialCustodyProjectionManifest, parent.id) is None
    assert db_session.get(models.ProductionMaterialCustodyProjectionManifest, target.id) is not None
    target.status = "accepted"
    target.accepted_at = target.cutoff
    db_session.get(models.PlanningTruthState, 1).current_generation_id = target.id
    db_session.flush()
    generation_id, state = load_compact_current_material_custody(
        db_session, consumer="handoff-test"
    )
    assert generation_id == int(target.id)
    assert state.by_warehouse_item[("WH", int(custody.component_item_id))] == 2.0
    repeated = handoff_current_material_custody_provenance(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
    )
    assert repeated.custody_idempotent is True


def test_handoff_rejects_event_tail_and_mixed_future_owner(db_session):
    parent, target, current, _custody = _world(db_session)
    current.source_generation_id = target.id
    db_session.flush()
    with pytest.raises(PhysicalRefreshProvenanceUnavailable, match="mixed|stale"):
        handoff_current_future_supply_provenance(
            db_session, parent_generation_id=parent.id, target_generation_id=target.id,
        )


def test_custody_handoff_rejects_unpublished_event_tail(db_session):
    parent, target, _current, custody = _world(db_session)
    db_session.add(models.ProductionMaterialCustodyEvent(
        product_id=int(custody.product_id),
        component_item_id=int(custody.component_item_id),
        source_kind="issue_created", effective_at=parent.cutoff,
        location_kind="workshop", warehouse_ref1c="WH", delta_qty=Decimal("1"),
        idempotency_key="handoff-tail",
    ))
    db_session.flush()
    with pytest.raises(PhysicalRefreshProvenanceUnavailable, match="event tail"):
        handoff_current_material_custody_provenance(
            db_session, parent_generation_id=parent.id, target_generation_id=target.id,
        )


def test_bounded_custody_tail_folds_only_explicit_sle_and_handoff_reuses_it(db_session):
    parent, target, _current, custody = _world(db_session)
    sle = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="custody-tail-sle",
        business_identity="custody-tail-sle",
        item_id=int(custody.component_item_id),
        characteristic_ref="",
        organization_ref="org",
        warehouse_ref1c="WH",
        qty=Decimal("1"),
        posting_at=target.cutoff,
        record_type="Receipt",
        movement_kind="transfer_in",
        recorder_type="Document_Transfer",
        recorder_ref="custody-tail",
        line_no="1",
        ingest_source="test",
    )
    db_session.add(sle)
    db_session.flush()
    event = models.ProductionMaterialCustodyEvent(
        product_id=int(custody.product_id),
        component_item_id=int(custody.component_item_id),
        source_kind="transfer_posted",
        source_sle_id=int(sle.id),
        effective_at=target.cutoff,
        location_kind="workshop",
        warehouse_ref1c="WH",
        delta_qty=Decimal("1"),
        idempotency_key="bounded-custody-tail",
    )
    db_session.add(event)
    db_session.flush()

    folded = apply_bounded_current_material_custody_events(
        db_session,
        parent_generation_id=parent.id,
        target_generation_id=target.id,
        source_sle_ids=(int(sle.id),),
    )
    assert folded == 1
    assert custody.reserved_qty == Decimal("3")
    assert custody.source_event_high_watermark_id == event.id
    result = handoff_current_material_custody_provenance(
        db_session,
        parent_generation_id=parent.id,
        target_generation_id=target.id,
    )
    assert result.custody_event_watermark == int(event.id)
    assert db_session.get(
        models.ProductionMaterialCustodyProjectionManifest, target.id
    ).source_event_high_watermark_id == event.id


def test_bounded_custody_tail_rejects_event_outside_explicit_sle_manifest(db_session):
    parent, target, _current, custody = _world(db_session)
    db_session.add(models.ProductionMaterialCustodyEvent(
        product_id=int(custody.product_id),
        component_item_id=int(custody.component_item_id),
        source_kind="transfer_posted",
        source_sle_id=999999,
        effective_at=target.cutoff,
        location_kind="workshop",
        warehouse_ref1c="WH",
        delta_qty=Decimal("1"),
        idempotency_key="bounded-custody-foreign-tail",
    ))
    db_session.flush()
    with pytest.raises(PhysicalRefreshProvenanceUnavailable, match="not covered"):
        apply_bounded_current_material_custody_events(
            db_session,
            parent_generation_id=parent.id,
            target_generation_id=target.id,
            source_sle_ids=(),
        )
