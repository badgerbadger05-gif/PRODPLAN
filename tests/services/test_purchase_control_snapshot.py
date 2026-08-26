from copy import deepcopy
from datetime import date, datetime, timezone
import math
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app import models
from app.routers.purchase_control import get_filters, get_order
from app.services.planning_truth import publish_generation
from app.services.purchase_control_journal import (
    get_selection_summary,
    get_order_card,
    _materialization_action,
    _reconcile_buy_row_for_horizon,
    list_filters,
    list_journal,
)
from app.routers.purchase_control import get_orders
from app.services.purchase_control_snapshot import (
    PurchaseJournalSnapshotUnavailable,
    PurchaseJournalPromotionError,
    build_candidate_snapshot,
    open_supplier_coverage_by_reservation,
    promote_candidate_snapshot,
)


CAPABILITIES = {
    "physical_ledger": True,
    "reservation_replay": True,
    "planning_snapshots": True,
    "purchase_control_journal": True,
}


def test_materialization_action_is_server_owned_and_fail_closed():
    assert _materialization_action({
        "row_generator": "mrp_reservation",
        "line_status": "to_order",
        "to_order_qty": 2,
    }) == (True, None)
    allowed, reason = _materialization_action({
        "row_generator": "mrp_reservation",
        "line_status": "to_order",
        "to_order_qty": None,
    })
    assert allowed is False
    assert reason == "Количество к заказу отсутствует"


def _context(db):
    cutoff = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="purchase-snapshot-physical",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="purchase-snapshot-generation",
        status="building",
        cutoff=cutoff,
        source_watermarks={},
        capabilities={},
        physical_import_batch=physical,
        algorithm_version="test",
    )
    supplier = models.Supplier(supplier_ref1c="SUP-1", supplier_name="Альфа")
    items = [
        models.Item(item_code="MAT-B", item_name="Материал Б", unit="шт"),
        models.Item(item_code="MAT-A", item_name="Материал А", unit="кг"),
    ]
    db.add_all([generation, supplier, *items])
    db.flush()
    order = models.SupplierOrder(
        order_number="ЗП-100",
        order_date=datetime(2026, 7, 1),
        order_ref1c="ORDER-1",
        supplier_id=supplier.supplier_id,
        order_state_name="В закупку",
    )
    db.add(order)
    db.flush()
    legacy_lines = [
        models.SupplierOrderItem(
            order_id=order.order_id, item_id_ref=item.item_id, line_number=index,
            quantity=Decimal("999"), received_qty=Decimal("998"),
            remaining_qty=Decimal("1"), delivery_date=datetime(2026, 8, index),
        )
        for index, item in enumerate(items, 1)
    ]
    batch = models.LedgerBuildBatch(
        ledger_generation_id=generation.id,
        stage="snapshot_build",
        batch_key="purchase-snapshot-build",
        status="building",
        algorithm_version="test",
        metrics={},
    )
    db.add_all([*legacy_lines, batch])
    db.flush()
    supplies = [
        models.LedgerFutureSupply(
            ledger_generation_id=generation.id,
            supply_kind="supplier_order",
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            planning_stock_pool="main",
            destination_warehouse_ref1c="WH-1",
            source_ref=order.order_ref1c,
            source_line_ref=str(index),
            source_local_id=str(line.item_id),
            ordered_qty_at_cutoff=Decimal(str(10 * index)),
            realized_qty_at_cutoff=Decimal(str(index)),
            open_qty_at_cutoff=Decimal(str(10 * index - index)),
            eta_date=date(2026, 8, index),
            source_state_key="В закупку",
            capture_cutoff=cutoff,
            source_content_hash=f"hash-{index}",
            capture_batch_id=batch.id,
            evidence_status="exact",
        )
        for index, (item, line) in enumerate(zip(items, legacy_lines), 1)
    ]
    db.add_all(supplies)
    db.flush()
    return generation, order, legacy_lines, supplies


def _accept(db, generation):
    snapshot = build_candidate_snapshot(db, generation.id)
    accepted_at = datetime(2026, 7, 23, 13, tzinfo=timezone.utc)
    generation.status = "accepted"
    generation.accepted_at = accepted_at
    generation.capabilities = dict(CAPABILITIES)
    snapshot.truth_status = "accepted"
    snapshot.reason = None
    snapshot.published_at = accepted_at
    publish_generation(db, generation)
    db.flush()
    return snapshot


