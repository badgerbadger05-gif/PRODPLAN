"""Per-order reservations of workshop (участок) stock.

Covers the new scheme that closes the double-count hole: components moved to
a workshop for one order must not "обеспечить" a second order, transfers are
created only for the part missing at the destination, and production is
blocked unless the line actually holds its kit on the workshop.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.models import (
    DefaultSpecification,
    Item,
    ItemWarehouseStock,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SpecComponent,
    Specification,
    StockWarehouse,
)
from app.services.production_control_material_availability import preview_materials
from app.services.production_control_material_issues import create_material_issues
from app.services.production_control_production_flow import (
    produce_line,
    return_leftover_components,
)
from app.services.production_control_reservations import (
    load_reservation_state,
    open_reservations_by_item,
)

WORKSHOP_WH = "wh-weld"
SOURCE_WH = "wh-metal"


def _add_warehouses(db, *, selected=(WORKSHOP_WH, SOURCE_WH)):
    for idx, ref in enumerate(selected, start=1):
        db.add(
            StockWarehouse(
                warehouse_ref1c=ref,
                warehouse_code=f"W{idx}",
                warehouse_name=ref,
                is_selected=True,
            )
        )
    db.commit()


def _set_stock(db, item: Item, breakdown: dict[str, float]):
    db.query(ItemWarehouseStock).filter(ItemWarehouseStock.item_id == item.item_id).delete()
    total = 0.0
    for ref, qty in breakdown.items():
        db.add(ItemWarehouseStock(item_id=item.item_id, warehouse_ref1c=ref, qty=qty))
        total += qty
    item.stock_qty = total
    db.commit()


def _make_order_line(db, parent: Item, *, order_qty: float, suffix: str):
    order = ProductionOrder(
        order_number=f"O-{suffix}",
        order_date=datetime(2026, 6, 1),
        is_posted=True,
        deletion_mark=False,
        order_ref1c=f"ord-{suffix}",
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
    return product


def _setup(db, *, qty_per_unit=1.0, order_qty=8.0, suffix="A"):
    """Parent item + one component + order line. Returns (parent, comp, product)."""
    parent = Item(
        item_code=f"P-{suffix}",
        item_name=f"Parent {suffix}",
        item_article=f"P-{suffix}",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    comp = Item(
        item_code=f"C-{suffix}",
        item_name=f"Comp {suffix}",
        item_article=f"C-{suffix}",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db.add_all([parent, comp])
    db.flush()
    spec = Specification(spec_name=f"Spec {suffix}", spec_ref1c=f"sr-{suffix}")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=qty_per_unit))
    db.commit()
    product = _make_order_line(db, parent, order_qty=order_qty, suffix=suffix)
    return parent, comp, product


def _post_full_transfer(db, product, comp, *, qty, source_wh=SOURCE_WH, dest_wh=WORKSHOP_WH):
    issue = ProductionMaterialIssue(
        document_number=f"MT-{product.product_id}",
        product_id=product.product_id,
        order_id=product.order_id,
        status="posted",
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
            required_qty=qty,
            issued_qty=qty,
            line_status="issued",
        )
    )
    state = (
        db.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one()
    )
    state.issue_status = "posted"
    state.status = "assembled"
    db.commit()
    return issue


# ---------------------------------------------------------------------------
# Reservation lifecycle
# ---------------------------------------------------------------------------


def test_posted_kit_stays_reserved_until_produced(db_session):
    db = db_session
    _add_warehouses(db)
    parent, comp, product = _setup(db, qty_per_unit=2.0, order_qty=5.0)
    _post_full_transfer(db, product, comp, qty=10.0)

    reserved = open_reservations_by_item(db, [comp.item_id])
    assert reserved[comp.item_id] == pytest.approx(10.0)

    produce_line(db, product.product_id, qty=3.0)
    reserved = open_reservations_by_item(db, [comp.item_id])
    assert reserved[comp.item_id] == pytest.approx(4.0)  # 10 - 3*2

    produce_line(db, product.product_id, qty=2.0)
    reserved = open_reservations_by_item(db, [comp.item_id])
    assert reserved.get(comp.item_id, 0.0) == pytest.approx(0.0)


def test_transit_reserves_at_source_posted_reserves_at_workshop(db_session):
    db = db_session
    _add_warehouses(db)
    parent, comp, product = _setup(db)

    issue = ProductionMaterialIssue(
        document_number="MT-TR",
        product_id=product.product_id,
        order_id=product.order_id,
        status="exported",
        direction="issue",
        warehouse_ref1c=WORKSHOP_WH,
        source_warehouse_ref1c=SOURCE_WH,
    )
    db.add(issue)
    db.flush()
    db.add(
        ProductionMaterialIssueLine(
            issue_id=issue.issue_id,
            component_item_id=comp.item_id,
            required_qty=8.0,
            issued_qty=0.0,
            line_status="planned",
        )
    )
    db.commit()

    state = load_reservation_state(db, item_ids=[comp.item_id])
    assert state.reserved_at_warehouse(SOURCE_WH, comp.item_id) == pytest.approx(8.0)
    assert state.reserved_at_warehouse(WORKSHOP_WH, comp.item_id) == pytest.approx(0.0)

    issue.status = "posted"
    for line in issue.lines:
        line.issued_qty = line.required_qty
    db.commit()

    state = load_reservation_state(db, item_ids=[comp.item_id])
    assert state.reserved_at_warehouse(SOURCE_WH, comp.item_id) == pytest.approx(0.0)
    assert state.reserved_at_warehouse(WORKSHOP_WH, comp.item_id) == pytest.approx(8.0)


def test_posted_without_issued_qty_still_reserves(db_session):
    """Legacy rows: sync marked the issue posted but never stamped issued_qty."""
    db = db_session
    _add_warehouses(db)
    parent, comp, product = _setup(db)
    issue = _post_full_transfer(db, product, comp, qty=8.0)
    for line in issue.lines:
        line.issued_qty = 0.0
    db.commit()

    reserved = open_reservations_by_item(db, [comp.item_id])
    assert reserved[comp.item_id] == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# Coverage: the original double-count scenario
# ---------------------------------------------------------------------------


def test_second_order_cannot_be_covered_by_first_orders_kit(db_session):
    """Order A's kit was moved to the welding workshop and the workshop is a
    selected warehouse. Order B for the same item must NOT show 'Обеспечен'
    out of A's components."""
    db = db_session
    _add_warehouses(db)
    parent, comp, product_a = _setup(db, qty_per_unit=1.0, order_qty=8.0, suffix="A")
    product_b = _make_order_line(db, parent, order_qty=8.0, suffix="B")

    # 8 pcs physically on the workshop (moved for order A), nothing else.
    _set_stock(db, comp, {WORKSHOP_WH: 8.0})
    _post_full_transfer(db, product_a, comp, qty=8.0)

    preview_b = preview_materials(db, product_b.product_id)
    comp_row = preview_b["components"][0]
    assert comp_row["available_qty"] == pytest.approx(0.0)
    assert comp_row["missing_qty"] == pytest.approx(8.0)
    assert preview_b["coverage_status"] == "shortage"

    # Order A itself stays covered by its own reservation.
    preview_a = preview_materials(db, product_a.product_id)
    comp_row_a = preview_a["components"][0]
    assert comp_row_a["reserved_for_order_qty"] == pytest.approx(8.0)
    assert comp_row_a["missing_qty"] == pytest.approx(0.0)
    assert preview_a["coverage_status"] == "ready"


