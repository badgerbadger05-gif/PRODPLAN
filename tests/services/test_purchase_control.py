from datetime import date, datetime

import pytest

from app.models import (
    Item,
    PlannedPurchase,
    PlanningTruthState,
    PlanningRun,
    ProductionPlanHeader,
    PhysicalImportBatch,
    LedgerGeneration,
    Supplier,
    SupplierOrder,
    SupplierOrderItem,
    SyncLink,
    Unit,
)
from app.services.one_c_purchase_order_export import PURCHASE_ORDER_ENTITY
from app.services.purchase_control_journal import get_order_card, list_filters, list_journal

TODAY = date(2026, 6, 11)


@pytest.fixture(autouse=True)
def accepted_journal_truth(db_session):
    """Every normal purchase-journal case reads an explicit published truth."""
    cutoff = datetime(2026, 6, 1)
    physical = PhysicalImportBatch(
        batch_key="purchase-journal-test-physical",
        status="completed", cutoff=cutoff, completed_at=cutoff, source_watermarks={},
    )
    generation = LedgerGeneration(
        generation_key="purchase-journal-test-generation",
        status="accepted", cutoff=cutoff, accepted_at=cutoff,
        source_watermarks={}, capabilities={}, physical_import_batch=physical,
        algorithm_version="tests/1",
    )
    db_session.add(generation)
    db_session.flush()
    db_session.add(PlanningTruthState(id=1, current_generation_id=generation.id))
    db_session.flush()
    db_session.info["accepted_journal_generation_id"] = generation.id
    return generation


def _make_mrp_run(db, *, period_from=None, period_to=None):
    period_from = period_from or date(2026, 6, 1)
    period_to = period_to or date(2026, 6, 30)
    generation_id = db.info["accepted_journal_generation_id"]
    plan = ProductionPlanHeader(
        name=f"Purchase journal plan {period_from.isoformat()} {period_to.isoformat()} {generation_id}",
        period_from=period_from, period_to=period_to, status="fixed",
    )
    db.add(plan)
    db.flush()
    run = PlanningRun(
        status="FIXED_SNAPSHOT", config_snapshot={}, source_plan_id=plan.id,
        period_from=period_from, period_to=period_to, ledger_generation_id=generation_id,
    )
    db.add(run)
    db.flush()
    return run


def _make_item(db, code, name, article=None, supplier_ref=None):
    item = Item(item_code=code, item_name=name, item_article=article, unit="шт", supplier_ref1c=supplier_ref)
    db.add(item)
    db.flush()
    return item


def _make_supplier(db, ref, name):
    supplier = Supplier(supplier_ref1c=ref, supplier_name=name)
    db.add(supplier)
    db.flush()
    return supplier


def _make_order(db, number, supplier, *, state="В закупку", deletion_mark=False, ref=None):
    order = SupplierOrder(
        order_number=number,
        order_date=datetime(2026, 6, 1),
        order_ref1c=ref,
        supplier_id=supplier.supplier_id,
        order_state_name=state,
        deletion_mark=deletion_mark,
    )
    db.add(order)
    db.flush()
    return order


def _make_line(db, order, item, *, qty=10, received=0, delivery=None, price=0):
    line = SupplierOrderItem(
        order_id=order.order_id,
        item_id_ref=item.item_id,
        quantity=qty,
        received_qty=received,
        remaining_qty=max(qty - received, 0),
        price=price,
        amount=price * qty,
        delivery_date=datetime.combine(delivery, datetime.min.time()) if delivery else None,
    )
    db.add(line)
    db.flush()
    return line


