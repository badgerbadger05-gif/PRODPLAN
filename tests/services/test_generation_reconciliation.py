from datetime import date, datetime
from decimal import Decimal

import pytest

from app import models
from app.services.generation_reconciliation import (
    GenerationReconciliationMismatch,
    build_generation_targets,
)
from app.services.mrp_reconciliation import _own_purchase_coverage
from app.services.mrp_reconciliation import reconcile_snapshot


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
        executed_qty=888888,
        covered_qty=777777,
        remaining_qty=666666,
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
        uncovered_qty=Decimal(uncovered),
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
        db.add(
            models.MrpExecutionAllocation(
                ledger_generation_id=generation.id,
                cycle_id=f"cycle-{key}",
                requirement_id=requirement.id,
                fact_type=(
                    "unlinked_production" if mode == "make" else "component_consumption"
                ),
                allocation_kind="execution",
                fact_ref=f"fact-{key}",
                fact_line_ref="1",
                allocated_qty=Decimal(realized),
            )
        )
    db.flush()
    return generation, run, requirement, reservation


def test_make_and_consume_targets_ignore_poisoned_requirement_caches(db_session):
    generation, run, requirement, make = _scope(
        db_session, "make", mode="make", reserved="10", realized="4", uncovered="91"
    )
    consume = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=make.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="default",
        run_id=run.run_id,
        freeze_version=1,
        requirement_id=requirement.id,
        priority_period_from=date(2026, 7, 1),
        priority_period_to=date(2026, 7, 31),
        realization_mode="consume",
        reserved_qty=Decimal("8"),
        realized_qty=Decimal("0"),
        uncovered_qty=Decimal("3"),
        lifecycle_status="active",
    )
    db_session.add(consume)
    db_session.flush()
    db_session.add(models.ReservationEvent(
        ledger_generation_id=generation.id,
        reservation_id=consume.id,
        item_id=consume.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="default",
        event_kind="open",
        reserved_delta=Decimal("8"),
        realized_delta=0,
        idempotency_key="consume:open",
    ))
    db_session.flush()

    targets = build_generation_targets(
        db_session, ledger_generation_id=generation.id, run_id=run.run_id
    )

    assert targets[(requirement.id, "make")].target_qty == Decimal("6")
    assert targets[(requirement.id, "consume")].target_qty == Decimal("3")


def test_two_generations_are_isolated_and_accepted_rows_remain_unchanged(db_session):
    first = _scope(
        db_session, "g1", mode="make", reserved="10", realized="4", uncovered="0"
    )
    second = _scope(
        db_session, "g2", mode="make", reserved="20", realized="5", uncovered="0"
    )
    before = (first[3].reserved_qty, first[3].realized_qty, first[3].uncovered_qty)

    with pytest.raises(GenerationReconciliationMismatch, match="currently published"):
        build_generation_targets(
            db_session, ledger_generation_id=first[0].id, run_id=first[1].run_id
        )
    target = build_generation_targets(
        db_session, ledger_generation_id=second[0].id, run_id=second[1].run_id
    )

    assert target[(second[2].id, "make")].target_qty == Decimal("15")
    assert (first[3].reserved_qty, first[3].realized_qty, first[3].uncovered_qty) == before
    assert first[2].id not in {key[0] for key in target}


def test_allocation_mismatch_fails_closed(db_session):
    generation, run, requirement, _reservation = _scope(
        db_session, "bad", mode="consume", reserved="9", realized="4", uncovered="2"
    )
    allocation = db_session.query(models.MrpExecutionAllocation).filter_by(
        ledger_generation_id=generation.id,
        requirement_id=requirement.id,
    ).one()
    allocation.allocated_qty = Decimal("3")
    db_session.flush()
    before_products = db_session.query(models.ProductionProduct).count()
    before_purchases = db_session.query(models.PlannedPurchase).count()

    with pytest.raises(GenerationReconciliationMismatch, match="allocation=3"):
        build_generation_targets(
            db_session, ledger_generation_id=generation.id, run_id=run.run_id
        )
    assert db_session.query(models.ProductionProduct).count() == before_products
    assert db_session.query(models.PlannedPurchase).count() == before_purchases


def test_purchase_commitment_is_generation_scoped_and_not_receipt_netting(db_session):
    generation, run, requirement, _reservation = _scope(
        db_session, "purchase", mode="consume", reserved="10", realized="0", uncovered="10"
    )
    old = models.LedgerGeneration(
        generation_key="generation-old-proposal",
        status="accepted",
        cutoff=datetime(2026, 7, 1),
        accepted_at=datetime(2026, 7, 1),
        physical_import_batch=models.PhysicalImportBatch(
            batch_key="batch-old-proposal", status="completed", cutoff=datetime(2026, 7, 1)
        ),
        algorithm_version="test",
        source_watermarks={},
        capabilities={},
    )
    db_session.add(old)
    db_session.flush()
    common = dict(
        run_id=run.run_id,
        item_id=requirement.item_id,
        requested_qty=4,
        planned_qty=4,
        qty=4,
        need_date=date(2026, 7, 31),
        order_date=date(2026, 7, 20),
        lead_time_days=3,
        bucket_date=date(2026, 7, 31),
        source_mrp_requirement_id=requirement.id,
    )
    current_pp = models.PlannedPurchase(**common, ledger_generation_id=generation.id)
    old_pp = models.PlannedPurchase(**common, ledger_generation_id=old.id)
    db_session.add_all([current_pp, old_pp])
    db_session.flush()
    for pp, ref in ((current_pp, "CURRENT"), (old_pp, "OLD")):
        db_session.add(models.SyncLink(
            source_system="PRODPLAN",
            source_doctype="planned_purchase",
            source_id=pp.purchase_id,
            target_system="1C",
            target_entity="Document_ЗаказПоставщику",
            target_ref_key=ref,
            status="success",
        ))
    db_session.flush()

    exported, unexported, commitments = _own_purchase_coverage(
        db_session,
        run,
        ledger_generation_id=generation.id,
        generation_truth=True,
    )

    assert exported == {current_pp.purchase_id}
    assert unexported == {}
    assert commitments == {requirement.item_id: 4.0}


def test_strict_reconcile_skips_all_legacy_repairs(db_session, monkeypatch):
    generation, run, requirement, _reservation = _scope(
        db_session, "no-repairs", mode="make", reserved="0", realized="0", uncovered="0"
    )
    item = db_session.get(models.Item, requirement.item_id)
    item.replenishment_method = "Производство"
    plan = models.ProductionPlanHeader(
        name="Ledger strict",
        period_from=date(2026, 7, 1),
        period_to=date(2026, 7, 31),
        status="fixed",
        created_by="test",
    )
    db_session.add(plan)
    db_session.flush()
    run.source_plan_id = plan.id
    run.period_from = plan.period_from
    run.period_to = plan.period_to
    run.active_freeze_version = 1
    db_session.flush()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy structural repair was called")

    monkeypatch.setattr("app.services.mrp_reconciliation._run_repairs", forbidden)
    result = reconcile_snapshot(db_session, run.run_id)

    assert result["status"] == "ok"
    assert result["binding_repair"] == {
        "status": "skipped",
        "reason": "generation_strict",
    }
    assert result["mrp_order_repair"]["status"] == "skipped"
