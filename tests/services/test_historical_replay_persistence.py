from datetime import date, datetime
from decimal import Decimal

import pytest

from app.models import (
    Item,
    LedgerBuildBatch,
    LedgerGeneration,
    MrpExecutionAllocation,
    MrpRequirement,
    MrpRequirementBucket,
    PhysicalImportBatch,
    PlanningRun,
    ProductionOrder,
    ProductionProduct,
    ReservationEntry,
    ReservationEvent,
    StockLedgerFactSupersession,
    StockLedgerEntry,
    StockRecorderPull,
)
from app.services.item_ledger.historical_replay_persistence import run_historical_replay


def _generation_scope(
    db,
    key: str,
    *,
    fact_qty: str = "7",
    reserve_qty: str = "5",
    item: Item | None = None,
    realization_mode: str = "make",
    movement_kind: str = "assembly_in",
):
    cutoff = datetime(2026, 7, 23, 12, 0)
    item = item or Item(item_code=f"ITEM-{key}", item_name=f"Item {key}")
    batch = PhysicalImportBatch(batch_key=f"physical-{key}", status="completed", cutoff=cutoff)
    db.add_all([item, batch])
    db.flush()
    generation = LedgerGeneration(
        generation_key=f"generation-{key}",
        status="building",
        cutoff=cutoff,
        physical_import_batch_id=batch.id,
        algorithm_version="test/1",
        replay_version="test-replay/1",
        source_watermarks={"replay_from": "2026-07-01T00:00:00"},
        capabilities={},
    )
    run = PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={})
    db.add_all([generation, run])
    db.flush()
    requirement = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=Decimal(reserve_qty),
        net_required_qty=Decimal(reserve_qty),
        covered_qty=0,
        remaining_qty=Decimal(reserve_qty),
        period_from=date(2026, 7, 1),
        period_to=date(2026, 7, 31),
        bom_level=0,
    )
    db.add(requirement)
    db.flush()
    reservation = ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="selected",
        run_id=run.run_id,
        freeze_version=1,
        requirement_id=requirement.id,
        priority_period_from=date(2026, 7, 1),
        priority_period_to=date(2026, 7, 31),
        realization_mode=realization_mode,
        reserved_qty=Decimal(reserve_qty),
        realized_qty=0,
        lifecycle_status="active",
    )
    fact = StockLedgerEntry(
        ingest_batch_id=batch.id,
        source_content_hash=f"hash-{key}",
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        warehouse_ref1c="WH",
        qty=Decimal(fact_qty),
        qty_after=Decimal(fact_qty),
        posting_at=datetime(2026, 7, 20, 10, 0),
        record_type="Receipt",
        movement_kind=movement_kind,
        recorder_type="Production",
        recorder_ref=f"REC-{key}",
        line_no="1",
        ingest_source="pull",
        active=True,
    )
    db.add_all([reservation, fact])
    db.commit()
    return generation, reservation


def test_replay_is_idempotent_and_folds_realized_cache(db_session):
    generation, reservation = _generation_scope(db_session, "IDEMP")

    first = run_historical_replay(db_session, generation.id)
    db_session.commit()
    second = run_historical_replay(db_session, generation.id)
    db_session.commit()

    assert Decimal(first["allocated_qty"]) == Decimal("5")
    assert Decimal(second["allocated_qty"]) == Decimal("5")
    assert second["events_inserted"] == 0
    assert second["execution_allocations_inserted"] == 0
    assert db_session.query(ReservationEvent).filter_by(
        ledger_generation_id=generation.id
    ).count() == 1
    assert db_session.query(MrpExecutionAllocation).filter_by(
        ledger_generation_id=generation.id
    ).count() == 1
    db_session.refresh(reservation)
    assert reservation.realized_qty == Decimal("5")
    assert db_session.query(LedgerBuildBatch).filter_by(
        ledger_generation_id=generation.id
    ).count() == 1