def test_list_journal_line_statuses(db_session):
    supplier = _make_supplier(db_session, "s-ref-1", "ООО Метиз")
    order = _make_order(db_session, "ЗП-001", supplier)
    item_overdue = _make_item(db_session, "M-1", "Болт М10")
    item_expected = _make_item(db_session, "M-2", "Гайка М10")
    item_partial = _make_item(db_session, "M-3", "Шайба 10")
    item_received = _make_item(db_session, "M-4", "Шпилька М10")
    item_no_date = _make_item(db_session, "M-5", "Винт М6")
    _make_line(db_session, order, item_overdue, qty=10, delivery=date(2026, 6, 1))
    _make_line(db_session, order, item_expected, qty=10, delivery=date(2026, 6, 20))
    _make_line(db_session, order, item_partial, qty=10, received=4, delivery=date(2026, 6, 20))
    _make_line(db_session, order, item_received, qty=10, received=10, delivery=date(2026, 6, 1))
    _make_line(db_session, order, item_no_date, qty=10)
    db_session.commit()

    result = list_journal(db_session, today=TODAY)

    by_code = {row["item_code"]: row for row in result["rows"]}
    assert by_code["M-1"]["line_status"] == "overdue"
    assert by_code["M-1"]["overdue_days"] == 10
    assert by_code["M-2"]["line_status"] == "expected"
    assert by_code["M-3"]["line_status"] == "partial"
    assert by_code["M-4"]["line_status"] == "received"
    assert by_code["M-5"]["line_status"] == "no_date"
    assert result["summary"]["overdue"] == 1
    assert result["summary"]["total_rows"] == 5

    overdue_only = list_journal(db_session, line_status="overdue", today=TODAY)
    assert [row["item_code"] for row in overdue_only["rows"]] == ["M-1"]
    # summary считается до фильтра по line_status
    assert overdue_only["summary"]["total_rows"] == 5


def test_list_journal_active_only_excludes_closed_orders(db_session):
    supplier = _make_supplier(db_session, "s-ref-1", "ООО Метиз")
    active = _make_order(db_session, "ЗП-001", supplier, state="В закупку")
    finished = _make_order(db_session, "ЗП-002", supplier, state="Завершен")
    deleted = _make_order(db_session, "ЗП-003", supplier, state="В закупку", deletion_mark=True)
    item = _make_item(db_session, "M-1", "Болт М10")
    for order in (active, finished, deleted):
        _make_line(db_session, order, item, qty=5, delivery=date(2026, 6, 20))
    db_session.commit()

    result = list_journal(db_session, today=TODAY)
    assert [row["order_number"] for row in result["rows"]] == ["ЗП-001"]

    everything = list_journal(db_session, active_only=False, today=TODAY)
    numbers = {row["order_number"] for row in everything["rows"]}
    assert numbers == {"ЗП-001", "ЗП-002"}  # deletion_mark отфильтрован и при active_only=False
    closed = [row for row in everything["rows"] if row["line_status"] == "closed"]
    assert [row["order_number"] for row in closed] == ["ЗП-002"]


def test_list_journal_phase_grouping_and_active_no_goods(db_session):
    supplier = _make_supplier(db_session, "s-ref-1", "ООО Метиз")
    # «Новый заказ» и «Бухгалтерия» — фаза no_goods, но активные (видимы)
    new_order = _make_order(db_session, "ЗП-NEW", supplier, state="Новый заказ")
    accounting = _make_order(db_session, "ЗП-ACC", supplier, state="Бухгалтерия")
    in_transit = _make_order(db_session, "ЗП-WAY", supplier, state="В пути")
    in_stock = _make_order(db_session, "ЗП-WH", supplier, state="Принят на склад")
    finished = _make_order(db_session, "ЗП-DONE", supplier, state="Завершен")
    item = _make_item(db_session, "M-1", "Болт М10")
    for order in (new_order, accounting, in_transit, in_stock, finished):
        _make_line(db_session, order, item, qty=5, delivery=date(2026, 6, 20))
    db_session.commit()

    result = list_journal(db_session, today=TODAY)
    by_number = {row["order_number"]: row for row in result["rows"]}

    # терминальный «Завершен» скрыт при active_only, фазовые — видны
    assert set(by_number) == {"ЗП-NEW", "ЗП-ACC", "ЗП-WAY", "ЗП-WH"}
    assert by_number["ЗП-NEW"]["supply_phase"] == "no_goods"
    assert by_number["ЗП-NEW"]["counts_in_mrp"] is False
    assert by_number["ЗП-WAY"]["supply_phase"] == "in_transit"
    assert by_number["ЗП-WAY"]["counts_in_mrp"] is True
    assert by_number["ЗП-WH"]["supply_phase"] == "in_stock"

    assert result["summary"]["by_phase"] == {"no_goods": 2, "in_transit": 1, "in_stock": 1}

    only_transit = list_journal(db_session, phase="in_transit", today=TODAY)
    assert [r["order_number"] for r in only_transit["rows"]] == ["ЗП-WAY"]
    # summary считается до фильтра по фазе
    assert only_transit["summary"]["by_phase"]["no_goods"] == 2


