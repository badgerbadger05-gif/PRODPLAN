"""Этап 0 ремонтного модуля: sync хранит закреплённую спеку компонента
(Спецификация_Key) и ключует строки состава по (item, спека), а не только по item.
"""
from __future__ import annotations

import pytest

import app.services.odata_client as odata_client_module
from app.models import (
    Item, ProductionStage, SpecComponent, Specification, SpecificationRebaseQueue, SpecificationRevision,
)
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


def test_same_material_lines_sum_and_rebase_a_previously_overwritten_bom(db_session, monkeypatch):
    _add_item(db_session, "nut")
    old = _comp("old", "nut", "4.000", None, type_="Материал")
    _patch_client(monkeypatch, [_spec_record([old])])
    specification_sync.sync_specifications_from_odata(db_session, _req())
    row_id = db_session.query(SpecComponent).one().component_id

    incoming = [
        {**_comp("spec-root", "nut", qty, None, type_="Материал"), "LineNumber": n}
        for n, qty in enumerate((73, 4, 4), 1)
    ]
    _patch_client(monkeypatch, [_spec_record(incoming)])
    repair = specification_sync.sync_specifications_from_odata(db_session, _req())
    row = db_session.query(SpecComponent).one()
    assert (row.component_id, row.quantity) == (row_id, 81)
    assert repair["components_updated"] == 1
    assert repair["rebase_requests_queued"] == 1

    _patch_client(monkeypatch, [_spec_record(list(reversed(incoming)))])
    replay = specification_sync.sync_specifications_from_odata(db_session, _req())
    assert replay["components_created"] == replay["components_updated"] == 0
    assert replay["revisions_created"] == replay["rebase_requests_queued"] == 0


def test_resync_repairs_multiple_legacy_rows_using_current_source_sum(db_session, monkeypatch):
    _add_item(db_session, "rivet")
    _patch_client(monkeypatch, [_spec_record([_comp("1", "rivet", 42, None, type_="Материал")])])
    specification_sync.sync_specifications_from_odata(db_session, _req())
    existing = db_session.query(SpecComponent).one()
    db_session.add(SpecComponent(
        spec_id=existing.spec_id,
        item_id=existing.item_id,
        component_type=existing.component_type,
        quantity=42,
    ))
    db_session.commit()

    _patch_client(monkeypatch, [_spec_record([
        _comp("1", "rivet", 42, None, type_="Материал"),
        _comp("2", "rivet", 4, None, type_="Материал"),
    ])])
    sync = specification_sync.sync_specifications_from_odata(db_session, _req())
    current = db_session.query(SpecComponent).one()
    assert current.component_id == existing.component_id
    assert current.quantity == 46
    assert sync["components_updated"] == sync["components_deleted"] == 1


def test_duplicate_sum_preserves_explicit_stage_and_component_spec_boundaries(db_session, monkeypatch):
    _add_item(db_session, "nom-a")
    first = ProductionStage(stage_name="Этап A", stage_ref1c="stage-a")
    second = ProductionStage(stage_name="Этап B", stage_ref1c="stage-b")
    db_session.add_all([first, second])
    db_session.commit()
    incoming = [
        {**_comp("1", "nom-a", 5, "spec-x"), "Этап_Key": "stage-a"},
        {**_comp("2", "nom-a", 2, "spec-x"), "Этап_Key": "stage-a"},
        {**_comp("3", "nom-a", 7, "spec-x"), "Этап_Key": "stage-b"},
        {**_comp("4", "nom-a", 9, "spec-y"), "Этап_Key": "stage-a"},
    ]
    _patch_client(monkeypatch, [_spec_record(incoming)])
    specification_sync.sync_specifications_from_odata(db_session, _req())
    assert {(r.stage_id, r.component_spec_ref1c, r.quantity) for r in db_session.query(SpecComponent)} == {
        (first.stage_id, "spec-x", 7), (second.stage_id, "spec-x", 7), (first.stage_id, "spec-y", 9),
    }

    _patch_client(monkeypatch, [_spec_record(incoming[:2] + incoming[3:])])
    updated = specification_sync.sync_specifications_from_odata(db_session, _req())
    assert updated["components_deleted"] == 1
    assert {(r.stage_id, r.component_spec_ref1c, r.quantity) for r in db_session.query(SpecComponent)} == {
        (first.stage_id, "spec-x", 7), (first.stage_id, "spec-y", 9),
    }


def test_duplicate_sum_when_odata_omits_locally_curated_stage(db_session, monkeypatch):
    _add_item(db_session, "nom-a")
    stage = ProductionStage(stage_name="Этап A", stage_ref1c="stage-a")
    db_session.add(stage)
    db_session.commit()
    _patch_client(monkeypatch, [_spec_record([{**_comp("1", "nom-a", 9, "spec-x"), "Этап_Key": "stage-a"}])])
    specification_sync.sync_specifications_from_odata(db_session, _req())

    _patch_client(monkeypatch, [_spec_record([_comp("1", "nom-a", 3, "spec-x"), _comp("2", "nom-a", 4, "spec-x")])])
    result = specification_sync.sync_specifications_from_odata(db_session, _req())
    row = db_session.query(SpecComponent).one()
    assert result["components_failed"] == 0
    assert (row.stage_id, row.component_spec_ref1c, row.quantity) == (stage.stage_id, "spec-x", 7)


