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
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionMaterialCustodyEvent,
    ProductionMaterialCustodyProjection,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SpecComponent,
    Specification,
    StockWarehouse,
    StockBin,
    PhysicalImportBatch,
    LedgerGeneration,
)
from app.services.production_control_material_availability import preview_materials
from app.services.production_control_material_issues import create_material_issues
from app.services.production_material_custody_projection import (
    initialize_material_custody_baseline,
)
from app.services.planning_truth import publish_generation

WORKSHOP_WH = "wh-weld"
SOURCE_WH = "wh-metal"


@pytest.fixture(autouse=True)
def accepted_material_truth(db_session):
    cutoff = datetime(2026, 6, 1)
    batch = PhysicalImportBatch(batch_key="section-stock-truth", status="completed", cutoff=cutoff, source_watermarks={})
    generation = LedgerGeneration(
        generation_key="section-stock-truth",
        status="building",
        cutoff=cutoff,
        accepted_at=None,
        physical_import_batch=batch,
        source_watermarks={},
        capabilities={"physical_ledger": True, "future_supply": True},
        algorithm_version="test",
    )
    db_session.add_all((batch, generation))
    db_session.flush()
    initialize_material_custody_baseline(
        db_session,
        ledger_generation_id=int(generation.id),
        cells=[],
        observed_at=cutoff,
    )
    generation.status = "accepted"
    generation.accepted_at = cutoff
    publish_generation(db_session, generation)
    db_session.flush()
    db_session.expire_all()
    db_session.info["section_stock_generation"] = generation.id
    return generation


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
    db.query(StockBin).filter(StockBin.ledger_generation_id == db.info["section_stock_generation"], StockBin.item_id == item.item_id).delete()
    for ref, qty in breakdown.items():
        db.add(StockBin(ledger_generation_id=db.info["section_stock_generation"], item_id=item.item_id, characteristic_ref="", organization_ref="", warehouse_ref1c=ref, on_hand=qty))
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
                status="active",
    )
    comp = Item(
        item_code=f"C-{suffix}",
        item_name=f"Comp {suffix}",
        item_article=f"C-{suffix}",
        unit="шт",
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


def _seed_accepted_custody_projection(db, *, product, component, qty: float) -> None:
    generation_id = int(db.info["section_stock_generation"])
    generation = db.get(LedgerGeneration, generation_id)
    db.add(
        ProductionMaterialCustodyProjection(
            ledger_generation_id=generation_id,
            product_id=int(product.product_id),
            component_item_id=int(component.item_id),
            location_kind="workshop",
            warehouse_ref1c=WORKSHOP_WH,
            reserved_qty=qty,
            source_event_high_watermark_id=0,
            built_at=generation.cutoff,
        )
    )
    db.commit()
    db.expire_all()


# ---------------------------------------------------------------------------
# Reservation lifecycle
# ---------------------------------------------------------------------------


def test_finished_production_line_releases_exported_transfer_reservations(db_session):
    """Completed 1C orders may keep historical transfer docs, but no reserve."""
    db = db_session
    _add_warehouses(db)
    parent, comp, product = _setup(db, order_qty=99.0, suffix="DONE")
    product.order.order_state_key = "ad28565a-991b-11eb-e39a-fa163e61326a"
    product.order.order_state_name = "Завершен"
    product.order.deletion_mark = True
    product.produced_qty = 99.0
    product.remaining_qty = 0.0
    product.control_state.status = "produced"

    for idx, status in enumerate(("exported", "posted", "exported"), start=1):
        issue = ProductionMaterialIssue(
            document_number=f"MT-DONE-{idx}",
            product_id=product.product_id,
            order_id=product.order_id,
            status=status,
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
                required_qty=99.0,
                issued_qty=99.0 if status == "posted" else 0.0,
                line_status="issued" if status == "posted" else "planned",
            )
        )
    db.commit()

    retry = create_material_issues(db, [product.product_id], initiated_by="op")
    assert retry["created"] == []
    assert retry["errors"] == [
        f"product_id={product.product_id}: строка заказа уже закрыта или завершена в 1С; "
        "новые перемещения не создаются"
    ]
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
    _seed_accepted_custody_projection(
        db,
        product=product_a,
        component=comp,
        qty=8.0,
    )

    preview_b = preview_materials(db, product_b.product_id)
    comp_row = preview_b["components"][0]
    assert comp_row["available_qty"] == pytest.approx(0.0)
    assert comp_row["missing_qty"] == pytest.approx(8.0)
    assert preview_b["coverage_status"] == "shortage"
    assert comp_row["reserved_orders"] == [
        {
            "product_id": product_a.product_id,
            "order_id": product_a.order_id,
            "order_number": "O-A",
            "order_ref1c": "ord-A",
            "item_name": "Parent A",
            "reserved_qty": pytest.approx(8.0),
            "reserved_at_workshop_qty": pytest.approx(8.0),
            "reserved_in_transit_qty": pytest.approx(0.0),
        }
    ]

    # Order A itself stays covered by its own reservation.
    preview_a = preview_materials(db, product_a.product_id)
    comp_row_a = preview_a["components"][0]
    assert comp_row_a["reserved_for_order_qty"] == pytest.approx(8.0)
    assert comp_row_a["missing_qty"] == pytest.approx(0.0)
    assert preview_a["coverage_status"] == "ready"


