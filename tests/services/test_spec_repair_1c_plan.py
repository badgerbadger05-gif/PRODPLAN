"""Резолверы локальной правки в ключи 1С (мост spec_repair -> spec_writeback_1c)."""
from __future__ import annotations

import pytest

from app.models import Item, ProductionStage, SpecComponent, Specification
from app.services import spec_repair
from app.services.spec_repair import SpecRepairError


def _spec(db, code, ref):
    s = Specification(spec_code=code, spec_name=code, spec_ref1c=ref)
    db.add(s)
    db.flush()
    return s


def _item(db, code, ref):
    it = Item(item_code=code, item_name=code, item_ref1c=ref)
    db.add(it)
    db.flush()
    return it


def _comp(db, spec, item, child_ref=None):
    c = SpecComponent(spec_id=spec.spec_id, item_id=item.item_id, quantity=1, component_type="Сборка", component_spec_ref1c=child_ref)
    db.add(c)
    db.flush()
    return c


def test_restage_plan_resolves_1c_keys(db_session):
    sp = _spec(db_session, "S1", "spec-ref-1")
    it = _item(db_session, "A", "nom-ref-a")
    st = ProductionStage(stage_name="Сварочный", stage_ref1c="stage-ref-w")
    db_session.add(st)
    db_session.flush()
    comp = _comp(db_session, sp, it, child_ref="child-x")

    plan = spec_repair.build_restage_plan(db_session, component_id=comp.component_id, new_stage_id=st.stage_id)

    assert plan == {
        "op": "restage",
        "spec_ref": "spec-ref-1",
        "nomenclature_key": "nom-ref-a",
        "child_spec_key": "child-x",
        "new_stage_key": "stage-ref-w",
    }


def test_move_plan_resolves_source_and_target(db_session):
    a = _spec(db_session, "A", "spec-a")
    b = _spec(db_session, "B", "spec-b")
    it = _item(db_session, "P", "nom-p")
    comp = _comp(db_session, a, it, child_ref=None)

    plan = spec_repair.build_move_plan(db_session, component_id=comp.component_id, target_spec_id=b.spec_id)

    assert plan["op"] == "move"
    assert plan["source_spec_ref"] == "spec-a"
    assert plan["target_spec_ref"] == "spec-b"
    assert plan["nomenclature_key"] == "nom-p"
    assert plan["child_spec_key"] is None
    assert plan["new_stage_key"] is None


def test_plan_raises_when_spec_has_no_ref1c(db_session):
    sp = _spec(db_session, "S", None)  # нет spec_ref1c
    it = _item(db_session, "A", "nom-a")
    comp = _comp(db_session, sp, it)
    with pytest.raises(SpecRepairError):
        spec_repair.build_restage_plan(db_session, component_id=comp.component_id, new_stage_id=None)


def test_move_plan_same_spec_raises(db_session):
    a = _spec(db_session, "A", "spec-a")
    it = _item(db_session, "P", "nom-p")
    comp = _comp(db_session, a, it)
    with pytest.raises(SpecRepairError):
        spec_repair.build_move_plan(db_session, component_id=comp.component_id, target_spec_id=a.spec_id)
