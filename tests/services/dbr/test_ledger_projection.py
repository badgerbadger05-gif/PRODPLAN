from datetime import date, datetime
from decimal import Decimal

import pytest

from app import models
from app.services.dbr.ledger_projection import (
    LedgerProjectionKey,
    build_generation_projection,
    build_ledger_projection,
)
from app.services.planning_truth import PlanningTruthUnavailable


CAPABILITIES = {
    "physical_ledger": True,
    "reservation_replay": True,
    "execution_allocations": True,
    "planning_snapshots": True,
}


def _generation(db, key: str, *, accepted: bool = True, capabilities=None):
    batch = models.PhysicalImportBatch(
        batch_key=f"physical-{key}",
        status="completed",
        cutoff=datetime(2026, 7, 23, 12),
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key=f"generation-{key}",
        status="accepted" if accepted else "building",
        cutoff=datetime(2026, 7, 23, 12),
        accepted_at=datetime(2026, 7, 23, 13) if accepted else None,
        source_watermarks={},
        capabilities=CAPABILITIES if capabilities is None else capabilities,
        physical_import_batch=batch,
        algorithm_version="test",
        replay_version="test",
    )
    db.add(generation)
    db.flush()
    return generation


def _publish(db, generation):
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None:
        pointer = models.PlanningTruthState(id=1)
        db.add(pointer)
    pointer.current_generation_id = generation.id
    db.flush()


def _item(db, code="ITEM-1"):
    item = models.Item(item_code=code, item_name=code)
    db.add(item)
    db.flush()
    return item


def _future(
    db,
    generation,
    item,
    *,
    status="exact",
    warehouse="WH-1",
    ref="SUP-1",
    line="1",
    qty="4",
    pool="main",
):
    batch = models.LedgerBuildBatch(
        ledger_generation_id=generation.id,
        stage="snapshot_build",
        batch_key=f"snapshot-{generation.id}-{ref}-{line}",
        status="completed",
        algorithm_version="test",
        metrics={},
        completed_at=datetime(2026, 7, 23, 13),
    )
    db.add(batch)
    db.flush()
    row = models.LedgerFutureSupply(
        ledger_generation_id=generation.id,
        supply_kind="supplier_order",
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool=pool,
        destination_warehouse_ref1c=warehouse,
        source_ref=ref,
        source_line_ref=line,
        ordered_qty_at_cutoff=Decimal(qty),
        realized_qty_at_cutoff=Decimal("0"),
        open_qty_at_cutoff=Decimal(qty),
        eta_date=date(2026, 8, 1),
        source_state_key="open",
        capture_cutoff=generation.cutoff,
        source_content_hash=f"hash-{generation.id}-{ref}-{line}",
        capture_batch_id=batch.id,
        evidence_status=status,
        reason="ambiguous destination" if status == "ambiguous" else None,
    )
    db.add(row)
    db.flush()
    return row


def _reservation(db, generation, item, *, pool="main", qty="10"):
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
    )
    db.add(run)
    db.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=Decimal(qty),
        net_required_qty=Decimal(qty),
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        bom_level=0,
    )
    db.add(requirement)
    db.flush()
    reservation = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool=pool,
        run_id=run.run_id,
        freeze_version=1,
        requirement_id=requirement.id,
        priority_period_from=date(2026, 8, 1),
        priority_period_to=date(2026, 8, 31),
        realization_mode="consume",
        reserved_qty=Decimal(qty),
        realized_qty=Decimal("3"),
        uncovered_qty=Decimal("2"),
        lifecycle_status="active",
    )
    db.add(reservation)
    db.flush()
    db.add(models.ReservationCoverage(
        reservation_id=reservation.id,
        source_kind="supplier_order",
        source_ref="SUP-1",
        source_line_ref="1",
        pin_kind="frozen",
        alloc_qty=Decimal("5"),
        fact_at_freeze=Decimal("5"),
        covered_qty=Decimal("4"),
        realized_qty=Decimal("1"),
        evaporated_qty=Decimal("0"),
        cycle_id="cycle-1",
    ))
    db.flush()
    return reservation


