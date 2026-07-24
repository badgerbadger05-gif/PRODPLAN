from datetime import date, datetime
from decimal import Decimal

import pytest

from app.models import (
    Item,
    LedgerBuildBatch,
    LedgerGeneration,
    StockBin,
    StockWarehouse,
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
from app.services.item_ledger.historical_obligations import (
    ALGORITHM_VERSION as OBLIGATION_ALGORITHM_VERSION,
)


def _generation_scope(
    db,
    key: str,
    *,
    fact_qty: str = "7",
    reserve_qty: str = "5",
    add_default_bucket: bool = True,
    item: Item | None = None,
    realization_mode: str = "make",
    movement_kind: str = "assembly_in",
    add_default_warehouse_policy: bool = True,
):
    cutoff = datetime(2026, 7, 23, 12, 0)
    item = item or Item(item_code=f"ITEM-{key}", item_name=f"Item {key}")
    batch = PhysicalImportBatch(batch_key=f"physical-{key}", status="completed", cutoff=cutoff)
    db.add_all([item, batch])
    if (
        add_default_warehouse_policy
        and db.query(StockWarehouse).filter_by(warehouse_ref1c="WH").count() == 0
    ):
        db.add(StockWarehouse(
            warehouse_ref1c="WH",
            warehouse_name="Outside default",
            is_selected=False,
            is_finished_goods=False,
        ))
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
    if add_default_bucket:
        db.add(MrpRequirementBucket(
            requirement_id=requirement.id,
            run_id=run.run_id,
            item_id=item.item_id,
            bucket_date=date(2026, 7, 20),
            gross_qty=Decimal(reserve_qty),
            net_qty=Decimal(reserve_qty),
        ))
    db.commit()
    return generation, reservation


def _append_obligation_batch(
    db,
    generation: LedgerGeneration,
    *,
    requirement_id: int,
    allow_unphased: bool,
):
    row = LedgerBuildBatch(
        ledger_generation_id=int(generation.id),
        stage="reservation_materialize",
        batch_key=f"seed-{generation.id}",
        status="completed",
        algorithm_version=OBLIGATION_ALGORITHM_VERSION,
        metrics={
            "selected_requirement_ids": [requirement_id],
            "legacy_net_phasing_requirement_ids": [requirement_id]
            if allow_unphased
            else [],
        },
        completed_at=datetime(2026, 7, 23, 12, 0),
    )
    db.add(row)
    db.flush()


def _address_fact_to_requirement(
    db,
    fact: StockLedgerEntry,
    reservation: ReservationEntry,
) -> None:
    order = ProductionOrder(
        order_number=f"ORDER-{fact.recorder_ref}",
        order_date=fact.posting_at,
        order_ref1c=f"ORDER-REF-{fact.recorder_ref}",
        deletion_mark=False,
        source="1c",
    )
    db.add(order)
    db.flush()
    db.add_all([
        ProductionProduct(
            order_id=order.order_id,
            item_id=reservation.item_id,
            line_number=1,
            quantity=abs(Decimal(str(fact.qty))),
            produced_qty=0,
            remaining_qty=abs(Decimal(str(fact.qty))),
            source_mrp_requirement_id=reservation.requirement_id,
        ),
        StockRecorderPull(
            recorder_type=fact.recorder_type,
            recorder_ref=fact.recorder_ref,
            status="done",
            order_ref=order.order_ref1c,
        ),
    ])
    db.flush()


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
        db_session, "BUCKET-SPLIT", fact_qty="5", reserve_qty="5", add_default_bucket=False
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


def test_legacy_net_mismatch_uses_only_unphased_slice(db_session):
    generation, reservation = _generation_scope(
        db_session,
        "LEGACY-UNPHASED",
        fact_qty="6",
        reserve_qty="6",
        add_default_bucket=False,
    )
    db_session.add_all([
        MrpRequirementBucket(
            requirement_id=reservation.requirement_id,
            run_id=reservation.run_id,
            item_id=reservation.item_id,
            bucket_date=date(2026, 7, 1),
            gross_qty=Decimal("2"),
            net_qty=Decimal("2"),
        ),
        MrpRequirementBucket(
            requirement_id=reservation.requirement_id,
            run_id=reservation.run_id,
            item_id=reservation.item_id,
            bucket_date=date(2026, 7, 2),
            gross_qty=Decimal("2"),
            net_qty=Decimal("2"),
        ),
    ])
    db_session.commit()
    _append_obligation_batch(
        db_session,
        generation,
        requirement_id=int(reservation.requirement_id),
        allow_unphased=True,
    )

    result = run_historical_replay(db_session, generation.id)
    db_session.commit()

    rows = (
        db_session.query(MrpExecutionAllocation)
        .filter_by(ledger_generation_id=generation.id)
        .order_by(MrpExecutionAllocation.id.asc())
        .all()
    )
    by_bucket = {row.bucket_id: row.allocated_qty for row in rows}

    assert Decimal(result["allocated_qty"]) == Decimal("6")
    assert len(by_bucket) == 1
    assert by_bucket[None] == Decimal("6")


def test_legacy_net_mismatch_ignores_stale_excess_bucket_capacity(db_session):
    generation, reservation = _generation_scope(
        db_session,
        "LEGACY-HIGHER",
        fact_qty="3",
        reserve_qty="3",
        add_default_bucket=False,
    )
    db_session.add(
        MrpRequirementBucket(
            requirement_id=reservation.requirement_id,
            run_id=reservation.run_id,
            item_id=reservation.item_id,
            bucket_date=date(2026, 7, 5),
            gross_qty=Decimal("10"),
            net_qty=Decimal("10"),
        )
    )
    db_session.commit()
    _append_obligation_batch(
        db_session,
        generation,
        requirement_id=int(reservation.requirement_id),
        allow_unphased=True,
    )

    result = run_historical_replay(db_session, generation.id)
    db_session.commit()

    rows = (
        db_session.query(MrpExecutionAllocation)
        .filter_by(ledger_generation_id=generation.id)
        .order_by(MrpExecutionAllocation.id.asc())
        .all()
    )

    assert Decimal(result["allocated_qty"]) == Decimal("3")
    assert len(rows) == 1
    assert rows[0].bucket_id is None


def test_replay_refuses_malformed_legacy_metric_ids(db_session):
    generation, _reservation = _generation_scope(
        db_session, "LEGACY-MALFORMED", fact_qty="4", reserve_qty="4"
    )
    db_session.add(LedgerBuildBatch(
        ledger_generation_id=int(generation.id),
        stage="reservation_materialize",
        batch_key=f"seed-{generation.id}",
        status="completed",
        algorithm_version=OBLIGATION_ALGORITHM_VERSION,
        metrics={
            "selected_requirement_ids": [1],
            "legacy_net_phasing_requirement_ids": ["bad-id"],
        },
        completed_at=datetime(2026, 7, 23, 12, 0),
    ))
    db_session.commit()

    with pytest.raises(
        ValueError,
        match="legacy_net_phasing_requirement_ids must contain integer ids",
    ):
        run_historical_replay(db_session, generation.id)


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


def test_replay_collapses_singleton_fact_identity_to_legacy_blank_pool(db_session):
    generation, reservation = _generation_scope(
        db_session, "ORG-COLLAPSE", fact_qty="5", reserve_qty="5"
    )
    fact = db_session.query(StockLedgerEntry).filter_by(
        ingest_batch_id=generation.physical_import_batch_id
    ).one()
    fact.characteristic_ref = "CHAR-1"
    fact.organization_ref = "ORG-1"
    db_session.flush()

    result = run_historical_replay(db_session, generation.id)

    assert Decimal(result["allocated_qty"]) == Decimal("5")
    assert result["legacy_identity_collapsed_pool_facts"] == 1
    assert result["ambiguous_pool_facts"] == 0
    assert Decimal(result["unplanned_qty"]) == Decimal("0")
    db_session.refresh(reservation)
    assert reservation.realized_qty == Decimal("5")


def test_replay_treats_no_pool_facts_as_unplanned_without_ambiguity(db_session):
    cutoff = datetime(2026, 7, 23, 12, 0)
    item = Item(item_code="ITEM-NO-POOL", item_name="Item No Pool")
    batch = PhysicalImportBatch(batch_key="physical-no-pool", status="completed", cutoff=cutoff)
    db_session.add_all([
        item,
        batch,
        StockWarehouse(
            warehouse_ref1c="WH",
            warehouse_name="Outside planning contour",
            is_selected=False,
            is_finished_goods=False,
        ),
    ])
    db_session.flush()
    generation = LedgerGeneration(
        generation_key="generation-no-pool",
        status="building",
        cutoff=cutoff,
        physical_import_batch_id=batch.id,
        algorithm_version="test/1",
        replay_version="test-replay/1",
        source_watermarks={"replay_from": "2026-07-01T00:00:00"},
        capabilities={},
    )
    db_session.add(generation)
    db_session.flush()
    db_session.add(StockLedgerEntry(
        ingest_batch_id=batch.id,
        source_content_hash="no-pool-fact",
        item_id=item.item_id,
        characteristic_ref="CHAR-NP",
        organization_ref="ORG-NP",
        warehouse_ref1c="WH",
        qty=Decimal("4"),
        qty_after=Decimal("4"),
        posting_at=datetime(2026, 7, 20, 10, 0),
        record_type="Receipt",
        movement_kind="assembly_in",
        recorder_type="Production",
        recorder_ref="REC-NO-POOL",
        line_no="1",
        ingest_source="test",
        active=True,
    ))
    db_session.commit()

    result = run_historical_replay(db_session, generation.id)

    assert Decimal(result["facts"]) == Decimal("1")
    assert result["allocations"] == 0
    assert result["unplanned_facts"] == 1
    assert Decimal(result["unplanned_qty"]) == Decimal("4")
    assert result["ambiguous_pool_facts"] == 0
    assert db_session.query(ReservationEvent).filter_by(
        ledger_generation_id=generation.id
    ).count() == 0


def test_replay_rejects_ambiguous_pools_even_with_no_pool_candidate_fact_identity(db_session):
    generation, reservation = _generation_scope(
        db_session, "AMBIGUOUS-POOL", fact_qty="4", reserve_qty="4"
    )
    run = PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={})
    db_session.add(run)
    db_session.flush()
    ambiguous_requirement = MrpRequirement(
        run_id=run.run_id,
        item_id=reservation.item_id,
        total_required_qty=4,
        net_required_qty=4,
        covered_qty=0,
        remaining_qty=4,
        period_from=date(2026, 7, 1),
        period_to=date(2026, 7, 31),
        bom_level=0,
    )
    db_session.add(ambiguous_requirement)
    db_session.flush()
    db_session.add(ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=reservation.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="legacy",
        run_id=run.run_id,
        freeze_version=1,
        requirement_id=ambiguous_requirement.id,
        priority_period_from=reservation.priority_period_from,
        priority_period_to=reservation.priority_period_to,
        realization_mode="make",
        reserved_qty=Decimal("4"),
        realized_qty=Decimal("0"),
        lifecycle_status="active",
    ))
    db_session.flush()

    result = run_historical_replay(db_session, generation.id)

    assert result["allocations"] == 0
    assert Decimal(result["unplanned_qty"]) == Decimal("4")
    assert result["ambiguous_pool_facts"] == 1
    assert db_session.query(ReservationEvent).filter_by(
        ledger_generation_id=generation.id
    ).count() == 0


