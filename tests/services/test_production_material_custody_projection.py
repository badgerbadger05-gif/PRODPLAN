from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    Item,
    LedgerGeneration,
    PhysicalImportBatch,
    ProductionMaterialCustodyEvent,
    ProductionMaterialIssue,
    ProductionOrder,
    ProductionMaterialCustodyProjectionManifest,
    ProductionMaterialCustodyProjection,
    ProductionProduct,
    ProductionOrderLineState,
    StockLedgerEntry,
    StockLedgerFactSupersession,
    Specification,
    SpecComponent,
)
from app.services.production_material_custody_projection import (
    MaterialCustodySnapshotUnavailable,
    build_material_custody_projection,
    _same_1c_timestamp,
    load_current_accepted_material_custody,
    load_material_custody_projection,
)
from app.services.planning_truth import publish_generation


def test_same_1c_timestamp_accepts_postgres_aware_and_legacy_naive_wall_time():
    naive = datetime(2026, 7, 2, 15, 49, 34)
    aware = datetime(2026, 7, 2, 15, 49, 34, tzinfo=timezone(timedelta(hours=3)))

    assert _same_1c_timestamp(naive, aware)
    assert not _same_1c_timestamp(naive, aware.replace(minute=50))


def _generation(db, *, key: str, cutoff: datetime) -> LedgerGeneration:
    batch = PhysicalImportBatch(
        batch_key=key,
        status="completed",
        cutoff=cutoff,
        completed_at=cutoff,
        source_watermarks={"explicit_empty_prefix": True},
    )
    generation = LedgerGeneration(
        generation_key=key,
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        source_watermarks={"explicit_empty_prefix": True},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
        },
        physical_import_batch=batch,
        algorithm_version="test",
        replay_version="test",
    )
    db.add_all([batch, generation])
    db.flush()
    publish_generation(db, generation)
    db.flush()
    return generation


def _manifest(
    db,
    *,
    generation_id: int,
    source_event_high_watermark_id: int,
    status: str = "complete",
) -> ProductionMaterialCustodyProjectionManifest:
    generation = db.get(LedgerGeneration, int(generation_id))
    assert generation is not None and generation.cutoff is not None
    manifest = ProductionMaterialCustodyProjectionManifest(
        ledger_generation_id=int(generation_id),
        cutoff=generation.cutoff,
        status=status,
        source_event_high_watermark_id=int(source_event_high_watermark_id),
    )
    db.add(manifest)
    db.flush()
    return manifest


def _product(db, *, item_code: str = "PRD") -> tuple[ProductionProduct, Item, Item]:
    parent = Item(
        item_code=f"{item_code}-P",
        item_name=f"{item_code} parent",
        item_article=f"{item_code}-PA",
                unit="шт",
        status="active",
    )
    component = Item(
        item_code=f"{item_code}-C",
        item_name=f"{item_code} component",
        item_article=f"{item_code}-CA",
        unit="шт",
                status="active",
    )
    db.add_all([parent, component])
    db.flush()
    spec = Specification(spec_name=f"{item_code} spec", spec_ref1c=f"spec-{item_code}")
    db.add(spec)
    db.flush()
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=1))
    db.flush()

    order = ProductionOrder(
        order_number=f"{item_code}-ORD",
        order_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
        source="1c",
        deletion_mark=False,
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=1,
        quantity=8,
        produced_qty=0,
        remaining_qty=8,
    )
    db.add(product)
    db.flush()
    db.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="shortage",
            issue_status="not_requested",
        )
    )
    db.flush()

    return product, parent, component


def _seed_projection(
    db,
    *,
    generation_id: int,
    product_id: int,
    component_id: int,
    qty: float,
    source_event_high_watermark_id: int,
) -> None:
    db.add(
        ProductionMaterialCustodyProjection(
            ledger_generation_id=int(generation_id),
            product_id=int(product_id),
            component_item_id=int(component_id),
            location_kind="workshop",
            warehouse_ref1c="WH-MAIN",
            reserved_qty=float(qty),
            source_event_high_watermark_id=int(source_event_high_watermark_id),
        )
    )


