import datetime

from app.models import Item, SupplierOrder, SupplierOrderItem
from app.schemas import ODataSyncRequest
from app.services.supplier_order_sync import sync_supplier_orders_from_odata


def test_supplier_order_sync_stores_state_deletion_and_item_rows(db_session, monkeypatch):
    db = db_session

    item = Item(
        item_code="SUP-SYNC-ITEM",
        item_name="Supplier Sync Item",
        item_article="SUP-SYNC-ITEM",
        item_ref1c="item-ref-1",
        replenishment_method="Покупка",
        stock_qty=0,
        status="active",
    )
    db.add(item)
    db.flush()

    class FakeODataClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_all(self, entity_name, filter_query=None, select_fields=None, **_kwargs):
            if entity_name == "Catalog_СостоянияЗаказовПоставщикам":
                return [{"Ref_Key": "state-active", "Description": "В закупку"}]
            if entity_name == "Catalog_Организации":
                return [{"Ref_Key": "org-zsm", "Description": "ЗСМ"}]
            if entity_name == "Document_ПриходнаяНакладная":
                return []
            assert entity_name == "Document_ЗаказПоставщику"
            assert "DeletionMark" in select_fields
            assert "СостояниеЗаказа_Key" in select_fields
            assert "Организация_Key" in select_fields
            return [
                {
                    "Ref_Key": "order-ref-1",
                    "Number": "SUP-1",
                    "Date": "2026-05-08T10:00:00",
                    "Posted": True,
                    "DeletionMark": False,
                    "СостояниеЗаказа_Key": "state-active",
                    "Организация_Key": "org-zsm",
                    "Контрагент_Key": "supplier-ref-1",
                    "Контрагент": {"Description": "Supplier One"},
                    "СуммаДокумента": 120.0,
                    "Запасы": [
                        {
                            "LineNumber": 1,
                            "Номенклатура_Key": "item-ref-1",
                            "Количество": 10.0,
                            "КоличествоПоступило": 3.0,
                            "Цена": 12.0,
                            "Сумма": 120.0,
                            "ДатаПоступления": "2026-05-12T00:00:00",
                        }
                    ],
                }
            ]

    monkeypatch.setattr("app.services.odata_client.OData1CClient", FakeODataClient)

    stats = sync_supplier_orders_from_odata(
        db,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_ЗаказПоставщику",
        ),
    )

    assert stats["orders_created"] == 1
    order = db.query(SupplierOrder).one()
    assert order.order_ref1c == "order-ref-1"
    assert order.order_state_key == "state-active"
    assert order.order_state_name == "В закупку"
    assert order.deletion_mark is False
    assert order.supplier_id is not None

    row = db.query(SupplierOrderItem).one()
    assert row.order_id == order.order_id
    assert row.item_id_ref == item.item_id
    assert row.line_number == 1
    assert float(row.quantity) == 10.0
    assert float(row.received_qty) == 3.0
    assert float(row.remaining_qty) == 7.0
    assert row.delivery_date == datetime.datetime(2026, 5, 12)


