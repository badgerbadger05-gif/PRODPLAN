import datetime

from app.models import Item, SupplierOrder, SupplierOrderItem
from app.schemas import ODataSyncRequest
from app.services.supplier_order_sync import (
    _resolve_supplier_destination,
    sync_supplier_orders_from_odata,
)


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
            if entity_name == "Document_РасходнаяНакладная":
                assert "Posted eq true" in filter_query
                assert "8d970138-9934-11eb-e39a-fa163e61326a" in filter_query
                return [
                    {
                        "Date": "2026-05-10T12:00:00",
                        "Posted": True,
                        "DeletionMark": False,
                        "ХозяйственнаяОперация_Key": "8d970138-9934-11eb-e39a-fa163e61326a",
                        "ДокументОснование": "order-ref-1",
                        "ДокументОснование_Type": "StandardODATA.Document_ЗаказПоставщику",
                    },
                    {
                        "Date": "2026-05-09T08:00:00",
                        "Posted": True,
                        "DeletionMark": False,
                        "ХозяйственнаяОперация_Key": "8d970138-9934-11eb-e39a-fa163e61326a",
                        "Заказ": "order-ref-1",
                        "Заказ_Type": "StandardODATA.Document_ЗаказПоставщику",
                    },
                    {
                        "Date": "2026-05-08T08:00:00",
                        "Posted": False,
                        "DeletionMark": False,
                        "ХозяйственнаяОперация_Key": "8d970138-9934-11eb-e39a-fa163e61326a",
                        "Заказ": "order-ref-1",
                        "Заказ_Type": "StandardODATA.Document_ЗаказПоставщику",
                    },
                ]
            if entity_name == "Document_ОтчетПереработчика":
                assert "Posted eq true" in filter_query
                assert "8d96ffe4-9934-11eb-e39a-fa163e61326a" in filter_query
                return [
                    {
                        "Date": "2026-05-13T12:00:00",
                        "Posted": True,
                        "DeletionMark": False,
                        "ХозяйственнаяОперация_Key": "8d96ffe4-9934-11eb-e39a-fa163e61326a",
                        "ДокументОснование_Key": "order-ref-1",
                        "Продукция": [
                            {
                                "Номенклатура_Key": "item-ref-1",
                                "Характеристика_Key": None,
                                "Количество": 4,
                            }
                        ],
                    },
                    {
                        "Date": "2026-05-15T16:00:00",
                        "Posted": True,
                        "DeletionMark": False,
                        "ХозяйственнаяОперация_Key": "8d96ffe4-9934-11eb-e39a-fa163e61326a",
                        "ДокументОснование_Key": "order-ref-1",
                        "Продукция": [
                            {
                                "Номенклатура_Key": "item-ref-1",
                                "Характеристика_Key": None,
                                "Количество": 3,
                            },
                            {
                                "Номенклатура_Key": "item-ref-1",
                                "Характеристика_Key": "char-2",
                                "Количество": 8,
                            },
                        ],
                    },
                    {
                        "Date": "2026-05-20T16:00:00",
                        "Posted": True,
                        "DeletionMark": True,
                        "ХозяйственнаяОперация_Key": "8d96ffe4-9934-11eb-e39a-fa163e61326a",
                        "ДокументОснование_Key": "order-ref-1",
                        "Продукция": [
                            {
                                "Номенклатура_Key": "item-ref-1",
                                "Характеристика_Key": None,
                                "Количество": 100,
                            }
                        ],
                    },
                    {
                        "Date": "2026-05-18T16:00:00",
                        "Posted": True,
                        "DeletionMark": False,
                        "ХозяйственнаяОперация_Key": "8d96ffe4-9934-11eb-e39a-fa163e61326a",
                        "ДокументОснование_Key": "order-ref-1",
                        "Продукция": [],
                    },
                ]
            assert entity_name == "Document_ЗаказПоставщику"
            assert "DeletionMark" in select_fields
            assert "ВидОперации" in select_fields
            assert "ХозяйственнаяОперация_Key" in select_fields
            assert "СостояниеЗаказа_Key" in select_fields
            assert "Организация_Key" in select_fields
            assert "СтруктурнаяЕдиницаРезерв_Key" in select_fields
            assert "УчетПотребностиПоСкладам" in select_fields
            return [
                {
                    "Ref_Key": "order-ref-1",
                    "Number": "SUP-1",
                    "Date": "2026-05-08T10:00:00",
                    "Posted": True,
                    "DeletionMark": False,
                    "ВидОперации": "ЗаказНаПереработку",
                    "ХозяйственнаяОперация_Key": "8D96F6A2-9934-11EB-E39A-FA163E61326A",
                    "СостояниеЗаказа_Key": "state-active",
                    "Организация_Key": "org-zsm",
                    "Контрагент_Key": "supplier-ref-1",
                    "Контрагент": {"Description": "Supplier One"},
                    "СуммаДокумента": 120.0,
                    "СтруктурнаяЕдиницаРезерв_Key": "warehouse-header",
                    "УчетПотребностиПоСкладам": "true",
                    "Запасы": [
                        {
                            "LineNumber": 1,
                            "Номенклатура_Key": "item-ref-1",
                            "Количество": 10.0,
                            "КоличествоПоступило": 3.0,
                            "Цена": 12.0,
                            "Сумма": 120.0,
                            "ДатаПоступления": "2026-05-12T00:00:00",
                            "СтруктурнаяЕдиницаРезерв_Key": "00000000-0000-0000-0000-000000000000",
                            "СтруктурнаяЕдиница_Key": "generic-must-not-be-used",
                        },
                        {
                            "LineNumber": 2,
                            "Номенклатура_Key": "item-ref-1",
                            "Характеристика_Key": "char-2",
                            "Количество": 5.0,
                            "КоличествоПоступило": 0.0,
                            "Цена": 12.0,
                            "Сумма": 60.0,
                        },
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
    assert order.operation_name == "ЗаказНаПереработку"
    assert (
        order.operation_key
        == "8d96f6a2-9934-11eb-e39a-fa163e61326a"
    )
    assert order.processing_transfer_date == datetime.datetime(2026, 5, 9, 8)
    assert order.processing_report_date == datetime.datetime(2026, 5, 15, 16)
    assert order.supplier_id is not None

    row = db.query(SupplierOrderItem).filter_by(line_number=1).one()
    assert row.order_id == order.order_id
    assert row.item_id_ref == item.item_id
    assert row.line_number == 1
    assert float(row.quantity) == 10.0
    assert float(row.received_qty) == 7.0
    assert float(row.remaining_qty) == 3.0
    assert row.delivery_date == datetime.datetime(2026, 5, 12)
    assert row.destination_warehouse_ref1c == "warehouse-header"

    completed_row = db.query(SupplierOrderItem).filter_by(line_number=2).one()
    assert completed_row.characteristic_ref1c == "char-2"
    assert float(completed_row.quantity) == 5.0
    assert float(completed_row.received_qty) == 5.0
    assert float(completed_row.remaining_qty) == 0.0


def test_supplier_destination_never_uses_generic_structural_unit():
    destination, source = _resolve_supplier_destination(
        {
            "СтруктурнаяЕдиницаРезерв_Key": "00000000-0000-0000-0000-000000000000",
            "СтруктурнаяЕдиница_Key": "generic-ref",
        },
        {"СтруктурнаяЕдиницаРезерв_Key": None},
    )

    assert destination is None
    assert source == "unresolved"


def test_processing_report_failure_preserves_progress_but_updates_transfer(
    db_session, monkeypatch
):
    db = db_session
    item = Item(
        item_code="PROC-KEEP",
        item_name="Processing progress",
        item_ref1c="proc-item-ref",
        replenishment_method="Переработка",
    )
    order = SupplierOrder(
        order_number="PROC-1",
        order_date=datetime.datetime(2026, 5, 1),
        order_ref1c="proc-order-ref",
        is_posted=True,
        operation_key="8d96f6a2-9934-11eb-e39a-fa163e61326a",
        operation_name="ЗаказНаПереработку",
        processing_report_date=datetime.datetime(2026, 5, 7),
        deletion_mark=False,
    )
    db.add_all([item, order])
    db.flush()
    db.add(
        SupplierOrderItem(
            order_id=order.order_id,
            item_id_ref=item.item_id,
            line_number=1,
            quantity=10,
            received_qty=6,
            remaining_qty=4,
        )
    )
    db.commit()

    class FakeODataClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_all(self, entity_name, **_kwargs):
            if entity_name in (
                "Catalog_СостоянияЗаказовПоставщикам",
                "Catalog_Организации",
                "Document_ПриходнаяНакладная",
            ):
                return []
            if entity_name == "Document_РасходнаяНакладная":
                return [
                    {
                        "Date": "2026-05-03T09:00:00",
                        "Posted": True,
                        "DeletionMark": False,
                        "ХозяйственнаяОперация_Key": "8d970138-9934-11eb-e39a-fa163e61326a",
                        "ДокументОснование": "proc-order-ref",
                        "ДокументОснование_Type": "Document_ЗаказПоставщику",
                    }
                ]
            if entity_name == "Document_ОтчетПереработчика":
                raise RuntimeError("report publication temporarily unavailable")
            if entity_name == "Catalog_Контрагенты":
                return []
            assert entity_name == "Document_ЗаказПоставщику"
            return [
                {
                    "Ref_Key": "proc-order-ref",
                    "Number": "PROC-1",
                    "Date": "2026-05-01T00:00:00",
                    "Posted": True,
                    "DeletionMark": False,
                    "ВидОперации": "ЗаказНаПереработку",
                    "ХозяйственнаяОперация_Key": "8d96f6a2-9934-11eb-e39a-fa163e61326a",
                    "Запасы": [
                        {
                            "LineNumber": 1,
                            "Номенклатура_Key": "proc-item-ref",
                            "Количество": 12,
                            "КоличествоПоступило": 0,
                        }
                    ],
                }
            ]

    monkeypatch.setattr("app.services.odata_client.OData1CClient", FakeODataClient)

    sync_supplier_orders_from_odata(
        db,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_ЗаказПоставщику",
        ),
    )

    db.refresh(order)
    row = db.query(SupplierOrderItem).filter_by(order_id=order.order_id).one()
    assert order.processing_transfer_date == datetime.datetime(2026, 5, 3, 9)
    assert order.processing_report_date == datetime.datetime(2026, 5, 7)
    assert float(row.quantity) == 12
    assert float(row.received_qty) == 6
    assert float(row.remaining_qty) == 6


def test_supplier_destination_line_precedes_header():
    destination, source = _resolve_supplier_destination(
        {"СтруктурнаяЕдиницаРезерв_Key": "line-ref"},
        {
            "СтруктурнаяЕдиницаРезерв_Key": "header-ref",
            "УчетПотребностиПоСкладам": False,
        },
    )

    assert (destination, source) == ("line-ref", "line")


def test_supplier_destination_header_requires_warehouse_demand_flag():
    item_row = {
        "СтруктурнаяЕдиницаРезерв_Key": "00000000-0000-0000-0000-000000000000"
    }

    for truthy in (
        True,
        1,
        "true",
        "1",
        "да",
        "истина",
        {"value": True},
        {"Value": "true"},
    ):
        destination, source = _resolve_supplier_destination(
            item_row,
            {
                "СтруктурнаяЕдиницаРезерв_Key": "header-ref",
                "УчетПотребностиПоСкладам": truthy,
            },
        )
        assert (destination, source) == ("header-ref", "header")


def test_supplier_destination_ignores_header_when_warehouse_demand_disabled():
    item_row = {"СтруктурнаяЕдиницаРезерв_Key": None}

    for disabled in (
        False,
        0,
        "false",
        "0",
        "нет",
        "ложь",
        None,
        {"value": False},
        {"Boolean": "false"},
    ):
        destination, source = _resolve_supplier_destination(
            item_row,
            {
                "СтруктурнаяЕдиницаРезерв_Key": "header-ref",
                "УчетПотребностиПоСкладам": disabled,
            },
        )
        assert destination is None
        assert source == "unresolved"


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
