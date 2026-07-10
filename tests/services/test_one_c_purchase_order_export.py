import datetime

import pytest

from app.models import Item, PlannedPurchase, PlanningRun, SyncLink, Unit
import app.services.one_c_purchase_order_export as exporter
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


def test_purchase_order_export_refuses_non_demo_base_without_override(db_session, monkeypatch):
    db = db_session

    item = Item(
        item_code="PO-1C-GUARD",
        item_name="Защищённая закупка",
        item_article="GUARD",
        item_ref1c="item-ref-guard",
        supplier_ref1c="supplier-guard",
        replenishment_method="Покупка",
        status="active",
    )
    db.add(item)
    db.flush()

    run = _mk_run(db)
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
        )
    )
    db.commit()

    monkeypatch.setattr(
        exporter,
        "_load_odata_config",
        lambda: {"base_url": "http://mtzw7/unf/odata/standard.odata"},
    )

    with pytest.raises(PermissionError):
        export_planned_purchases_to_1c(db, run.run_id, dry_run=False)


def test_purchase_order_export_stamps_sync_links_for_source_purchases(db_session, monkeypatch):
    db = db_session

    item = Item(
        item_code="PO-1C-LINK",
        item_name="Связанная закупка",
        item_article="LINK",
        item_ref1c="item-ref-link",
        supplier_ref1c="supplier-link",
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
        requested_qty=3,
        planned_qty=3,
        qty=3,
        need_date=datetime.date(2026, 5, 25),
        order_date=datetime.date(2026, 5, 20),
        lead_time_days=5,
        bucket_date=datetime.date(2026, 5, 25),
    )
    db.add_all([first, second])
    db.commit()

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def get_all(self, *args, **kwargs):
            return []

        def post(self, *args, **kwargs):
            return {"Ref_Key": "purchase-order-ref"}

    monkeypatch.setattr(
        exporter,
        "_load_odata_config",
        lambda: {"base_url": "http://mtzdock/unf_demo/odata/standard.odata"},
    )
    monkeypatch.setattr(exporter, "OData1CClient", FakeClient)

    result = export_planned_purchases_to_1c(db, run.run_id, dry_run=False)

    assert result["status"] == "ok"
    assert result["orders_created"] == 1
    assert result["orders"][0]["lines"][0]["purchase_ids"] == [first.purchase_id, second.purchase_id]
    links = db.query(SyncLink).filter_by(source_doctype="planned_purchase").order_by(SyncLink.source_id).all()
    assert [link.source_id for link in links] == [first.purchase_id, second.purchase_id]
    assert {link.target_ref_key for link in links} == {"purchase-order-ref"}
    assert {link.target_entity for link in links} == {"Document_ЗаказПоставщику"}
    assert {link.status for link in links} == {"success"}


def test_purchase_order_export_creates_delta_order_after_old_filled_order(db_session, monkeypatch):
    db = db_session
    item = Item(
        item_code="PO-1C-DELTA",
        item_name="Дозаявка",
        item_article="DELTA",
        item_ref1c="item-ref-delta",
        supplier_ref1c="supplier-delta",
        replenishment_method="Покупка",
        status="active",
    )
    db.add(item)
    db.flush()
    run = _mk_run(db)
    purchase = PlannedPurchase(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=7,
        planned_qty=7,
        qty=7,
        need_date=datetime.date(2026, 5, 25),
        order_date=datetime.date(2026, 5, 20),
        lead_time_days=5,
        bucket_date=datetime.date(2026, 5, 25),
    )
    db.add(purchase)
    db.commit()

    first_number = exporter._short_order_number(run.run_id, 1)
    second_number = exporter._short_order_number(run.run_id, 2)

    class FakeClient:
        posted_payload = None

        def __init__(self, **kwargs):
            pass

        def get_all(self, *args, **kwargs):
            number = first_number if first_number in kwargs["filter_query"] else second_number
            if number == first_number:
                return [{
                    "Ref_Key": "old-order-ref",
                    "Number": first_number,
                    "Контрагент_Key": "supplier-delta",
                    "Комментарий": (
                        f"PRODPLAN source=planned_purchase/run:{run.run_id}; "
                        f"number={first_number}"
                    ),
                    "Запасы": [{"Количество": 3}],
                }]
            return []

        def post(self, entity, payload):
            # A success link must not be recorded until the delta is in the
            # document payload and 1C has accepted the POST.
            assert db.query(SyncLink).filter_by(
                source_doctype="planned_purchase", source_id=purchase.purchase_id
            ).count() == 0
            assert payload["Number"] == second_number
            assert payload["Запасы"][0]["Количество"] == 7.0
            self.__class__.posted_payload = payload
            return {"Ref_Key": "delta-order-ref"}

    monkeypatch.setattr(exporter, "_load_odata_config", lambda: {
        "base_url": "http://mtzdock/unf_demo/odata/standard.odata"
    })
    monkeypatch.setattr(exporter, "OData1CClient", FakeClient)

    result = export_planned_purchases_to_1c(db, run.run_id, dry_run=False)

    assert result["status"] == "ok"
    assert result["orders_created"] == 1
    assert result["orders"][0]["number"] == second_number
    assert FakeClient.posted_payload["Запасы"][0]["Количество"] == 7.0
    link = db.query(SyncLink).filter_by(
        source_doctype="planned_purchase", source_id=purchase.purchase_id
    ).one()
    assert link.status == "success"
    assert link.target_ref_key == "delta-order-ref"


