"""Этап 0 ремонтного модуля: sync хранит закреплённую спеку компонента
(Спецификация_Key) и ключует строки состава по (item, спека), а не только по item.
"""
from __future__ import annotations

import app.services.odata_client as odata_client_module
from app.models import Item, SpecComponent
from app.schemas import ODataSyncRequest
from app.services import specification_sync
from app.services.specification_sync import _norm_component_spec_ref


def _patch_client(monkeypatch, records):
    class _Fake:
        def __init__(self, *_a, **_k):
            pass

        def get_all(self, _entity_name, filter_query=None, select_fields=None):
            return records

    monkeypatch.setattr(odata_client_module, "OData1CClient", _Fake)


def _req():
    return ODataSyncRequest(base_url="http://demo/odata", entity_name="Catalog_Спецификации")


def _spec_record(components):
    return {
        "Ref_Key": "spec-root",
        "Code": "S-1",
        "Description": "Корневая спека",
        "ВидПроизводства_Key": "",
        "Состав": components,
        "Операции": [],
    }


def _comp(ref, nom, qty, spec_key, type_="Сборка"):
    return {
        "Ref_Key": ref,
        "Номенклатура_Key": nom,
        "Количество": qty,
        "Этап_Key": "",
        "ТипСтрокиСостава": type_,
        "Спецификация_Key": spec_key,
    }


def _add_item(db, ref1c):
    item = Item(item_code=ref1c.upper(), item_name=f"Деталь {ref1c}", item_ref1c=ref1c)
    db.add(item)
    db.commit()
    return item


def test_norm_component_spec_ref():
    assert _norm_component_spec_ref(None) is None
    assert _norm_component_spec_ref("") is None
    assert _norm_component_spec_ref("   ") is None
    assert _norm_component_spec_ref("00000000-0000-0000-0000-000000000000") is None
    assert _norm_component_spec_ref("ABCD-1234") == "abcd-1234"


def test_sync_stores_component_spec_ref(db_session, monkeypatch):
    _add_item(db_session, "nom-a")
    _patch_client(monkeypatch, [_spec_record([_comp("c1", "nom-a", 1, "SPEC-X")])])

    specification_sync.sync_specifications_from_odata(db_session, _req())

    rows = db_session.query(SpecComponent).all()
    assert len(rows) == 1
    assert rows[0].component_spec_ref1c == "spec-x"


def test_same_item_with_different_child_specs_kept_as_two_rows(db_session, monkeypatch):
    _add_item(db_session, "nom-a")
    _patch_client(
        monkeypatch,
        [_spec_record([
            _comp("c1", "nom-a", 1, "SPEC-X"),
            _comp("c2", "nom-a", 2, "SPEC-Y"),
        ])],
    )

    specification_sync.sync_specifications_from_odata(db_session, _req())

    rows = db_session.query(SpecComponent).all()
    assert len(rows) == 2
    assert {r.component_spec_ref1c for r in rows} == {"spec-x", "spec-y"}


def test_empty_child_spec_normalized_to_none(db_session, monkeypatch):
    _add_item(db_session, "nom-a")
    _patch_client(monkeypatch, [_spec_record([_comp("c1", "nom-a", 1, "", type_="Материал")])])

    specification_sync.sync_specifications_from_odata(db_session, _req())

    rows = db_session.query(SpecComponent).all()
    assert len(rows) == 1
    assert rows[0].component_spec_ref1c is None


def test_reconcile_keys_by_item_and_child_spec(db_session, monkeypatch):
    """Исчезла строка (A, SPEC-Y) — её удаляем, сестру (A, SPEC-X) сохраняем."""
    _add_item(db_session, "nom-a")
    _patch_client(
        monkeypatch,
        [_spec_record([
            _comp("c1", "nom-a", 1, "SPEC-X"),
            _comp("c2", "nom-a", 2, "SPEC-Y"),
        ])],
    )
    specification_sync.sync_specifications_from_odata(db_session, _req())
    assert db_session.query(SpecComponent).count() == 2

    # Повторная выгрузка — осталась только SPEC-X.
    _patch_client(monkeypatch, [_spec_record([_comp("c1", "nom-a", 1, "SPEC-X")])])
    specification_sync.sync_specifications_from_odata(db_session, _req())

    rows = db_session.query(SpecComponent).all()
    assert len(rows) == 1
    assert rows[0].component_spec_ref1c == "spec-x"
