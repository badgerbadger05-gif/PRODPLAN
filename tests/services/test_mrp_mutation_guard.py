from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app import models
from app.services import one_c_purchase_order_export as purchase_exporter
from app.services.mrp_mutation_guard import (
    MrpMutationLineageError,
    require_current_run,
    require_materialized_orders,
    require_selected_proposals,
)
from app.services.planning_truth import PlanningTruthUnavailable


MUTATION_CAPABILITIES = {
    "physical_ledger": True,
    "reservation_replay": True,
    "execution_allocations": True,
}


def _generation(db, *, key, cutoff, parent=None, accept=True):
    physical = models.PhysicalImportBatch(
        batch_key=f"{key}-physical",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
        completed_at=cutoff,
    )
    db.add(physical)
    db.flush()
    marks = {"generation_kind": "physical_refresh"}
    if parent is not None:
        marks["parent_generation_id"] = int(parent.id)
    generation = models.LedgerGeneration(
        generation_key=key,
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        capabilities=dict(MUTATION_CAPABILITIES),
        source_watermarks=marks,
        physical_import_batch_id=physical.id,
        algorithm_version="test",
    )
    db.add(generation)
    db.flush()
    if accept:
        pointer = db.get(models.PlanningTruthState, 1)
        if pointer is None:
            db.add(
                models.PlanningTruthState(id=1, current_generation_id=int(generation.id))
            )
        else:
            pointer.current_generation_id = int(generation.id)
        db.flush()
    return generation


def _frozen_purchase_run(db, generation, *, code):
    item = models.Item(item_code=code, item_name=f"Item {code}")
    db.add(item)
    db.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=int(generation.id),
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    db.add(run)
    db.flush()
    purchase = models.PlannedPurchase(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=1,
        planned_qty=1,
        qty=1,
        need_date=date(2026, 7, 30),
        order_date=date(2026, 7, 23),
        lead_time_days=7,
        priority_index=1,
        bucket_date=date(2026, 7, 30),
        ledger_generation_id=int(generation.id),
    )
    db.add(purchase)
    db.flush()
    return run, purchase