def test_replay_multi_org_facts_do_not_fallback_to_blank_org(db_session):
    generation, reservation = _generation_scope(
        db_session, "ORG-MULTI", fact_qty="0", reserve_qty="8"
    )
    reservation.reserved_qty = Decimal("8")
    db_session.add_all([
        StockLedgerEntry(
            ingest_batch_id=generation.physical_import_batch_id,
            source_content_hash="org-multi-a",
            item_id=reservation.item_id,
            characteristic_ref="",
            organization_ref="ORG-A",
            warehouse_ref1c="WH",
            qty=Decimal("4"),
            qty_after=Decimal("4"),
            posting_at=datetime(2026, 7, 20, 11, 0),
            record_type="Receipt",
            movement_kind="assembly_in",
            recorder_type="Production",
            recorder_ref="REC-ORG-MULTI-A",
            line_no="1",
            ingest_source="test",
            active=True,
        ),
        StockLedgerEntry(
            ingest_batch_id=generation.physical_import_batch_id,
            source_content_hash="org-multi-b",
            item_id=reservation.item_id,
            characteristic_ref="",
            organization_ref="ORG-B",
            warehouse_ref1c="WH",
            qty=Decimal("4"),
            qty_after=Decimal("8"),
            posting_at=datetime(2026, 7, 20, 11, 0),
            record_type="Receipt",
            movement_kind="assembly_in",
            recorder_type="Production",
            recorder_ref="REC-ORG-MULTI-B",
            line_no="1",
            ingest_source="test",
            active=True,
        ),
    ])
    db_session.query(StockLedgerEntry).filter(
        StockLedgerEntry.ingest_batch_id == generation.physical_import_batch_id,
        StockLedgerEntry.source_content_hash == f"hash-ORG-MULTI",
    ).delete(synchronize_session=False)
    db_session.flush()

    result = run_historical_replay(db_session, generation.id)

    assert result["ambiguous_pool_facts"] == 2
    assert result["legacy_identity_collapsed_pool_facts"] == 0
    assert Decimal(result["allocated_qty"]) == Decimal("0")
    assert Decimal(result["unplanned_qty"]) == Decimal("8")
    assert db_session.query(ReservationEvent).filter_by(
        ledger_generation_id=generation.id
    ).count() == 0
    db_session.refresh(reservation)
    assert reservation.realized_qty == Decimal("0")


