from datetime import date, datetime
from decimal import Decimal

import pytest

from app import models
from app.services.generation_reconciliation import (
    GenerationReconciliationMismatch,
    build_generation_targets,
)


def _scope(db, key: str, *, mode: str, reserved: str, realized: str, uncovered: str):
    batch = models.PhysicalImportBatch(
        batch_key=f"batch-{key}", status="completed", cutoff=datetime(2026, 7, 23)
    )
    generation = models.LedgerGeneration(
        generation_key=f"generation-{key}",
        status="accepted",
        cutoff=datetime(2026, 7, 23),
        accepted_at=datetime(2026, 7, 23),
        physical_import_batch=batch,
        algorithm_version="test",
        replay_version="test",
        source_watermarks={},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
        },
    )
    item = models.Item(item_code=f"I-{key}", item_name=key)
    run = models.PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={})
    db.add_all([generation, item, run])
    db.flush()
    truth_state = db.get(models.PlanningTruthState, 1)
    if truth_state is None:
        truth_state = models.PlanningTruthState(id=1)
        db.add(truth_state)
    truth_state.current_generation_id = generation.id
    db.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=999999,
        net_required_qty=999999,
        period_from=date(2026, 7, 1),
        period_to=date(2026, 7, 31),
        bom_level=0,
    )
    db.add(requirement)
    db.flush()
    reservation = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="default",
        run_id=run.run_id,
        freeze_version=1,
        requirement_id=requirement.id,
        priority_period_from=date(2026, 7, 1),
        priority_period_to=date(2026, 7, 31),
        realization_mode=mode,
        reserved_qty=Decimal(reserved),
        realized_qty=Decimal(realized),
        covered_from_stock_at_freeze_qty=Decimal("0"),
        replenishment_required_qty=Decimal(reserved),
        replenishment_received_qty=Decimal(realized),
        lifecycle_status="active",
    )
    db.add(reservation)
    db.flush()
    db.add(
        models.ReservationEvent(
            ledger_generation_id=generation.id,
            reservation_id=reservation.id,
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            planning_stock_pool="default",
            event_kind="open",
            reserved_delta=Decimal(reserved),
            realized_delta=0,
            idempotency_key=f"{key}:open",
        )
    )
    if Decimal(realized):
        db.add(
            models.ReservationEvent(
                ledger_generation_id=generation.id,
                reservation_id=reservation.id,
                item_id=item.item_id,
                characteristic_ref="",
                organization_ref="",
                planning_stock_pool="default",
                event_kind="realize",
                reserved_delta=0,
                realized_delta=Decimal(realized),
                idempotency_key=f"{key}:realize",
            )
        )
    db.flush()
    return generation, run, requirement, reservation


def test_make_target_reads_single_replenishment_reservation(db_session):
    generation, run, requirement, make = _scope(
        db_session, "make", mode="make", reserved="10", realized="4", uncovered="91"
    )
    targets = build_generation_targets(
        db_session, ledger_generation_id=generation.id, run_id=run.run_id
    )

    assert targets[(requirement.id, "make")].target_qty == Decimal("6")


def test_buy_target_ignores_legacy_uncovered_cache(db_session):
    generation, run, requirement, _reservation = _scope(
        db_session, "buy", mode="buy", reserved="10", realized="0", uncovered="4"
    )
    targets = build_generation_targets(
        db_session, ledger_generation_id=generation.id, run_id=run.run_id
    )
    assert targets[(requirement.id, "buy")].target_qty == Decimal("10")