def _event(
    db,
    *,
    source_kind: str,
    issue_id: int,
    product_id: int,
    component_id: int,
    location: str,
    warehouse: str,
    qty: float,
    key: str,
    effective_at: datetime,
    source_sle_id: int | None = None,
) -> None:
    db.add(
        ProductionMaterialCustodyEvent(
            issue_id=int(issue_id),
            product_id=int(product_id),
            component_item_id=int(component_id),
            source_kind=str(source_kind),
            source_sle_id=source_sle_id,
            effective_at=effective_at,
            location_kind=str(location),
            warehouse_ref1c=warehouse,
            delta_qty=float(qty),
            idempotency_key=str(key),
            document_number="DOC",
            document_line_no="1",
        )
    )


def test_current_accepted_custody_folds_local_events_after_cutoff(db_session):
    cutoff = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
    generation = _generation(db_session, key="custody-live-tail", cutoff=cutoff)
    product, _parent, component = _product(db_session, item_code="LIVE")
    issue = ProductionMaterialIssue(
        document_number="MT-LIVE",
        product_id=product.product_id,
        order_id=product.order_id,
        status="draft",
        direction="issue",
        warehouse_ref1c="WH-DST",
        source_warehouse_ref1c="WH-SRC",
        ledger_generation_id=generation.id,
    )
    db_session.add(issue)
    db_session.flush()
    manifest = _manifest(db_session, generation_id=generation.id, source_event_high_watermark_id=0)
    manifest.is_baseline = True
    _event(
        db_session,
        source_kind="issue_created",
        issue_id=issue.issue_id,
        product_id=product.product_id,
        component_id=component.item_id,
        location="transit",
        warehouse="WH-SRC",
        qty=5,
        key="custody:live:created",
        effective_at=cutoff + timedelta(minutes=1),
    )
    db_session.commit()

    generation_id, state = load_current_accepted_material_custody(
        db_session,
        consumer="test.live_tail",
    )

    assert generation_id == generation.id
    assert state.for_product(product.product_id).in_transit[component.item_id] == 5
    assert state.reserved_at_warehouse("WH-SRC", component.item_id) == 5


def test_current_accepted_custody_ignores_unaccepted_physical_tail(db_session):
    cutoff = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
    generation = _generation(db_session, key="custody-live-physical", cutoff=cutoff)
    product, _parent, component = _product(db_session, item_code="PHY")
    issue = ProductionMaterialIssue(
        document_number="MT-PHY",
        product_id=product.product_id,
        order_id=product.order_id,
        status="draft",
        direction="issue",
        warehouse_ref1c="WH-DST",
        source_warehouse_ref1c="WH-SRC",
        ledger_generation_id=generation.id,
    )
    db_session.add(issue)
    db_session.flush()
    manifest = _manifest(db_session, generation_id=generation.id, source_event_high_watermark_id=0)
    manifest.is_baseline = True
    _event(
        db_session,
        source_kind="issue_created",
        issue_id=issue.issue_id,
        product_id=product.product_id,
        component_id=component.item_id,
        location="transit",
        warehouse="WH-SRC",
        qty=5,
        key="custody:physical:created",
        effective_at=cutoff + timedelta(minutes=1),
    )
    db_session.flush()
    sle = StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash="f" * 64,
        item_id=component.item_id,
        characteristic_ref="",
        organization_ref="",
        warehouse_ref1c="WH-SRC",
        qty=-5,
        qty_after=0,
        posting_at=cutoff + timedelta(minutes=2),
        record_type="Expense",
        movement_kind="transfer_out",
        recorder_type="Document_Transfer",
        recorder_ref="transfer-live",
        line_no="1",
        ingest_source="pull",
    )
    db_session.add(sle)
    db_session.flush()
    _event(
        db_session,
        source_kind="transfer_posted",
        issue_id=issue.issue_id,
        product_id=product.product_id,
        component_id=component.item_id,
        location="transit",
        warehouse="WH-SRC",
        qty=-5,
        key="custody:physical:posted",
        effective_at=cutoff + timedelta(minutes=2),
        source_sle_id=sle.id,
    )
    db_session.commit()

    _generation_id, state = load_current_accepted_material_custody(
        db_session,
        consumer="test.live_physical_tail",
    )

    assert state.for_product(product.product_id).in_transit[component.item_id] == 5
    assert state.reserved_at_warehouse("WH-SRC", component.item_id) == 5