def _add_buy_plan_run(
    db,
    *,
    generation,
    period_from: date,
    period_to: date,
    item,
    required_qty: Decimal,
    realized_qty: Decimal,
    covered_incoming: Decimal,
    uncovered: Decimal,
    planning_stock_pool: str = "main",
):
    run = models.ProductionPlanHeader(
        name=f"purchase-buy-run-{period_to.isoformat()}",
        period_from=period_from,
        period_to=period_to,
        status="fixed",
    )
    db.add(run)
    db.flush()

    planning_run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        source_plan_id=run.id,
        period_from=period_from,
        period_to=period_to,
        ledger_generation_id=generation.id,
    )
    db.add(planning_run)
    db.flush()

    requirement = models.MrpRequirement(
        run_id=planning_run.run_id,
        item_id=item.item_id,
        total_required_qty=required_qty,
        net_required_qty=required_qty,
        period_from=period_from,
        period_to=period_to,
        bom_level=1,
    )
    db.add(requirement)
    db.flush()

    reservation = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool=planning_stock_pool,
        run_id=planning_run.run_id,
        freeze_version=0,
        requirement_id=requirement.id,
        priority_period_from=period_from,
        priority_period_to=period_to,
        realization_mode="buy",
        reserved_qty=required_qty,
        realized_qty=realized_qty,
        covered_from_stock_at_freeze_qty=Decimal("0"),
        replenishment_required_qty=required_qty,
        replenishment_received_qty=realized_qty,
        lifecycle_status="active",
    )
    db.add(reservation)
    db.flush()
    work_item = models.ReplenishmentWorkItem(
        ledger_generation_id=generation.id,
        reservation=reservation,
        plan_id=run.id,
        run_id=planning_run.run_id,
        requirement_id=requirement.id,
        item_id=item.item_id,
        replenishment_method="buy",
        replenishment_required_qty=required_qty,
        replenishment_fulfilled_qty=realized_qty,
        replenishment_remaining_qty=required_qty - realized_qty,
    )
    db.add(work_item)
    db.flush()

    db.flush()

    return planning_run


def _build_buy_horizon_generation(
    db,
    *,
    accounting_price: Decimal | None = Decimal("12.50"),
):
    cutoff = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="purchase-snapshot-buy-horizon",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="purchase-snapshot-buy-horizon-generation",
        status="building",
        cutoff=cutoff,
        source_watermarks={},
        capabilities={},
        physical_import_batch=physical,
        algorithm_version="test",
    )
    item = models.Item(
        item_code="RAW-HRZ",
        item_name="Материал HORZ",
        item_article="ARTICLE-HRZ",
        accounting_price=accounting_price,
        unit="шт",
        supplier_ref1c="SUP-HRZ",
    )
    supplier = models.Supplier(supplier_ref1c="SUP-HRZ", supplier_name="Гамма")
    db.add_all([generation, item, supplier])
    db.flush()

    aug = _add_buy_plan_run(
        db,
        generation=generation,
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        item=item,
        required_qty=Decimal("10"),
        realized_qty=Decimal("3"),
        covered_incoming=Decimal("2"),
        uncovered=Decimal("5"),
    )
    sep = _add_buy_plan_run(
        db,
        generation=generation,
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        item=item,
        required_qty=Decimal("10"),
        realized_qty=Decimal("2"),
        covered_incoming=Decimal("1"),
        uncovered=Decimal("7"),
    )

    snapshot = build_candidate_snapshot(db, generation.id)
    _accept(db, generation)
    return generation, item, aug, sep, snapshot


def test_candidate_is_idempotent_and_groups_multiple_lines(db_session):
    generation, order, _legacy, _supplies = _context(db_session)

    first = build_candidate_snapshot(db_session, generation.id)
    second = build_candidate_snapshot(db_session, generation.id)

    assert second.id == first.id
    assert second.payload == first.payload
    assert first.payload["rows"] == []
    assert [line["item_code"] for line in first.payload["cards"][str(order.order_id)]["lines"]] == [
        "MAT-A", "MAT-B",
    ]
    assert all(row["row_generator"] == "mrp_reservation" for row in first.payload["rows"])


