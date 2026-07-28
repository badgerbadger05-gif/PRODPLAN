import datetime

from app.models import (
    Item,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionResource,
    ProductionStage,
    Specification,
    WorkshopWarehouseBinding,
)
from app.schemas import ODataSyncRequest
from app.services.production_order_sync import (
    PRODUCTION_ORDER_SYNC_FROM_1C,
    _resolve_product_destination,
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
                assert "СтруктурнаяЕдиницаПродукции_Key" in select_fields
                return [
                    {
                        "Ref_Key": "order-ref-done",
                        "Number": "2047",
                        "Date": "2026-06-15T00:00:00",
                        "Posted": True,
                        "DeletionMark": False,
                        "СостояниеЗаказа_Key": DONE_STATE_KEY,
                        "СтруктурнаяЕдиницаПродукции_Key": "warehouse-header",
                    }
                ]
            if entity_name == "Document_ЗаказНаПроизводство_Продукция":
                assert "СтруктурнаяЕдиница_Key" in select_fields
                return [
                    {
                        "Ref_Key": "order-ref-done",
                        "LineNumber": 1,
                        "Номенклатура_Key": "item-ref-1",
                        "Количество": 2.0,
                        "СтруктурнаяЕдиница_Key": "00000000-0000-0000-0000-000000000000",
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
    assert product.destination_warehouse_ref1c == "warehouse-header"


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


def test_production_fact_sync_with_only_blank_references_makes_no_odata_requests(
    db_session, monkeypatch
):
    db_session.add(
        ProductionOrder(
            order_number="LOCAL-BLANK",
            order_date=datetime.datetime(2026, 7, 20),
            order_ref1c="   ",
            deletion_mark=False,
        )
    )
    db_session.commit()

    class FakeODataClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_all(self, *_args, **_kwargs):
            raise AssertionError("OData must not be called for blank references")

    monkeypatch.setattr("app.services.odata_client.OData1CClient", FakeODataClient)

    stats = sync_production_fact_from_odata(
        db_session,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_ЗаказНаПроизводство",
        ),
    )

    assert stats["orders_skipped_missing_ref"] == 1
    assert stats["dry_run"] is True


def test_production_destination_falls_back_to_existing_workshop_binding(db_session):
    item = Item(item_code="BIND", item_name="Binding item")
    order = ProductionOrder(
        order_number="BIND-1",
        order_date=datetime.datetime(2026, 8, 1),
        order_ref1c="bind-order",
    )
    workshop = ProductionResource(resource_name="Binding workshop")
    db_session.add_all([item, order, workshop])
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        quantity=1,
        produced_qty=0,
        remaining_qty=1,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add_all(
        [
            ProductionOrderLineState(
                product_id=product.product_id, workshop_id=workshop.resource_id
            ),
            WorkshopWarehouseBinding(
                workshop_id=workshop.resource_id,
                warehouse_ref1c="reserve-ref",
                production_warehouse_ref1c="production-ref",
            ),
        ]
    )
    db_session.flush()

    destination, source = _resolve_product_destination(
        db_session,
        {"СтруктурнаяЕдиница_Key": "00000000-0000-0000-0000-000000000000"},
        {
            "СтруктурнаяЕдиницаПродукции_Key": "00000000-0000-0000-0000-000000000000"
        },
        product,
    )

    assert (destination, source) == ("production-ref", "binding")


def _spec_stage_fixture(db):
    """Item + Specification + ProductionStage all carrying 1C refs."""
    item = Item(
        item_code="SPEC-SYNC-ITEM",
        item_name="Spec Sync Item",
        item_article="SPEC-SYNC-ITEM",
        item_ref1c="item-ref-spec",
        stock_qty=0,
        status="active",
    )
    spec = Specification(
        spec_code="SP-1",
        spec_name="Spec 1",
        spec_ref1c="spec-ref-1",
    )
    stage = ProductionStage(stage_name="Сварка", stage_ref1c="stage-ref-1")
    db.add_all([item, spec, stage])
    db.flush()
    return item, spec, stage


def _spec_stage_client(product_row, *, reject_extended=False, calls=None):
    class FakeODataClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_all(self, entity_name, filter_query=None, select_fields=None, **_kwargs):
            if entity_name == "Document_ЗаказНаПроизводство":
                return [
                    {
                        "Ref_Key": "order-ref-spec",
                        "Number": "3001",
                        "Date": "2026-06-15T00:00:00",
                        "Posted": True,
                        "DeletionMark": False,
                        "СостояниеЗаказа_Key": DONE_STATE_KEY,
                    }
                ]
            if entity_name == "Document_ЗаказНаПроизводство_Продукция":
                if calls is not None:
                    calls.append(list(select_fields or []))
                if reject_extended and "Спецификация_Key" in (select_fields or []):
                    raise RuntimeError("Bad request: path segment is not found")
                return [dict(product_row)]
            if entity_name == "Document_СборкаЗапасов":
                return []
            raise AssertionError(f"Unexpected OData entity: {entity_name}")

    return FakeODataClient


def test_production_order_sync_resolves_spec_and_stage_refs(db_session, monkeypatch):
    db = db_session
    _item, spec, stage = _spec_stage_fixture(db)
    db.commit()

    calls = []
    monkeypatch.setattr(
        "app.services.odata_client.OData1CClient",
        _spec_stage_client(
            {
                "Ref_Key": "order-ref-spec",
                "LineNumber": 1,
                "Номенклатура_Key": "item-ref-spec",
                "Количество": 3.0,
                "Спецификация_Key": "spec-ref-1",
                "Этап_Key": "stage-ref-1",
            },
            calls=calls,
        ),
    )

    sync_production_orders_from_odata(
        db,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_ЗаказНаПроизводство",
        ),
    )

    assert "Спецификация_Key" in calls[0]
    assert "Этап_Key" in calls[0]
    product = db.query(ProductionProduct).one()
    assert product.spec_id == spec.spec_id
    assert product.stage_id == stage.stage_id


def test_production_order_sync_keeps_spec_and_stage_when_1c_omits_them(
    db_session, monkeypatch
):
    """Regression: a sync without Спецификация_Key/Этап_Key used to NULL them."""
    db = db_session
    item, spec, stage = _spec_stage_fixture(db)
    order = ProductionOrder(
        order_number="3001",
        order_date=datetime.datetime(2026, 6, 15),
        order_ref1c="order-ref-spec",
        is_posted=True,
        deletion_mark=False,
    )
    db.add(order)
    db.flush()
    db.add(
        ProductionProduct(
            order_id=order.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=3,
            produced_qty=0,
            remaining_qty=3,
            spec_id=spec.spec_id,
            stage_id=stage.stage_id,
        )
    )
    db.commit()

    monkeypatch.setattr(
        "app.services.odata_client.OData1CClient",
        _spec_stage_client(
            {
                "Ref_Key": "order-ref-spec",
                "LineNumber": 1,
                "Номенклатура_Key": "item-ref-spec",
                "Количество": 4.0,
            }
        ),
    )

    sync_production_orders_from_odata(
        db,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_ЗаказНаПроизводство",
        ),
    )

    product = db.query(ProductionProduct).one()
    assert float(product.quantity) == 4.0
    assert product.spec_id == spec.spec_id
    assert product.stage_id == stage.stage_id