def test_projection_folds_from_baseline(db_session):
    base = _generation(db_session, key="custody-proj-base", cutoff=datetime(2026, 7, 1, tzinfo=timezone.utc))
    target = _generation(
        db_session,
        key="custody-proj-target",
        cutoff=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    product, _parent, component = _product(db_session, item_code="FOLD")

    _manifest(db_session, generation_id=base.id, source_event_high_watermark_id=0)
    _seed_projection(
        db_session,
        generation_id=base.id,
        product_id=product.product_id,
        component_id=component.item_id,
        qty=4.0,
        source_event_high_watermark_id=0,
    )

    issue = ProductionMaterialIssue(
        document_number="MT-001",
        product_id=product.product_id,
        order_id=product.order_id,
        status="posted",
        direction="issue",
        warehouse_ref1c="WH-MAIN",
        source_warehouse_ref1c="WH-SRC",
    )
    db_session.add(issue)
    db_session.flush()
    _event(
        db_session,
        source_kind="issue_created",
        issue_id=issue.issue_id,
        product_id=product.product_id,
        component_id=component.item_id,
        location="workshop",
        warehouse="WH-MAIN",
        qty=2.5,
        key="custody:event:target:1",
        effective_at=target.cutoff,
    )
    _manifest(db_session, generation_id=target.id, source_event_high_watermark_id=1)
    db_session.commit()

    state = load_material_custody_projection(db_session, ledger_generation_id=target.id)
    assert state.for_product(int(product.product_id)).at_workshop[int(component.item_id)] == pytest.approx(6.5)
    assert state.by_warehouse_item[("WH-MAIN", component.item_id)] == pytest.approx(6.5)

    repeat = load_material_custody_projection(db_session, ledger_generation_id=target.id)
    assert (
        repeat.for_product(int(product.product_id)).at_workshop[int(component.item_id)]
        == pytest.approx(6.5)
    )


def test_projection_orders_late_recovered_issue_opening_before_same_time_transfer(
    db_session,
):
    """A recovery append must not be folded after its already-recorded SLE event."""
    base = _generation(
        db_session,
        key="custody-proj-recovery-base",
        cutoff=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )
    target = _generation(
        db_session,
        key="custody-proj-recovery-target",
        cutoff=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )
    product, _parent, component = _product(db_session, item_code="RECOVERY")
    issue = ProductionMaterialIssue(
        document_number="MT-RECOVERY-1",
        product_id=product.product_id,
        order_id=product.order_id,
        status="posted",
        direction="issue",
        warehouse_ref1c="WH-DEST",
        source_warehouse_ref1c="WH-SRC",
    )
    db_session.add(issue)
    db_session.flush()
    posted_at = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    outbound = StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="a" * 64,
        item_id=component.item_id,
        warehouse_ref1c="WH-SRC",
        qty=-93,
        posting_at=posted_at,
        movement_kind="transfer_out",
        recorder_type="Document_ПеремещениеЗапасов",
        recorder_ref="transfer-recovery",
        line_no="1",
    )
    inbound = StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="b" * 64,
        item_id=component.item_id,
        warehouse_ref1c="WH-DEST",
        qty=93,
        posting_at=posted_at,
        movement_kind="transfer_in",
        recorder_type="Document_ПеремещениеЗапасов",
        recorder_ref="transfer-recovery",
        line_no="2",
    )
    db_session.add_all([outbound, inbound])
    db_session.flush()
    # These reproduce historical rows 1 and 2.  The opening is deliberately
    # appended last (row 3), yet has the exact same 1C timestamp.
    _event(
        db_session, source_kind="transfer_posted", issue_id=issue.issue_id,
        product_id=product.product_id, component_id=component.item_id,
        location="transit", warehouse="WH-SRC", qty=-93,
        key="custody:event:recovery:out", effective_at=posted_at,
        source_sle_id=outbound.id,
    )
    _event(
        db_session, source_kind="transfer_posted", issue_id=issue.issue_id,
        product_id=product.product_id, component_id=component.item_id,
        location="workshop", warehouse="WH-DEST", qty=93,
        key="custody:event:recovery:in", effective_at=posted_at,
        source_sle_id=inbound.id,
    )
    _event(
        db_session, source_kind="issue_created", issue_id=issue.issue_id,
        product_id=product.product_id, component_id=component.item_id,
        location="transit", warehouse="WH-SRC", qty=93,
        key="custody:event:recovery:opening", effective_at=posted_at,
    )
    _manifest(db_session, generation_id=base.id, source_event_high_watermark_id=0)
    _manifest(db_session, generation_id=target.id, source_event_high_watermark_id=3)
    db_session.commit()

    state = load_material_custody_projection(db_session, ledger_generation_id=target.id)
    custody = state.for_product(int(product.product_id))
    assert custody.in_transit == {}
    assert custody.at_workshop[int(component.item_id)] == pytest.approx(93.0)