# ---------------------------------------------------------------------------
# create_material_issues: zero-distance claims + delta transfers
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
    workshop_rows = [
        row for row in created if str(row.get("source_warehouse_ref1c") or "") == WORKSHOP_WH
    ]
    transfers = [
        row for row in created if str(row.get("source_warehouse_ref1c") or "") != WORKSHOP_WH
    ]
    assert len(workshop_rows) == 1
    assert len(transfers) == 1

    claim_issue = db.get(ProductionMaterialIssue, workshop_rows[0]["issue_id"])
    assert claim_issue.status == "posted"
    assert claim_issue.direction == "issue"
    assert claim_issue.document_number.startswith("MT")
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
    events = (
        db.query(ProductionMaterialCustodyEvent)
        .filter(ProductionMaterialCustodyEvent.product_id == product.product_id)
        .all()
    )
    assert len(events) == 2
    workshop_events = [e for e in events if e.location_kind == "workshop"]
    transit_events = [e for e in events if e.location_kind == "transit"]
    assert len(workshop_events) == 1
    assert len(transit_events) == 1
    assert workshop_events[0].source_kind == "issue_created"
    assert transit_events[0].source_kind == "issue_created"
    assert workshop_events[0].delta_qty == pytest.approx(4.0)
    assert transit_events[0].delta_qty == pytest.approx(4.0)
    assert claim_issue.lines[0].custody_event_revision == 1
    assert transfer_issue.lines[0].custody_event_revision == 1


def test_create_material_issues_fully_from_workshop_marks_line_assembled(db_session):
    db = db_session
    _add_warehouses(db)
    parent, comp, product = _setup(db, order_qty=8.0)
    _set_stock(db, comp, {WORKSHOP_WH: 10.0})

    result = create_material_issues(
        db, [product.product_id], warehouse_ref1c=WORKSHOP_WH
    )
    assert result["errors"] == []
    assert len(result["created"]) == 1
    assert result["created"][0]["direction"] == "issue"

    state = (
        db.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one()
    )
    assert state.issue_status == "posted"
    assert state.status == "assembled"

    # The second order sees the first order's live workshop claim immediately.
    # Only two units remain free, so the remainder requires source selection;
    # no physical refresh is needed to reach that decision.
    product_b = _make_order_line(db, parent, order_qty=8.0, suffix="B2")
    second = create_material_issues(
        db, [product_b.product_id], warehouse_ref1c=WORKSHOP_WH
    )
    assert len(second["created"]) == 2
    assert len(second["already_on_destination"]) == 1
    covered = second["already_on_destination"][0]["components"][0]
    assert covered["covered_qty"] == pytest.approx(2.0)
    assert covered["remaining_qty"] == pytest.approx(6.0)


def test_create_issues_reuses_retry_after_live_custody_changes(db_session):
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
    assert (
        db.query(ProductionMaterialCustodyEvent)
        .filter(ProductionMaterialCustodyEvent.product_id == product.product_id)
        .count()
        == 2
    )


def test_quantity_increase_extends_live_custody_without_refresh(db_session):
    db = db_session
    _add_warehouses(db)
    parent, comp, product = _setup(db, order_qty=8.0)
    _set_stock(db, comp, {SOURCE_WH: 100.0})

    create_material_issues(db, [product.product_id], warehouse_ref1c=WORKSHOP_WH)
    product.quantity = 10.0
    product.remaining_qty = 10.0
    db.commit()

    second = create_material_issues(db, [product.product_id], warehouse_ref1c=WORKSHOP_WH)
    assert second["created"] == []
    assert len(second["reused"]) == 1
    issue = (
        db.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.product_id == product.product_id)
        .one()
    )
    assert issue.lines[0].required_qty == pytest.approx(10.0)


def test_quantity_decrease_releases_live_custody_without_refresh(db_session):
    db = db_session
    _add_warehouses(db)
    parent, comp, product = _setup(db, order_qty=8.0)
    _set_stock(db, comp, {SOURCE_WH: 100.0})

    create_material_issues(db, [product.product_id], warehouse_ref1c=WORKSHOP_WH)
    product.quantity = 5.0
    product.remaining_qty = 5.0
    db.commit()

    second = create_material_issues(db, [product.product_id], warehouse_ref1c=WORKSHOP_WH)
    assert second["created"] == []
    assert len(second["reused"]) == 1
    issue = (
        db.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.product_id == product.product_id)
        .one()
    )
    assert issue.lines[0].required_qty == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# 1C export isolation
# ---------------------------------------------------------------------------


def test_zero_distance_issue_is_never_exported_to_1c(db_session):
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
    assert "source=destination" in str(skipped[0]["reason"])
