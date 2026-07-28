"""Tests for nomenclature_sync (folder filtering + honest commit failures)."""
from __future__ import annotations

import pytest

from app.models import Item
from app.schemas import ODataSyncRequest
from app.services.nomenclature_sync import sync_nomenclature_from_odata


class _FakeODataClient:
    pages: list = []
    count = 0

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


def _request(dry_run: bool = False) -> ODataSyncRequest:
    return ODataSyncRequest(
        base_url="http://1c.example/odata",
        entity_name="Catalog_Номенклатура",
        dry_run=dry_run,
    )


@pytest.mark.parametrize("folder_flag", [True, "true", "Истина", 1])
def test_nomenclature_sync_skips_catalog_folders(db_session, monkeypatch, folder_flag):
    _FakeODataClient.count = 0
    _FakeODataClient.pages = [
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
    ]
    monkeypatch.setattr("app.services.odata_client.OData1CClient", _FakeODataClient)

    stats = sync_nomenclature_from_odata(db_session, _request())

    assert stats["items_created"] == 1
    items = db_session.query(Item).all()
    assert [item.item_code for item in items] == ["IT-1"]


def test_nomenclature_sync_propagates_commit_failure(db_session, monkeypatch):
    """A failed commit must not be reported as a successful sync."""
    _FakeODataClient.count = 0
    _FakeODataClient.pages = [
        [
            {
                "Ref_Key": "item-ref",
                "Code": "IT-1",
                "Description": "Болт М8",
                "IsFolder": False,
            }
        ]
    ]
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