def test_projection_orders_issue_opening_before_a_subsecond_later_transfer(
    db_session,
):
    """1C publishes whole seconds; the opening carries microseconds.

    The shadow stand froze on exactly this: an issue created at 10:50:28.269806
    was exported and posted in 1C at 10:50:28, so the transfer looked 0.27 s
    older than the reservation it consumes.  The fold went negative, the whole
    Ledger refresh failed closed, and with it every planning consumer.
    """
    base = _generation(
        db_session,
        key="custody-proj-subsecond-base",
        cutoff=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )
    target = _generation(
        db_session,
        key="custody-proj-subsecond-target",
        cutoff=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )
    product, _parent, component = _product(db_session, item_code="SUBSECOND")
    issue = ProductionMaterialIssue(
        document_number="MT-SUBSECOND-1",
        product_id=product.product_id,
        order_id=product.order_id,
        status="posted",
        direction="issue",
        warehouse_ref1c="WH-DEST",
        source_warehouse_ref1c="WH-SRC",
    )
    db_session.add(issue)
    db_session.flush()
    posted_at = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    created_at = posted_at + timedelta(microseconds=269806)
    outbound = StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="c" * 64,
        item_id=component.item_id,
        warehouse_ref1c="WH-SRC",
        qty=-20,
        posting_at=posted_at,
        movement_kind="transfer_out",
        recorder_type="Document_ПеремещениеЗапасов",
        recorder_ref="transfer-subsecond",
        line_no="1",
    )
    inbound = StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="d" * 64,
        item_id=component.item_id,
        warehouse_ref1c="WH-DEST",
        qty=20,
        posting_at=posted_at,
        movement_kind="transfer_in",
        recorder_type="Document_ПеремещениеЗапасов",
        recorder_ref="transfer-subsecond",
        line_no="2",
    )
    db_session.add_all([outbound, inbound])
    db_session.flush()
    # The opening is stamped by PRODPLAN with microseconds, the physical events
    # by 1C with whole seconds — so the opening looks the *newest* of the three.
    _event(
        db_session, source_kind="issue_created", issue_id=issue.issue_id,
        product_id=product.product_id, component_id=component.item_id,
        location="transit", warehouse="WH-SRC", qty=20,
        key="custody:event:subsecond:opening", effective_at=created_at,
    )
    _event(
        db_session, source_kind="transfer_posted", issue_id=issue.issue_id,
        product_id=product.product_id, component_id=component.item_id,
        location="transit", warehouse="WH-SRC", qty=-20,
        key="custody:event:subsecond:out", effective_at=posted_at,
        source_sle_id=outbound.id,
    )
    _event(
        db_session, source_kind="transfer_posted", issue_id=issue.issue_id,
        product_id=product.product_id, component_id=component.item_id,
        location="workshop", warehouse="WH-DEST", qty=20,
        key="custody:event:subsecond:in", effective_at=posted_at,
        source_sle_id=inbound.id,
    )
    _manifest(db_session, generation_id=base.id, source_event_high_watermark_id=0)
    _manifest(db_session, generation_id=target.id, source_event_high_watermark_id=3)
    db_session.commit()

    state = load_material_custody_projection(db_session, ledger_generation_id=target.id)
    custody = state.for_product(int(product.product_id))
    assert custody.in_transit == {}
    assert custody.at_workshop[int(component.item_id)] == pytest.approx(20.0)