def test_list_journal_closed_only_on_terminal_states(db_session):
    supplier = _make_supplier(db_session, "s-ref-1", "ООО Метиз")
    accounting = _make_order(db_session, "ЗП-ACC", supplier, state="Бухгалтерия")
    cancelled = _make_order(db_session, "ЗП-CANCEL", supplier, state="Отменён")
    item = _make_item(db_session, "M-1", "Болт М10")
    for order in (accounting, cancelled):
        _make_line(db_session, order, item, qty=5, delivery=date(2026, 6, 20))
    db_session.commit()

    everything = list_journal(db_session, active_only=False, today=TODAY)
    by_number = {row["order_number"]: row for row in everything["rows"]}
    # «Бухгалтерия» активна (не закрыта), «Отменён» — closed
    assert by_number["ЗП-ACC"]["line_status"] != "closed"
    assert by_number["ЗП-CANCEL"]["line_status"] == "closed"


def test_list_journal_includes_unordered_mrp_purchases(db_session):
    run = _make_mrp_run(db_session)
    supplier = _make_supplier(db_session, "s-ref-1", "ООО Метиз")
    item = _make_item(db_session, "M-1", "Болт М10", supplier_ref="s-ref-1")
    exported = PlannedPurchase(
        run_id=run.run_id, item_id=item.item_id, requested_qty=5, planned_qty=5, qty=5,
        need_date=date(2026, 6, 25), order_date=date(2026, 6, 15), lead_time_days=10,
        bucket_date=date(2026, 6, 25), supplier_ref1c="s-ref-1",
    )
    pending = PlannedPurchase(
        run_id=run.run_id, item_id=item.item_id, requested_qty=7, planned_qty=7, qty=7,
        need_date=date(2026, 6, 30), order_date=date(2026, 6, 20), lead_time_days=10,
        bucket_date=date(2026, 6, 30), supplier_ref1c="s-ref-1",
    )
    db_session.add_all([exported, pending])
    db_session.flush()
    db_session.add(
        SyncLink(
            source_doctype="planned_purchase",
            source_id=exported.purchase_id,
            target_entity=PURCHASE_ORDER_ENTITY,
            target_ref_key="ref-po-1",
            status="success",
        )
    )
    db_session.commit()

    result = list_journal(db_session, today=TODAY)

    to_order = [row for row in result["rows"] if row["line_status"] == "to_order"]
    assert len(to_order) == 1
    row = to_order[0]
    assert row["purchase_id"] == pending.purchase_id
    assert row["source_purchase_ids"] == [pending.purchase_id]
    assert row["quantity"] == 7
    assert row["need_date"] == "2026-06-30"
    assert row["supplier_name"] == "ООО Метиз"
    assert row["run_id"] == run.run_id
    assert result["run_id"] == run.run_id
    assert result["summary"]["to_order"] == 1

    without = list_journal(db_session, include_to_order=False, today=TODAY)
    assert all(r["line_status"] != "to_order" for r in without["rows"])


def test_journal_aggregates_active_runs(db_session):
    # Two active FIXED_SNAPSHOT runs (one per open plan), each owning its own
    # PlannedPurchase → the journal's "to order" rows must come from BOTH runs.
    supplier = _make_supplier(db_session, "s-ref-1", "ООО Метиз")
    item = _make_item(db_session, "M-AGG", "Болт М10", supplier_ref="s-ref-1")

    run_a = _make_mrp_run(
        db_session, period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
    )
    run_b = _make_mrp_run(
        db_session, period_from=date(2026, 9, 1), period_to=date(2026, 9, 30),
    )

    db_session.add(
        PlannedPurchase(
            run_id=run_a.run_id, item_id=item.item_id, requested_qty=5, planned_qty=5, qty=5,
            need_date=date(2026, 8, 20), order_date=date(2026, 8, 10), lead_time_days=10,
            bucket_date=date(2026, 8, 20), supplier_ref1c="s-ref-1",
        )
    )
    db_session.add(
        PlannedPurchase(
            run_id=run_b.run_id, item_id=item.item_id, requested_qty=7, planned_qty=7, qty=7,
            need_date=date(2026, 9, 20), order_date=date(2026, 9, 10), lead_time_days=10,
            bucket_date=date(2026, 9, 20), supplier_ref1c="s-ref-1",
        )
    )
    db_session.commit()

    result = list_journal(db_session, today=TODAY)

    to_order = [row for row in result["rows"] if row["line_status"] == "to_order"]
    assert {row["run_id"] for row in to_order} == {run_a.run_id, run_b.run_id}
    assert sorted(row["quantity"] for row in to_order) == [5, 7]
    assert result["summary"]["to_order"] == 2


