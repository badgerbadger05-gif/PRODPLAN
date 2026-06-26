"""Операция Б (read-only превью): чек-лист каскада при смене вида производства."""
from __future__ import annotations

import pytest

from app.models import (
    DefaultSpecification,
    Item,
    ProductionKind,
    SpecComponent,
    Specification,
)
from app.services import spec_repair
from app.services.spec_repair import SpecRepairError


def _kind(db, name, ref):
    k = ProductionKind(ref_1c=ref, name=name)
    db.add(k)
    db.flush()
    return k


def _setup(db):
    """Деталь DET со своей спекой (ref 'det-spec-old', вид «Токарный»),
    закреплена в двух родителях и не закреплена (пусто) в третьем."""
    det = Item(item_code="DET", item_name="Деталь", item_ref1c="det")
    db.add(det)
    db.flush()
    k_old = _kind(db, "Токарный", "k-old")
    k_new = _kind(db, "Фрезерный", "k-new")
    det_spec = Specification(spec_code="DET", spec_name="Спека детали", spec_ref1c="det-spec-old", production_kind_id=k_old.id)
    db.add(det_spec)
    db.flush()
    db.add(DefaultSpecification(item_id=det.item_id, spec_id=det_spec.spec_id))

    p1 = Specification(spec_code="P1", spec_name="P1", spec_ref1c="p1")
    p2 = Specification(spec_code="P2", spec_name="P2", spec_ref1c="p2")
    p3 = Specification(spec_code="P3", spec_name="P3", spec_ref1c="p3")
    db.add_all([p1, p2, p3])
    db.flush()
    # p1, p2 закрепили старую спеку; p3 — пусто (пойдёт по основной сам).
    db.add(SpecComponent(spec_id=p1.spec_id, item_id=det.item_id, quantity=1, component_type="Сборка", component_spec_ref1c="det-spec-old"))
    db.add(SpecComponent(spec_id=p2.spec_id, item_id=det.item_id, quantity=1, component_type="Узел", component_spec_ref1c="det-spec-old"))
    db.add(SpecComponent(spec_id=p3.spec_id, item_id=det.item_id, quantity=1, component_type="Сборка", component_spec_ref1c=None))
    db.commit()
    return det, k_old, k_new, (p1, p2, p3)


def test_preview_lists_only_pinned_parents(db_session):
    det, k_old, k_new, (p1, p2, p3) = _setup(db_session)

    res = spec_repair.preview_kind_change(db_session, item_id=det.item_id, new_production_kind_id=k_new.id)

    assert res["current_kind"]["name"] == "Токарный"
    assert res["new_kind"]["name"] == "Фрезерный"
    assert res["cascade"]["affected_parent_rows"] == 2
    parent_ids = {p["parent_spec_id"] for p in res["cascade"]["parents"]}
    assert parent_ids == {p1.spec_id, p2.spec_id}  # p3 (пустая спека) не входит


def test_preview_unknown_kind_raises(db_session):
    det, k_old, k_new, _ = _setup(db_session)
    with pytest.raises(SpecRepairError):
        spec_repair.preview_kind_change(db_session, item_id=det.item_id, new_production_kind_id=999999)


def test_preview_without_default_spec_raises(db_session):
    item = Item(item_code="NO", item_name="Без спеки", item_ref1c="no")
    db_session.add(item)
    db_session.flush()
    k = _kind(db_session, "Любой", "k")
    db_session.commit()
    with pytest.raises(SpecRepairError):
        spec_repair.preview_kind_change(db_session, item_id=item.item_id, new_production_kind_id=k.id)