@pytest.mark.parametrize(
    "bad_qty",
    [None, "", True, math.nan, math.inf, -math.inf],
)
def test_candidate_promotion_rejects_nonfinite_or_blank_numeric(db_session, bad_qty):
    generation, _order, _legacy, _supplies = _context(db_session)
    item = db_session.query(models.Item).filter_by(item_code="MAT-B").one()
    _add_buy_plan_run(
        db_session,
        generation=generation,
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        item=item,
        required_qty=Decimal("7"),
        realized_qty=Decimal("2"),
        covered_incoming=Decimal("1"),
        uncovered=Decimal("4"),
    )
    snapshot = build_candidate_snapshot(db_session, generation.id)
    row = next(
        row
        for row in snapshot.payload["rows"]
        if row["row_generator"] == "mrp_reservation"
    )
    row["remaining_qty"] = bad_qty

    with pytest.raises(PurchaseJournalPromotionError):
        promote_candidate_snapshot(
            db_session,
            generation=generation,
            accepted_at=datetime(2026, 7, 23, 13, tzinfo=timezone.utc),
        )


def test_direct_supplier_supply_covers_frozen_reservations_fifo(db_session):
    generation, _order, _legacy, supplies = _context(db_session)
    item = db_session.query(models.Item).filter_by(item_code="MAT-B").one()
    senior_run = _add_buy_plan_run(
        db_session,
        generation=generation,
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        item=item,
        required_qty=Decimal("7"),
        realized_qty=Decimal("0"),
        covered_incoming=Decimal("0"),
        uncovered=Decimal("7"),
    )
    junior_run = _add_buy_plan_run(
        db_session,
        generation=generation,
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        item=item,
        required_qty=Decimal("7"),
        realized_qty=Decimal("0"),
        covered_incoming=Decimal("0"),
        uncovered=Decimal("7"),
    )
    # This exact generation-scoped line has no MrpFreezeAllocation: it models a
    # supplier order created directly in 1C after both plans were frozen.
    supplies[0].open_qty_at_cutoff = Decimal("10")
    supplies[0].ordered_qty_at_cutoff = Decimal("10")
    supplies[0].realized_qty_at_cutoff = Decimal("0")
    entries = (
        db_session.query(
            models.ReplenishmentWorkItem,
            models.ReservationEntry,
            models.Item,
        )
        .join(
            models.ReservationEntry,
            models.ReservationEntry.id
            == models.ReplenishmentWorkItem.reservation_id,
        )
        .join(models.Item, models.Item.item_id == models.ReservationEntry.item_id)
        .filter(
            models.ReplenishmentWorkItem.run_id.in_(
                (senior_run.run_id, junior_run.run_id)
            )
        )
        .all()
    )

    covered, slices = open_supplier_coverage_by_reservation(
        db_session,
        generation.id,
        entries,
    )

    reservations = {
        int(reservation.run_id): reservation
        for _work_item, reservation, _item in entries
    }
    senior = reservations[senior_run.run_id]
    junior = reservations[junior_run.run_id]
    assert covered == {senior.id: 7.0, junior.id: 3.0}
    assert slices[senior.id][0]["source_ref"] == supplies[0].source_ref
    assert slices[senior.id][0]["covered_qty"] == 7.0
    assert slices[junior.id][0]["covered_qty"] == 3.0
    assert sum(
        row["covered_qty"]
        for reservation_slices in slices.values()
        for row in reservation_slices
    ) == 10.0


def test_candidate_conflict_is_rejected(db_session):
    generation, _order, _legacy, supplies = _context(db_session)
    build_candidate_snapshot(db_session, generation.id)
    supplies[0].open_qty_at_cutoff = Decimal("8")

    with pytest.raises(ValueError, match="candidate conflict"):
        build_candidate_snapshot(db_session, generation.id)


def test_candidate_rejects_open_greater_than_ordered(db_session):
    generation, _order, _legacy, supplies = _context(db_session)
    supplies[0].open_qty_at_cutoff = Decimal("11")

    with pytest.raises(ValueError, match="ordered/open invariant"):
        build_candidate_snapshot(db_session, generation.id)


def test_public_reads_are_byte_stable_after_legacy_line_mutation(db_session):
    generation, order, legacy, _supplies = _context(db_session)
    _accept(db_session, generation)
    before_list = deepcopy(list_journal(db_session, active_only=False))
    before_card = deepcopy(get_order_card(db_session, order.order_id))

    legacy[0].quantity = Decimal("1")
    legacy[0].received_qty = Decimal("1")
    legacy[0].remaining_qty = Decimal("0")
    legacy[1].quantity = Decimal("5000")
    legacy[1].remaining_qty = Decimal("5000")
    db_session.flush()

    assert list_journal(db_session, active_only=False) == before_list
    assert get_order_card(db_session, order.order_id) == before_card