def test_replay_is_strictly_isolated_by_generation_and_import_batch(db_session):
    generation_a, reservation_a = _generation_scope(
        db_session, "A", fact_qty="0", reserve_qty="4"
    )
    generation_b, reservation_b = _generation_scope(
        db_session,
        "B",
        fact_qty="9",
        reserve_qty="9",
        item=reservation_a.item,
    )

    result = run_historical_replay(db_session, generation_a.id)
    db_session.commit()

    db_session.refresh(reservation_a)
    db_session.refresh(reservation_b)
    assert result["facts"] == 0
    assert reservation_a.realized_qty == Decimal("0")
    assert reservation_b.realized_qty == Decimal("0")
    assert db_session.query(ReservationEvent).filter_by(
        ledger_generation_id=generation_b.id
    ).count() == 0


def test_replay_reports_conservation_and_unplanned_surplus(db_session):
    generation, _reservation = _generation_scope(
        db_session, "CONSERVE", fact_qty="7", reserve_qty="5"
    )

    result = run_historical_replay(db_session, generation.id)

    assert Decimal(result["fact_qty"]) == Decimal(result["allocated_qty"]) + Decimal(
        result["unplanned_qty"]
    )
    assert Decimal(result["fact_qty"]) == Decimal("7")
    assert Decimal(result["allocated_qty"]) == Decimal("5")
    assert Decimal(result["unplanned_qty"]) == Decimal("2")
    assert result["unplanned_facts"] == 1
    assert len(result["input_checksum"]) == 64
    assert len(result["allocation_checksum"]) == 64


def test_physical_import_watermark_includes_all_earlier_batches(db_session):
    generation, reservation = _generation_scope(
        db_session, "PREFIX", fact_qty="3", reserve_qty="5"
    )
    newer_batch = PhysicalImportBatch(
        batch_key="physical-prefix-watermark",
        status="completed",
        cutoff=generation.cutoff,
    )
    db_session.add(newer_batch)
    db_session.flush()
    generation.physical_import_batch_id = newer_batch.id
    db_session.add(StockLedgerEntry(
        ingest_batch_id=newer_batch.id,
        source_content_hash="hash-prefix-newer",
        item_id=reservation.item_id,
        characteristic_ref="",
        organization_ref="",
        warehouse_ref1c="WH",
        qty=Decimal("2"),
        qty_after=Decimal("5"),
        posting_at=datetime(2026, 7, 21, 10, 0),
        record_type="Receipt",
        movement_kind="assembly_in",
        recorder_type="Production",
        recorder_ref="REC-PREFIX-NEWER",
        line_no="1",
        ingest_source="pull",
        active=True,
    ))
    db_session.commit()

    result = run_historical_replay(db_session, generation.id)

    assert result["facts"] == 2
    assert Decimal(result["allocated_qty"]) == Decimal("5")


def test_generic_receipt_cannot_realize_consume_reservation(db_session):
    generation, reservation = _generation_scope(
        db_session,
        "RECEIPT",
        fact_qty="5",
        reserve_qty="5",
        realization_mode="consume",
        movement_kind="receipt",
    )

    result = run_historical_replay(db_session, generation.id)
    db_session.flush()

    assert result["facts"] == 0
    assert result["ignored_facts"] == 1
    assert Decimal(result["ignored_fact_qty"]) == Decimal("5")
    assert db_session.query(ReservationEvent).filter_by(
        ledger_generation_id=generation.id
    ).count() == 0
    db_session.refresh(reservation)
    assert reservation.realized_qty == Decimal("0")


def test_generic_expense_is_ignored_not_execution(db_session):
    generation, reservation = _generation_scope(
        db_session,
        "EXPENSE",
        fact_qty="-4",
        reserve_qty="4",
        realization_mode="consume",
        movement_kind="expense",
    )

    result = run_historical_replay(db_session, generation.id)

    assert result["facts"] == 0
    assert result["ignored_facts"] == 1
    assert Decimal(result["ignored_fact_qty"]) == Decimal("4")
    assert db_session.query(MrpExecutionAllocation).filter_by(
        ledger_generation_id=generation.id
    ).count() == 0
    db_session.refresh(reservation)
    assert reservation.realized_qty == Decimal("0")