def test_replay_exact_org_match_takes_precedence_over_blank_org_fallback(db_session):
    generation, reserved_blank = _generation_scope(
        db_session, "ORG-EXACT", fact_qty="5", reserve_qty="5"
    )
    exact_run = PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={})
    db_session.add(exact_run)
    db_session.flush()
    exact_requirement = MrpRequirement(
        run_id=exact_run.run_id,
        item_id=reserved_blank.item_id,
        total_required_qty=Decimal("5"),
        net_required_qty=Decimal("5"),
        covered_qty=0,
        remaining_qty=Decimal("5"),
        period_from=date(2026, 7, 1),
        period_to=date(2026, 7, 31),
        bom_level=0,
    )
    db_session.add(exact_requirement)
    db_session.flush()
    db_session.add(MrpRequirementBucket(
        requirement_id=exact_requirement.id,
        run_id=exact_run.run_id,
        item_id=reserved_blank.item_id,
        bucket_date=date(2026, 7, 20),
        gross_qty=Decimal("5"),
        net_qty=Decimal("5"),
    ))
    exact_reservation = ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=reserved_blank.item_id,
        characteristic_ref="",
        organization_ref="ORG-1",
        planning_stock_pool="legacy",
        run_id=exact_run.run_id,
        freeze_version=1,
        requirement_id=exact_requirement.id,
        priority_period_from=reserved_blank.priority_period_from,
        priority_period_to=reserved_blank.priority_period_to,
        realization_mode="make",
        reserved_qty=Decimal("5"),
        realized_qty=Decimal("0"),
        lifecycle_status="active",
    )
    db_session.add(exact_reservation)
    db_session.flush()
    fact = db_session.query(StockLedgerEntry).filter_by(
        ingest_batch_id=generation.physical_import_batch_id
    ).one()
    fact.organization_ref = "ORG-1"
    db_session.flush()

    result = run_historical_replay(db_session, generation.id)
    events = db_session.query(ReservationEvent).filter_by(
        ledger_generation_id=generation.id
    ).all()

    assert Decimal(result["allocated_qty"]) == Decimal("5")
    assert result["legacy_identity_collapsed_pool_facts"] == 0
    assert len(events) == 1
    assert events[0].reservation_id == exact_reservation.id
    assert events[0].planning_stock_pool == "legacy"


