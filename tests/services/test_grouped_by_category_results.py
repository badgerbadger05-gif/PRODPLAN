from app.models import Item, ItemCategory
from app.schemas import ODataSyncRequest
from app.services.nomenclature_sync import sync_nomenclature_from_odata


def test_sync_nomenclature_persists_item_category_link(db_session, monkeypatch):
    db = db_session

    class StubClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_count(self, entity_name, filter_query):
            return 1

        def get_all(self, entity_name, **kwargs):
            if entity_name == "Catalog_ВидыЦен":
                return [{"Ref_Key": "accounting-price-ref", "Description": "Учётная цена"}]
            if entity_name.startswith("InformationRegister_ЦеныНоменклатуры/SliceLast"):
                return []
            raise AssertionError(f"Unexpected OData entity: {entity_name}")

        def iter_pages(self, entity_name, filter_query=None, select_fields=None, top=1000, max_pages=1000, order_by=None):
            yield [
                {
                    "Ref_Key": "item-ref-1",
                    "Code": "ITEM-001",
                    "Description": "Test Item",
                    "Артикул": "ART-001",
                    "ЕдиницаИзмерения_Key": "u1",
                    "КатегорияНоменклатуры_Key": "cat-ref-1",
                    "КатегорияНоменклатуры": {"Description": "Категория А"},
                    "СпособПополнения": "Покупка",
                    "СрокПополнения": 5,
                }
            ]

    monkeypatch.setattr("app.services.odata_client.OData1CClient", StubClient)

    result = sync_nomenclature_from_odata(
        db,
        ODataSyncRequest(base_url="http://example.test", entity_name="Catalog_Номенклатура"),
    )

    item = db.query(Item).filter(Item.item_code == "ITEM-001").one()
    category = db.query(ItemCategory).filter(ItemCategory.category_ref1c == "cat-ref-1").one()

    assert result["items_created"] == 1
    assert result["categories_created"] == 1
    assert item.category_id == category.category_id
    assert item.category.category_name == "Категория А"