def _historical_materialized_order(
    db,
    generation,
    *,
    order_qty: Decimal = Decimal("6"),
    current_remaining: Decimal = Decimal("100"),
):
    plan = models.ProductionPlanHeader(
        name="Historical execution plan",
        period_from=date(2026, 10, 1),
        period_to=date(2026, 10, 31),
        status="fixed",
    )
    item = models.Item(
        item_code="guard-historical-order",
        item_name="Historical order item",
    )
    db.add_all([plan, item])
    db.flush()
    source_run = models.PlanningRun(
        status="CLOSED",
        config_snapshot={},
        ledger_generation_id=int(generation.id),
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
        source_plan_id=int(plan.id),
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    live_run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=int(generation.id),
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
        source_plan_id=int(plan.id),
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    db.add_all([source_run, live_run])
    db.flush()
    old_requirement = models.MrpRequirement(
        run_id=int(source_run.run_id),
        item_id=int(item.item_id),
        total_required_qty=order_qty,
        net_required_qty=order_qty,
        period_from=plan.period_from,
        period_to=plan.period_to,
        freeze_version=1,
    )
    current_requirement = models.MrpRequirement(
        run_id=int(live_run.run_id),
        item_id=int(item.item_id),
        total_required_qty=current_remaining,
        net_required_qty=current_remaining,
        period_from=plan.period_from,
        period_to=plan.period_to,
        freeze_version=1,
    )
    db.add_all([old_requirement, current_requirement])
    db.flush()
    reservation = models.ReservationEntry(
        ledger_generation_id=int(generation.id),
        item_id=int(item.item_id),
        run_id=int(live_run.run_id),
        freeze_version=1,
        requirement_id=int(current_requirement.id),
        priority_period_from=plan.period_from,
        priority_period_to=plan.period_to,
        realization_mode="make",
        reserved_qty=current_remaining,
        replenishment_required_qty=current_remaining,
        lifecycle_status="active",
    )
    db.add(reservation)
    db.flush()
    db.add(models.ReplenishmentWorkItem(
        ledger_generation_id=int(generation.id),
        reservation_id=int(reservation.id),
        plan_id=int(plan.id),
        run_id=int(live_run.run_id),
        requirement_id=int(current_requirement.id),
        item_id=int(item.item_id),
        replenishment_method="make",
        replenishment_required_qty=current_remaining,
        replenishment_fulfilled_qty=Decimal("0"),
        replenishment_remaining_qty=current_remaining,
    ))
    order = models.ProductionOrder(
        order_number="MRP-HISTORICAL-1",
        order_date=datetime(2026, 8, 25, tzinfo=timezone.utc),
        source="mrp",
        source_run_id=int(source_run.run_id),
        deletion_mark=False,
    )
    db.add(order)
    db.flush()
    product = models.ProductionProduct(
        order_id=int(order.order_id),
        item_id=int(item.item_id),
        line_number=1,
        quantity=order_qty,
        produced_qty=Decimal("0"),
        remaining_qty=order_qty,
        source_mrp_requirement_id=int(old_requirement.id),
        ledger_generation_id=int(generation.id),
    )
    db.add(product)
    db.flush()
    return order, product, live_run


def test_guard_accepts_the_obligation_a_physical_refresh_inherited(db_session):
    """Materialization and 1C export must survive a fact-only fork.

    The obligation stays anchored where it was frozen, so demanding the accepted
    generation id here blocked every export from the first refresh onwards.
    """
    anchor = _generation(
        db_session, key="guard-anchor", cutoff=datetime(2026, 7, 23, tzinfo=timezone.utc)
    )
    run, purchase = _frozen_purchase_run(db_session, anchor, code="guard-inherited")
    child = _generation(
        db_session,
        key="guard-child",
        cutoff=datetime(2026, 7, 24, tzinfo=timezone.utc),
        parent=anchor,
    )

    resolved, generation_id = require_current_run(
        db_session, run.run_id, consumer="test"
    )

    assert int(resolved.run_id) == int(run.run_id)
    assert generation_id == int(child.id)
    # The proposal row is stamped with its own freeze generation, not today's.
    require_selected_proposals(
        db_session,
        [purchase],
        run=resolved,
        generation_id=generation_id,
        consumer="test",
    )


def test_guard_rejects_a_run_from_a_foreign_branch_of_truth(db_session):
    anchor = _generation(
        db_session, key="guard-fork-anchor", cutoff=datetime(2026, 7, 23, tzinfo=timezone.utc)
    )
    foreign = _generation(
        db_session,
        key="guard-foreign",
        cutoff=datetime(2026, 7, 24, tzinfo=timezone.utc),
        parent=anchor,
        accept=False,
    )
    run, _purchase = _frozen_purchase_run(db_session, foreign, code="guard-foreign-run")
    _generation(
        db_session,
        key="guard-accepted",
        cutoff=datetime(2026, 7, 25, tzinfo=timezone.utc),
        parent=anchor,
    )

    with pytest.raises(MrpMutationLineageError, match="outside the sealed lineage"):
        require_current_run(db_session, run.run_id, consumer="test")


def test_guard_rejects_a_proposal_stamped_outside_the_sealed_lineage(db_session):
    anchor = _generation(
        db_session, key="guard-prop-anchor", cutoff=datetime(2026, 7, 23, tzinfo=timezone.utc)
    )
    run, purchase = _frozen_purchase_run(db_session, anchor, code="guard-prop")
    sibling = _generation(
        db_session,
        key="guard-prop-sibling",
        cutoff=datetime(2026, 7, 24, tzinfo=timezone.utc),
        parent=anchor,
        accept=False,
    )
    child = _generation(
        db_session,
        key="guard-prop-child",
        cutoff=datetime(2026, 7, 25, tzinfo=timezone.utc),
        parent=anchor,
    )
    purchase.ledger_generation_id = int(sibling.id)
    db_session.flush()

    with pytest.raises(MrpMutationLineageError, match="stale Ledger lineage"):
        require_selected_proposals(
            db_session,
            [purchase],
            run=run,
            generation_id=int(child.id),
            consumer="test",
        )


def test_guard_accepts_closed_run_order_netted_by_current_make_work(db_session):
    generation = _generation(
        db_session,
        key="guard-historical-order",
        cutoff=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )
    order, _product, _live_run = _historical_materialized_order(
        db_session, generation
    )

    generation_id = require_materialized_orders(
        db_session, [order], consumer="test"
    )

    assert generation_id == int(generation.id)


def test_guard_rejects_closed_run_order_above_current_make_remainder(db_session):
    generation = _generation(
        db_session,
        key="guard-historical-overcommit",
        cutoff=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )
    order, _product, _live_run = _historical_materialized_order(
        db_session,
        generation,
        order_qty=Decimal("6"),
        current_remaining=Decimal("5"),
    )

    with pytest.raises(MrpMutationLineageError, match="exceeds current MAKE remainder"):
        require_materialized_orders(db_session, [order], consumer="test")


def test_dry_run_is_blocked_before_network_when_truth_unavailable(
    db_session, monkeypatch
):
    item = models.Item(
        item_code="guard-purchase-unavailable",
        item_name="Guard purchase",
        item_ref1c="ITEM-REF",
        supplier_ref1c="SUPPLIER-REF",
        status="active",
    )
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        active_freeze_version=1,
    )
    db_session.add_all([item, run])
    db_session.flush()
    purchase = models.PlannedPurchase(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=1,
        planned_qty=1,
        qty=1,
        need_date=date(2026, 7, 30),
        order_date=date(2026, 7, 23),
        lead_time_days=7,
        priority_index=1,
        bucket_date=date(2026, 7, 30),
    )
    db_session.add(purchase)
    db_session.commit()

    network_called = False

    def network_bomb(*_args, **_kwargs):
        nonlocal network_called
        network_called = True
        raise AssertionError("network client must not be constructed")

    monkeypatch.setattr(purchase_exporter, "_create_odata_client", network_bomb)
    with pytest.raises(PlanningTruthUnavailable):
        purchase_exporter.export_planned_purchases_to_1c(
            db_session,
            run.run_id,
            purchase_ids=[purchase.purchase_id],
            dry_run=True,
        )

    assert network_called is False
    assert db_session.query(models.SyncLink).count() == 0
    assert db_session.query(models.PurchaseExportLineAllocation).count() == 0