def test_filters_sort_pagination_and_summary_use_only_snapshot(db_session):
    generation, _order, _legacy, _supplies = _context(db_session)
    _accept(db_session, generation)

    result = list_journal(
        db_session, search="материал", supplier_id=1, sort_by="remaining_qty",
        sort_dir="desc", limit=1, offset=1, active_only=True,
    )

    assert result["total"] == 0
    assert result["summary"] == {
        "total_rows": 0,
        "by_status": {},
        "by_phase": {},
        "to_order": 0,
        "overdue": 0,
        "expected_7d": 0,
        "in_transit_amount": 0.0,
        "fact_status": "available",
    }
    assert list_filters(db_session) == {
        "suppliers": [{"supplier_id": 1, "supplier_name": "Альфа"}],
        "states": ["В закупку"],
    }


def test_list_journal_searches_buy_rows_by_item_article(db_session):
    _generation, item, _aug, _sep, _snapshot = _build_buy_horizon_generation(
        db_session
    )

    result = list_journal(db_session, search="article-hrz")

    assert result["total"] == 1
    assert result["rows"][0]["item_id"] == item.item_id
    assert result["rows"][0]["item_article"] == "ARTICLE-HRZ"
    assert result["rows"][0]["price"] == 12.5
    assert result["rows"][0]["amount"] == 187.5

    selection = get_selection_summary(
        db_session,
        snapshot_id=int(result["meta"]["snapshot_id"]),
        row_keys=[result["rows"][0]["row_key"]],
    )
    assert selection == {
        "snapshot_id": int(result["meta"]["snapshot_id"]),
        "selected_rows": 1,
        "priced_rows": 1,
        "unpriced_rows": 0,
        "known_amount": 187.5,
        "total_amount": 187.5,
        "amount_status": "complete",
    }
    august_selection = get_selection_summary(
        db_session,
        snapshot_id=int(result["meta"]["snapshot_id"]),
        row_keys=[result["rows"][0]["row_key"]],
        horizon_period_to=date(2026, 8, 31),
    )
    assert august_selection["total_amount"] == 87.5


def test_selection_summary_rejects_stale_snapshot(db_session):
    _generation, _item, _aug, _sep, _snapshot = _build_buy_horizon_generation(
        db_session
    )

    with pytest.raises(ValueError, match="Снимок журнала изменился"):
        get_selection_summary(
            db_session,
            snapshot_id=999999,
            row_keys=["buy:missing:main"],
        )


def test_buy_row_keeps_missing_accounting_price_nullable(db_session):
    _generation, _item, _aug, _sep, _snapshot = _build_buy_horizon_generation(
        db_session,
        accounting_price=None,
    )

    result = list_journal(db_session)
    row = result["rows"][0]
    assert row["price"] is None
    assert row["amount"] is None

    selection = get_selection_summary(
        db_session,
        snapshot_id=int(result["meta"]["snapshot_id"]),
        row_keys=[row["row_key"]],
    )
    assert selection["amount_status"] == "unavailable"
    assert selection["known_amount"] == 0.0
    assert selection["total_amount"] is None
    assert selection["unpriced_rows"] == 1


def test_missing_or_stale_snapshot_fails_closed(db_session):
    with pytest.raises(PurchaseJournalSnapshotUnavailable) as missing:
        list_journal(db_session)
    assert missing.value.as_dict()["code"] == "purchase_control_snapshot_unavailable"

    generation, _order, _legacy, _supplies = _context(db_session)
    _accept(db_session, generation)
    generation.capabilities = {**generation.capabilities, "purchase_control_journal": False}
    db_session.flush()
    with pytest.raises(PurchaseJournalSnapshotUnavailable) as stale:
        list_journal(db_session)
    assert stale.value.as_dict()["status"] == "unavailable"


def test_unknown_order_detail_is_not_fabricated(db_session):
    generation, _order, _legacy, _supplies = _context(db_session)
    _accept(db_session, generation)

    with pytest.raises(ValueError, match="not found"):
        get_order_card(db_session, 999999)

    with pytest.raises(HTTPException) as response:
        get_order(999999, db=db_session)
    assert response.value.status_code == 404


def test_router_returns_structured_503_when_snapshot_is_missing(db_session):
    with pytest.raises(HTTPException) as response:
        get_filters(db=db_session)

    assert response.value.status_code == 503
    assert response.value.detail["code"] == "purchase_control_snapshot_unavailable"