# ---------------------------------------------------------------------------
# create_material_issues: in-place claims + delta transfers
# ---------------------------------------------------------------------------


def test_create_issues_claims_destination_stock_and_moves_only_delta(db_session):
    db = db_session
    _add_warehouses(db)
    parent, comp, product = _setup(db, order_qty=8.0)
    # 4 free on the workshop already, plenty on the source warehouse.
    _set_stock(db, comp, {WORKSHOP_WH: 4.0, SOURCE_WH: 100.0})

    result = create_material_issues(
        db, [product.product_id], warehouse_ref1c=WORKSHOP_WH
    )
    assert result["errors"] == []
    created = result["created"]
    in_place = [row for row in created if row.get("direction") == "in_place"]
    transfers = [row for row in created if row.get("direction") != "in_place"]
    assert len(in_place) == 1
    assert len(transfers) == 1

    claim_issue = db.get(ProductionMaterialIssue, in_place[0]["issue_id"])
    assert claim_issue.status == "posted"
    assert claim_issue.document_number.startswith("RS")
    assert claim_issue.lines[0].required_qty == pytest.approx(4.0)
    assert claim_issue.lines[0].issued_qty == pytest.approx(4.0)

    transfer_issue = db.get(ProductionMaterialIssue, transfers[0]["issue_id"])
    assert transfer_issue.status == "draft"
    assert transfer_issue.source_warehouse_ref1c == SOURCE_WH
    assert transfer_issue.lines[0].required_qty == pytest.approx(4.0)

    state = (
        db.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one()
    )
    assert state.issue_status == "requested"
    assert state.status == "to_move"


