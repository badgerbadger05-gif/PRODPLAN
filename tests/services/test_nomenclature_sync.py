"""Tests for nomenclature_sync (folder filtering + honest commit failures)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import Item, ItemCategory
from app.schemas import ODataSyncRequest
from app.services.nomenclature_sync import sync_nomenclature_from_odata


class _FakeODataClient:
    pages: list = []
    count = 0
    price_types: list = [{"Ref_Key": "accounting-price-ref", "Description": "Учётная цена"}]
    prices: list = []
    price_error: Exception | None = None

    def __init__(self, *_args, **_kwargs):
        pass

    def get_count(self, entity_name, filter_query=None):
        return self.count

    def iter_pages(
        self,
        entity_name,
        filter_query=None,
        select_fields=None,
        top=1000,
        max_pages=1000,
        order_by="Ref_Key",
    ):
        yield from self.pages

    def get_all(self, entity_name, **_kwargs):
        if entity_name == "Catalog_ВидыЦен":
            return self.price_types
        if entity_name.startswith("InformationRegister_ЦеныНоменклатуры/SliceLast"):
            if self.price_error is not None:
                raise self.price_error
            return self.prices
        raise AssertionError(f"Unexpected OData entity: {entity_name}")


def _request(dry_run: bool = False) -> ODataSyncRequest:
    return ODataSyncRequest(
        base_url="http://1c.example/odata",
        entity_name="Catalog_Номенклатура",
        dry_run=dry_run,
    )


def _set_fake_data(*, pages: list, prices: list | None = None, count: int = 0) -> None:
    _FakeODataClient.pages = pages
    _FakeODataClient.prices = prices or []
    _FakeODataClient.price_error = None
    _FakeODataClient.count = count


@pytest.mark.parametrize("folder_flag", [True, "true", "Истина", 1])
def test_nomenclature_sync_skips_catalog_folders(db_session, monkeypatch, folder_flag):
    _set_fake_data(pages=[
        [
            {
                "Ref_Key": "folder-ref",
                "Code": "GRP-1",
                "Description": "Группа материалов",
                "IsFolder": folder_flag,
            },
            {
                "Ref_Key": "item-ref",
                "Code": "IT-1",
                "Description": "Болт М8",
                "IsFolder": False,
            },
        ]
    ])
    monkeypatch.setattr("app.services.odata_client.OData1CClient", _FakeODataClient)

    stats = sync_nomenclature_from_odata(db_session, _request())

    assert stats["items_created"] == 1
    items = db_session.query(Item).all()
    assert [item.item_code for item in items] == ["IT-1"]


def test_nomenclature_sync_propagates_commit_failure(db_session, monkeypatch):
    """A failed commit must not be reported as a successful sync."""
    _set_fake_data(pages=[
        [
            {
                "Ref_Key": "item-ref",
                "Code": "IT-1",
                "Description": "Болт М8",
                "IsFolder": False,
            }
        ]
    ])
    monkeypatch.setattr("app.services.odata_client.OData1CClient", _FakeODataClient)

    def _boom():
        raise RuntimeError(
            'duplicate key value violates unique constraint "items_item_code_key"'
        )

    monkeypatch.setattr(db_session, "commit", _boom)

    with pytest.raises(Exception) as excinfo:
        sync_nomenclature_from_odata(db_session, _request())

    assert "Ошибка синхронизации номенклатуры" in str(excinfo.value)
    assert "duplicate key value" in str(excinfo.value)


def test_partial_odata_row_preserves_local_planning_attributes(
    db_session, monkeypatch
):
    category = ItemCategory(
        category_name="Крепёж",
        category_ref1c="category-existing",
    )
    item = Item(
        item_code="IT-PRESERVE",
        item_name="Before",
        item_ref1c="item-preserve-ref",
        category=category,
        replenishment_method="Покупка",
        replenishment_time=14,
    )
    db_session.add_all([category, item])
    db_session.commit()

    _set_fake_data(count=1, pages=[[{
        "Ref_Key": "item-preserve-ref",
        "Code": "IT-PRESERVE",
        "Description": "After",
        "IsFolder": False,
        "СпособПополнения": "",
        "СрокПополнения": None,
        "КатегорияНоменклатуры_Key": "",
    }]])
    monkeypatch.setattr("app.services.odata_client.OData1CClient", _FakeODataClient)

    sync_nomenclature_from_odata(db_session, _request())

    db_session.refresh(item)
    assert item.item_name == "After"
    assert item.replenishment_method == "Покупка"
    assert item.replenishment_time == 14
    assert item.category_id == category.category_id


def test_nomenclature_sync_imports_nullable_accounting_prices(db_session, monkeypatch):
    existing_without_price = Item(
        item_code="IT-NO-PRICE",
        item_name="Без цены",
        item_ref1c="item-no-price-ref",
        accounting_price=Decimal("77.00"),
    )
    db_session.add(existing_without_price)
    db_session.commit()

    _set_fake_data(
        count=2,
        pages=[[
            {
                "Ref_Key": "item-with-price-ref",
                "Code": "IT-WITH-PRICE",
                "Description": "С ценой",
                "IsFolder": False,
            },
            {
                "Ref_Key": "item-no-price-ref",
                "Code": "IT-NO-PRICE",
                "Description": "Без цены",
                "IsFolder": False,
            },
        ]],
        prices=[{
            "Номенклатура_Key": "item-with-price-ref",
            "Цена": "123.45",
            "Актуальность": True,
        }],
    )
    monkeypatch.setattr("app.services.odata_client.OData1CClient", _FakeODataClient)

    stats = sync_nomenclature_from_odata(db_session, _request())

    with_price = db_session.query(Item).filter_by(item_code="IT-WITH-PRICE").one()
    db_session.refresh(existing_without_price)
    assert with_price.accounting_price == Decimal("123.45")
    assert existing_without_price.accounting_price is None
    assert stats["accounting_prices_found"] == 1
    assert stats["accounting_prices_missing"] == 1


def test_nomenclature_sync_does_not_clear_prices_when_register_read_fails(
    db_session, monkeypatch
):
    item = Item(
        item_code="IT-KEEP-PRICE",
        item_name="Старая цена",
        item_ref1c="item-keep-price-ref",
        accounting_price=Decimal("77.00"),
    )
    db_session.add(item)
    db_session.commit()
    _set_fake_data(pages=[])
    _FakeODataClient.price_error = RuntimeError("price register unavailable")
    monkeypatch.setattr("app.services.odata_client.OData1CClient", _FakeODataClient)

    with pytest.raises(Exception, match="price register unavailable"):
        sync_nomenclature_from_odata(db_session, _request())

    db_session.refresh(item)
    assert item.accounting_price == Decimal("77.00")