def test_candidate_subtracts_frozen_open_supplier_order_coverage(db_session):
    cutoff = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="purchase-snapshot-buy-physical",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="purchase-snapshot-buy-generation",
        status="building",
        cutoff=cutoff,
        source_watermarks={},
        capabilities={},
        physical_import_batch=physical,
        algorithm_version="test",
    )
    item = models.Item(item_code="RAW-MAT", item_name="Покупной материал", unit="шт")
    supplier = models.Supplier(supplier_ref1c="SUP-B", supplier_name="Бета")
    db_session.add_all([generation, item, supplier])
    db_session.flush()

    plan = models.ProductionPlanHeader(
        name="purchase-buy-plan",
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status="fixed",
    )
    db_session.add(plan)
    db_session.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
        ledger_generation_id=generation.id,
        active_freeze_version=0,
    )
    db_session.add(run)
    db_session.flush()

    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=Decimal("10"),
        net_required_qty=Decimal("10"),
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        bom_level=1,
    )
    db_session.add(requirement)
    db_session.flush()

    reservation = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="main",
        run_id=run.run_id,
        freeze_version=0,
        requirement_id=requirement.id,
        priority_period_from=date(2026, 8, 1),
        priority_period_to=date(2026, 8, 31),
        realization_mode="buy",
        reserved_qty=Decimal("10"),
        realized_qty=Decimal("3"),
        covered_from_stock_at_freeze_qty=Decimal("0"),
        replenishment_required_qty=Decimal("10"),
        replenishment_received_qty=Decimal("3"),
        lifecycle_status="active",
    )
    db_session.add(reservation)
    db_session.flush()
    work_item = models.ReplenishmentWorkItem(
        ledger_generation_id=generation.id,
        reservation_id=reservation.id,
        plan_id=plan.id,
        run_id=run.run_id,
        requirement_id=requirement.id,
        item_id=int(item.item_id),
        replenishment_method="buy",
        replenishment_required_qty=Decimal("10"),
        replenishment_fulfilled_qty=Decimal("3"),
        replenishment_remaining_qty=Decimal("7"),
    )
    db_session.add(work_item)
    db_session.flush()

    junior_plan = models.ProductionPlanHeader(
        name="purchase-buy-plan-junior",
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        status="fixed",
    )
    db_session.add(junior_plan)
    db_session.flush()
    junior_run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        source_plan_id=junior_plan.id,
        period_from=junior_plan.period_from,
        period_to=junior_plan.period_to,
        ledger_generation_id=generation.id,
        active_freeze_version=0,
    )
    db_session.add(junior_run)
    db_session.flush()
    junior_requirement = models.MrpRequirement(
        run_id=junior_run.run_id,
        item_id=item.item_id,
        total_required_qty=Decimal("5"),
        net_required_qty=Decimal("5"),
        period_from=junior_plan.period_from,
        period_to=junior_plan.period_to,
        bom_level=1,
    )
    db_session.add(junior_requirement)
    db_session.flush()
    junior_reservation = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="main",
        run_id=junior_run.run_id,
        freeze_version=0,
        requirement_id=junior_requirement.id,
        priority_period_from=junior_plan.period_from,
        priority_period_to=junior_plan.period_to,
        realization_mode="buy",
        reserved_qty=Decimal("5"),
        realized_qty=Decimal("0"),
        covered_from_stock_at_freeze_qty=Decimal("0"),
        replenishment_required_qty=Decimal("5"),
        replenishment_received_qty=Decimal("0"),
        lifecycle_status="active",
    )
    db_session.add(junior_reservation)
    db_session.flush()
    db_session.add(
        models.ReplenishmentWorkItem(
            ledger_generation_id=generation.id,
            reservation_id=junior_reservation.id,
            plan_id=junior_plan.id,
            run_id=junior_run.run_id,
            requirement_id=junior_requirement.id,
            item_id=int(item.item_id),
            replenishment_method="buy",
            replenishment_required_qty=Decimal("5"),
            replenishment_fulfilled_qty=Decimal("0"),
            replenishment_remaining_qty=Decimal("5"),
        )
    )
    db_session.flush()

    capture = models.LedgerBuildBatch(
        ledger_generation_id=generation.id,
        stage="snapshot_build",
        batch_key="purchase-snapshot-open-order",
        status="completed",
        algorithm_version="test",
        metrics={},
    )
    db_session.add(capture)
    db_session.flush()
    db_session.add_all(
        [
            models.LedgerFutureSupply(
                ledger_generation_id=generation.id,
                supply_kind="supplier_order",
                item_id=item.item_id,
                characteristic_ref="",
                organization_ref="",
                planning_stock_pool="main",
                destination_warehouse_ref1c="WH-1",
                source_ref="ORDER-OPEN",
                source_line_ref="1",
                source_local_id="test",
                ordered_qty_at_cutoff=Decimal("6"),
                realized_qty_at_cutoff=Decimal("2"),
                open_qty_at_cutoff=Decimal("4"),
                eta_date=date(2026, 8, 15),
                source_state_key="exported",
                source_updated_at=cutoff,
                capture_cutoff=cutoff,
                source_content_hash="open-order-test",
                capture_batch_id=capture.id,
                evidence_status="exact",
            ),
            models.MrpFreezeAllocation(
                run_id=run.run_id,
                freeze_version=0,
                requirement_id=requirement.id,
                item_id=item.item_id,
                characteristic_ref="",
                organization_ref="",
                planning_stock_pool="main",
                source_type="supplier_order",
                source_ref="ORDER-OPEN",
                source_line_ref="1",
                alloc_qty=Decimal("6"),
                fact_at_freeze=Decimal("6"),
                realized_qty=Decimal("0"),
                evaporated_qty=Decimal("0"),
            ),
            models.MrpFreezeAllocation(
                run_id=junior_run.run_id,
                freeze_version=0,
                requirement_id=junior_requirement.id,
                item_id=item.item_id,
                characteristic_ref="",
                organization_ref="",
                planning_stock_pool="main",
                source_type="supplier_order",
                source_ref="ORDER-OPEN",
                source_line_ref="1",
                alloc_qty=Decimal("5"),
                fact_at_freeze=Decimal("6"),
                realized_qty=Decimal("0"),
                evaporated_qty=Decimal("0"),
            ),
        ]
    )
    db_session.flush()

    # A later refreeze pointer is mutable operational state. Coverage for this
    # generation must remain pinned to each ReservationEntry.freeze_version.
    run.active_freeze_version = 9
    junior_run.active_freeze_version = 9
    db_session.flush()

    snapshot = build_candidate_snapshot(db_session, generation.id)
    rows = [row for row in snapshot.payload["rows"] if row.get("row_generator") == "mrp_reservation"]

    assert len(rows) == 1
    row = rows[0]
    assert row["item_code"] == "RAW-MAT"
    assert row["line_status"] == "to_order"
    assert row["realized_qty"] == 3.0
    assert row["open_order_covered_qty"] == 4.0
    assert row["to_order_qty"] == 8.0
    assert row["required_qty"] == 15.0
    assert row["received_qty"] == 3.0
    assert row["required_qty"] == (
        row["realized_qty"]
        + row["open_order_covered_qty"]
        + row["to_order_qty"]
    )
    slices = row.get("slices")
    assert isinstance(slices, list) and slices
    for bucket in slices:
        assert bucket["work_item_id"] is not None
    assert slices[0]["reservation_id"] == reservation.id
    assert slices[0]["coverage_slices"] == [
        {
            "source_type": "supplier_order",
            "source_ref": "ORDER-OPEN",
            "source_line_ref": "1",
            "covered_qty": 4.0,
        }
    ]
    assert slices[1]["reservation_id"] == junior_reservation.id
    assert slices[1]["coverage_slices"] == []