def test_create_issues_fully_in_place_marks_line_assembled(db_session):
    db = db_session
    _add_warehouses(db)
    parent, comp, product = _setup(db, order_qty=8.0)
    _set_stock(db, comp, {WORKSHOP_WH: 10.0})

    result = create_material_issues(
        db, [product.product_id], warehouse_ref1c=WORKSHOP_WH
    )
    assert result["errors"] == []
    assert len(result["created"]) == 1
    assert result["created"][0]["direction"] == "in_place"

    state = (
        db.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one()
    )
    assert state.issue_status == "posted"
    assert state.status == "assembled"

    # And the second order for the same item cannot claim the same pieces:
    product_b = _make_order_line(db, parent, order_qty=8.0, suffix="B2")
    result_b = create_material_issues(
        db, [product_b.product_id], warehouse_ref1c=WORKSHOP_WH
    )
    created_b = result_b["created"]
    in_place_b = [row for row in created_b if row.get("direction") == "in_place"]
    # only 2 left free on the workshop (10 - 8 claimed)
    assert len(in_place_b) == 1
    issue_b = db.get(ProductionMaterialIssue, in_place_b[0]["issue_id"])
    assert issue_b.lines[0].required_qty == pytest.approx(2.0)


def test_create_issues_is_idempotent(db_session):
    db = db_session
    _add_warehouses(db)
    parent, comp, product = _setup(db, order_qty=8.0)
    _set_stock(db, comp, {WORKSHOP_WH: 4.0, SOURCE_WH: 100.0})

    first = create_material_issues(db, [product.product_id], warehouse_ref1c=WORKSHOP_WH)
    assert len(first["created"]) == 2
    second = create_material_issues(db, [product.product_id], warehouse_ref1c=WORKSHOP_WH)
    assert second["created"] == []
    assert len(second["reused"]) == 2

    issues = (
        db.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.product_id == product.product_id)
        .all()
    )
    assert len(issues) == 2


def test_quantity_increase_creates_delta_transfer(db_session):
    db = db_session
    _add_warehouses(db)
    parent, comp, product = _setup(db, order_qty=8.0)
    _set_stock(db, comp, {SOURCE_WH: 100.0})

    create_material_issues(db, [product.product_id], warehouse_ref1c=WORKSHOP_WH)
    product.quantity = 10.0
    product.remaining_qty = 10.0
    db.commit()

    result = create_material_issues(db, [product.product_id], warehouse_ref1c=WORKSHOP_WH)
    assert result["errors"] == []
    issue = (
        db.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.product_id == product.product_id)
        .one()
    )
    assert issue.lines[0].required_qty == pytest.approx(10.0)


def test_quantity_decrease_releases_open_reservation(db_session):
    db = db_session
    _add_warehouses(db)
    parent, comp, product = _setup(db, order_qty=8.0)
    _set_stock(db, comp, {SOURCE_WH: 100.0})

    create_material_issues(db, [product.product_id], warehouse_ref1c=WORKSHOP_WH)
    product.quantity = 5.0
    product.remaining_qty = 5.0
    db.commit()

    create_material_issues(db, [product.product_id], warehouse_ref1c=WORKSHOP_WH)
    issue = (
        db.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.product_id == product.product_id)
        .one()
    )
    assert issue.lines[0].required_qty == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Produce guard
# ---------------------------------------------------------------------------


