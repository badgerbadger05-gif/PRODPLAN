"""Ремонтный модуль, операция A: restage / move / add."""
from __future__ import annotations

import pytest

from app.models import Item, ProductionStage, SpecComponent, Specification
from app.services import spec_repair
from app.services.spec_repair import SpecRepairError


def _item(db, code, ref=None):
    it = Item(item_code=code, item_name=f"Деталь {code}", item_ref1c=ref or code.lower())
    db.add(it)
    db.flush()
    return it


def _spec(db, code, ref):
    sp = Specification(spec_code=code, spec_name=f"Спека {code}", spec_ref1c=ref)
    db.add(sp)
    db.flush()
    return sp


def _stage(db, name, ref):
    st = ProductionStage(stage_name=name, stage_ref1c=ref)
    db.add(st)
    db.flush()
    return st


def _comp(db, spec, item, qty=1, stage=None, ctype="Сборка", child_ref=None):
    c = SpecComponent(
        spec_id=spec.spec_id,
        item_id=item.item_id,
        quantity=qty,
        stage_id=(stage.stage_id if stage else None),
        component_type=ctype,
        component_spec_ref1c=child_ref,
    )
    db.add(c)
    db.flush()
    return c


# ---------- restage ----------

def test_restage_persists_when_not_dry_run(db_session):
    sp = _spec(db_session, "S1", "s1")
    it = _item(db_session, "A")
    st1 = _stage(db_session, "Этап-1", "st1")
    st2 = _stage(db_session, "Этап-2", "st2")
    comp = _comp(db_session, sp, it, stage=st1)

    res = spec_repair.restage_component(db_session, component_id=comp.component_id, new_stage_id=st2.stage_id, dry_run=False)

    assert res["ok"] and res["old_stage_id"] == st1.stage_id and res["new_stage_id"] == st2.stage_id
    assert db_session.query(SpecComponent).filter_by(component_id=comp.component_id).one().stage_id == st2.stage_id


def test_restage_dry_run_leaves_db_unchanged(db_session):
    sp = _spec(db_session, "S1", "s1")
    it = _item(db_session, "A")
    st1 = _stage(db_session, "Этап-1", "st1")
    st2 = _stage(db_session, "Этап-2", "st2")
    comp = _comp(db_session, sp, it, stage=st1)
    db_session.commit()

    res = spec_repair.restage_component(db_session, component_id=comp.component_id, new_stage_id=st2.stage_id, dry_run=True)

    assert res["dry_run"] is True
    assert db_session.query(SpecComponent).filter_by(component_id=comp.component_id).one().stage_id == st1.stage_id


def test_restage_unknown_stage_raises(db_session):
    sp = _spec(db_session, "S1", "s1")
    it = _item(db_session, "A")
    comp = _comp(db_session, sp, it)
    with pytest.raises(SpecRepairError):
        spec_repair.restage_component(db_session, component_id=comp.component_id, new_stage_id=999999, dry_run=False)


# ---------- move ----------

def test_move_relocates_and_preserves_presence(db_session):
    a = _spec(db_session, "A", "a")
    b = _spec(db_session, "B", "b")
    part = _item(db_session, "P")
    comp = _comp(db_session, a, part)

    res = spec_repair.move_component(db_session, component_id=comp.component_id, target_spec_id=b.spec_id, dry_run=False)

    assert res["from_spec_id"] == a.spec_id and res["to_spec_id"] == b.spec_id
    assert res["safety"]["global_presence_after"] == 1
    assert spec_repair.specs_containing_item(db_session, part.item_id) == [b.spec_id]


def test_move_uses_neighbor_stage_by_default(db_session):
    a = _spec(db_session, "A", "a")
    b = _spec(db_session, "B", "b")
    part = _item(db_session, "P")
    neighbor_item = _item(db_session, "N")
    st = _stage(db_session, "Сборочный", "st-asm")
    _comp(db_session, b, neighbor_item, stage=st)  # сосед в целевой спеке задаёт этап
    comp = _comp(db_session, a, part, stage=None)

    res = spec_repair.move_component(db_session, component_id=comp.component_id, target_spec_id=b.spec_id, dry_run=False)

    assert res["stage_id"] == st.stage_id
    moved = db_session.query(SpecComponent).filter_by(spec_id=b.spec_id, item_id=part.item_id).one()
    assert moved.stage_id == st.stage_id


def test_move_to_same_spec_raises(db_session):
    a = _spec(db_session, "A", "a")
    part = _item(db_session, "P")
    comp = _comp(db_session, a, part)
    with pytest.raises(SpecRepairError):
        spec_repair.move_component(db_session, component_id=comp.component_id, target_spec_id=a.spec_id)


def test_move_to_unknown_spec_raises(db_session):
    a = _spec(db_session, "A", "a")
    part = _item(db_session, "P")
    comp = _comp(db_session, a, part)
    with pytest.raises(SpecRepairError):
        spec_repair.move_component(db_session, component_id=comp.component_id, target_spec_id=424242)


# ---------- add ----------

def test_add_inserts_row(db_session):
    sp = _spec(db_session, "S1", "s1")
    it = _item(db_session, "A")

    res = spec_repair.add_component(db_session, spec_id=sp.spec_id, item_id=it.item_id, quantity=2, dry_run=False)

    assert res["ok"] and res["warnings"] == []
    assert db_session.query(SpecComponent).filter_by(spec_id=sp.spec_id, item_id=it.item_id).count() == 1


def test_add_warns_when_item_used_elsewhere(db_session):
    a = _spec(db_session, "A", "a")
    b = _spec(db_session, "B", "b")
    part = _item(db_session, "P")
    _comp(db_session, a, part)  # уже стоит в A

    res = spec_repair.add_component(db_session, spec_id=b.spec_id, item_id=part.item_id, quantity=1, dry_run=False)

    codes = [w["code"] for w in res["warnings"]]
    assert "ALREADY_USED_ELSEWHERE" in codes
    assert a.spec_id in res["warnings"][0]["specs"]


def test_add_uses_neighbor_stage(db_session):
    sp = _spec(db_session, "S1", "s1")
    sibling = _item(db_session, "SIB")
    new_item = _item(db_session, "NEW")
    st = _stage(db_session, "Малярный", "st-paint")
    _comp(db_session, sp, sibling, stage=st)

    res = spec_repair.add_component(db_session, spec_id=sp.spec_id, item_id=new_item.item_id, quantity=1, dry_run=False)

    assert res["stage_id"] == st.stage_id


def test_add_non_positive_qty_raises(db_session):
    sp = _spec(db_session, "S1", "s1")
    it = _item(db_session, "A")
    with pytest.raises(SpecRepairError):
        spec_repair.add_component(db_session, spec_id=sp.spec_id, item_id=it.item_id, quantity=0)