def test_list_journal_returns_snapshot_meta_run_ids_and_to_order_buckets(db_session):
    generation, _order, _legacy_lines, supplies = _context(db_session)

    run = models.ProductionPlanHeader(
        name="purchase-buy-plan",
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status="fixed",
    )
    db_session.add(run)
    db_session.flush()

    item = db_session.query(models.Item).filter(models.Item.item_code == "MAT-A").first()
    if item is None:
        item = db_session.query(models.Item).filter(models.Item.item_name == "Материал А").first()
    for supply in supplies:
        if int(supply.item_id) == int(item.item_id):
            supply.evidence_status = "rejected"
            supply.reason = "test keeps the demand uncovered"

    planning_run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        source_plan_id=run.id,
        period_from=run.period_from,
        period_to=run.period_to,
        ledger_generation_id=generation.id,
    )
    db_session.add(planning_run)
    db_session.flush()

    requirement = models.MrpRequirement(
        run_id=planning_run.run_id,
        item_id=int(item.item_id),
        total_required_qty=Decimal("10"),
        net_required_qty=Decimal("10"),
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        bom_level=1,
    )
    db_session.add(requirement)
    db_session.flush()

    reservation = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=int(item.item_id),
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="main",
        run_id=planning_run.run_id,
        freeze_version=0,
        requirement_id=requirement.id,
        priority_period_from=date(2026, 8, 1),
        priority_period_to=date(2026, 8, 31),
        realization_mode="buy",
        reserved_qty=Decimal("10"),
        realized_qty=Decimal("3"),
        covered_from_stock_at_freeze_qty=Decimal("0"),
        replenishment_required_qty=Decimal("10"),
        replenishment_received_qty=Decimal("3"),
        lifecycle_status="active",
    )
    db_session.add(reservation)
    db_session.flush()
    work_item = models.ReplenishmentWorkItem(
        ledger_generation_id=generation.id,
        reservation_id=reservation.id,
        plan_id=int(run.id),
        run_id=planning_run.run_id,
        requirement_id=requirement.id,
        item_id=int(item.item_id),
        replenishment_method="buy",
        replenishment_required_qty=Decimal("10"),
        replenishment_fulfilled_qty=Decimal("3"),
        replenishment_remaining_qty=Decimal("7"),
    )
    db_session.add(work_item)
    db_session.flush()

    db_session.flush()

    build_candidate_snapshot(db_session, generation.id)
    _accept(db_session, generation)

    result = list_journal(db_session)
    assert result["run_ids"] == [int(planning_run.run_id)]
    assert len(result["to_order_by_period"]) == 1
    bucket = result["to_order_by_period"][0]
    assert bucket["plan_period_to"] == "2026-08-31"
    assert bucket["period_to"] == "2026-08-31"
    assert bucket["total_qty"] == 7.0
    assert bucket["item_count"] == 1