def test_supplier_order_sync_skips_non_zsm_organization_and_deactivates_existing(db_session, monkeypatch):
    db = db_session

    existing = SupplierOrder(
        order_number="SUP-OLD",
        order_date=datetime.datetime(2026, 5, 1),
        order_ref1c="order-ref-other",
        is_posted=True,
        order_state_key="state-active",
        order_state_name="В закупку",
        deletion_mark=False,
    )
    db.add(existing)
    db.commit()

    class FakeODataClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_all(self, entity_name, filter_query=None, select_fields=None, **_kwargs):
            if entity_name == "Catalog_СостоянияЗаказовПоставщикам":
                return [{"Ref_Key": "state-active", "Description": "В закупку"}]
            if entity_name == "Catalog_Организации":
                return [
                    {"Ref_Key": "org-zsm", "Description": "ЗСМ"},
                    {"Ref_Key": "org-mtz", "Description": "МТЗ"},
                ]
            if entity_name == "Document_ПриходнаяНакладная":
                return []
            assert entity_name == "Document_ЗаказПоставщику"
            return [
                {
                    "Ref_Key": "order-ref-other",
                    "Number": "SUP-OLD",
                    "Date": "2026-05-08T10:00:00",
                    "Posted": True,
                    "DeletionMark": False,
                    "СостояниеЗаказа_Key": "state-active",
                    "Организация_Key": "org-mtz",
                    "Контрагент_Key": "supplier-ref-1",
                    "СуммаДокумента": 120.0,
                    "Запасы": [],
                }
            ]

    monkeypatch.setattr("app.services.odata_client.OData1CClient", FakeODataClient)

    stats = sync_supplier_orders_from_odata(
        db,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_ЗаказПоставщику",
        ),
    )

    assert stats["orders_skipped_by_organization"] == 1
    assert stats["orders_updated"] == 1
    assert db.query(SupplierOrder).one().deletion_mark is True


def test_supplier_order_sync_applies_receipts_from_incoming_invoices(db_session, monkeypatch):
    db = db_session

    item = Item(
        item_code="SUP-RECEIPT-ITEM",
        item_name="Supplier Receipt Item",
        item_article="SUP-RECEIPT-ITEM",
        item_ref1c="item-ref-1",
        replenishment_method="Покупка",
        stock_qty=0,
        status="active",
    )
    db.add(item)
    db.flush()

    class FakeODataClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_all(self, entity_name, filter_query=None, select_fields=None, **_kwargs):
            if entity_name == "Catalog_СостоянияЗаказовПоставщикам":
                return [{"Ref_Key": "state-active", "Description": "В закупку"}]
            if entity_name == "Catalog_Организации":
                return [{"Ref_Key": "org-zsm", "Description": "ЗСМ"}]
            if entity_name == "Document_ПриходнаяНакладная":
                assert filter_query == "Posted eq true and DeletionMark eq false"
                return [
                    {
                        "Ref_Key": "receipt-ref-1",
                        "Posted": True,
                        "DeletionMark": False,
                        "Заказ": "order-ref-1",
                        "Заказ_Type": "StandardODATA.Document_ЗаказПоставщику",
                        "Запасы": [
                            {
                                "LineNumber": 1,
                                "Номенклатура_Key": "item-ref-1",
                                "Количество": 10.0,
                                "Заказ": "order-ref-1",
                                "Заказ_Type": "StandardODATA.Document_ЗаказПоставщику",
                            }
                        ],
                    }
                ]
            assert entity_name == "Document_ЗаказПоставщику"
            return [
                {
                    "Ref_Key": "order-ref-1",
                    "Number": "SUP-1",
                    "Date": "2026-05-08T10:00:00",
                    "Posted": True,
                    "DeletionMark": False,
                    "СостояниеЗаказа_Key": "state-active",
                    "Организация_Key": "org-zsm",
                    "Контрагент_Key": "supplier-ref-1",
                    "СуммаДокумента": 120.0,
                    "Запасы": [
                        {
                            "LineNumber": 1,
                            "Номенклатура_Key": "item-ref-1",
                            "Количество": 10.0,
                            "КоличествоПоступило": 0.0,
                            "Цена": 12.0,
                            "Сумма": 120.0,
                            "ДатаПоступления": "2026-05-12T00:00:00",
                        }
                    ],
                }
            ]

    monkeypatch.setattr("app.services.odata_client.OData1CClient", FakeODataClient)

    stats = sync_supplier_orders_from_odata(
        db,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_ЗаказПоставщику",
        ),
    )

    row = db.query(SupplierOrderItem).one()
    assert float(row.quantity) == 10.0
    assert float(row.received_qty) == 10.0
    assert float(row.remaining_qty) == 0.0
    assert stats["receipt_rows_applied"] == 1