def test_later_supersession_does_not_change_generation_replay(db_session):
    generation, reservation = _generation_scope(
        db_session, "SUPERSESSION", fact_qty="5", reserve_qty="5"
    )
    original = (
        db_session.query(StockLedgerEntry)
        .filter(StockLedgerEntry.ingest_batch_id == generation.physical_import_batch_id)
        .one()
    )
    first = run_historical_replay(db_session, generation.id)
    db_session.commit()

    later_batch = PhysicalImportBatch(
        batch_key="physical-supersession-later",
        status="completed",
        cutoff=generation.cutoff,
        source_watermarks={"replay_from": "2026-07-01T00:00:00"},
    )
    db_session.add(later_batch)
    db_session.flush()
    replacement = StockLedgerEntry(
        ingest_batch_id=later_batch.id,
        source_content_hash="hash-supersession-later",
        item_id=reservation.item_id,
        characteristic_ref="",
        organization_ref="",
        warehouse_ref1c="WH",
        qty=Decimal("99"),
        qty_after=Decimal("99"),
        posting_at=datetime(2026, 7, 20, 10, 0),
        record_type="Receipt",
        movement_kind="assembly_in",
        recorder_type="Production",
        recorder_ref="REC-SUPERSESSION-LATER",
        line_no="1",
        ingest_source="pull",
        active=True,
    )
    db_session.add(replacement)
    db_session.flush()
    original.active = False
    db_session.add(StockLedgerFactSupersession(
        old_sle_id=original.id,
        new_sle_id=replacement.id,
        import_batch_id=later_batch.id,
    ))
    db_session.commit()

    second = run_historical_replay(db_session, generation.id)

    assert second["facts"] == first["facts"] == 1
    assert second["fact_qty"] == first["fact_qty"]
    assert second["allocated_qty"] == first["allocated_qty"]
    assert second["unplanned_qty"] == first["unplanned_qty"]
    assert second["input_checksum"] == first["input_checksum"]
    assert second["allocation_checksum"] == first["allocation_checksum"]


def test_replay_excludes_facts_at_or_before_lower_bound(db_session):
    generation, reservation = _generation_scope(
        db_session, "LOWER-BOUND", fact_qty="5", reserve_qty="5"
    )

    result = run_historical_replay(
        db_session,
        generation.id,
        replay_from=datetime(2026, 7, 20, 10, 0),
    )

    assert result["facts"] == 0
    assert result["excluded_pre_replay_facts"] == 1
    db_session.refresh(reservation)
    assert reservation.realized_qty == Decimal("0")


def test_replay_refuses_unbounded_history(db_session):
    generation, _reservation = _generation_scope(
        db_session, "NO-LOWER-BOUND", fact_qty="5", reserve_qty="5"
    )
    generation.source_watermarks = {}
    generation.physical_import_batch.source_watermarks = {}
    db_session.flush()

    with pytest.raises(ValueError, match="requires explicit replay_from"):
        run_historical_replay(db_session, generation.id)


def test_realization_is_split_across_all_requirement_buckets(db_session):
    generation, reservation = _generation_scope(
        db_session, "BUCKET-SPLIT", fact_qty="5", reserve_qty="5"
    )
    for bucket_date, qty in (
        (date(2026, 7, 10), Decimal("2")),
        (date(2026, 7, 20), Decimal("3")),
    ):
        db_session.add(MrpRequirementBucket(
            requirement_id=reservation.requirement_id,
            run_id=reservation.run_id,
            item_id=reservation.item_id,
            bucket_date=bucket_date,
            gross_qty=qty,
            net_qty=qty,
        ))
    db_session.commit()

    first = run_historical_replay(db_session, generation.id)
    db_session.commit()
    second = run_historical_replay(db_session, generation.id)
    rows = (
        db_session.query(MrpExecutionAllocation)
        .filter_by(ledger_generation_id=generation.id)
        .order_by(MrpExecutionAllocation.bucket_id.asc())
        .all()
    )

    assert [row.allocated_qty for row in rows] == [Decimal("2"), Decimal("3")]
    assert sum((row.allocated_qty for row in rows), Decimal("0")) == Decimal("5")
    assert first["execution_allocations_inserted"] == 2
    assert second["execution_allocations_inserted"] == 0