def test_list_journal_horizon_selector_aggregates_all_buy_slices(db_session):
    generation, item, aug_run, sep_run, _snapshot = _build_buy_horizon_generation(db_session)

    result = list_journal(db_session)
    rows = [row for row in result["rows"] if row["row_generator"] == "mrp_reservation" and row["item_id"] == int(item.item_id)]
    assert len(rows) == 1

    row = rows[0]
    assert row["plan_period_from"] == "2026-08-01"
    assert row["plan_period_to"] == "2026-09-30"
    assert row["run_ids"] == [int(aug_run.run_id), int(sep_run.run_id)]
    assert row["required_qty"] == 20.0
    assert row["realized_qty"] == 5.0
    assert row["open_order_covered_qty"] == 0.0
    assert row["to_order_qty"] == 15.0
    assert row["to_order_pct"] == 75.0
    assert row["open_order_covered_pct"] == 0.0
    assert abs(row["required_qty"] - (row["realized_qty"] + row["open_order_covered_qty"] + row["to_order_qty"]) ) < 1e-9
    assert row["horizon_bucket_count"] == 2
    assert [bucket["plan_period_to"] for bucket in row["horizon_buckets"]] == ["2026-08-31", "2026-09-30"]
    assert [bucket["total_qty"] for bucket in result["to_order_by_period"]] == [7.0, 8.0]


def test_router_horizon_selector_filters_buy_rows_by_plan_slice(db_session):
    _generation, _item, aug_run, _sep_run, _snapshot = _build_buy_horizon_generation(db_session)

    result = get_orders(
        horizon_period_to=date(2026, 8, 31),
        db=db_session,
        limit=100,
        offset=0,
    )

    rows = [row for row in result["rows"] if row["row_generator"] == "mrp_reservation"]
    assert len(rows) == 1

    row = rows[0]
    assert row["plan_period_to"] == "2026-08-31"
    assert row["plan_period_from"] == "2026-08-01"
    assert row["run_ids"] == [int(aug_run.run_id)]
    assert row["required_qty"] == 10.0
    assert row["realized_qty"] == 3.0
    assert row["open_order_covered_qty"] == 0.0
    assert row["to_order_qty"] == 7.0
    assert row["horizon_bucket_count"] == 1
    assert result["to_order_by_period"] == [
        {
            "plan_period_to": "2026-08-31",
            "period_to": "2026-08-31",
            "period_label": "Август 2026",
            "item_count": 1,
            "total_qty": 7.0,
        }
    ]
    assert row["to_order_pct"] == 70.0
    assert row["open_order_covered_pct"] == 0.0


