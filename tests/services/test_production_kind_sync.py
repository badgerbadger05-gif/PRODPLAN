"""Tests for production_kind_sync (name must survive the $select fallback)."""
from __future__ import annotations

from app.models import ProductionKind
from app.schemas import ODataSyncRequest
from app.services.production_kind_sync import sync_production_kinds_from_odata


REF = "aaaaaaaa-1111-2222-3333-444444444444"


class _FallbackODataClient:
    """1C rejects the Description $select and answers with Ref_Key only."""

    def __init__(self, *_args, **_kwargs):
        pass

    def get_all(self, entity_name, filter_query=None, select_fields=None, **_kwargs):
        if select_fields and "Description" in select_fields:
            raise RuntimeError("Bad request: path segment is not found")
        return [{"Ref_Key": REF}]


class _FullODataClient:
    def __init__(self, *_args, **_kwargs):
        pass

    def get_all(self, entity_name, filter_query=None, select_fields=None, **_kwargs):
        return [{"Ref_Key": REF, "Description": "Сварочное производство"}]


def _request() -> ODataSyncRequest:
    return ODataSyncRequest(
        base_url="http://1c.example/odata",
        entity_name="Catalog_ВидыПроизводства",
    )


def test_fallback_select_keeps_existing_local_name(db_session, monkeypatch):
    """Regression: the Ref_Key-only retry used to rename the kind to its GUID."""
    db_session.add(ProductionKind(ref_1c=REF, name="Сварочное производство"))
    db_session.commit()

    monkeypatch.setattr(
        "app.services.odata_client.OData1CClient", _FallbackODataClient
    )

    stats = sync_production_kinds_from_odata(db_session, _request())

    assert stats["kinds_updated"] == 0
    assert stats["kinds_unchanged"] == 1
    kind = db_session.query(ProductionKind).one()
    assert kind.name == "Сварочное производство"


def test_fallback_select_uses_ref_as_name_for_new_rows(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.services.odata_client.OData1CClient", _FallbackODataClient
    )

    stats = sync_production_kinds_from_odata(db_session, _request())

    assert stats["kinds_created"] == 1
    kind = db_session.query(ProductionKind).one()
    assert kind.ref_1c == REF
    assert kind.name == REF


def test_description_still_updates_the_local_name(db_session, monkeypatch):
    db_session.add(ProductionKind(ref_1c=REF, name="Старое имя"))
    db_session.commit()

    monkeypatch.setattr("app.services.odata_client.OData1CClient", _FullODataClient)

    stats = sync_production_kinds_from_odata(db_session, _request())

    assert stats["kinds_updated"] == 1
    kind = db_session.query(ProductionKind).one()
    assert kind.name == "Сварочное производство"