def test_projection_ignores_late_duplicate_events_from_exact_sle_reimport(db_session):
    base = _generation(
        db_session, key="custody-proj-reimport-base",
        cutoff=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )
    target = _generation(
        db_session, key="custody-proj-reimport-target",
        cutoff=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )
    product, _parent, component = _product(db_session, item_code="REIMPORT")
    issue = ProductionMaterialIssue(
        document_number="MT-REIMPORT-1", product_id=product.product_id,
        order_id=product.order_id, status="posted", direction="issue",
        warehouse_ref1c="WH-DEST", source_warehouse_ref1c="WH-SRC",
    )
    db_session.add(issue)
    db_session.flush()
    posted_at = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

    def sle(*, batch_id, warehouse, qty, line_no):
        row = StockLedgerEntry(
            ingest_batch_id=batch_id, source_content_hash="c" * 64,
            item_id=component.item_id, warehouse_ref1c=warehouse, qty=qty,
            posting_at=posted_at, movement_kind=("transfer_out" if qty < 0 else "transfer_in"),
            recorder_type="Document_ПеремещениеЗапасов", recorder_ref="transfer-reimport",
            line_no=line_no,
        )
        db_session.add(row)
        db_session.flush()
        return row

    old_out = sle(batch_id=base.physical_import_batch_id, warehouse="WH-SRC", qty=-93, line_no="1")
    old_in = sle(batch_id=base.physical_import_batch_id, warehouse="WH-DEST", qty=93, line_no="2")
    new_out = sle(batch_id=target.physical_import_batch_id, warehouse="WH-SRC", qty=-93, line_no="1")
    new_in = sle(batch_id=target.physical_import_batch_id, warehouse="WH-DEST", qty=93, line_no="2")
    db_session.add_all([
        StockLedgerFactSupersession(old_sle_id=old_out.id, new_sle_id=new_out.id, import_batch_id=target.physical_import_batch_id),
        StockLedgerFactSupersession(old_sle_id=old_in.id, new_sle_id=new_in.id, import_batch_id=target.physical_import_batch_id),
    ])
    _event(db_session, source_kind="transfer_posted", issue_id=issue.issue_id, product_id=product.product_id, component_id=component.item_id, location="transit", warehouse="WH-SRC", qty=-93, key="custody:event:reimport:old-out", effective_at=posted_at, source_sle_id=old_out.id)
    _event(db_session, source_kind="transfer_posted", issue_id=issue.issue_id, product_id=product.product_id, component_id=component.item_id, location="workshop", warehouse="WH-DEST", qty=93, key="custody:event:reimport:old-in", effective_at=posted_at, source_sle_id=old_in.id)
    _event(db_session, source_kind="issue_created", issue_id=issue.issue_id, product_id=product.product_id, component_id=component.item_id, location="transit", warehouse="WH-SRC", qty=93, key="custody:event:reimport:opening", effective_at=posted_at)
    _event(db_session, source_kind="transfer_posted", issue_id=issue.issue_id, product_id=product.product_id, component_id=component.item_id, location="transit", warehouse="WH-SRC", qty=-93, key="custody:event:reimport:new-out", effective_at=posted_at, source_sle_id=new_out.id)
    _event(db_session, source_kind="transfer_posted", issue_id=issue.issue_id, product_id=product.product_id, component_id=component.item_id, location="workshop", warehouse="WH-DEST", qty=93, key="custody:event:reimport:new-in", effective_at=posted_at, source_sle_id=new_in.id)
    _seed_projection(db_session, generation_id=base.id, product_id=product.product_id, component_id=component.item_id, qty=93, source_event_high_watermark_id=3)
    _manifest(db_session, generation_id=base.id, source_event_high_watermark_id=3)
    _manifest(db_session, generation_id=target.id, source_event_high_watermark_id=5)
    db_session.commit()

    state = load_material_custody_projection(db_session, ledger_generation_id=target.id)
    assert state.for_product(int(product.product_id)).at_workshop[int(component.item_id)] == pytest.approx(93.0)


def test_projection_fails_when_no_baseline_can_place_a_late_event(db_session):
    """With nothing older to fold from, a late-dated event is a real break."""
    base = _generation(
        db_session,
        key="custody-proj-base-stale",
        cutoff=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )
    target = _generation(
        db_session,
        key="custody-proj-target-stale",
        cutoff=datetime(2026, 7, 11, tzinfo=timezone.utc),
    )
    product, _parent, component = _product(db_session, item_code="STALE")

    issue = ProductionMaterialIssue(
        document_number="MT-STALE-1",
        product_id=product.product_id,
        order_id=product.order_id,
        status="posted",
        direction="issue",
        warehouse_ref1c="WH-MAIN",
        source_warehouse_ref1c="WH-SRC",
    )
    db_session.add(issue)
    db_session.flush()

    _manifest(db_session, generation_id=base.id, source_event_high_watermark_id=1)
    _event(
        db_session,
        source_kind="issue_created",
        issue_id=issue.issue_id,
        product_id=product.product_id,
        component_id=component.item_id,
        location="workshop",
        warehouse="WH-MAIN",
        qty=1.0,
        key="custody:event:stale-baseline",
        effective_at=base.cutoff,
    )

    _event(
        db_session,
        source_kind="issue_created",
        issue_id=issue.issue_id,
        product_id=product.product_id,
        component_id=component.item_id,
        location="workshop",
        warehouse="WH-MAIN",
        qty=1.0,
        key="custody:event:stale-fallback",
        effective_at=base.cutoff,
    )
    _manifest(db_session, generation_id=target.id, source_event_high_watermark_id=2)
    db_session.commit()

    with pytest.raises(MaterialCustodySnapshotUnavailable) as caught:
        load_material_custody_projection(db_session, ledger_generation_id=target.id)
    assert "behind every available baseline" in caught.value.detail["reason"]


