"""Tests for the paint↔weld pairs registry (окраска↔сварка), stage 1.

Pairing is by PRODUCTION KIND: an item is "painted" when its default spec's
production_kind name matches покрас/окрас/маляр. The pair is the spec's single
'Сборка' component whose own default spec has a welding production kind;
extra non-'Сборка' components (расходники) do not break the pair. Only
predecessors with replenishment_method 'Производство' are greyed (is_welded_blocked).
Painting specs with 0 or >1 'Сборка' are reported as unpaired (not blocked).
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app import models
from app.models import (
    DefaultSpecification,
    Item,
    PaintWeldPair,
    ProductionKind,
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
    UNPAIRED_NO_ASSEMBLY,
    UNPAIRED_MULTIPLE_ASSEMBLY,
    UNPAIRED_NON_WELD_PREDECESSOR,
)
from app.services.bom_specification_resolver import (
    BomSpecificationResolutionError,
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
                replenishment_method=method,
        replenishment_time=0,
        status="active",
    )
    db.add(item)
    db.flush()
    return item


_KIND_SEQ = [0]


def _kind(db, name: str) -> ProductionKind:
    # ref_1c is unique; the NAME is what drives paint detection, so allow the
    # same name across kinds by generating a distinct ref each call.
    _KIND_SEQ[0] += 1
    kind = ProductionKind(ref_1c=f"kind-{_KIND_SEQ[0]}", name=name)
    db.add(kind)
    db.flush()
    return kind


def _spec(db, name: str, *, kind: ProductionKind | None = None) -> Specification:
    spec = Specification(
        spec_code=name,
        spec_name=name,
        spec_ref1c=f"spec-{name}",
        production_kind_id=kind.id if kind else None,
    )
    db.add(spec)
    db.flush()
    return spec


def _painted_with_predecessor(
    db,
    tag: str,
    *,
    kind_name: str = "Узел (покраска)",
    predecessor_name: str | None = None,
    predecessor_method: str = "Производство",
    extra_material: bool = False,
):
    """Painted item (paint production_kind) + default spec with one 'Сборка'
    predecessor, optionally plus a non-assembly расходник."""
    painted = _item(db, f"P-{tag}", f"Изделие {tag}, окрашенное")
    predecessor = _item(
        db,
        f"W-{tag}",
        predecessor_name or f"Изделие {tag}, после обработки",
        method=predecessor_method,
    )
    weld_kind = _kind(db, "Сварочное производство")
    weld_spec = _spec(db, f"s-weld-{tag}", kind=weld_kind)
    db.add(DefaultSpecification(item_id=predecessor.item_id, spec_id=weld_spec.spec_id))
    kind = _kind(db, kind_name)
    spec = _spec(db, f"s-{tag}", kind=kind)
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=predecessor.item_id, quantity=1, component_type="Сборка"))
    if extra_material:
        rubber = _item(db, f"R-{tag}", f"Резинка {tag}", method="Закупка")
        db.add(SpecComponent(spec_id=spec.spec_id, item_id=rubber.item_id, quantity=4, component_type="Материал"))
    db.add(DefaultSpecification(item_id=painted.item_id, spec_id=spec.spec_id))
    db.flush()
    return painted, predecessor, spec


# ---------------------------------------------------------------------------
# rebuild_auto_pairs — detection by production kind
# ---------------------------------------------------------------------------

def test_rebuild_detects_pair_by_production_kind(db_session):
    painted, predecessor, _s = _painted_with_predecessor(db_session, "A")
    summary = rebuild_auto_pairs(db_session)

    assert summary["created"] == 1
    assert summary["active_pairs"] == 1
    pair = db_session.query(PaintWeldPair).one()
    assert pair.painted_item_id == painted.item_id
    assert pair.welded_item_id == predecessor.item_id
    assert pair.source == "auto"


@pytest.mark.parametrize("kind_name", ["Узел (покраска)", "Порошковая окраска", "Малярный цех"])
def test_rebuild_accepts_all_paint_kind_markers(db_session, kind_name):
    _painted_with_predecessor(db_session, kind_name[:3], kind_name=kind_name)
    summary = rebuild_auto_pairs(db_session)
    assert summary["created"] == 1


def test_rebuild_ignores_non_paint_kind(db_session):
    _painted_with_predecessor(db_session, "MECH", kind_name="Механическая обработка")
    summary = rebuild_auto_pairs(db_session)
    assert summary["created"] == 0
    assert summary["active_pairs"] == 0


def test_rebuild_predecessor_name_not_filtered(db_session):
    # Classification comes from production kind, not the item name.
    painted, predecessor, _s = _painted_with_predecessor(
        db_session, "TURN", predecessor_name="Вал, после токарки"
    )
    summary = rebuild_auto_pairs(db_session)
    assert summary["created"] == 1
    assert db_session.query(PaintWeldPair).one().welded_item_id == predecessor.item_id


def test_rebuild_rejects_bending_predecessor(db_session):
    painted, predecessor, _s = _painted_with_predecessor(db_session, "BEND")
    predecessor_default = (
        db_session.query(DefaultSpecification)
        .filter(DefaultSpecification.item_id == predecessor.item_id)
        .one()
    )
    bending_kind = _kind(db_session, "Гибка")
    bending_spec = _spec(db_session, "s-bend", kind=bending_kind)
    predecessor_default.spec_id = bending_spec.spec_id
    db_session.add(
        PaintWeldPair(
            painted_item_id=painted.item_id,
            welded_item_id=predecessor.item_id,
            source="auto",
            is_active=True,
        )
    )
    db_session.flush()

    summary = rebuild_auto_pairs(db_session)

    assert summary["created"] == 0
    assert summary["deactivated"] == 1
    assert summary["active_pairs"] == 0
    assert summary["unpaired"]["by_reason"] == {
        UNPAIRED_NON_WELD_PREDECESSOR: 1
    }


def test_rebuild_extra_material_component_does_not_break_pair(db_session):
    painted, predecessor, _s = _painted_with_predecessor(db_session, "RUB", extra_material=True)
    summary = rebuild_auto_pairs(db_session)
    assert summary["created"] == 1
    assert db_session.query(PaintWeldPair).one().welded_item_id == predecessor.item_id


def test_rebuild_fails_closed_for_ambiguous_default_spec(db_session):
    # Two distinct defaults cannot be resolved without guessing which BOM is
    # authoritative. Auto-pair rebuilding must fail closed.
    painted = _item(db_session, "P-DEF", "Изделие, окрашенное")
    predecessor = _item(db_session, "W-DEF", "Заготовка")
    mech_kind = _kind(db_session, "Механическая обработка")
    paint_kind = _kind(db_session, "Узел (покраска)")
    default_spec = _spec(db_session, "s-default", kind=mech_kind)
    paint_spec = _spec(db_session, "s-paint", kind=paint_kind)
    db_session.add(SpecComponent(spec_id=paint_spec.spec_id, item_id=predecessor.item_id, quantity=1, component_type="Сборка"))
    # default_spec inserted first -> lower default_specifications.id
    db_session.add(DefaultSpecification(item_id=painted.item_id, spec_id=default_spec.spec_id))
    db_session.flush()
    db_session.add(DefaultSpecification(item_id=painted.item_id, spec_id=paint_spec.spec_id))
    db_session.flush()

    with pytest.raises(
        BomSpecificationResolutionError,
        match="ambiguous default specifications",
    ):
        rebuild_auto_pairs(db_session)
    assert db_session.query(PaintWeldPair).count() == 0


# ---------------------------------------------------------------------------
# unpaired (orphans)
# ---------------------------------------------------------------------------

def test_rebuild_reports_zero_assembly_as_unpaired(db_session):
    painted = _item(db_session, "P-ZERO", "Изделие без сборки, окрашенное")
    kind = _kind(db_session, "Узел (покраска)")
    spec = _spec(db_session, "s-zero", kind=kind)
    rubber = _item(db_session, "R-ZERO", "Резинка", method="Закупка")
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=rubber.item_id, quantity=2, component_type="Материал"))
    db_session.add(DefaultSpecification(item_id=painted.item_id, spec_id=spec.spec_id))
    db_session.flush()

    summary = rebuild_auto_pairs(db_session)
    assert summary["created"] == 0
    orphans = summary["unpaired"]
    assert orphans["count"] == 1
    assert orphans["by_reason"] == {UNPAIRED_NO_ASSEMBLY: 1}
    assert orphans["examples"][0]["item_id"] == painted.item_id
    # backward-compatible alias
    assert summary["orphans"] == orphans


def test_rebuild_reports_multiple_assembly_as_unpaired(db_session):
    painted = _item(db_session, "P-MULTI", "Рама, окрашенная")
    w1 = _item(db_session, "W-M1", "Рама, после сварки")
    w2 = _item(db_session, "W-M2", "Стойка, после сварки")
    kind = _kind(db_session, "Узел (покраска)")
    spec = _spec(db_session, "s-multi", kind=kind)
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=w1.item_id, quantity=1, component_type="Сборка"))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=w2.item_id, quantity=1, component_type="Сборка"))
    db_session.add(DefaultSpecification(item_id=painted.item_id, spec_id=spec.spec_id))
    db_session.flush()

    summary = rebuild_auto_pairs(db_session)
    assert summary["created"] == 0
    assert summary["unpaired"]["by_reason"] == {UNPAIRED_MULTIPLE_ASSEMBLY: 1}


def test_manual_pair_removes_item_from_unpaired(db_session):
    painted = _item(db_session, "P-MAN", "Изделие, окрашенное")
    kind = _kind(db_session, "Узел (покраска)")
    spec = _spec(db_session, "s-man", kind=kind)  # zero Сборка -> would be unpaired
    db_session.add(DefaultSpecification(item_id=painted.item_id, spec_id=spec.spec_id))
    predecessor = _item(db_session, "W-MAN", "Заготовка")
    db_session.flush()

    upsert_manual_pair(db_session, painted_item_id=painted.item_id, welded_item_id=predecessor.item_id)
    summary = rebuild_auto_pairs(db_session)
    # manual pin covers it -> not reported as unpaired
    assert summary["unpaired"]["count"] == 0


# ---------------------------------------------------------------------------
# lifecycle: deactivation / reactivation / manual protection
# ---------------------------------------------------------------------------

def test_rebuild_deactivates_vanished_pair(db_session):
    _painted_with_predecessor(db_session, "VAN")
    rebuild_auto_pairs(db_session)
    db_session.query(DefaultSpecification).delete()
    db_session.commit()

    summary = rebuild_auto_pairs(db_session)
    assert summary["deactivated"] == 1
    assert db_session.query(PaintWeldPair).one().is_active is False


def test_rebuild_reactivates_returning_pair(db_session):
    _painted_with_predecessor(db_session, "RET")
    rebuild_auto_pairs(db_session)
    pair = db_session.query(PaintWeldPair).one()
    pair.is_active = False
    db_session.commit()

    summary = rebuild_auto_pairs(db_session)
    assert summary["reactivated"] == 1
    assert db_session.query(PaintWeldPair).one().is_active is True


def test_rebuild_does_not_touch_manual_pair(db_session):
    painted, predecessor, _s = _painted_with_predecessor(db_session, "PROT")
    other = _item(db_session, "W-ALT", "Другой предшественник")
    upsert_manual_pair(db_session, painted_item_id=painted.item_id, welded_item_id=other.item_id)

    summary = rebuild_auto_pairs(db_session)
    assert summary["created"] == 0
    pair = db_session.query(PaintWeldPair).one()
    assert pair.source == "manual"
    assert pair.welded_item_id == other.item_id


# ---------------------------------------------------------------------------
# is_welded_blocked — only Производство predecessors
# ---------------------------------------------------------------------------

def test_is_welded_blocked_only_production_predecessor(db_session):
    _painted_with_predecessor(db_session, "PROD", predecessor_method="Производство")
    _painted_with_predecessor(db_session, "BUY", predecessor_method="Закупка")
    rebuild_auto_pairs(db_session)

    prod_welded = db_session.query(Item).filter(Item.item_code == "W-PROD").one()
    buy_welded = db_session.query(Item).filter(Item.item_code == "W-BUY").one()

    blocked = is_welded_blocked(db_session, [prod_welded.item_id, buy_welded.item_id])
    assert blocked == {prod_welded.item_id}


def test_is_welded_blocked_ignores_inactive(db_session):
    painted, predecessor, _s = _painted_with_predecessor(db_session, "INA")
    rebuild_auto_pairs(db_session)
    deactivate_pair(db_session, db_session.query(PaintWeldPair).one().id)
    assert is_welded_blocked(db_session, [predecessor.item_id]) == set()


# ---------------------------------------------------------------------------
# guard_paint_order — three verdicts
# ---------------------------------------------------------------------------

def _open_weld_order(db, predecessor: Item, remaining: float):
    order = ProductionOrder(
        order_number=f"WELD-{predecessor.item_code}",
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
            item_id=predecessor.item_id,
            line_number=1,
            quantity=remaining,
            produced_qty=0,
            remaining_qty=remaining,
        )
    )
    db.flush()
    return order


def _publish_stock(db, generation, item_id: int, quantity: float) -> None:
    db.add(
        models.StockBin(
            ledger_generation_id=generation.id,
            item_id=item_id,
            warehouse_ref1c="guard-main",
            on_hand=quantity,
        )
    )
    generation.status = "accepted"
    generation.cutoff = datetime(2026, 7, 26)
    generation.accepted_at = datetime(2026, 7, 26)
    pointer = db.get(models.PlanningTruthState, 1)
    pointer.current_generation_id = generation.id
    db.add(
        models.ProductionMaterialCustodyProjectionManifest(
            ledger_generation_id=generation.id,
            cutoff=generation.cutoff,
            status="complete",
            is_baseline=True,
            source_event_high_watermark_id=0,
        )
    )
    db.flush()


def test_guard_stock_covers(db_session, building_ledger_generation):
    painted, predecessor, _s = _painted_with_predecessor(db_session, "G1")
    _publish_stock(db_session, building_ledger_generation, predecessor.item_id, 10)
    rebuild_auto_pairs(db_session)

    result = guard_paint_order(db_session, painted.item_id, qty=5)
    assert result["verdict"] == "stock_covers"
    assert result["welded_item"]["item_id"] == predecessor.item_id
    assert result["stock_qty"] == pytest.approx(10.0)


def test_guard_order_open(db_session, building_ledger_generation):
    painted, predecessor, _s = _painted_with_predecessor(db_session, "G2")
    rebuild_auto_pairs(db_session)
    _open_weld_order(db_session, predecessor, remaining=8)
    _publish_stock(db_session, building_ledger_generation, predecessor.item_id, 0)

    result = guard_paint_order(db_session, painted.item_id, qty=5)
    assert result["verdict"] == "order_open"
    assert result["open_orders"][0]["remaining"] == pytest.approx(8.0)


def test_guard_need_weld(db_session, building_ledger_generation):
    painted, predecessor, _s = _painted_with_predecessor(db_session, "G3")
    rebuild_auto_pairs(db_session)
    _publish_stock(db_session, building_ledger_generation, predecessor.item_id, 0)

    result = guard_paint_order(db_session, painted.item_id, qty=5)
    assert result["verdict"] == "need_weld"
    assert result["stock_qty"] == pytest.approx(0.0)
    assert result["open_orders"] == []


def test_guard_no_pair(db_session):
    painted = _item(db_session, "NP", "Нечто, окрашенное")
    result = guard_paint_order(db_session, painted.item_id, qty=5)
    assert result["verdict"] == "no_pair"
    assert result["welded_item"] is None
