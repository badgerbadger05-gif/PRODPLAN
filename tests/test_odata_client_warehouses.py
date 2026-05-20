from app.services.odata_client import get_stock_from_1c_odata
import app.services.odata_client as odata_client


def test_get_stock_resolves_warehouse_name_from_catalog(monkeypatch):
    warehouse_ref = "0740c584-d98a-11ef-98ea-9ee51454587f"
    item_ref = "11111111-2222-3333-4444-555555555555"

    def _fake_get_all(self, entity_name, filter_query=None, select_fields=None, top=1000, max_records=None, max_pages=1000, order_by=None):
        assert "/Balance" in str(entity_name)
        return [
            {
                "Номенклатура_Key": item_ref,
                "СтруктурнаяЕдиница_Key": warehouse_ref,
                "КоличествоBalance": 3.0,
            }
        ]

    def _fake_make_request(self, endpoint, params=None, timeout=60, retries=4, retry_backoff_sec=1.0):
        ep = str(endpoint)
        if ep == "Catalog_Номенклатура":
            return {
                "value": [
                    {
                        "Ref_Key": item_ref,
                        "Code": "CP-000681-RT",
                        "Description": "Компонент",
                        "Артикул": "CP-000681-RT",
                    }
                ]
            }
        if ep == "Catalog_Склады":
            return {
                "value": [
                    {
                        "Ref_Key": warehouse_ref,
                        "Code": "СКЛ-01",
                        "Description": "Склад комплектующих",
                    }
                ]
            }
        return {"value": []}

    monkeypatch.setattr(odata_client.OData1CClient, "get_all", _fake_get_all)
    monkeypatch.setattr(odata_client.OData1CClient, "_make_request", _fake_make_request)

    out = get_stock_from_1c_odata(
        base_url="http://example.local/odata",
        entity_name="AccumulationRegister_ЗапасыНаСкладах/Balance",
        username=None,
        password=None,
        token=None,
        filter_query=None,
        select_fields=None,
    )

    assert len(out) == 1
    row = out[0]
    assert row["warehouse_ref"] == warehouse_ref
    assert row["warehouse_code"] == "СКЛ-01"
    assert row["warehouse_name"] == "Склад комплектующих"


def test_get_stock_warehouse_resolve_skips_non_guid_refs(monkeypatch):
    warehouse_ref = "0740c584-d98a-11ef-98ea-9ee51454587f"
    item_ref_1 = "11111111-2222-3333-4444-555555555555"
    item_ref_2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def _fake_get_all(self, entity_name, filter_query=None, select_fields=None, top=1000, max_records=None, max_pages=1000, order_by=None):
        assert "/Balance" in str(entity_name)
        return [
            {
                "Номенклатура_Key": item_ref_1,
                "СтруктурнаяЕдиница_Key": "NOT-A-GUID-WAREHOUSE",
                "КоличествоBalance": 1.0,
            },
            {
                "Номенклатура_Key": item_ref_2,
                "СтруктурнаяЕдиница_Key": warehouse_ref,
                "КоличествоBalance": 2.0,
            },
        ]

    def _fake_make_request(self, endpoint, params=None, timeout=60, retries=4, retry_backoff_sec=1.0):
        ep = str(endpoint)
        if ep == "Catalog_Номенклатура":
            return {
                "value": [
                    {"Ref_Key": item_ref_1, "Code": "I-1", "Description": "Item 1", "Артикул": "I-1"},
                    {"Ref_Key": item_ref_2, "Code": "I-2", "Description": "Item 2", "Артикул": "I-2"},
                ]
            }
        if ep == "Catalog_Склады":
            return {"value": []}
        if ep == "Catalog_СтруктурныеЕдиницы":
            return {
                "value": [
                    {
                        "Ref_Key": warehouse_ref,
                        "Code": "НФ-000079",
                        "Description": "Разработка склад(ЗСМ)",
                    }
                ]
            }
        return {"value": []}

    monkeypatch.setattr(odata_client.OData1CClient, "get_all", _fake_get_all)
    monkeypatch.setattr(odata_client.OData1CClient, "_make_request", _fake_make_request)

    out = get_stock_from_1c_odata(
        base_url="http://example.local/odata",
        entity_name="AccumulationRegister_ЗапасыНаСкладах/Balance",
        username=None,
        password=None,
        token=None,
        filter_query=None,
        select_fields=None,
    )

    assert len(out) == 2
    by_wh = {x["warehouse_ref"]: x for x in out}
    assert by_wh[warehouse_ref]["warehouse_name"] == "Разработка склад(ЗСМ)"
    assert by_wh[warehouse_ref]["warehouse_code"] == "НФ-000079"