def _make_horizon_runs(db):
    """Two active FIXED_SNAPSHOT runs (Aug + Sep 2026), one PlannedPurchase each."""
    _make_supplier(db, "s-ref-1", "ООО Метиз")
    item = _make_item(db, "M-HZ", "Болт М10", supplier_ref="s-ref-1")

    run_aug = _make_mrp_run(
        db, period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
    )
    run_sep = _make_mrp_run(
        db, period_from=date(2026, 9, 1), period_to=date(2026, 9, 30),
    )

    db.add(
        PlannedPurchase(
            run_id=run_aug.run_id, item_id=item.item_id, requested_qty=5, planned_qty=5, qty=5,
            need_date=date(2026, 8, 20), order_date=date(2026, 8, 10), lead_time_days=10,
            bucket_date=date(2026, 8, 20), supplier_ref1c="s-ref-1",
        )
    )
    db.add(
        PlannedPurchase(
            run_id=run_sep.run_id, item_id=item.item_id, requested_qty=7, planned_qty=7, qty=7,
            need_date=date(2026, 9, 20), order_date=date(2026, 9, 10), lead_time_days=10,
            bucket_date=date(2026, 9, 20), supplier_ref1c="s-ref-1",
        )
    )
    db.commit()
    return run_aug, run_sep


def test_to_order_rows_carry_horizon_and_by_period_summary(db_session):
    run_aug, run_sep = _make_horizon_runs(db_session)

    result = list_journal(db_session, today=TODAY)
    to_order = [row for row in result["rows"] if row["line_status"] == "to_order"]

    by_run = {row["run_id"]: row for row in to_order}
    assert by_run[run_aug.run_id]["plan_period_to"] == "2026-08-31"
    assert by_run[run_aug.run_id]["plan_period_from"] == "2026-08-01"
    assert by_run[run_aug.run_id]["period_label"] == "Август 2026"
    assert by_run[run_sep.run_id]["plan_period_to"] == "2026-09-30"
    assert by_run[run_sep.run_id]["period_label"] == "Сентябрь 2026"

    buckets = result["to_order_by_period"]
    assert [b["period_to"] for b in buckets] == ["2026-08-31", "2026-09-30"]
    assert buckets[0] == {
        "period_to": "2026-08-31",
        "period_label": "Август 2026",
        "item_count": 1,
        "total_qty": 5.0,
    }
    assert buckets[1] == {
        "period_to": "2026-09-30",
        "period_label": "Сентябрь 2026",
        "item_count": 1,
        "total_qty": 7.0,
    }


def test_horizon_period_to_filter_restricts_to_order_rows(db_session):
    run_aug, run_sep = _make_horizon_runs(db_session)

    # Horizon = end of August → only the August run's to_order rows are shown.
    filtered = list_journal(db_session, horizon_period_to=date(2026, 8, 31), today=TODAY)
    to_order = [row for row in filtered["rows"] if row["line_status"] == "to_order"]
    assert {row["run_id"] for row in to_order} == {run_aug.run_id}
    assert [row["plan_period_to"] for row in to_order] == ["2026-08-31"]

    # Full need stays visible in the by-period breakdown even when narrowed.
    assert [b["period_to"] for b in filtered["to_order_by_period"]] == [
        "2026-08-31",
        "2026-09-30",
    ]

    # None → both runs (current behavior).
    full = list_journal(db_session, horizon_period_to=None, today=TODAY)
    full_to_order = [row for row in full["rows"] if row["line_status"] == "to_order"]
    assert {row["run_id"] for row in full_to_order} == {run_aug.run_id, run_sep.run_id}


