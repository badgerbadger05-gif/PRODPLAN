import datetime

from app.models import Item, ProductionOrder, ProductionProduct
from app.schemas import ODataSyncRequest
from app.services.production_order_sync import (
    PRODUCTION_ORDER_SYNC_FROM_1C,
    sync_production_fact_from_odata,
    sync_production_orders_from_odata,
)


DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"


def test_production_order_sync_includes_completed_orders(db_session, monkeypatch):
    db = db_session

    item = Item(
        item_code="PROD-SYNC-ITEM",
        item_name="Production Sync Item",
        item_article="PROD-SYNC-ITEM",
        item_ref1c="item-ref-1",
        stock_qty=0,
        status="active",
    )
    db.add(item)
    db.flush()

    seen_order_filter = {}

    class FakeODataClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_all(self, entity_name, filter_query=None, select_fields=None, **_kwargs):
            if entity_name == "Document_ЗаказНаПроизводство":
                seen_order_filter["value"] = filter_query
                assert filter_query == f"Date ge datetime'{PRODUCTION_ORDER_SYNC_FROM_1C}' and Posted eq true"
                assert "DeletionMark" in select_fields
                assert "СостояниеЗаказа_Key" in select_fields
                return [
                    {
                        "Ref_Key": "order-ref-done",
                        "Number": "2047",
                        "Date": "2026-06-15T00:00:00",
                        "Posted": True,
                        "DeletionMark": False,
                        "СостояниеЗаказа_Key": DONE_STATE_KEY,
                    }
                ]
            if entity_name == "Document_ЗаказНаПроизводство_Продукция":
                return [
                    {
                        "Ref_Key": "order-ref-done",
                        "LineNumber": 1,
                        "Номенклатура_Key": "item-ref-1",
                        "Количество": 2.0,
                    }
                ]
            if entity_name == "Document_СборкаЗапасов":
                return []
            raise AssertionError(f"Unexpected OData entity: {entity_name}")

    monkeypatch.setattr("app.services.odata_client.OData1CClient", FakeODataClient)

    stats = sync_production_orders_from_odata(
        db,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_ЗаказНаПроизводство",
        ),
    )

    assert stats["orders_total"] == 1
    assert stats["orders_created"] == 1
    assert seen_order_filter["value"] == f"Date ge datetime'{PRODUCTION_ORDER_SYNC_FROM_1C}' and Posted eq true"

    order = db.query(ProductionOrder).one()
    assert order.order_ref1c == "order-ref-done"
    assert order.order_number == "2047"
    assert order.order_date == datetime.datetime(2026, 6, 15)
    assert order.order_state_key == DONE_STATE_KEY
    assert order.deletion_mark is False

    product = db.query(ProductionProduct).one()
    assert product.order_id == order.order_id
    assert product.item_id == item.item_id
    assert product.line_number == 1
    assert float(product.quantity) == 2.0


def test_production_order_sync_closes_missing_orders_only_inside_sync_horizon(db_session, monkeypatch):
    db = db_session

    old_order = ProductionOrder(
        order_number="APR-OLD",
        order_date=datetime.datetime(2026, 4, 20),
        order_ref1c="order-ref-april",
        is_posted=True,
        deletion_mark=False,
    )
    may_order = ProductionOrder(
        order_number="MAY-MISSING",
        order_date=datetime.datetime(2026, 5, 10),
        order_ref1c="order-ref-may",
        is_posted=True,
        deletion_mark=False,
    )
    db.add_all([old_order, may_order])
    db.commit()

    class FakeODataClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_all(self, entity_name, **_kwargs):
            if entity_name == "Document_ЗаказНаПроизводство":
                return [
                    {
                        "Ref_Key": "order-ref-loaded",
                        "Number": "MAY-LOADED",
                        "Date": "2026-05-11T00:00:00",
                        "Posted": True,
                        "DeletionMark": False,
                        "СостояниеЗаказа_Key": DONE_STATE_KEY,
                    }
                ]
            if entity_name == "Document_ЗаказНаПроизводство_Продукция":
                return []
            if entity_name == "Document_СборкаЗапасов":
                return []
            raise AssertionError(f"Unexpected OData entity: {entity_name}")

    monkeypatch.setattr("app.services.odata_client.OData1CClient", FakeODataClient)

    sync_production_orders_from_odata(
        db,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_ЗаказНаПроизводство",
        ),
    )

    db.refresh(old_order)
    db.refresh(may_order)
    assert old_order.deletion_mark is False
    assert may_order.deletion_mark is True


def test_production_fact_sync_skips_orders_without_1c_reference(
    db_session, monkeypatch
):
    db_session.add_all(
        [
            ProductionOrder(
                order_number="LOCAL-ONLY",
                order_date=datetime.datetime(2026, 7, 20),
                order_ref1c=None,
                deletion_mark=False,
            ),
            ProductionOrder(
                order_number="SYNCED",
                order_date=datetime.datetime(2026, 7, 20),
                order_ref1c="valid-order-ref",
                deletion_mark=False,
            ),
        ]
    )
    db_session.commit()

    seen_filters = []

    class FakeODataClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_all(self, entity_name, filter_query=None, **_kwargs):
            seen_filters.append((entity_name, filter_query))
            return []

    monkeypatch.setattr("app.services.odata_client.OData1CClient", FakeODataClient)

    stats = sync_production_fact_from_odata(
        db_session,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_ЗаказНаПроизводство",
        ),
    )

    assert stats["orders_skipped_missing_ref"] == 1
    assert seen_filters == [
        (
            "Document_СборкаЗапасов",
            "ЗаказНаПроизводство_Key eq guid'valid-order-ref' and Posted eq true",
        )
    ]
    assert all("None" not in (filter_query or "") for _, filter_query in seen_filters)