def test_fail_closed_when_readiness_or_capability_is_missing(db_session):
    with pytest.raises(PlanningTruthUnavailable):
        build_ledger_projection(
            db_session, [("ITEM-1", "WH-1")], {"WH-1": "main"}
        )

    generation = _generation(
        db_session,
        "partial",
        capabilities={**CAPABILITIES, "planning_snapshots": False},
    )
    _publish(db_session, generation)

    with pytest.raises(PlanningTruthUnavailable) as raised:
        build_ledger_projection(
            db_session, [("ITEM-1", "WH-1")], {"WH-1": "main"}
        )
    assert raised.value.consumer == "dbr-ledger-projection"
    assert "planning_snapshots" in str(raised.value)


def test_reads_only_current_generation_and_exact_destination(db_session):
    old = _generation(db_session, "old")
    current = _generation(db_session, "current")
    item = _item(db_session)
    db_session.add_all([
        models.StockBin(
            ledger_generation_id=old.id,
            item_id=item.item_id,
            warehouse_ref1c="WH-1",
            on_hand=Decimal("999"),
        ),
        models.StockBin(
            ledger_generation_id=current.id,
            item_id=item.item_id,
            warehouse_ref1c="WH-1",
            on_hand=Decimal("6"),
        ),
        models.StockBin(
            ledger_generation_id=current.id,
            item_id=item.item_id,
            warehouse_ref1c="WH-2",
            on_hand=Decimal("50"),
        ),
    ])
    _future(db_session, old, item, qty="100")
    _future(db_session, current, item, ref="CURRENT", qty="4")
    _future(db_session, current, item, warehouse="WH-2", ref="OTHER", qty="30")
    _publish(db_session, current)

    result = build_ledger_projection(
        db_session,
        [LedgerProjectionKey("ITEM-1", "WH-1")],
        {"WH-1": "main"},
    )

    assert result.generation_id == current.id
    assert result.rows[0].generation_id == current.id
    assert result.rows[0].on_hand == Decimal("6")
    assert result.rows[0].inbound == Decimal("4")
    assert [line.source_ref for line in result.rows[0].future_supply] == ["CURRENT"]


def test_ambiguous_future_is_excluded_and_visible_as_diagnostic(db_session):
    generation = _generation(db_session, "ambiguous")
    item = _item(db_session)
    _future(db_session, generation, item, status="ambiguous", qty="9")
    _publish(db_session, generation)

    row = build_ledger_projection(
        db_session, [("ITEM-1", "WH-1")], {"WH-1": "main"}
    ).rows[0]

    assert row.inbound == Decimal("0")
    assert row.future_supply == ()
    assert len(row.excluded_future_supply) == 1
    assert row.excluded_future_supply[0].evidence_status == "ambiguous"
    assert row.excluded_future_supply[0].reason == "ambiguous destination"


def test_active_outstanding_reservation_and_coverage_are_ledger_derived(db_session):
    generation = _generation(db_session, "reservation")
    item = _item(db_session)
    reservation = _reservation(db_session, generation, item)
    closed = _reservation(db_session, generation, item)
    closed.lifecycle_status = "closed"
    _publish(db_session, generation)

    row = build_ledger_projection(
        db_session, [("ITEM-1", "WH-1")], {"WH-1": "main"}
    ).rows[0]

    assert row.outstanding_obligation_qty == Decimal("7")
    assert row.uncovered_qty == Decimal("2")
    assert [entry.reservation_id for entry in row.obligations] == [reservation.id]
    obligation = row.obligations[0]
    assert obligation.outstanding_qty == Decimal("7")
    assert obligation.uncovered_qty == Decimal("2")
    assert obligation.coverage[0].source_ref == "SUP-1"
    assert obligation.coverage[0].covered_qty == Decimal("4")


def test_ready_empty_truth_is_zero_not_unavailable_and_order_is_deterministic(db_session):
    generation = _generation(db_session, "empty")
    _item(db_session, "ITEM-A")
    _item(db_session, "ITEM-B")
    _publish(db_session, generation)

    result = build_ledger_projection(
        db_session,
        [("ITEM-B", "WH-2"), ("ITEM-A", "WH-1"), ("ITEM-A", "WH-1")],
        {"WH-1": "main", "WH-2": "other"},
    )

    assert [row.key for row in result.rows] == [
        LedgerProjectionKey("ITEM-A", "WH-1"),
        LedgerProjectionKey("ITEM-B", "WH-2"),
    ]
    assert all(row.on_hand == Decimal("0") for row in result.rows)
    assert all(row.inbound == Decimal("0") for row in result.rows)
    assert all(row.obligations == () for row in result.rows)