def test_two_generations_are_isolated_and_accepted_rows_remain_unchanged(db_session):
    first = _scope(
        db_session, "g1", mode="make", reserved="10", realized="4", uncovered="0"
    )
    second = _scope(
        db_session, "g2", mode="make", reserved="20", realized="5", uncovered="0"
    )
    before = (
        first[3].reserved_qty,
        first[3].realized_qty,
        first[3].replenishment_required_qty,
    )

    with pytest.raises(GenerationReconciliationMismatch, match="currently published"):
        build_generation_targets(
            db_session, ledger_generation_id=first[0].id, run_id=first[1].run_id
        )
    target = build_generation_targets(
        db_session, ledger_generation_id=second[0].id, run_id=second[1].run_id
    )

    assert target[(second[2].id, "make")].target_qty == Decimal("15")
    assert (
        first[3].reserved_qty,
        first[3].realized_qty,
        first[3].replenishment_required_qty,
    ) == before
    assert first[2].id not in {key[0] for key in target}


def test_reservation_cache_mismatch_fails_closed(db_session):
    generation, run, _requirement, reservation = _scope(
        db_session, "bad", mode="buy", reserved="9", realized="4", uncovered="2"
    )
    reservation.realized_qty = Decimal("3")
    db_session.flush()
    before_products = db_session.query(models.ProductionProduct).count()
    before_purchases = db_session.query(models.PlannedPurchase).count()

    with pytest.raises(GenerationReconciliationMismatch, match="cache does not equal"):
        build_generation_targets(
            db_session, ledger_generation_id=generation.id, run_id=run.run_id
        )
    assert db_session.query(models.ProductionProduct).count() == before_products
    assert db_session.query(models.PlannedPurchase).count() == before_purchases


def test_supplier_receipt_execution_allocation_is_reconciled_as_buy(db_session):
    batch = models.PhysicalImportBatch(
        batch_key="supplier-reconcile",
        status="completed",
        cutoff=datetime(2026, 7, 23),
    )
    generation = models.LedgerGeneration(
        generation_key="supplier-reconcile-generation",
        status="accepted",
        cutoff=datetime(2026, 7, 23),
        accepted_at=datetime(2026, 7, 23),
        physical_import_batch=batch,
        algorithm_version="test",
        replay_version="test",
        source_watermarks={},
        capabilities={},
    )
    item = models.Item(item_code="SUP-REC", item_name="Supplier receipt item")
    run = models.PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={})
    db_session.add_all([batch, generation, item, run])
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=10,
        net_required_qty=10,
        period_from=date(2026, 7, 1),
        period_to=date(2026, 7, 31),
        bom_level=1,
    )
    db_session.add(requirement)
    db_session.flush()
    reservation = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="default",
        run_id=run.run_id,
        freeze_version=1,
        requirement_id=requirement.id,
        priority_period_from=date(2026, 7, 1),
        priority_period_to=date(2026, 7, 31),
        realization_mode="buy",
        reserved_qty=Decimal("10"),
        realized_qty=Decimal("4"),
        replenishment_required_qty=Decimal("10"),
        replenishment_received_qty=Decimal("4"),
    )
    db_session.add(reservation)
    db_session.flush()
    db_session.add_all([
        models.ReservationEvent(
            ledger_generation_id=generation.id,
            reservation_id=reservation.id,
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            planning_stock_pool="default",
            event_kind="open",
            reserved_delta=Decimal("10"),
            realized_delta=Decimal("0"),
            idempotency_key="buy:open",
        ),
        models.ReservationEvent(
            ledger_generation_id=generation.id,
            reservation_id=reservation.id,
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            planning_stock_pool="default",
            event_kind="realize",
            reserved_delta=Decimal("0"),
            realized_delta=Decimal("4"),
            idempotency_key="buy:realize",
        ),
    ])
    req = requirement
    db_session.add(
        models.PlanningTruthState(
            id=1,
            current_generation_id=generation.id,
        )
    ) if db_session.get(models.PlanningTruthState, 1) is None else None
    db_session.flush()

    targets = build_generation_targets(db_session, ledger_generation_id=generation.id, run_id=run.run_id)
    assert targets[(req.id, "buy")].realized_qty == Decimal("4")
    assert targets[(req.id, "buy")].target_qty == Decimal("6")
