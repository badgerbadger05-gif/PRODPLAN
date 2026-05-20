import datetime

from app.models import Item, PlannedPurchase, PlanningRun, Unit
from app.services.one_c_purchase_order_export import export_planned_purchases_to_1c


def _mk_run(db) -> PlanningRun:
    run = PlanningRun(
        status="SUCCESS",
        started_by="test",
        horizon_days=10,
        pinned=False,
        config_version_id=None,
        config_snapshot={},
        warnings=[],
        kpi={},
        started_at=datetime.datetime.utcnow(),
        finished_at=datetime.datetime.utcnow(),
    )
    db.add(run)
    db.flush()
    return run


def test_purchase_order_export_dry_run_groups_lines_by_supplier(db_session):
    db = db_session

    unit = Unit(unit_ref1c="u-po-1c", unit_name="шт", short_name="шт", precision=0)
    item_a = Item(
        item_code="PO-1C-A",
        item_name="Деталь A",
        item_article="A",
        item_ref1c="item-ref-a",
        supplier_ref1c="supplier-a",
        replenishment_method="Покупка",
        unit="u-po-1c",
        status="active",
    )
    item_b = Item(
        item_code="PO-1C-B",
        item_name="Деталь B",
        item_article="B",
        item_ref1c="item-ref-b",
        supplier_ref1c="supplier-b",
        replenishment_method="Покупка",
        unit="u-po-1c",
        status="active",
    )
    db.add_all([unit, item_a, item_b])
    db.flush()

    run = _mk_run(db)
    db.add_all(
        [
            PlannedPurchase(
                run_id=run.run_id,
                item_id=item_a.item_id,
                requested_qty=2,
                planned_qty=2,
                qty=2,
                need_date=datetime.date(2026, 5, 25),
                order_date=datetime.date(2026, 5, 20),
                lead_time_days=5,
                bucket_date=datetime.date(2026, 5, 25),
                supplier_ref1c=None,
            ),
            PlannedPurchase(
                run_id=run.run_id,
                item_id=item_a.item_id,
                requested_qty=3,
                planned_qty=3,
                qty=3,
                need_date=datetime.date(2026, 5, 25),
                order_date=datetime.date(2026, 5, 20),
                lead_time_days=5,
                bucket_date=datetime.date(2026, 5, 25),
                supplier_ref1c=None,
            ),
            PlannedPurchase(
                run_id=run.run_id,
                item_id=item_b.item_id,
                requested_qty=4,
                planned_qty=4,
                qty=4,
                need_date=datetime.date(2026, 5, 26),
                order_date=datetime.date(2026, 5, 21),
                lead_time_days=5,
                bucket_date=datetime.date(2026, 5, 26),
                supplier_ref1c="supplier-b-override",
            ),
        ]
    )
    db.commit()

    result = export_planned_purchases_to_1c(db, run.run_id, dry_run=True)

    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert result["orders_planned"] == 2
    assert result["lines_total"] == 2

    orders = {order["supplier_ref1c"]: order for order in result["orders"]}
    assert orders["supplier-a"]["lines"][0]["qty"] == 5.0
    assert orders["supplier-b-override"]["lines"][0]["qty"] == 4.0


def test_purchase_order_export_skips_rows_without_supplier_or_item_ref(db_session):
    db = db_session

    item_no_supplier = Item(
        item_code="PO-1C-NOS",
        item_name="Без поставщика",
        item_article="NOS",
        item_ref1c="item-ref-nos",
        replenishment_method="Покупка",
        status="active",
    )
    item_no_ref = Item(
        item_code="PO-1C-NOR",
        item_name="Без Ref",
        item_article="NOR",
        supplier_ref1c="supplier-a",
        replenishment_method="Покупка",
        status="active",
    )
    db.add_all([item_no_supplier, item_no_ref])
    db.flush()

    run = _mk_run(db)
    for item in (item_no_supplier, item_no_ref):
        db.add(
            PlannedPurchase(
                run_id=run.run_id,
                item_id=item.item_id,
                requested_qty=1,
                planned_qty=1,
                qty=1,
                need_date=datetime.date(2026, 5, 25),
                order_date=datetime.date(2026, 5, 20),
                lead_time_days=5,
                bucket_date=datetime.date(2026, 5, 25),
                supplier_ref1c=None,
            )
        )
    db.commit()

    result = export_planned_purchases_to_1c(db, run.run_id, dry_run=True)

    assert result["orders_planned"] == 0
    assert len(result["skipped_rows"]) == 2
    assert {row["item_id"] for row in result["skipped_rows"]} == {item_no_supplier.item_id, item_no_ref.item_id}


def test_purchase_order_export_limits_to_selected_purchase_ids(db_session):
    db = db_session

    item = Item(
        item_code="PO-1C-SEL",
        item_name="Выбранная деталь",
        item_article="SEL",
        item_ref1c="item-ref-selected",
        supplier_ref1c="supplier-selected",
        replenishment_method="Покупка",
        status="active",
    )
    db.add(item)
    db.flush()

    run = _mk_run(db)
    first = PlannedPurchase(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=2,
        planned_qty=2,
        qty=2,
        need_date=datetime.date(2026, 5, 25),
        order_date=datetime.date(2026, 5, 20),
        lead_time_days=5,
        bucket_date=datetime.date(2026, 5, 25),
    )
    second = PlannedPurchase(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=7,
        planned_qty=7,
        qty=7,
        need_date=datetime.date(2026, 5, 26),
        order_date=datetime.date(2026, 5, 21),
        lead_time_days=5,
        bucket_date=datetime.date(2026, 5, 26),
    )
    db.add_all([first, second])
    db.commit()

    result = export_planned_purchases_to_1c(db, run.run_id, purchase_ids=[first.purchase_id], dry_run=True)

    assert result["orders_planned"] == 1
    assert result["lines_total"] == 1
    assert result["orders"][0]["lines"][0]["qty"] == 2.0