def test_unknown_item_code_fails_closed_before_returning_zero_projection(db_session):
    generation = _generation(db_session, "unknown-item")
    _item(db_session, "KNOWN")
    _publish(db_session, generation)

    with pytest.raises(ValueError, match="unknown item_code\\(s\\)") as raised:
        build_ledger_projection(
            db_session,
            [("KNOWN", "WH-1"), ("MISSING-B", "WH-2"), ("MISSING-A", "WH-3")],
            {"WH-1": "main", "WH-2": "other", "WH-3": "third"},
        )

    assert str(raised.value).endswith("'MISSING-A', 'MISSING-B'")


def test_building_generation_is_projected_without_current_pointer(db_session):
    generation = _generation(db_session, "building", accepted=False)
    item = _item(db_session)
    db_session.add_all([
        models.StockBin(
            ledger_generation_id=generation.id,
            item_id=item.item_id,
            warehouse_ref1c="WH-A",
            on_hand=Decimal("3"),
        ),
        models.StockBin(
            ledger_generation_id=generation.id,
            item_id=item.item_id,
            warehouse_ref1c="WH-B",
            on_hand=Decimal("5"),
        ),
    ])
    _future(
        db_session,
        generation,
        item,
        warehouse="WH-A",
        pool="assembly",
        qty="4",
    )
    _reservation(db_session, generation, item, pool="assembly")

    result = build_generation_projection(
        db_session,
        generation.id,
        [("ITEM-1", "WH-A")],
        {"WH-A": "assembly", "WH-B": "assembly"},
        "building",
    )

    assert result.generation_id == generation.id
    assert result.rows[0].on_hand == Decimal("8")
    assert result.rows[0].inbound == Decimal("4")
    assert result.rows[0].outstanding_obligation_qty == Decimal("7")


def test_two_requested_warehouses_for_same_item_and_pool_fail_closed(db_session):
    generation = _generation(db_session, "duplicate-axis", accepted=False)

    with pytest.raises(ValueError, match="duplicate DBR projection axis"):
        build_generation_projection(
            db_session,
            generation.id,
            [("ITEM-1", "WH-A"), ("ITEM-1", "WH-B")],
            {"WH-A": "main", "WH-B": "main"},
            "building",
        )


def test_same_item_in_two_pools_is_independent(db_session):
    generation = _generation(db_session, "two-pools", accepted=False)
    item = _item(db_session)
    db_session.add_all([
        models.StockBin(
            ledger_generation_id=generation.id,
            item_id=item.item_id,
            warehouse_ref1c="WH-A",
            on_hand=Decimal("3"),
        ),
        models.StockBin(
            ledger_generation_id=generation.id,
            item_id=item.item_id,
            warehouse_ref1c="WH-B",
            on_hand=Decimal("11"),
        ),
    ])
    _future(
        db_session,
        generation,
        item,
        warehouse="WH-A",
        pool="pool-a",
        ref="A",
        qty="2",
    )
    _future(
        db_session,
        generation,
        item,
        warehouse="WH-B",
        pool="pool-b",
        ref="B",
        qty="7",
    )
    _reservation(db_session, generation, item, pool="pool-a")

    result = build_generation_projection(
        db_session,
        generation.id,
        [("ITEM-1", "WH-A"), ("ITEM-1", "WH-B")],
        {"WH-A": "pool-a", "WH-B": "pool-b"},
        "building",
    )

    first, second = result.rows
    assert (first.on_hand, first.inbound, first.outstanding_obligation_qty) == (
        Decimal("3"), Decimal("2"), Decimal("7"),
    )
    assert (second.on_hand, second.inbound, second.outstanding_obligation_qty) == (
        Decimal("11"), Decimal("7"), Decimal("0"),
    )
