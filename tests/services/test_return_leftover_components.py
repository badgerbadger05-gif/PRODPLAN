"""Tests for return_leftover_components (plan #6)."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.models import (
    DefaultSpecification,
    Item,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SpecComponent,
    Specification,
)
from app.services.production_control_production_flow import produce_line, return_leftover_components


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_scenario(db, *, parent_code="RET-P", comp_code="RET-C", qty_per_unit=2, order_qty=5):
    """Build a basic parent + 1 component + ProductionOrder + product +
    spec/default — returns (parent, comp, product)."""
    parent = Item(
        item_code=parent_code,
        item_name=f"Parent {parent_code}",
        item_article=parent_code,
        unit="шт",
        stock_qty=0,
        status="active",
    )
    comp = Item(
        item_code=comp_code,
        item_name=f"Comp {comp_code}",
        item_article=comp_code,
        unit="м",
        stock_qty=100,
        status="active",
    )
    db.add_all([parent, comp])
    db.flush()
    spec = Specification(spec_name=f"Spec {parent_code}", spec_ref1c=f"sr-{parent_code}")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=qty_per_unit))

    order = ProductionOrder(
        order_number=f"O-{parent_code}",
        order_date=datetime(2026, 5, 20),
        is_posted=True,
        deletion_mark=False,
        order_ref1c=f"ord-{parent_code}",
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=1,
        quantity=order_qty,
        produced_qty=0,
        remaining_qty=order_qty,
    )
    db.add(product)
    db.flush()
    db.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="ready",
            issue_status="not_requested",
        )
    )
    db.commit()
    return parent, comp, product


def _add_outgoing_issue(
    db,
    product: ProductionProduct,
    comp: Item,
    *,
    required_qty: float,
    status: str = "exported",
    source_wh: str = "src-warehouse",
    dest_wh: str = "workshop-warehouse",
    spec_id: int | None = None,
) -> ProductionMaterialIssue:
    issue = ProductionMaterialIssue(
        document_number=f"MI-{product.product_id}-{status}",
        product_id=product.product_id,
        order_id=product.order_id,
        status=status,
        direction="issue",
        warehouse_ref1c=dest_wh,
        source_warehouse_ref1c=source_wh,
    )
    db.add(issue)
    db.flush()
    db.add(
        ProductionMaterialIssueLine(
            issue_id=issue.issue_id,
            component_item_id=comp.item_id,
            required_qty=required_qty,
            issued_qty=0,
            line_status="planned",
            source_spec_id=spec_id,
        )
    )
    db.commit()
    return issue


def _mark_locally_produced(db, product: ProductionProduct, *, qty: float) -> None:
    product.produced_qty = qty
    product.remaining_qty = max(0.0, float(product.quantity or 0.0) - float(qty or 0.0))
    state = (
        db.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one_or_none()
    )
    if state:
        state.status = "produced" if float(product.remaining_qty or 0.0) <= 1e-9 else "produced_partial"
    db.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_partial_produce_creates_return_with_swapped_warehouses(db_session):
    """Produced 3 of 5; spec says 2 m per unit -> issued 5*2=10, consumed
    3*2=6, leftover = 4. The new return-issue must have swapped warehouses
    and one line with required_qty=4."""
    db = db_session
    parent, comp, product = _setup_scenario(db, order_qty=5)
    _add_outgoing_issue(db, product, comp, required_qty=10, status="posted")
    produce_line(db, product.product_id, qty=3)

    result = return_leftover_components(db, product.product_id, initiated_by="op")
    assert result["status"] == "ok"
    assert result["reused"] is False
    assert result["source_warehouse_ref1c"] == "workshop-warehouse"
    assert result["destination_warehouse_ref1c"] == "src-warehouse"
    [line] = result["lines"]
    assert line["component_item_id"] == comp.item_id
    assert line["issued_qty"] == 10.0
    assert line["consumed_qty"] == 6.0
    assert line["leftover_qty"] == 4.0

    return_issue = (
        db.query(ProductionMaterialIssue)
        .filter_by(issue_id=result["return_issue_id"])
        .one()
    )
    assert return_issue.direction == "return"
    assert return_issue.status == "draft"
    assert return_issue.warehouse_ref1c == "src-warehouse"
    assert return_issue.source_warehouse_ref1c == "workshop-warehouse"

    db_lines = (
        db.query(ProductionMaterialIssueLine)
        .filter_by(issue_id=return_issue.issue_id)
        .all()
    )
    assert len(db_lines) == 1
    assert float(db_lines[0].required_qty) == 4.0


def test_idempotent_second_call_reuses_existing_return_draft(db_session):
    db = db_session
    parent, comp, product = _setup_scenario(db, order_qty=4)
    _add_outgoing_issue(db, product, comp, required_qty=8, status="posted")
    produce_line(db, product.product_id, qty=1)

    r1 = return_leftover_components(db, product.product_id)
    assert r1["status"] == "ok"
    assert r1["reused"] is False
    first_id = r1["return_issue_id"]

    r2 = return_leftover_components(db, product.product_id)
    assert r2["status"] == "ok"
    assert r2["reused"] is True
    assert r2["return_issue_id"] == first_id

    # Exactly one return-issue exists.
    assert (
        db.query(ProductionMaterialIssue)
        .filter_by(product_id=product.product_id, direction="return")
        .count()
        == 1
    )


def test_full_produce_yields_no_leftover_skipped(db_session):
    db = db_session
    parent, comp, product = _setup_scenario(db, order_qty=3)
    _add_outgoing_issue(db, product, comp, required_qty=6, status="posted")
    produce_line(db, product.product_id, qty=3)  # full

    result = return_leftover_components(db, product.product_id)
    assert result["status"] == "skipped"
    assert "положительным остатком" in result["skipped_reason"]


def test_no_produced_yet_skipped(db_session):
    db = db_session
    parent, comp, product = _setup_scenario(db, order_qty=4)
    _add_outgoing_issue(db, product, comp, required_qty=8, status="posted")
    # No produce_line called yet.

    result = return_leftover_components(db, product.product_id)
    assert result["status"] == "skipped"
    assert "нечего возвращать" in result["skipped_reason"]


def test_no_outgoing_issue_skipped(db_session):
    """Even if produce_line was called, return needs an outgoing issue to
    compute issued qty against."""
    db = db_session
    parent, comp, product = _setup_scenario(db, order_qty=4)
    _mark_locally_produced(db, product, qty=2)

    result = return_leftover_components(db, product.product_id)
    assert result["status"] == "skipped"
    assert "выгруженных" in result["skipped_reason"]


def test_draft_outgoing_issue_does_not_count(db_session):
    """An outgoing issue still at status='draft' didn't physically deliver
    materials, so it must not feed the return calculation."""
    db = db_session
    parent, comp, product = _setup_scenario(db, order_qty=5)
    _add_outgoing_issue(db, product, comp, required_qty=10, status="draft")
    _mark_locally_produced(db, product, qty=2)

    result = return_leftover_components(db, product.product_id)
    assert result["status"] == "skipped"
    assert "выгруженных" in result["skipped_reason"]


def test_return_unique_constraint_lets_issue_and_return_coexist(db_session):
    """Outgoing draft + return draft for the same product must both fit
    (the partial-unique index was scoped to direction='issue' only)."""
    db = db_session
    parent, comp, product = _setup_scenario(db, order_qty=4)
    # First, a draft outgoing issue.
    draft_out = _add_outgoing_issue(db, product, comp, required_qty=8, status="draft")
    # Then mark it as physically delivered so it counts toward return calc.
    exported_out = _add_outgoing_issue(
        db,
        product,
        comp,
        required_qty=8,
        status="posted",
    )
    produce_line(db, product.product_id, qty=1)

    r = return_leftover_components(db, product.product_id)
    assert r["status"] == "ok"
    # Both draft issue + return draft coexist.
    by_dir = {
        "issue": db.query(ProductionMaterialIssue)
        .filter_by(product_id=product.product_id, direction="issue", status="draft")
        .count(),
        "return": db.query(ProductionMaterialIssue)
        .filter_by(product_id=product.product_id, direction="return", status="draft")
        .count(),
    }
    assert by_dir == {"issue": 1, "return": 1}


def test_unknown_product_raises(db_session):
    with pytest.raises(ValueError, match="не найдена"):
        return_leftover_components(db_session, 999_999)