def test_full_need_visible_without_horizon_filter(db_session):
    _make_horizon_runs(db_session)

    result = list_journal(db_session, today=TODAY)
    to_order = [row for row in result["rows"] if row["line_status"] == "to_order"]

    # No regression: aggregate to_order qty == sum across all active runs.
    assert sum(row["quantity"] for row in to_order) == 12
    assert sum(b["total_qty"] for b in result["to_order_by_period"]) == 12
    assert result["summary"]["to_order"] == 2


def test_list_journal_aggregates_duplicate_unordered_mrp_purchases(db_session):
    run = _make_mrp_run(db_session, period_from=date(2026, 8, 1), period_to=date(2026, 8, 31))
    _make_supplier(db_session, "s-ref-1", "ООО Метиз")
    _make_supplier(db_session, "s-ref-2", "ООО Сталь")
    item = _make_item(db_session, "M-1", "Болт М10", supplier_ref="s-ref-1")

    def _purchase(qty, need_date, order_date, supplier_ref="s-ref-1"):
        purchase = PlannedPurchase(
            run_id=run.run_id,
            item_id=item.item_id,
            requested_qty=qty,
            planned_qty=qty,
            qty=qty,
            need_date=need_date,
            order_date=order_date,
            lead_time_days=10,
            bucket_date=need_date,
            supplier_ref1c=supplier_ref,
        )
        db_session.add(purchase)
        db_session.flush()
        return purchase

    first = _purchase(1.032, date(2026, 8, 31), date(2026, 8, 18))
    second = _purchase(1.032, date(2026, 8, 31), date(2026, 8, 17))
    exported = _purchase(9, date(2026, 8, 31), date(2026, 8, 16))
    other_date = _purchase(3, date(2026, 9, 1), date(2026, 8, 19))
    other_supplier = _purchase(4, date(2026, 8, 31), date(2026, 8, 18), "s-ref-2")
    db_session.add(
        SyncLink(
            source_doctype="planned_purchase",
            source_id=exported.purchase_id,
            target_entity=PURCHASE_ORDER_ENTITY,
            target_ref_key="ref-po-exported",
            status="success",
        )
    )
    db_session.commit()

    result = list_journal(db_session, today=TODAY)

    assert result["total"] == 3
    assert result["summary"]["to_order"] == 3
    aggregated = next(
        row
        for row in result["rows"]
        if row["need_date"] == "2026-08-31" and row["supplier_name"] == "ООО Метиз"
    )
    assert aggregated["purchase_id"] == min(first.purchase_id, second.purchase_id)
    assert aggregated["source_purchase_ids"] == sorted([first.purchase_id, second.purchase_id])
    assert aggregated["quantity"] == 2.064
    assert aggregated["remaining_qty"] == 2.064
    assert aggregated["order_date"] == "2026-08-17"
    row_key = aggregated["row_key"]
    assert row_key.startswith(f"purchase-group:{run.run_id}:{item.item_id}:")

    separate_ids = {
        tuple(row["source_purchase_ids"])
        for row in result["rows"]
        if row is not aggregated
    }
    assert separate_ids == {(other_date.purchase_id,), (other_supplier.purchase_id,)}

    top_up = _purchase(1, date(2026, 8, 31), date(2026, 8, 20))
    db_session.commit()
    refreshed = list_journal(db_session, today=TODAY)
    refreshed_aggregate = next(
        row
        for row in refreshed["rows"]
        if row["need_date"] == "2026-08-31" and row["supplier_name"] == "ООО Метиз"
    )
    assert refreshed_aggregate["row_key"] == row_key
    assert refreshed_aggregate["source_purchase_ids"] == sorted(
        [first.purchase_id, second.purchase_id, top_up.purchase_id]
    )
    assert refreshed_aggregate["quantity"] == 3.064