def test_purchase_order_export_recovers_exact_posted_batch_without_duplicate(db_session, monkeypatch):
    db = db_session
    item = Item(
        item_code="PO-1C-RETRY",
        item_name="Повтор экспорта",
        item_article="RETRY",
        item_ref1c="item-ref-retry",
        supplier_ref1c="supplier-retry",
        replenishment_method="Покупка",
        status="active",
    )
    db.add(item)
    db.flush()
    run = _mk_run(db)
    purchase = PlannedPurchase(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=11,
        planned_qty=11,
        qty=11,
        need_date=datetime.date(2026, 6, 1),
        order_date=datetime.date(2026, 5, 27),
        lead_time_days=5,
        bucket_date=datetime.date(2026, 6, 1),
    )
    db.add(purchase)
    db.commit()

    class FakeClient:
        stored_doc = None
        post_count = 0

        def __init__(self, **kwargs):
            pass

        def get_all(self, *args, **kwargs):
            return [self.__class__.stored_doc] if self.__class__.stored_doc else []

        def post(self, entity, payload):
            self.__class__.post_count += 1
            self.__class__.stored_doc = {
                **payload,
                "Ref_Key": "retry-order-ref",
                "Контрагент_Key": payload["Контрагент_Key"],
            }
            return {"Ref_Key": "retry-order-ref"}

    monkeypatch.setattr(exporter, "_load_odata_config", lambda: {
        "base_url": "http://mtzdock/unf_demo/odata/standard.odata"
    })
    monkeypatch.setattr(exporter, "OData1CClient", FakeClient)

    first = export_planned_purchases_to_1c(db, run.run_id, dry_run=False)
    assert first["orders_created"] == 1
    assert FakeClient.post_count == 1

    # Simulate the narrow failure window: 1C kept the POST, while the local
    # transaction (including its success link) was lost.
    db.query(SyncLink).filter_by(
        source_doctype="planned_purchase", source_id=purchase.purchase_id
    ).delete()
    db.commit()

    second = export_planned_purchases_to_1c(db, run.run_id, dry_run=False)

    assert second["status"] == "ok"
    assert second["orders_created"] == 0
    assert second["orders_existing"] == 1
    assert FakeClient.post_count == 1
    link = db.query(SyncLink).filter_by(
        source_doctype="planned_purchase", source_id=purchase.purchase_id
    ).one()
    assert link.status == "success"
    assert link.target_ref_key == "retry-order-ref"


def test_purchase_order_batch_token_is_independent_of_line_order():
    first = exporter.PurchaseOrderExportLine(
        purchase_ids=[12, 10],
        item_id=2,
        item_ref1c="item-b",
        item_name="Одинаковое имя",
        item_article="SAME",
        unit_ref1c="unit-ref",
        unit_name="шт",
        qty=5,
        need_date="2026-06-01",
        order_date="2026-05-27",
    )
    second = exporter.PurchaseOrderExportLine(
        purchase_ids=[11],
        item_id=1,
        item_ref1c="item-a",
        item_name="Одинаковое имя",
        item_article="SAME",
        unit_ref1c="unit-ref",
        unit_name="шт",
        qty=7,
        need_date="2026-06-01",
        order_date="2026-05-27",
    )
    forward = exporter.PurchaseOrderExportGroup(
        supplier_ref1c="supplier-token",
        number="PP-1",
        lines=[first, second],
    )
    reversed_group = exporter.PurchaseOrderExportGroup(
        supplier_ref1c="supplier-token",
        number="PP-99",
        lines=[second, first],
    )

    assert exporter._group_batch_token(forward) == exporter._group_batch_token(reversed_group)