def test_production_order_sync_falls_back_when_1c_rejects_spec_select(
    db_session, monkeypatch
):
    db = db_session
    item, spec, stage = _spec_stage_fixture(db)
    order = ProductionOrder(
        order_number="3001",
        order_date=datetime.datetime(2026, 6, 15),
        order_ref1c="order-ref-spec",
        is_posted=True,
        deletion_mark=False,
    )
    db.add(order)
    db.flush()
    db.add(
        ProductionProduct(
            order_id=order.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=3,
            produced_qty=0,
            remaining_qty=3,
            spec_id=spec.spec_id,
            stage_id=stage.stage_id,
        )
    )
    db.commit()

    calls = []
    monkeypatch.setattr(
        "app.services.odata_client.OData1CClient",
        _spec_stage_client(
            {
                "Ref_Key": "order-ref-spec",
                "LineNumber": 1,
                "Номенклатура_Key": "item-ref-spec",
                "Количество": 3.0,
            },
            reject_extended=True,
            calls=calls,
        ),
    )

    stats = sync_production_orders_from_odata(
        db,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_ЗаказНаПроизводство",
        ),
    )

    # Первая попытка с расширенным $select, вторая — на минимальном наборе.
    assert "Спецификация_Key" in calls[0]
    assert "Спецификация_Key" not in calls[1]
    assert stats["products_failed"] == 0
    product = db.query(ProductionProduct).one()
    assert product.spec_id == spec.spec_id
    assert product.stage_id == stage.stage_id


def test_production_destination_line_precedes_header(db_session):
    destination, source = _resolve_product_destination(
        db_session,
        {"СтруктурнаяЕдиница_Key": "line-ref"},
        {"СтруктурнаяЕдиницаПродукции_Key": "header-ref"},
    )

    assert (destination, source) == ("line-ref", "line")