def test_ambiguous_omitted_stage_preserves_the_existing_component_snapshot(db_session, monkeypatch):
    _add_item(db_session, "nom-a")
    first = ProductionStage(stage_name="Этап A", stage_ref1c="stage-a")
    second = ProductionStage(stage_name="Этап B", stage_ref1c="stage-b")
    db_session.add_all([first, second])
    db_session.commit()
    _patch_client(monkeypatch, [_spec_record([
        {**_comp("1", "nom-a", 3, "spec-x"), "Этап_Key": "stage-a"},
        {**_comp("2", "nom-a", 4, "spec-x"), "Этап_Key": "stage-b"},
    ])])
    specification_sync.sync_specifications_from_odata(db_session, _req())
    _patch_client(monkeypatch, [_spec_record([_comp("1", "nom-a", 8, "spec-x")])])

    rejected = specification_sync.sync_specifications_from_odata(db_session, _req())
    assert {(row.stage_id, row.quantity) for row in db_session.query(SpecComponent)} == {
        (first.stage_id, 3), (second.stage_id, 4),
    }
    assert rejected["components_failed"] == 1
    assert rejected["rebase_requests_queued"] == 0


@pytest.mark.parametrize("malformed", [
    {"Номенклатура_Key": "unknown-item"},
    {"Ref_Key": ""},
    {"Количество": None},
])
def test_invalid_source_component_keeps_previous_spec_and_revision(db_session, monkeypatch, malformed):
    known = _add_item(db_session, "known")
    retained = _add_item(db_session, "retained")
    initial = _spec_record([
        _comp("1", "known", 3, None),
        _comp("2", "retained", 7, "PIN-X"),
    ])
    _patch_client(monkeypatch, [initial])
    specification_sync.sync_specifications_from_odata(db_session, _req())
    old_hash = db_session.query(Specification).one().content_hash

    valid = _comp("1", "known", 9, None)
    del valid["Ref_Key"]
    valid["LineNumber"] = "1"
    incomplete = {**_comp("2", "retained", 2, "PIN-X"), **malformed}
    newer = {**_spec_record([valid, incomplete]), "Description": "Не применённая версия"}
    _patch_client(monkeypatch, [newer])

    result = specification_sync.sync_specifications_from_odata(db_session, _req())
    assert result["components_failed"] == 1
    assert result["components_updated"] == result["components_deleted"] == 0
    assert result["revisions_created"] == result["rebase_requests_queued"] == 0
    saved = db_session.query(Specification).one()
    assert saved.spec_name == initial["Description"]
    assert saved.content_hash == old_hash
    assert db_session.query(SpecificationRevision).count() == 1
    assert {(row.item_id, row.component_spec_ref1c, row.quantity) for row in db_session.query(SpecComponent)} == {
        (known.item_id, None, 3), (retained.item_id, "pin-x", 7),
    }


def test_body_with_line_number_without_ref_key_is_a_complete_row(db_session, monkeypatch):
    _add_item(db_session, "known")
    row = _comp("1", "known", 3, None)
    del row["Ref_Key"]
    row["LineNumber"] = "1"
    _patch_client(monkeypatch, [_spec_record([row])])
    result = specification_sync.sync_specifications_from_odata(db_session, _req())
    assert result["components_failed"] == 0
    assert db_session.query(SpecComponent).one().quantity == 3


def test_second_sync_change_enqueues_automatic_rebase(db_session, monkeypatch):
    _add_item(db_session, "nom-a")
    _patch_client(
        monkeypatch,
        [_spec_record([_comp("c1", "nom-a", 1, "", type_="Материал")])],
    )
    first = specification_sync.sync_specifications_from_odata(db_session, _req())
    assert first["rebase_requests_queued"] == 0

    _patch_client(
        monkeypatch,
        [_spec_record([_comp("c1", "nom-a", 2, "", type_="Материал")])],
    )
    second = specification_sync.sync_specifications_from_odata(db_session, _req())

    assert second["rebase_requests_queued"] == 1
    queued = db_session.query(SpecificationRebaseQueue).one()
    assert queued.status == "pending"
    assert queued.old_content_hash != queued.new_content_hash


def test_semantically_equivalent_spec_import_is_idempotent(db_session, monkeypatch):
    """Ordering/decimal/display noise does not create a specification rebase."""
    _add_item(db_session, "nom-a")
    _add_item(db_session, "nom-b")
    _patch_client(
        monkeypatch,
        [_spec_record([
            _comp("c1", "nom-a", "1.000", "", type_="Материал"),
            _comp("c2", "nom-b", "2.000", "", type_="Материал"),
        ])],
    )
    first = specification_sync.sync_specifications_from_odata(db_session, _req())

    _patch_client(
        monkeypatch,
        [_spec_record([
            {**_comp("c2", "nom-b", "2", "", type_="Материал"), "Presentation": "noise"},
            {**_comp("c1", "nom-a", "1", "", type_="Материал"), "Presentation": "other"},
        ])],
    )
    second = specification_sync.sync_specifications_from_odata(db_session, _req())

    assert first["revisions_created"] == 1
    assert second["revisions_created"] == 0
    assert second["rebase_requests_queued"] == 0