def test_produce_blocked_until_kit_is_at_workshop(db_session):
    db = db_session
    _add_warehouses(db)
    parent, comp, product = _setup(db, qty_per_unit=2.0, order_qty=5.0)

    issue = ProductionMaterialIssue(
        document_number="MT-G",
        product_id=product.product_id,
        order_id=product.order_id,
        status="issued",
        direction="issue",
        warehouse_ref1c=WORKSHOP_WH,
        source_warehouse_ref1c=SOURCE_WH,
    )
    db.add(issue)
    db.flush()
    db.add(
        ProductionMaterialIssueLine(
            issue_id=issue.issue_id,
            component_item_id=comp.item_id,
            required_qty=10.0,
            issued_qty=0.0,
            line_status="planned",
        )
    )
    db.commit()

    with pytest.raises(ValueError, match="зарезервированных на участке"):
        produce_line(db, product.product_id, qty=5.0)

    issue.status = "posted"
    for line in issue.lines:
        line.issued_qty = line.required_qty
    db.commit()

    result = produce_line(db, product.product_id, qty=5.0)
    assert result["status"] == "ok"


def test_produce_blocked_when_kit_partially_consumed_by_overproduction(db_session):
    db = db_session
    _add_warehouses(db)
    parent, comp, product = _setup(db, qty_per_unit=2.0, order_qty=5.0)
    _post_full_transfer(db, product, comp, qty=10.0)

    produce_line(db, product.product_id, qty=5.0)
    # remaining_qty is now 0; bump the plan as the overproduce UI flow does.
    product.quantity = 7.0
    product.remaining_qty = 2.0
    db.commit()

    with pytest.raises(ValueError, match="зарезервированных на участке"):
        produce_line(db, product.product_id, qty=2.0)


# ---------------------------------------------------------------------------
# 1C export isolation
# ---------------------------------------------------------------------------


def test_in_place_issue_is_never_exported_to_1c(db_session):
    db = db_session
    _add_warehouses(db)
    parent, comp, product = _setup(db, order_qty=8.0)
    _set_stock(db, comp, {WORKSHOP_WH: 10.0})

    result = create_material_issues(db, [product.product_id], warehouse_ref1c=WORKSHOP_WH)
    issue_id = result["created"][0]["issue_id"]

    from app.services.one_c_stock_transfer_export import _collect_export_entries

    entries, skipped = _collect_export_entries(db, [issue_id])
    assert entries == []
    assert len(skipped) == 1
    assert "in_place" in str(skipped[0]["reason"])


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------


def test_return_releases_in_place_claim_without_1c_document(db_session):
    db = db_session
    _add_warehouses(db)
    parent, comp, product = _setup(db, qty_per_unit=2.0, order_qty=5.0)
    _set_stock(db, comp, {WORKSHOP_WH: 10.0})

    create_material_issues(db, [product.product_id], warehouse_ref1c=WORKSHOP_WH)
    produce_line(db, product.product_id, qty=3.0)

    result = return_leftover_components(db, product.product_id)
    # All leftover (10 - 6 = 4) is an in-place claim: release locally, no doc.
    assert result.get("return_issue_id") is None
    assert sum(row["released_qty"] for row in result["released_in_place"]) == pytest.approx(4.0)

    reserved = open_reservations_by_item(db, [comp.item_id])
    assert reserved.get(comp.item_id, 0.0) == pytest.approx(0.0)


def test_return_mixes_in_place_release_and_physical_return(db_session):
    db = db_session
    _add_warehouses(db)
    parent, comp, product = _setup(db, qty_per_unit=2.0, order_qty=5.0)
    # 4 already on the workshop, the rest comes from the source warehouse.
    _set_stock(db, comp, {WORKSHOP_WH: 4.0, SOURCE_WH: 100.0})

    result = create_material_issues(db, [product.product_id], warehouse_ref1c=WORKSHOP_WH)
    transfer_ids = [
        row["issue_id"] for row in result["created"] if row.get("direction") != "in_place"
    ]
    transfer = db.get(ProductionMaterialIssue, transfer_ids[0])
    transfer.status = "posted"
    for line in transfer.lines:
        line.issued_qty = line.required_qty
        line.line_status = "issued"
    db.commit()

    produce_line(db, product.product_id, qty=3.0)

    result = return_leftover_components(db, product.product_id)
    # leftover = 10 - 6 = 4: first the 4-pc in-place claim is released...
    assert sum(row["released_qty"] for row in result["released_in_place"]) == pytest.approx(4.0)
    # ...which fully covers the leftover, so no physical return document.
    assert result.get("return_issue_id") is None