def test_projection_rewinds_to_an_older_baseline_for_a_late_event(db_session):
    """A movement projected after its own posting must not stop the Ledger.

    The custody events of a transfer are appended when the recorder is pulled,
    which can be long after the transfer was posted.  Judged against the newest
    baseline such an event looks impossible — it is dated inside a window that
    was already closed — and the refresh failed closed, so no new generation
    could be published at all and planning truth went stale behind it.  An
    older baseline still covers it: fold from there instead.
    """
    old_base = _generation(
        db_session,
        key="custody-rewind-old-base",
        cutoff=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    recent_base = _generation(
        db_session,
        key="custody-rewind-recent-base",
        cutoff=datetime(2026, 7, 25, tzinfo=timezone.utc),
    )
    target = _generation(
        db_session,
        key="custody-rewind-target",
        cutoff=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    product, _parent, component = _product(db_session, item_code="REWIND")
    issue = ProductionMaterialIssue(
        document_number="MT-REWIND-1",
        product_id=product.product_id,
        order_id=product.order_id,
        status="posted",
        direction="issue",
        warehouse_ref1c="WH-MAIN",
        source_warehouse_ref1c="WH-SRC",
    )
    db_session.add(issue)
    db_session.flush()

    _manifest(db_session, generation_id=old_base.id, source_event_high_watermark_id=0)
    # Posted between the two baselines, but projected only now: its id lands
    # above the newer baseline's watermark while its date lands below its cutoff.
    _event(
        db_session,
        source_kind="issue_created",
        issue_id=issue.issue_id,
        product_id=product.product_id,
        component_id=component.item_id,
        location="workshop",
        warehouse="WH-MAIN",
        qty=4.0,
        key="custody:event:rewind-late",
        effective_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    _manifest(
        db_session, generation_id=recent_base.id, source_event_high_watermark_id=0
    )
    db_session.commit()

    target.status = "building"          # the builder runs on a candidate
    db_session.flush()
    result = build_material_custody_projection(
        db_session, ledger_generation_id=target.id
    )
    target.status = "accepted"
    db_session.commit()

    assert result["valid"] is True
    # Folded from the older baseline, not from the one the event slipped behind.
    assert result["baseline_generation_id"] == int(old_base.id)
    state = load_material_custody_projection(
        db_session, ledger_generation_id=target.id
    )
    assert state.for_product(int(product.product_id)).at_workshop[
        int(component.item_id)
    ] == pytest.approx(4.0)


def test_projection_read_refolds_when_the_watermark_moved_under_it(db_session):
    """A stored projection is a cache, not an answer that may go dark.

    An event appended after the projection was built moves the visible
    watermark; refusing to read then took every material-coverage consumer down
    with it.  The read folds again instead.
    """
    base = _generation(
        db_session,
        key="custody-refold-base",
        cutoff=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    target = _generation(
        db_session,
        key="custody-refold-target",
        cutoff=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    product, _parent, component = _product(db_session, item_code="REFOLD")
    issue = ProductionMaterialIssue(
        document_number="MT-REFOLD-1",
        product_id=product.product_id,
        order_id=product.order_id,
        status="posted",
        direction="issue",
        warehouse_ref1c="WH-MAIN",
        source_warehouse_ref1c="WH-SRC",
    )
    db_session.add(issue)
    db_session.flush()
    _manifest(db_session, generation_id=base.id, source_event_high_watermark_id=0)
    _event(
        db_session,
        source_kind="issue_created",
        issue_id=issue.issue_id,
        product_id=product.product_id,
        component_id=component.item_id,
        location="workshop",
        warehouse="WH-MAIN",
        qty=3.0,
        key="custody:event:refold-known",
        effective_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
    )
    db_session.commit()
    target.status = "building"
    db_session.flush()
    build_material_custody_projection(db_session, ledger_generation_id=target.id)
    target.status = "accepted"
    db_session.commit()

    # Appended after the projection was built, dated inside its window.
    _event(
        db_session,
        source_kind="issue_created",
        issue_id=issue.issue_id,
        product_id=product.product_id,
        component_id=component.item_id,
        location="workshop",
        warehouse="WH-MAIN",
        qty=2.0,
        key="custody:event:refold-late",
        effective_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
    )
    db_session.commit()

    state = load_material_custody_projection(
        db_session, ledger_generation_id=target.id
    )

    assert state.for_product(int(product.product_id)).at_workshop[
        int(component.item_id)
    ] == pytest.approx(5.0)


def test_projection_replays_event_by_time_even_if_its_id_is_not_after_baseline_watermark(db_session):
    base = _generation(
        db_session,
        key="custody-proj-base-time-order",
        cutoff=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
    )
    target = _generation(
        db_session,
        key="custody-proj-target-time-order",
        cutoff=datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
    )
    product, _parent, component = _product(db_session, item_code="TIMEORDER")

    issue = ProductionMaterialIssue(
        document_number="MT-005",
        product_id=product.product_id,
        order_id=product.order_id,
        status="posted",
        direction="issue",
        warehouse_ref1c="WH-MAIN",
        source_warehouse_ref1c="WH-SRC",
    )
    db_session.add(issue)
    db_session.flush()

    _event(
        db_session,
        source_kind="issue_created",
        issue_id=issue.issue_id,
        product_id=product.product_id,
        component_id=component.item_id,
        location="workshop",
        warehouse="WH-MAIN",
        qty=1.0,
        key="custody:event:time-order:newer",
        effective_at=datetime(2026, 7, 13, 18, 0, tzinfo=timezone.utc),
    )
    _event(
        db_session,
        source_kind="issue_created",
        issue_id=issue.issue_id,
        product_id=product.product_id,
        component_id=component.item_id,
        location="workshop",
        warehouse="WH-MAIN",
        qty=1.0,
        key="custody:event:time-order:baseline",
        effective_at=datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc),
    )

    _manifest(db_session, generation_id=base.id, source_event_high_watermark_id=2)
    _manifest(db_session, generation_id=target.id, source_event_high_watermark_id=2)
    db_session.commit()

    state = load_material_custody_projection(db_session, ledger_generation_id=target.id)
    assert (
        state.for_product(int(product.product_id)).at_workshop[int(component.item_id)]
        == pytest.approx(1.0)
    )


def test_projection_reader_fails_without_baseline(db_session):
    base = _generation(db_session, key="custody-proj-base-missing", cutoff=datetime(2026, 7, 3, tzinfo=timezone.utc))
    target = _generation(
        db_session,
        key="custody-proj-target-missing",
        cutoff=datetime(2026, 7, 4, tzinfo=timezone.utc),
    )
    product, _parent, component = _product(db_session, item_code="MISS")

    issue = ProductionMaterialIssue(
        document_number="MT-002",
        product_id=product.product_id,
        order_id=product.order_id,
        status="posted",
        direction="issue",
        warehouse_ref1c="WH-MAIN",
        source_warehouse_ref1c="WH-SRC",
    )
    db_session.add(issue)
    db_session.flush()
    _event(
        db_session,
        source_kind="issue_created",
        issue_id=issue.issue_id,
        product_id=product.product_id,
        component_id=component.item_id,
        location="workshop",
        warehouse="WH-MAIN",
        qty=1.0,
        key="custody:event:target:2",
        effective_at=target.cutoff,
    )

    _manifest(db_session, generation_id=target.id, source_event_high_watermark_id=1)
    db_session.commit()
    with pytest.raises(MaterialCustodySnapshotUnavailable) as caught:
        load_material_custody_projection(db_session, ledger_generation_id=target.id)
    detail = caught.value.as_dict()
    assert detail["code"] == "material_custody_snapshot_unavailable"
    assert "baseline" in str(detail["reason"])


def test_projection_reader_accepts_empty_baseline_projection(db_session):
    base = _generation(
        db_session,
        key="custody-proj-empty-base",
        cutoff=datetime(2026, 7, 11, tzinfo=timezone.utc),
    )
    target = _generation(
        db_session,
        key="custody-proj-empty-target",
        cutoff=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )
    product, _parent, component = _product(db_session, item_code="EMPTY")
    _manifest(db_session, generation_id=base.id, source_event_high_watermark_id=0)
    _manifest(db_session, generation_id=target.id, source_event_high_watermark_id=0)
    db_session.commit()

    state = load_material_custody_projection(db_session, ledger_generation_id=target.id)
    assert state.for_product(int(product.product_id)).at_workshop == {}
    assert state.for_product(int(product.product_id)).in_transit == {}
    assert state.by_warehouse_item == {}


def test_projection_reader_fails_without_prior_manifest_even_if_empty(db_session):
    base = _generation(db_session, key="custody-proj-base2", cutoff=datetime(2026, 7, 5, tzinfo=timezone.utc))
    target = _generation(
        db_session,
        key="custody-proj-target2",
        cutoff=datetime(2026, 7, 6, tzinfo=timezone.utc),
    )
    product, _parent, component = _product(db_session, item_code="NOBAS")
    _seed_projection(
        db_session,
        generation_id=base.id,
        product_id=product.product_id,
        component_id=component.item_id,
        qty=0.0,
        source_event_high_watermark_id=0,
    )
    _manifest(db_session, generation_id=target.id, source_event_high_watermark_id=0)

    with pytest.raises(MaterialCustodySnapshotUnavailable):
        load_material_custody_projection(db_session, ledger_generation_id=target.id)


def test_projection_rejects_negative_workshop_hold(db_session):
    base = _generation(
        db_session,
        key="custody-proj-base-negative",
        cutoff=datetime(2026, 7, 7, tzinfo=timezone.utc),
    )
    target = _generation(
        db_session,
        key="custody-proj-target-negative",
        cutoff=datetime(2026, 7, 8, tzinfo=timezone.utc),
    )
    product, _parent, component = _product(db_session, item_code="NEG")
    _manifest(db_session, generation_id=base.id, source_event_high_watermark_id=0)
    _seed_projection(
        db_session,
        generation_id=base.id,
        product_id=product.product_id,
        component_id=component.item_id,
        qty=1.0,
        source_event_high_watermark_id=0,
    )

    issue = ProductionMaterialIssue(
        document_number="MT-003",
        product_id=product.product_id,
        order_id=product.order_id,
        status="posted",
        direction="return",
        warehouse_ref1c=component.item_code,
        source_warehouse_ref1c="WH-SRC",
    )
    db_session.add(issue)
    db_session.flush()
    _event(
        db_session,
        source_kind="terminal_release",
        issue_id=issue.issue_id,
        product_id=product.product_id,
        component_id=component.item_id,
        location="workshop",
        warehouse="WH-MAIN",
        qty=-3.0,
        key="custody:event:target:3",
        effective_at=target.cutoff,
    )
    _manifest(db_session, generation_id=target.id, source_event_high_watermark_id=1)
    db_session.commit()

    with pytest.raises(MaterialCustodySnapshotUnavailable):
        load_material_custody_projection(db_session, ledger_generation_id=target.id)


def test_event_idempotency_key_is_uniquely_enforced(db_session):
    generation = _generation(
        db_session,
        key="custody-proj-idem",
        cutoff=datetime(2026, 7, 9, tzinfo=timezone.utc),
    )
    product, _parent, component = _product(db_session, item_code="IDE")
    issue = ProductionMaterialIssue(
        document_number="MT-004",
        product_id=product.product_id,
        order_id=product.order_id,
        status="posted",
        direction="issue",
        warehouse_ref1c="WH-MAIN",
        source_warehouse_ref1c="WH-SRC",
    )
    db_session.add(issue)
    db_session.flush()
    _event(
        db_session,
        source_kind="issue_created",
        issue_id=issue.issue_id,
        product_id=product.product_id,
        component_id=component.item_id,
        location="workshop",
        warehouse="WH-MAIN",
        qty=1.0,
        key="dup:key",
        effective_at=generation.cutoff,
    )
    _event(
        db_session,
        source_kind="issue_created",
        issue_id=issue.issue_id,
        product_id=product.product_id,
        component_id=component.item_id,
        location="workshop",
        warehouse="WH-MAIN",
        qty=1.0,
        key="dup:key",
        effective_at=generation.cutoff,
    )

    with pytest.raises(IntegrityError):
        db_session.commit()