def test_recorder_order_identity_addresses_consume_exactly(db_session):
    generation, reservation = _generation_scope(
        db_session,
        "EXACT-CONSUME",
        fact_qty="4",
        reserve_qty="4",
        realization_mode="consume",
        movement_kind="assembly_out",
    )
    sle = db_session.query(StockLedgerEntry).filter_by(
        ingest_batch_id=generation.physical_import_batch_id
    ).one()
    order = ProductionOrder(
        order_number="EXACT",
        order_date=datetime(2026, 7, 1),
        order_ref1c="ORDER-EXACT",
        deletion_mark=False,
        source="1c",
    )
    db_session.add(order)
    db_session.flush()
    db_session.add(ProductionProduct(
        order_id=order.order_id,
        item_id=reservation.item_id,
        line_number=1,
        quantity=4,
        produced_qty=0,
        remaining_qty=4,
        source_mrp_requirement_id=reservation.requirement_id,
    ))
    db_session.add(StockRecorderPull(
        recorder_type=sle.recorder_type,
        recorder_ref=sle.recorder_ref,
        status="done",
        order_ref=order.order_ref1c,
    ))
    db_session.commit()

    result = run_historical_replay(db_session, generation.id)

    assert Decimal(result["allocated_qty"]) == Decimal("4")
    assert Decimal(result["unplanned_qty"]) == Decimal("0")
    event = db_session.query(ReservationEvent).filter_by(
        ledger_generation_id=generation.id
    ).one()
    assert event.match_rule == "pegged"


def test_ambiguous_order_identity_leaves_consume_unplanned(db_session):
    generation, reservation = _generation_scope(
        db_session,
        "AMBIGUOUS-CONSUME",
        fact_qty="4",
        reserve_qty="4",
        realization_mode="consume",
        movement_kind="assembly_out",
    )
    sle = db_session.query(StockLedgerEntry).filter_by(
        ingest_batch_id=generation.physical_import_batch_id
    ).one()
    order = ProductionOrder(
        order_number="AMBIGUOUS",
        order_date=datetime(2026, 7, 1),
        order_ref1c="ORDER-AMBIGUOUS",
        deletion_mark=False,
        source="1c",
    )
    other_run = PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={})
    db_session.add_all([order, other_run])
    db_session.flush()
    other_requirement = MrpRequirement(
        run_id=other_run.run_id,
        item_id=reservation.item_id,
        total_required_qty=4,
        net_required_qty=4,
        covered_qty=0,
        remaining_qty=4,
        period_from=date(2026, 7, 1),
        period_to=date(2026, 7, 31),
        bom_level=0,
    )
    db_session.add(other_requirement)
    db_session.flush()
    db_session.add_all([
        ProductionProduct(
            order_id=order.order_id,
            item_id=reservation.item_id,
            line_number=1,
            quantity=2,
            produced_qty=0,
            remaining_qty=2,
            source_mrp_requirement_id=reservation.requirement_id,
        ),
        ProductionProduct(
            order_id=order.order_id,
            item_id=reservation.item_id,
            line_number=2,
            quantity=2,
            produced_qty=0,
            remaining_qty=2,
            source_mrp_requirement_id=other_requirement.id,
        ),
        StockRecorderPull(
            recorder_type=sle.recorder_type,
            recorder_ref=sle.recorder_ref,
            status="done",
            order_ref=order.order_ref1c,
        ),
    ])
    db_session.commit()

    result = run_historical_replay(db_session, generation.id)

    assert Decimal(result["allocated_qty"]) == Decimal("0")
    assert Decimal(result["unplanned_qty"]) == Decimal("4")
    assert result["ambiguous_identity_facts"] == 1
