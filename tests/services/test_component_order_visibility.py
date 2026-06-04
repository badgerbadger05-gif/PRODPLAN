"""Components panel ("Комплектующие") must surface OPEN production journal
orders as coverage ("в заказах").

Regression for the case where MRP reconciliation creates a catch-up production
order for a deep component (no PlannedOrder row), yet the parent's card showed
"В заказах нет" because preview_materials only consulted PlannedOrder /
PlannedPurchase / supplier orders.
"""

import datetime as _dt
import json
from datetime import datetime

from app.models import (
    DefaultSpecification,
    Item,
    PlannedOrder,
    PlanningRun,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SpecComponent,
    Specification,
)
from app.services.production_control_material_availability import preview_materials


# ---------------------------------------------------------------------------
# Helpers (local copies — kept independent of test_production_control.py, which
# carries unrelated in-flight work).
# ---------------------------------------------------------------------------

def _make_basic_spec(db, parent_name="Parent", child_specs=()):
    parent = Item(
        item_code=f"P-{parent_name}",
        item_name=parent_name,
        item_article=f"ART-{parent_name}",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db.add(parent)
    db.flush()
    spec = Specification(spec_name=f"Spec {parent_name}", spec_ref1c=f"spec-{parent_name}")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))

    components: list[Item] = []
    for code, name, stock, qty_per_unit in child_specs:
        comp = Item(
            item_code=code,
            item_name=name,
            item_article=code,
            unit="м",
            stock_qty=stock,
            status="active",
        )
        db.add(comp)
        db.flush()
        db.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=qty_per_unit))
        components.append(comp)
    return parent, spec, components


def _make_parent_order(db, parent, qty=2):
    order = ProductionOrder(
        order_number=f"COV-{parent.item_id}",
        order_date=datetime(2026, 5, 20),
        is_posted=False,
        deletion_mark=False,
        source="1c",
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=1,
        quantity=qty,
        produced_qty=0,
        remaining_qty=qty,
    )
    db.add(product)
    db.commit()
    return order, product


def _make_open_production_order(db, item, *, order_number, qty=2, planned_finish=None):
    order = ProductionOrder(
        order_number=order_number,
        order_date=datetime(2026, 6, 4),
        is_posted=False,
        deletion_mark=False,
        source="mrp",
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=qty,
        produced_qty=0,
        remaining_qty=qty,
    )
    db.add(product)
    db.flush()
    if planned_finish is not None:
        db.add(
            ProductionOrderLineState(
                product_id=product.product_id,
                status="shortage",
                issue_status="not_requested",
                planned_finish_date=planned_finish,
            )
        )
    db.commit()
    return order, product


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_preview_lists_open_production_order_for_component(db_session):
    """A component covered by an open production journal order (no PlannedOrder
    row, like a reconcile catch-up order) shows "в заказах". The stock-based
    badge stays 'shortage' — the part is not physically on hand yet."""
    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="WipParent",
        child_specs=[("WOC1", "Труба после гибки", 0, 1)],  # need 1*2 = 2, have 0
    )
    comp = comps[0]
    _order, product = _make_parent_order(db_session, parent, qty=2)
    _make_open_production_order(
        db_session, comp, order_number="MRP-RC-13-9386", qty=2,
        planned_finish=_dt.date(2026, 6, 20),
    )

    preview = preview_materials(db_session, product.product_id)

    only_comp = preview["components"][0]
    # Badge unchanged: still a stock shortage (part not on hand yet).
    assert only_comp["coverage"] == "shortage"
    assert only_comp["missing_qty"] == 2
    # ...but the open production order is now visible as an expected arrival.
    prod_etas = [e for e in only_comp["eta_dates"] if e["ref"] == "MRP-RC-13-9386"]
    assert prod_etas, only_comp["eta_dates"]
    assert prod_etas[0]["source"] == "planned_production"
    assert prod_etas[0]["date"] == "2026-06-20"
    assert abs(prod_etas[0]["qty"] - 2.0) < 1e-6


def test_preview_handles_production_order_without_planned_finish(db_session):
    """An unscheduled production order (planned_finish_date NULL) still counts
    as "в заказах"; date is surfaced as None for the UI to render gracefully."""
    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="UndatedParent",
        child_specs=[("UDC1", "Comp undated", 0, 1)],
    )
    comp = comps[0]
    _order, product = _make_parent_order(db_session, parent, qty=2)
    _make_open_production_order(
        db_session, comp, order_number="MRP-RC-13-2222", qty=2, planned_finish=None,
    )

    preview = preview_materials(db_session, product.product_id)
    etas = preview["components"][0]["eta_dates"]
    refs = {e["ref"]: e for e in etas}
    assert "MRP-RC-13-2222" in refs
    assert refs["MRP-RC-13-2222"]["date"] is None


def test_preview_prefers_production_order_over_planned_order(db_session):
    """With BOTH a PlannedOrder (planning run) and the real journal order it was
    materialised into, the panel lists the journal order once — the PlannedOrder
    duplicate is suppressed to avoid double-counting."""
    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="DedupParent",
        child_specs=[("DDC1", "Comp dup", 0, 1)],
    )
    comp = comps[0]
    _order, product = _make_parent_order(db_session, parent, qty=2)

    run = PlanningRun(status="COMPLETED", config_snapshot=json.dumps({}))
    db_session.add(run)
    db_session.flush()
    db_session.add(
        PlannedOrder(
            run_id=run.run_id,
            item_id=comp.item_id,
            requested_qty=2,
            planned_qty=2,
            qty=2,
            need_date=_dt.date(2026, 6, 18),
            bucket_date=_dt.date(2026, 6, 18),
        )
    )
    db_session.flush()
    _make_open_production_order(
        db_session, comp, order_number="MRP-RC-13-1111", qty=2,
        planned_finish=_dt.date(2026, 6, 20),
    )

    preview = preview_materials(db_session, product.product_id)
    etas = preview["components"][0]["eta_dates"]
    prod_sources = [e for e in etas if e["source"] == "planned_production"]
    # Exactly one production entry — the journal order, not the PlannedOrder too.
    assert len(prod_sources) == 1
    assert prod_sources[0]["ref"] == "MRP-RC-13-1111"