def test_list_journal_resolves_unit_guid_to_label(db_session):
    unit_ref = "aae0017c-991b-11eb-e39a-fa163e61326a"
    db_session.add(
        Unit(
            unit_ref1c=unit_ref,
            unit_code="796",
            unit_name="шт",
            short_name="шт",
        )
    )
    run = _make_mrp_run(db_session)
    supplier = _make_supplier(db_session, "s-ref-1", "ООО Метиз")
    item = _make_item(db_session, "M-1", "Болт М10", supplier_ref="s-ref-1")
    item.unit = unit_ref
    order = _make_order(db_session, "ЗП-001", supplier)
    _make_line(db_session, order, item, qty=5, delivery=date(2026, 6, 20))
    pending = PlannedPurchase(
        run_id=run.run_id, item_id=item.item_id, requested_qty=7, planned_qty=7, qty=7,
        need_date=date(2026, 6, 30), order_date=date(2026, 6, 20), lead_time_days=10,
        bucket_date=date(2026, 6, 30), supplier_ref1c="s-ref-1",
    )
    db_session.add(pending)
    db_session.commit()

    result = list_journal(db_session, today=TODAY)

    assert {row["unit"] for row in result["rows"]} == {"шт"}


def test_list_journal_marks_mrp_origin_orders(db_session):
    supplier = _make_supplier(db_session, "s-ref-1", "ООО Метиз")
    from_mrp = _make_order(db_session, "PO00001001", supplier, ref="REF-PO-1")
    plain = _make_order(db_session, "ЗП-002", supplier, ref="REF-PLAIN")
    item = _make_item(db_session, "M-1", "Болт М10")
    _make_line(db_session, from_mrp, item, qty=5, delivery=date(2026, 6, 20))
    _make_line(db_session, plain, item, qty=5, delivery=date(2026, 6, 20))
    db_session.add(
        SyncLink(
            source_doctype="planned_purchase",
            source_id=999,
            target_entity=PURCHASE_ORDER_ENTITY,
            target_ref_key="REF-PO-1",
            status="success",
        )
    )
    db_session.commit()

    result = list_journal(db_session, today=TODAY)
    by_number = {row["order_number"]: row for row in result["rows"]}
    assert by_number["PO00001001"]["source"] == "mrp"
    assert by_number["ЗП-002"]["source"] == "1c"


def test_list_journal_search_pagination_and_sort(db_session):
    supplier = _make_supplier(db_session, "s-ref-1", "ООО Метиз")
    order = _make_order(db_session, "ЗП-001", supplier)
    late = _make_item(db_session, "M-2", "Гайка М10")
    early = _make_item(db_session, "M-1", "Болт М10", article="АРТ-77")
    _make_line(db_session, order, late, qty=1, delivery=date(2026, 6, 25))
    _make_line(db_session, order, early, qty=1, delivery=date(2026, 6, 15))
    db_session.commit()

    result = list_journal(db_session, today=TODAY)
    assert [row["item_code"] for row in result["rows"]] == ["M-1", "M-2"]

    page = list_journal(db_session, limit=1, offset=1, today=TODAY)
    assert page["total"] == 2
    assert [row["item_code"] for row in page["rows"]] == ["M-2"]

    found = list_journal(db_session, search="АРТ-77", today=TODAY)
    assert [row["item_code"] for row in found["rows"]] == ["M-1"]


def test_get_order_card_and_filters(db_session):
    supplier = _make_supplier(db_session, "s-ref-1", "ООО Метиз")
    order = _make_order(db_session, "ЗП-001", supplier, state="В закупку")
    item = _make_item(db_session, "M-1", "Болт М10")
    _make_line(db_session, order, item, qty=10, received=4, delivery=date(2026, 6, 20), price=12)
    db_session.commit()

    card = get_order_card(db_session, order.order_id, today=TODAY)
    assert card["order"]["order_number"] == "ЗП-001"
    assert card["order"]["supplier_name"] == "ООО Метиз"
    assert card["order"]["active"] is True
    assert len(card["lines"]) == 1
    assert card["lines"][0]["line_status"] == "partial"

    filters = list_filters(db_session)
    assert filters["suppliers"] == [{"supplier_id": supplier.supplier_id, "supplier_name": "ООО Метиз"}]
    assert filters["states"] == ["В закупку"]