def test_reconcile_buy_row_for_horizon_with_zero_required_returns_none_percentages():
    row = {
        "row_generator": "mrp_reservation",
        "slices": [
            {
                "required_qty": 0,
                "realized_qty": 0,
                "open_order_covered_qty": 0,
                "to_order_qty": 5,
                "run_id": 1,
                "plan_period_from": "2026-01-01",
                "plan_period_to": "2026-01-31",
                "period_label": "Январь 2026",
                "need_date": "2026-01-01",
                "need_period_to": "2026-01-31",
                "reservation_id": 10,
                "requirement_id": 20,
            }
        ],
    }

    projected = _reconcile_buy_row_for_horizon(row, "2026-01-31")

    assert projected is not None
    assert projected["to_order_pct"] is None
    assert projected["open_order_covered_pct"] is None


def test_reconcile_buy_row_for_horizon_over_coverage_is_capped():
    row = {
        "row_generator": "mrp_reservation",
        "slices": [
            {
                "required_qty": 10,
                "realized_qty": 0,
                "open_order_covered_qty": 20,
                "to_order_qty": 50,
                "run_id": 1,
                "plan_period_from": "2026-01-01",
                "plan_period_to": "2026-01-31",
                "period_label": "Январь 2026",
                "need_date": "2026-01-01",
                "need_period_to": "2026-01-31",
                "reservation_id": 10,
                "requirement_id": 20,
            }
        ],
    }

    projected = _reconcile_buy_row_for_horizon(row, "2026-01-31")

    assert projected is not None
    assert projected["to_order_pct"] == 100.0
    assert projected["open_order_covered_pct"] == 100.0


def test_build_candidate_snapshot_freezes_materialization_input_refs(db_session):
    cutoff = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="purchase-materialization-input-freeze",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="purchase-materialization-input-freeze-generation",
        status="building",
        cutoff=cutoff,
        source_watermarks={},
        capabilities={},
        physical_import_batch=physical,
        algorithm_version="test",
    )
    item = models.Item(
        item_code="RAW-MAT-FREEZE",
        item_name="Материал freeze",
        item_ref1c="ITEM-FREEZE-REF",
        supplier_ref1c="SUP-ITEM-FREEZE",
        unit="шт",
    )
    supplier = models.Supplier(
        supplier_ref1c="SUP-ITEM-FREEZE",
        supplier_name="Поставщик freeze",
    )
    plan = models.ProductionPlanHeader(
        name="purchase-freeze-plan",
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        status="fixed",
    )
    db_session.add_all([generation, item, supplier, plan])
    db_session.flush()
    planning_run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
        ledger_generation_id=generation.id,
    )
    db_session.add(planning_run)
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=planning_run.run_id,
        item_id=item.item_id,
        total_required_qty=Decimal("10"),
        net_required_qty=Decimal("10"),
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=1,
    )
    db_session.add(requirement)
    db_session.flush()
    reservation = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="main",
        run_id=planning_run.run_id,
        freeze_version=0,
        requirement_id=requirement.id,
        priority_period_from=plan.period_from,
        priority_period_to=plan.period_to,
        realization_mode="buy",
        reserved_qty=Decimal("10"),
        realized_qty=Decimal("2"),
        covered_from_stock_at_freeze_qty=Decimal("0"),
        replenishment_required_qty=Decimal("10"),
        replenishment_received_qty=Decimal("2"),
        lifecycle_status="active",
    )
    db_session.add(reservation)
    db_session.flush()
    db_session.add(
        models.ReplenishmentWorkItem(
            ledger_generation_id=generation.id,
            reservation_id=reservation.id,
            plan_id=plan.id,
            run_id=planning_run.run_id,
            requirement_id=requirement.id,
            item_id=item.item_id,
            replenishment_method="buy",
            replenishment_required_qty=Decimal("10"),
            replenishment_fulfilled_qty=Decimal("2"),
            replenishment_remaining_qty=Decimal("8"),
        )
    )
    db_session.flush()

    snapshot = build_candidate_snapshot(db_session, generation.id)
    rows = [row for row in snapshot.payload["rows"] if row.get("row_generator") == "mrp_reservation"]
    assert len(rows) == 1

    row = rows[0]
    materialization_input = row.get("materialization_input")
    assert isinstance(materialization_input, dict)
    assert materialization_input["supplier_ref1c"] == item.supplier_ref1c.lower()
    assert materialization_input["item_ref1c"] == item.item_ref1c
    assert materialization_input["unit_ref1c"] == item.unit
    assert "destination_warehouse_ref1c" in materialization_input
