from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.local_mrp_order_reconciliation import reconcile_local_mrp_orders


def _scenario(
    db,
    *,
    order_qty: Decimal,
    target_qty: Decimal,
    order_ref1c: str | None = None,
):
    cutoff = datetime(2026, 9, 4, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key=f"reconcile-physical-{order_qty}-{target_qty}-{order_ref1c}",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
        completed_at=cutoff,
    )
    db.add(physical)
    db.flush()
    generation = models.LedgerGeneration(
        generation_key=f"reconcile-{order_qty}-{target_qty}-{order_ref1c}",
        status="building",
        cutoff=cutoff,
        capabilities={},
        source_watermarks={"generation_kind": "obligation_refresh"},
        physical_import_batch_id=int(physical.id),
        algorithm_version="test",
    )
    plan = models.ProductionPlanHeader(
        name=f"Reconcile {order_qty} to {target_qty}",
        period_from=date(2026, 10, 1),
        period_to=date(2026, 10, 31),
        status="fixed",
    )
    item = models.Item(
        item_code=f"reconcile-{order_qty}-{target_qty}-{order_ref1c}",
        item_name="Reconciliation item",
    )
    db.add_all([generation, plan, item])
    db.flush()
    historical_run = models.PlanningRun(
        status="CLOSED",
        config_snapshot={},
        ledger_generation_id=int(generation.id),
        ledger_cutoff=cutoff,
        active_freeze_version=1,
        source_plan_id=int(plan.id),
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    live_run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=int(generation.id),
        ledger_cutoff=cutoff,
        active_freeze_version=1,
        source_plan_id=int(plan.id),
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    db.add_all([historical_run, live_run])
    db.flush()
    historical_requirement = models.MrpRequirement(
        run_id=int(historical_run.run_id),
        item_id=int(item.item_id),
        total_required_qty=order_qty,
        net_required_qty=order_qty,
        period_from=plan.period_from,
        period_to=plan.period_to,
        freeze_version=1,
    )
    live_requirement = models.MrpRequirement(
        run_id=int(live_run.run_id),
        item_id=int(item.item_id),
        total_required_qty=target_qty,
        net_required_qty=target_qty,
        period_from=plan.period_from,
        period_to=plan.period_to,
        freeze_version=1,
    )
    db.add_all([historical_requirement, live_requirement])
    db.flush()
    reservation = models.ReservationEntry(
        ledger_generation_id=int(generation.id),
        item_id=int(item.item_id),
        run_id=int(live_run.run_id),
        freeze_version=1,
        requirement_id=int(live_requirement.id),
        priority_period_from=plan.period_from,
        priority_period_to=plan.period_to,
        realization_mode="make",
        reserved_qty=target_qty,
        replenishment_required_qty=target_qty,
        lifecycle_status="active",
    )
    db.add(reservation)
    db.flush()
    if target_qty > 0:
        db.add(
            models.ReplenishmentWorkItem(
                ledger_generation_id=int(generation.id),
                reservation_id=int(reservation.id),
                plan_id=int(plan.id),
                run_id=int(live_run.run_id),
                requirement_id=int(live_requirement.id),
                item_id=int(item.item_id),
                replenishment_method="make",
                replenishment_required_qty=target_qty,
                replenishment_fulfilled_qty=Decimal("0"),
                replenishment_remaining_qty=target_qty,
            )
        )
    order = models.ProductionOrder(
        order_number=f"MRP-RECONCILE-{order_qty}",
        order_date=cutoff,
        order_ref1c=order_ref1c,
        source="mrp",
        source_run_id=int(historical_run.run_id),
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
        source_mrp_requirement_id=int(historical_requirement.id),
        ledger_generation_id=int(generation.id),
    )
    db.add(product)
    db.flush()
    return generation, live_run, order, product


@pytest.mark.parametrize(
    ("order_qty", "target_qty"),
    [
        (Decimal("40"), Decimal("30")),
        (Decimal("30"), Decimal("50")),
    ],
)
def test_reconciles_untouched_local_order_both_directions(
    db_session, order_qty, target_qty
):
    generation, live_run, order, product = _scenario(
        db_session,
        order_qty=order_qty,
        target_qty=target_qty,
    )

    summary = reconcile_local_mrp_orders(
        db_session,
        ledger_generation_id=int(generation.id),
        live_run_ids=[int(live_run.run_id)],
    )

    assert Decimal(product.quantity) == target_qty
    assert Decimal(product.remaining_qty) == target_qty
    assert order.deletion_mark is False
    assert summary["resized"] == 1
    assert summary["cancelled"] == 0
    assert Decimal(summary["entries"][0]["previous_quantity"]) == order_qty
    assert Decimal(summary["entries"][0]["quantity"]) == target_qty


def test_cancels_untouched_local_order_when_make_target_disappears(db_session):
    generation, live_run, order, product = _scenario(
        db_session,
        order_qty=Decimal("40"),
        target_qty=Decimal("0"),
    )

    summary = reconcile_local_mrp_orders(
        db_session,
        ledger_generation_id=int(generation.id),
        live_run_ids=[int(live_run.run_id)],
    )

    assert order.deletion_mark is True
    assert product.control_state.status == "cancelled"
    assert summary["resized"] == 0
    assert summary["cancelled"] == 1


def test_preserves_order_already_linked_to_1c(db_session):
    generation, live_run, order, product = _scenario(
        db_session,
        order_qty=Decimal("40"),
        target_qty=Decimal("30"),
        order_ref1c="11111111-1111-1111-1111-111111111111",
    )

    summary = reconcile_local_mrp_orders(
        db_session,
        ledger_generation_id=int(generation.id),
        live_run_ids=[int(live_run.run_id)],
    )

    assert Decimal(product.quantity) == Decimal("40")
    assert order.deletion_mark is False
    assert summary["resized"] == 0
    assert summary["cancelled"] == 0
    assert summary["locked"] == 1
    assert summary["entries"] == []


def test_preserves_order_after_even_a_failed_1c_export_attempt(db_session):
    generation, live_run, order, product = _scenario(
        db_session,
        order_qty=Decimal("40"),
        target_qty=Decimal("30"),
    )
    db_session.add(
        models.SyncLink(
            source_system="PRODPLAN",
            source_doctype="production_order",
            source_id=int(order.order_id),
            target_system="1C",
            target_entity="Document_ЗаказНаПроизводство",
            target_number=str(order.order_number),
            payload_hash="failed-attempt",
            status="error",
            last_error="timeout",
        )
    )
    db_session.flush()

    summary = reconcile_local_mrp_orders(
        db_session,
        ledger_generation_id=int(generation.id),
        live_run_ids=[int(live_run.run_id)],
    )

    assert Decimal(product.quantity) == Decimal("40")
    assert summary["resized"] == 0
    assert summary["locked"] == 1


def test_local_draft_material_issue_does_not_lock_quantity(db_session):
    generation, live_run, order, product = _scenario(
        db_session,
        order_qty=Decimal("40"),
        target_qty=Decimal("30"),
    )
    db_session.add(
        models.ProductionMaterialIssue(
            document_number="LOCAL-DRAFT-ISSUE",
            product_id=int(product.product_id),
            order_id=int(order.order_id),
            status="draft",
            direction="issue",
        )
    )
    db_session.flush()

    summary = reconcile_local_mrp_orders(
        db_session,
        ledger_generation_id=int(generation.id),
        live_run_ids=[int(live_run.run_id)],
    )

    assert Decimal(product.quantity) == Decimal("30")
    assert summary["resized"] == 1
    assert summary["locked"] == 0


def test_exported_material_issue_locks_quantity(db_session):
    generation, live_run, order, product = _scenario(
        db_session,
        order_qty=Decimal("40"),
        target_qty=Decimal("30"),
    )
    db_session.add(
        models.ProductionMaterialIssue(
            document_number="EXPORTED-ISSUE",
            product_id=int(product.product_id),
            order_id=int(order.order_id),
            status="requested",
            direction="issue",
            exported_ref1c="22222222-2222-2222-2222-222222222222",
        )
    )
    db_session.flush()

    summary = reconcile_local_mrp_orders(
        db_session,
        ledger_generation_id=int(generation.id),
        live_run_ids=[int(live_run.run_id)],
    )

    assert Decimal(product.quantity) == Decimal("40")
    assert summary["resized"] == 0
    assert summary["locked"] == 1
