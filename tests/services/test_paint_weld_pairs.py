"""Tests for the paint↔weld pairs registry (окраска↔сварка), stage 1.

Covers rebuild_auto_pairs (detection, upsert, deactivation, manual protection,
orphans) and guard_paint_order (all three verdicts) plus is_welded_blocked.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.models import (
    DefaultSpecification,
    Item,
    PaintWeldPair,
    ProductionOrder,
    ProductionProduct,
    SpecComponent,
    Specification,
)
from app.services.paint_weld_pairs import (
    guard_paint_order,
    is_welded_blocked,
    rebuild_auto_pairs,
    upsert_manual_pair,
    deactivate_pair,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item(db, code: str, name: str, *, method: str = "Производство", stock: float = 0.0) -> Item:
    item = Item(
        item_code=code,
        item_name=name,
        item_article=code,
        unit="шт",
        stock_qty=stock,
        replenishment_method=method,
        replenishment_time=0,
        status="active",
    )
    db.add(item)
    db.flush()
    return item


def _spec(db, name: str) -> Specification:
    spec = Specification(spec_code=name, spec_name=name, spec_ref1c=f"spec-{name}")
    db.add(spec)
    db.flush()
    return spec


def _painted_with_welded_component(
    db, painted_name: str, welded_name: str, *, comp_type: str = "Сборка", welded_method: str = "Производство"
):
    """Painted item + default spec whose single Сборка component is the welded part."""
    painted = _item(db, f"P-{painted_name}", painted_name)
    welded = _item(db, f"W-{welded_name}", welded_name, method=welded_method)
    spec = _spec(db, f"spec-{painted_name}")
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=welded.item_id, quantity=1, component_type=comp_type))
    db.add(DefaultSpecification(item_id=painted.item_id, spec_id=spec.spec_id))
    db.flush()
    return painted, welded, spec


# ---------------------------------------------------------------------------
# rebuild_auto_pairs
# ---------------------------------------------------------------------------

def test_rebuild_detects_auto_pair(db_session):
    painted, welded, _spec_ = _painted_with_welded_component(
        db_session, "Вал ведущий, после покраски", "Вал ведущий, после сварки"
    )
    summary = rebuild_auto_pairs(db_session)

    assert summary["created"] == 1
    assert summary["active_pairs"] == 1
    pair = db_session.query(PaintWeldPair).one()
    assert pair.painted_item_id == painted.item_id
    assert pair.welded_item_id == welded.item_id
    assert pair.source == "auto"
    assert pair.is_active is True


def test_rebuild_accepts_bez_pokraski_marker(db_session):
    _painted_with_welded_component(
        db_session, "Кронштейн, после покраски", "Кронштейн, без покраски"
    )
    summary = rebuild_auto_pairs(db_session)
    assert summary["created"] == 1


def test_rebuild_skips_when_component_not_welded(db_session):
    # single Сборка component but it's a turned ("токарка") part, not welded
    _painted_with_welded_component(
        db_session, "Вал, после покраски", "Вал, после токарки"
    )
    summary = rebuild_auto_pairs(db_session)
    assert summary["created"] == 0
    assert summary["active_pairs"] == 0


def test_rebuild_skips_when_two_assembly_components(db_session):
    painted = _item(db_session, "P-X", "Рама, после покраски")
    w1 = _item(db_session, "W-1", "Рама, после сварки")
    w2 = _item(db_session, "W-2", "Стойка, после сварки")
    spec = _spec(db_session, "spec-X")
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=w1.item_id, quantity=1, component_type="Сборка"))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=w2.item_id, quantity=1, component_type="Сборка"))
    db_session.add(DefaultSpecification(item_id=painted.item_id, spec_id=spec.spec_id))
    db_session.flush()

    summary = rebuild_auto_pairs(db_session)
    assert summary["created"] == 0


def test_rebuild_ignores_material_components_counting_only_assembly(db_session):
    # one Сборка (welded) + several Материал rows -> still exactly one assembly
    painted = _item(db_session, "P-Y", "Ось, после покраски")
    welded = _item(db_session, "W-Y", "Ось, после сварки")
    raw = _item(db_session, "M-Y", "Труба", method="Закупка")
    spec = _spec(db_session, "spec-Y")
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=welded.item_id, quantity=1, component_type="Сборка"))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=raw.item_id, quantity=2, component_type="Материал"))
    db_session.add(DefaultSpecification(item_id=painted.item_id, spec_id=spec.spec_id))
    db_session.flush()

    summary = rebuild_auto_pairs(db_session)
    assert summary["created"] == 1
    assert db_session.query(PaintWeldPair).one().welded_item_id == welded.item_id


def test_rebuild_deactivates_vanished_auto_pair(db_session):
    painted, welded, spec = _painted_with_welded_component(
        db_session, "Деталь, после покраски", "Деталь, после сварки"
    )
    rebuild_auto_pairs(db_session)
    assert db_session.query(PaintWeldPair).filter_by(is_active=True).count() == 1

    # remove the default spec -> pair no longer detected
    db_session.query(DefaultSpecification).delete()
    db_session.commit()

    summary = rebuild_auto_pairs(db_session)
    assert summary["deactivated"] == 1
    pair = db_session.query(PaintWeldPair).one()
    assert pair.is_active is False


def test_rebuild_reactivates_returning_pair(db_session):
    painted, welded, spec = _painted_with_welded_component(
        db_session, "Кольцо, после покраски", "Кольцо, после сварки"
    )
    rebuild_auto_pairs(db_session)
    # deactivate manually then rebuild -> reactivates
    pair = db_session.query(PaintWeldPair).one()
    pair.is_active = False
    db_session.commit()

    summary = rebuild_auto_pairs(db_session)
    assert summary["reactivated"] == 1
    assert db_session.query(PaintWeldPair).one().is_active is True


def test_rebuild_does_not_touch_manual_pair(db_session):
    painted, welded, spec = _painted_with_welded_component(
        db_session, "Втулка, после покраски", "Втулка, после сварки"
    )
    other_welded = _item(db_session, "W-ALT", "Втулка альт, после сварки")
    # manual pair pins painted -> other_welded
    upsert_manual_pair(
        db_session, painted_item_id=painted.item_id, welded_item_id=other_welded.item_id
    )

    summary = rebuild_auto_pairs(db_session)
    # auto did not override the manual pin
    assert summary["created"] == 0
    pair = db_session.query(PaintWeldPair).one()
    assert pair.source == "manual"
    assert pair.welded_item_id == other_welded.item_id


# ---------------------------------------------------------------------------
# orphans
# ---------------------------------------------------------------------------

def test_rebuild_reports_orphans(db_session):
    # paired welded (has painted parent)
    _painted_with_welded_component(
        db_session, "A, после покраски", "A, после сварки"
    )
    # orphan welded: производство "после сварки" without a painted parent
    orphan = _item(db_session, "ORPH", "Балка, после сварки", method="Производство")
    # not-an-orphan: after сварки but purchased flow -> excluded from orphan scan
    _item(db_session, "BUY", "Хомут, после сварки", method="Закупка")

    summary = rebuild_auto_pairs(db_session)
    orphans = summary["orphans"]
    assert orphans["count"] == 1
    assert orphans["examples"][0]["item_id"] == orphan.item_id


# ---------------------------------------------------------------------------
# is_welded_blocked
# ---------------------------------------------------------------------------

def test_is_welded_blocked_returns_active_welded_only(db_session):
    painted, welded, spec = _painted_with_welded_component(
        db_session, "Z, после покраски", "Z, после сварки"
    )
    rebuild_auto_pairs(db_session)
    orphan = _item(db_session, "ORPH2", "Косынка, после сварки")

    blocked = is_welded_blocked(db_session, [welded.item_id, orphan.item_id, painted.item_id])
    assert blocked == {welded.item_id}

    # deactivating the pair unblocks the welded item
    deactivate_pair(db_session, db_session.query(PaintWeldPair).one().id)
    assert is_welded_blocked(db_session, [welded.item_id]) == set()


# ---------------------------------------------------------------------------
# guard_paint_order — three verdicts
# ---------------------------------------------------------------------------

def _open_weld_order(db, welded: Item, remaining: float):
    order = ProductionOrder(
        order_number=f"WELD-{welded.item_code}",
        order_date=date(2026, 6, 1),
        is_posted=False,
        deletion_mark=False,
        source="1c",
    )
    db.add(order)
    db.flush()
    db.add(
        ProductionProduct(
            order_id=order.order_id,
            item_id=welded.item_id,
            line_number=1,
            quantity=remaining,
            produced_qty=0,
            remaining_qty=remaining,
        )
    )
    db.flush()
    return order


def test_guard_stock_covers(db_session):
    painted, welded, spec = _painted_with_welded_component(
        db_session, "G1, после покраски", "G1, после сварки"
    )
    welded.stock_qty = 10
    db_session.flush()
    rebuild_auto_pairs(db_session)

    result = guard_paint_order(db_session, painted.item_id, qty=5)
    assert result["verdict"] == "stock_covers"
    assert result["welded_item"]["item_id"] == welded.item_id
    assert result["stock_qty"] == pytest.approx(10.0)


def test_guard_order_open(db_session):
    painted, welded, spec = _painted_with_welded_component(
        db_session, "G2, после покраски", "G2, после сварки"
    )
    rebuild_auto_pairs(db_session)
    _open_weld_order(db_session, welded, remaining=8)

    result = guard_paint_order(db_session, painted.item_id, qty=5)
    assert result["verdict"] == "order_open"
    assert result["open_orders"][0]["remaining"] == pytest.approx(8.0)


def test_guard_need_weld(db_session):
    painted, welded, spec = _painted_with_welded_component(
        db_session, "G3, после покраски", "G3, после сварки"
    )
    rebuild_auto_pairs(db_session)

    result = guard_paint_order(db_session, painted.item_id, qty=5)
    assert result["verdict"] == "need_weld"
    assert result["stock_qty"] == pytest.approx(0.0)
    assert result["open_orders"] == []


def test_guard_no_pair(db_session):
    painted = _item(db_session, "NP", "Нечто, после покраски")
    result = guard_paint_order(db_session, painted.item_id, qty=5)
    assert result["verdict"] == "no_pair"
    assert result["welded_item"] is None