def test_replay_fifo_remains_oldest_pool_for_later_output(db_session):
    generation, reservation_a = _generation_scope(
        db_session, "ORG-FIFO", fact_qty="0", reserve_qty="2"
    )
    generation.cutoff = datetime(2027, 7, 21)
    generation.physical_import_batch.cutoff = datetime(2027, 7, 21)
    reservation_a.organization_ref = ""
    reservation_a.reserved_qty = Decimal("2")
    req = db_session.query(MrpRequirement).filter_by(
        item_id=reservation_a.item_id
    ).first()
    assert req is not None
    reservation_a.reserved_qty = Decimal("2")
    later_run = PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={})
    db_session.add(later_run)
    db_session.flush()
    db_session.add(
        MrpRequirement(
            run_id=later_run.run_id,
            item_id=reservation_a.item_id,
            total_required_qty=3,
            net_required_qty=3,
            covered_qty=0,
            remaining_qty=3,
            period_from=date(2027, 1, 1),
            period_to=date(2027, 1, 31),
            bom_level=0,
        )
    )
    db_session.flush()
    req2 = db_session.query(MrpRequirement).filter(
        MrpRequirement.run_id == later_run.run_id,
        MrpRequirement.item_id == reservation_a.item_id,
    ).order_by(MrpRequirement.id.desc()).first()
    assert req2 is not None
    db_session.add_all([
        MrpRequirementBucket(
            requirement_id=reservation_a.requirement_id,
            run_id=reservation_a.run_id,
            item_id=reservation_a.item_id,
            bucket_date=date(2026, 6, 30),
            gross_qty=2,
            net_qty=2,
        ),
        MrpRequirementBucket(
            requirement_id=req2.id,
            run_id=req2.run_id,
            item_id=req2.item_id,
            bucket_date=date(2027, 1, 31),
            gross_qty=3,
            net_qty=3,
        ),
        ReservationEntry(
            ledger_generation_id=generation.id,
            item_id=reservation_a.item_id,
            characteristic_ref="",
            organization_ref="",
            planning_stock_pool="selected",
            run_id=later_run.run_id,
            freeze_version=1,
            requirement_id=req2.id,
            priority_period_from=req2.period_from,
            priority_period_to=req2.period_to,
            realization_mode="make",
            reserved_qty=Decimal("3"),
            realized_qty=Decimal("0"),
            lifecycle_status="active",
        ),
    ])
    db_session.add(StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash="org-fifo",
        item_id=reservation_a.item_id,
        characteristic_ref="",
        organization_ref="ORG-LATE",
        warehouse_ref1c="WH",
        qty=Decimal("5"),
        qty_after=Decimal("5"),
        posting_at=datetime(2027, 7, 20, 10, 0),
        record_type="Receipt",
        movement_kind="assembly_in",
        recorder_type="Production",
        recorder_ref="REC-ORG-FIFO",
        line_no="1",
        ingest_source="test",
        active=True,
    ))
    db_session.query(StockLedgerEntry).filter(
        StockLedgerEntry.ingest_batch_id == generation.physical_import_batch_id,
        StockLedgerEntry.source_content_hash == "hash-ORG-FIFO",
    ).delete(synchronize_session=False)
    db_session.flush()

    result = run_historical_replay(db_session, generation.id)
    allocations = (
        db_session.query(MrpExecutionAllocation)
        .filter_by(ledger_generation_id=generation.id)
        .order_by(MrpExecutionAllocation.bucket_id.asc(), MrpExecutionAllocation.id.asc())
        .all()
    )

    assert result["allocated_qty"] == "5.000"
    assert len(allocations) == 2
    assert allocations[0].requirement_id == reservation_a.requirement_id
    assert allocations[0].allocated_qty == Decimal("2")
    assert allocations[1].requirement_id == req2.id
    assert allocations[1].allocated_qty == Decimal("3")


def test_replay_uses_gross_capacity_for_consume_when_bucket_net_is_zero(db_session):
    generation, reservation = _generation_scope(
        db_session,
        "CONSUME-GROSS",
        fact_qty="26",
        reserve_qty="26",
        realization_mode="consume",
        movement_kind="assembly_out",
        add_default_bucket=False,
    )
    db_session.add(MrpRequirementBucket(
        requirement_id=reservation.requirement_id,
        run_id=reservation.run_id,
        item_id=reservation.item_id,
        bucket_date=date(2026, 7, 20),
        gross_qty=Decimal("26"),
        net_qty=Decimal("0"),
    ))
    reservation.reserved_qty = Decimal("26")
    db_session.flush()
    fact = db_session.query(StockLedgerEntry).filter_by(
        ingest_batch_id=generation.physical_import_batch_id
    ).one()
    _address_fact_to_requirement(db_session, fact, reservation)
    _append_obligation_batch(
        db_session,
        generation,
        requirement_id=int(reservation.requirement_id),
        allow_unphased=True,
    )

    result = run_historical_replay(db_session, generation.id)

    assert Decimal(result["allocated_qty"]) == Decimal("26")
    rows = (
        db_session.query(MrpExecutionAllocation)
        .filter_by(ledger_generation_id=generation.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].allocated_qty == Decimal("26")
    assert rows[0].bucket_id is not None


def test_replay_rejects_make_allocation_when_bucket_net_is_zero(db_session):
    generation, reservation = _generation_scope(
        db_session,
        "MAKE-NET-ZERO",
        fact_qty="26",
        reserve_qty="26",
        realization_mode="make",
        add_default_bucket=False,
    )
    db_session.add(MrpRequirementBucket(
        requirement_id=reservation.requirement_id,
        run_id=reservation.run_id,
        item_id=reservation.item_id,
        bucket_date=date(2026, 7, 20),
        gross_qty=Decimal("26"),
        net_qty=Decimal("0"),
    ))
    reservation.reserved_qty = Decimal("26")
    db_session.flush()

    with pytest.raises(
        ValueError, match="bucket capacity is below realization",
    ):
        run_historical_replay(db_session, generation.id)


def test_replay_excludes_make_facts_for_selected_non_fg_contour_warehouse(db_session):
    generation, reservation = _generation_scope(
        db_session,
        "MAKE-IN-CONTOUR",
        fact_qty="5",
        reserve_qty="5",
        realization_mode="make",
    )
    warehouse = db_session.query(StockWarehouse).filter_by(
        warehouse_ref1c="WH"
    ).one()
    warehouse.warehouse_name = "Contour A"
    warehouse.is_selected = True
    db_session.flush()
    fact = db_session.query(StockLedgerEntry).filter_by(
        ingest_batch_id=generation.physical_import_batch_id
    ).one()
    db_session.add(StockBin(
        ledger_generation_id=generation.id,
        item_id=reservation.item_id,
        characteristic_ref=fact.characteristic_ref or "",
        organization_ref=fact.organization_ref or "",
        warehouse_ref1c=fact.warehouse_ref1c,
        on_hand=fact.qty,
    ))
    db_session.commit()

    result = run_historical_replay(db_session, generation.id)

    assert result["facts"] == 0
    assert result["allocations"] == 0
    assert result["unplanned_facts"] == 0
    assert Decimal(result["excluded_make_facts"]) == Decimal("1")
    assert Decimal(result["excluded_make_qty"]) == Decimal("5")
    assert Decimal(result["allocated_qty"]) == Decimal("0")
    assert Decimal(result["unplanned_qty"]) == Decimal("0")
    assert db_session.query(ReservationEvent).filter_by(
        ledger_generation_id=generation.id
    ).count() == 0
    db_session.refresh(reservation)
    assert reservation.realized_qty == Decimal("0")


def test_replay_excludes_make_when_warehouse_policy_is_missing(db_session):
    generation, reservation = _generation_scope(
        db_session,
        "MAKE-NO-WAREHOUSE-POLICY",
        fact_qty="5",
        reserve_qty="5",
        realization_mode="make",
        add_default_warehouse_policy=False,
    )

    result = run_historical_replay(db_session, generation.id)

    assert result["facts"] == 0
    assert result["excluded_make_facts"] == 1
    assert result["excluded_make_samples"][0]["reason"] == "warehouse_policy_missing"
    assert result["allocated_qty"] == "0"
    assert db_session.query(ReservationEvent).filter_by(
        ledger_generation_id=generation.id
    ).count() == 0
    db_session.refresh(reservation)
    assert reservation.realized_qty == Decimal("0")


def test_replay_allows_make_facts_for_outside_contour_warehouse(db_session):
    generation, reservation = _generation_scope(
        db_session,
        "MAKE-OUT-OF-CONTOUR",
        fact_qty="5",
        reserve_qty="5",
        realization_mode="make",
    )
    db_session.add_all([
        StockWarehouse(
            warehouse_ref1c="WH-IN",
            warehouse_name="Contour A",
            is_selected=True,
            is_finished_goods=False,
        ),
        StockWarehouse(
            warehouse_ref1c="WH-OUT",
            warehouse_name="Outside A",
            is_selected=False,
            is_finished_goods=False,
        ),
    ])
    fact = db_session.query(StockLedgerEntry).filter_by(
        ingest_batch_id=generation.physical_import_batch_id
    ).one()
    fact.warehouse_ref1c = "WH-OUT"
    db_session.add(StockBin(
        ledger_generation_id=generation.id,
        item_id=reservation.item_id,
        characteristic_ref=fact.characteristic_ref or "",
        organization_ref=fact.organization_ref or "",
        warehouse_ref1c=fact.warehouse_ref1c,
        on_hand=fact.qty,
    ))
    db_session.flush()

    result = run_historical_replay(db_session, generation.id)

    assert result["facts"] == 1
    assert result["excluded_make_facts"] == 0
    assert Decimal(result["allocated_qty"]) == Decimal("5")
    assert result["allocations"] == 1
    assert result["unplanned_facts"] == 0
    assert db_session.query(ReservationEvent).filter_by(
        ledger_generation_id=generation.id
    ).count() == 1
    db_session.refresh(reservation)
    assert reservation.realized_qty == Decimal("5")


def test_replay_preserves_mode_isolated_bucket_capacity(db_session):
    generation, make_reservation = _generation_scope(
        db_session,
        "MODE-ISOLATED",
        fact_qty="26",
        reserve_qty="26",
        realization_mode="make",
        add_default_bucket=False,
    )
    db_session.add(MrpRequirementBucket(
        requirement_id=make_reservation.requirement_id,
        run_id=make_reservation.run_id,
        item_id=make_reservation.item_id,
        bucket_date=date(2026, 7, 20),
        gross_qty=Decimal("26"),
        net_qty=Decimal("26"),
    ))
    consume_fact = StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash="consume-fact",
        item_id=make_reservation.item_id,
        characteristic_ref="",
        organization_ref="",
        warehouse_ref1c="WH",
        qty=Decimal("26"),
        qty_after=Decimal("26"),
        posting_at=datetime(2026, 7, 20, 12, 0),
        record_type="Receipt",
        movement_kind="assembly_out",
        recorder_type="Consumption",
        recorder_ref="REC-MODE-ISOLATED-CONSUME",
        line_no="2",
        ingest_source="test",
        active=True,
    )
    db_session.add(consume_fact)
    consume_reservation = ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=make_reservation.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="selected",
        run_id=make_reservation.run_id,
        freeze_version=1,
        requirement_id=make_reservation.requirement_id,
        priority_period_from=make_reservation.priority_period_from,
        priority_period_to=make_reservation.priority_period_to,
        realization_mode="consume",
        reserved_qty=Decimal("26"),
        realized_qty=Decimal("0"),
        lifecycle_status="active",
    )
    db_session.add(consume_reservation)
    db_session.flush()
    make_fact = db_session.query(StockLedgerEntry).filter_by(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash="hash-MODE-ISOLATED",
    ).one()
    _address_fact_to_requirement(db_session, make_fact, make_reservation)
    _address_fact_to_requirement(db_session, consume_fact, consume_reservation)

    result = run_historical_replay(db_session, generation.id)

    assert Decimal(result["allocated_qty"]) == Decimal("52")
    rows = (
        db_session.query(MrpExecutionAllocation)
        .filter_by(ledger_generation_id=generation.id)
        .order_by(MrpExecutionAllocation.id.asc())
        .all()
    )
    assert len(rows) == 2
    assert {row.fact_type for row in rows} == {"linked_production", "component_consumption"}
    by_type = {row.fact_type: row.allocated_qty for row in rows}
    assert by_type["linked_production"] == Decimal("26")
    assert by_type["component_consumption"] == Decimal("26")
    assert len({row.bucket_id for row in rows}) == 1
