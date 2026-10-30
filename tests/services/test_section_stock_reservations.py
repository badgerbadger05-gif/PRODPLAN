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
    ProductionResource,
    ProductionKind,
    ResourceProductionKind,
    SpecComponent,
    Specification,
    StockWarehouse,
    StockBin,
    WorkshopWarehouseBinding,
    PhysicalImportBatch,
    LedgerGeneration,
)
from app.services import production_control_material_availability as material_availability
from app.services.production_control_material_availability import preview_materials
from app.services.production_control_material_availability import preview_make_work_item_materials
from app.services import production_control_material_issues as material_issues
from app.services.production_control_material_issues import create_material_issues
from app.services.production_control_journal import list_journal
from app.services.production_material_custody_projection import (
    initialize_material_custody_baseline,
)
from app.services.planning_truth import publish_generation
from app.services.one_c_export_common import DEFAULT_ORGANIZATION_REF1C

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
        db.add(StockBin(ledger_generation_id=db.info["section_stock_generation"], item_id=item.item_id, characteristic_ref="", organization_ref=DEFAULT_ORGANIZATION_REF1C, warehouse_ref1c=ref, on_hand=qty))
    db.commit()


def _set_feeder(db, item: Item, warehouse_ref1c: str):
    bindings = db.query(WorkshopWarehouseBinding).all()
    assert bindings
    for binding in bindings:
        binding.feeder_warehouse_ref1c = warehouse_ref1c
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
    state = ProductionOrderLineState(
        product_id=product.product_id,
        status="ready",
        issue_status="not_requested",
    )
    existing = (
        db.query(ProductionOrderLineState)
        .join(ProductionProduct, ProductionProduct.product_id == ProductionOrderLineState.product_id)
        .filter(ProductionProduct.item_id == parent.item_id)
        .filter(ProductionOrderLineState.workshop_id.isnot(None))
        .first()
    )
    if existing is not None:
        state.workshop_id = existing.workshop_id
        state.workshop_id_source = "manual"
    db.add(state)
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
    workshop = ProductionResource(resource_name=f"Workshop {suffix}")
    db.add(workshop)
    db.flush()
    db.add(WorkshopWarehouseBinding(
        workshop_id=workshop.resource_id,
        warehouse_ref1c=WORKSHOP_WH,
        feeder_warehouse_ref1c=(
            SOURCE_WH
            if db.query(StockWarehouse).filter_by(warehouse_ref1c=SOURCE_WH).first() is not None
            else None
        ),
    ))
    product.control_state.workshop_id = workshop.resource_id
    product.control_state.workshop_id_source = "manual"
    db.commit()
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


def test_global_free_allows_launch_but_zero_feeder_creates_no_fake_custody(db_session):
    db = db_session
    other = "wh-other"
    _add_warehouses(db, selected=(WORKSHOP_WH, SOURCE_WH, other))
    _parent, component, product = _setup(db, order_qty=10, suffix="GLOBAL-FEEDER-ZERO")
    _set_feeder(db, component, SOURCE_WH)
    _set_stock(db, component, {SOURCE_WH: 0, other: 100})

    materials = preview_materials(db, product.product_id)
    assert materials["coverage_status"] == "ready"
    assert materials["components"][0]["available_qty"] == pytest.approx(100)
    assert materials["components"][0]["feeder_warehouse_ref1c"] == SOURCE_WH
    assert materials["components"][0]["feeder_free_qty"] == pytest.approx(0)
    assert materials["components"][0]["feeder_pending_qty"] == pytest.approx(10)

    result = create_material_issues(db, [product.product_id], warehouse_ref1c=WORKSHOP_WH)
    assert result["created"] == []
    pending = result["feeder_shortages"][0]["components"][0]
    assert pending["feeder_warehouse_ref1c"] == SOURCE_WH
    assert pending["available_at_feeder_qty"] == 0
    assert pending["pending_qty"] == pytest.approx(10)
    assert db.query(ProductionMaterialCustodyEvent).filter_by(
        product_id=product.product_id,
    ).count() == 0


def test_partial_feeder_allocates_only_free_feeder_qty_and_leaves_remainder_pending(db_session):
    db = db_session
    other = "wh-other-partial"
    _add_warehouses(db, selected=(WORKSHOP_WH, SOURCE_WH, other))
    _parent, component, product = _setup(db, order_qty=10, suffix="GLOBAL-FEEDER-PARTIAL")
    _set_feeder(db, component, SOURCE_WH)
    _set_stock(db, component, {SOURCE_WH: 4, other: 100})

    result = create_material_issues(db, [product.product_id], warehouse_ref1c=WORKSHOP_WH)
    assert len(result["created"]) == 1
    issue = db.get(ProductionMaterialIssue, result["created"][0]["issue_id"])
    assert issue.source_warehouse_ref1c == SOURCE_WH
    assert issue.lines[0].required_qty == pytest.approx(4)
    pending = result["feeder_shortages"][0]["components"][0]
    assert pending["pending_qty"] == pytest.approx(6)
    custody = db.query(ProductionMaterialCustodyEvent).filter_by(
        product_id=product.product_id, source_kind="issue_created",
    ).one()
    assert custody.warehouse_ref1c == SOURCE_WH
    assert custody.delta_qty == pytest.approx(4)
    refreshed = preview_materials(db, product.product_id)["components"][0]
    assert refreshed["feeder_warehouse_ref1c"] == SOURCE_WH
    assert refreshed["feeder_pending_qty"] == pytest.approx(6)

    repeated = create_material_issues(
        db, [product.product_id], warehouse_ref1c=WORKSHOP_WH,
    )
    assert repeated["created"] == []
    assert repeated["reused"] == []
    assert repeated["feeder_shortages"][0]["components"][0][
        "pending_qty"
    ] == pytest.approx(6)
    assert db.query(ProductionMaterialIssue).filter_by(
        product_id=product.product_id,
    ).count() == 1
    assert db.query(ProductionMaterialCustodyEvent).filter_by(
        product_id=product.product_id, source_kind="issue_created",
    ).count() == 1

    _set_stock(db, component, {SOURCE_WH: 10, other: 100})
    refilled = create_material_issues(
        db, [product.product_id], warehouse_ref1c=WORKSHOP_WH,
    )
    assert refilled["created"] == []
    assert len(refilled["reused"]) == 1
    assert refilled["feeder_shortages"] == []
    db.refresh(issue)
    assert issue.lines[0].required_qty == pytest.approx(10)
    assert preview_materials(db, product.product_id)["components"][0][
        "feeder_pending_qty"
    ] == pytest.approx(0)


def test_missing_feeder_binding_fails_clear_without_warehouse_picker(db_session):
    db = db_session
    _add_warehouses(db)
    _parent, component, product = _setup(db, order_qty=2, suffix="NO-FEEDER")
    binding = db.query(WorkshopWarehouseBinding).filter_by(
        workshop_id=product.control_state.workshop_id
    ).one()
    binding.feeder_warehouse_ref1c = None
    db.commit()
    _set_stock(db, component, {SOURCE_WH: 20})

    result = create_material_issues(db, [product.product_id], warehouse_ref1c=WORKSHOP_WH)
    assert result["created"] == []
    assert result["selection_required"] == []
    assert "workshop feeder warehouse is not configured" in result["errors"][0]
    component_row = preview_materials(db, product.product_id)["components"][0]
    assert component_row["feeder_unavailable_reason"] == "workshop_feeder_assignment_missing"
    assert "не указан склад-питатель" in component_row["feeder_unavailable_label"]


def test_same_component_uses_the_feeder_of_each_order_workshop(db_session):
    db = db_session
    second_feeder = "wh-feeder-two"
    _add_warehouses(db, selected=(WORKSHOP_WH, SOURCE_WH, second_feeder))
    parent, component, first = _setup(db, order_qty=1, suffix="TWO-WORKSHOPS")
    second = _make_order_line(db, parent, order_qty=1, suffix="TWO-WORKSHOPS-B")
    second_workshop = ProductionResource(resource_name="Second workshop")
    db.add(second_workshop)
    db.flush()
    db.add(WorkshopWarehouseBinding(
        workshop_id=second_workshop.resource_id,
        warehouse_ref1c=WORKSHOP_WH,
        feeder_warehouse_ref1c=second_feeder,
    ))
    second.control_state.workshop_id = second_workshop.resource_id
    second.control_state.workshop_id_source = "manual"
    _set_stock(db, component, {SOURCE_WH: 2, second_feeder: 3})
    db.commit()

    first_row = preview_materials(db, first.product_id)["components"][0]
    second_row = preview_materials(db, second.product_id)["components"][0]
    assert first_row["component_item_id"] == second_row["component_item_id"]
    assert first_row["feeder_warehouse_ref1c"] == SOURCE_WH
    assert second_row["feeder_warehouse_ref1c"] == second_feeder


def test_make_proposal_uses_one_workshop_feeder_for_every_component(db_session):
    db = db_session
    _add_warehouses(db)
    parent, first_component, product = _setup(db, order_qty=26, suffix="PROPOSAL-FEEDER")
    spec = db.query(Specification).join(
        DefaultSpecification, DefaultSpecification.spec_id == Specification.spec_id
    ).filter(DefaultSpecification.item_id == parent.item_id).one()
    kind = ProductionKind(ref_1c="proposal-welding-kind", name="Proposal welding kind")
    db.add(kind)
    db.flush()
    spec.production_kind_id = kind.id
    db.add(ResourceProductionKind(
        resource_id=product.control_state.workshop_id,
        production_kind_id=kind.id,
    ))
    components = [first_component]
    for index in range(3):
        component = Item(
            item_code=f"PROPOSAL-C-{index}", item_name=f"Proposal C {index}",
            item_article=f"PC-{index}", unit="шт", status="active",
        )
        db.add(component)
        db.flush()
        db.add(SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=1))
        components.append(component)
    for component in components:
        _set_stock(db, component, {SOURCE_WH: 100})
    db.commit()

    snapshot = preview_make_work_item_materials(
        db,
        work_item_id=10867765,
        item_id=parent.item_id,
        quantity=26,
        spec_id=spec.spec_id,
        ledger_generation_id=db.info["section_stock_generation"],
        order_number="MRP-R-632799",
        run_id=747,
    )

    assert len(snapshot["components"]) == 4
    assert {row["feeder_warehouse_ref1c"] for row in snapshot["components"]} == {SOURCE_WH}
    assert all(row["feeder_unavailable_reason"] is None for row in snapshot["components"])


def test_manual_source_override_is_rejected(db_session):
    with pytest.raises(ValueError, match="Ручной выбор склада-источника отменён"):
        create_material_issues(
            db_session, [], source_warehouse_ref1c="operator-choice",
        )


def test_linked_requested_issue_is_not_extended_for_new_workshop_feeder_demand(db_session):
    db = db_session
    _add_warehouses(db)
    _parent, component, product = _setup(db, order_qty=10, suffix="LINKED-IMMUTABLE")
    _set_stock(db, component, {SOURCE_WH: 20})
    linked = ProductionMaterialIssue(
        document_number="MT-LINKED", product_id=product.product_id, order_id=product.order_id,
        status="requested", direction="issue", warehouse_ref1c=WORKSHOP_WH,
        source_warehouse_ref1c=SOURCE_WH, exported_ref1c="linked-1c-ref",
        ledger_generation_id=db.info["section_stock_generation"],
    )
    db.add(linked)
    db.flush()
    linked_line = ProductionMaterialIssueLine(
        issue_id=linked.issue_id, component_item_id=component.item_id,
        required_qty=4, issued_qty=0, line_status="planned",
    )
    db.add(linked_line)
    generation = db.get(LedgerGeneration, db.info["section_stock_generation"])
    db.add(ProductionMaterialCustodyProjection(
        ledger_generation_id=generation.id, product_id=product.product_id,
        component_item_id=component.item_id, location_kind="transit",
        warehouse_ref1c=SOURCE_WH, reserved_qty=4,
        source_event_high_watermark_id=0, built_at=generation.cutoff,
    ))
    db.commit()

    result = create_material_issues(db, [product.product_id], warehouse_ref1c=WORKSHOP_WH)
    db.refresh(linked_line)

    assert linked_line.required_qty == pytest.approx(4)
    assert result["errors"] == []
    new_issue = db.query(ProductionMaterialIssue).filter(
        ProductionMaterialIssue.product_id == product.product_id,
        ProductionMaterialIssue.issue_id != linked.issue_id,
    ).one()
    assert new_issue.status == "draft"
    assert new_issue.source_warehouse_ref1c == SOURCE_WH
    assert new_issue.lines[0].required_qty == pytest.approx(6)
    repeated = create_material_issues(db, [product.product_id], warehouse_ref1c=WORKSHOP_WH)
    assert repeated["created"] == []
    assert db.query(ProductionMaterialIssue).filter(
        ProductionMaterialIssue.product_id == product.product_id,
    ).count() == 2


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
    assert comp_row["workshop_warehouse_ref1c"] == WORKSHOP_WH
    assert comp_row["workshop_stock_qty"] == pytest.approx(8.0)
    assert comp_row["workshop_free_qty"] == pytest.approx(0.0)

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


def test_fully_held_physical_workshop_cell_cannot_create_another_local_claim(db_session):
    """52 on the workshop minus accepted holds 27 and 25 is locally zero.

    Both accepted movements are workshop custody in the same destination
    physical cell.  Global stock on another warehouse must neither make this
    cell look free nor let a third order claim it locally.
    """
    db = db_session
    _add_warehouses(db)
    parent, comp, transit_owner = _setup(db, order_qty=27.0, suffix="HELD")
    workshop_owner = _make_order_line(db, parent, order_qty=25.0, suffix="HELD-W")
    current = _make_order_line(db, parent, order_qty=9.0, suffix="CURRENT")
    _set_stock(db, comp, {WORKSHOP_WH: 52.0, SOURCE_WH: 100.0})
    # Same item and warehouse, but another characteristic is a different
    # physical key and must not inflate this BOM component's local stock.
    db.add(
        StockBin(
            ledger_generation_id=int(db.info["section_stock_generation"]),
            item_id=int(comp.item_id),
            characteristic_ref="other-characteristic",
            organization_ref=DEFAULT_ORGANIZATION_REF1C,
            warehouse_ref1c=WORKSHOP_WH,
            on_hand=100.0,
        )
    )
    workshop = ProductionResource(resource_name="Сварочный участок")
    db.add(workshop)
    db.flush()
    db.add(
        WorkshopWarehouseBinding(
            workshop_id=int(workshop.resource_id),
            warehouse_ref1c=WORKSHOP_WH,
            feeder_warehouse_ref1c=SOURCE_WH,
        )
    )
    current.control_state.workshop_id = int(workshop.resource_id)
    current.control_state.workshop_id_source = "manual"
    generation = db.get(LedgerGeneration, int(db.info["section_stock_generation"]))
    db.add_all(
        (
            ProductionMaterialCustodyProjection(
                ledger_generation_id=int(generation.id),
                product_id=int(transit_owner.product_id),
                component_item_id=int(comp.item_id),
                location_kind="workshop",
                warehouse_ref1c=WORKSHOP_WH,
                reserved_qty=27.0,
                source_event_high_watermark_id=0,
                built_at=generation.cutoff,
            ),
            ProductionMaterialCustodyProjection(
                ledger_generation_id=int(generation.id),
                product_id=int(workshop_owner.product_id),
                component_item_id=int(comp.item_id),
                location_kind="workshop",
                warehouse_ref1c=WORKSHOP_WH,
                reserved_qty=25.0,
                source_event_high_watermark_id=0,
                built_at=generation.cutoff,
            ),
        )
    )
    db.commit()
    db.expire_all()

    before = preview_materials(db, current.product_id)
    component = before["components"][0]
    assert component["stock_qty"] == pytest.approx(252.0)
    assert component["workshop_warehouse_ref1c"] == WORKSHOP_WH
    assert component["workshop_stock_qty"] == pytest.approx(52.0)
    assert component["workshop_reserved_for_order_qty"] == pytest.approx(0.0)
    assert component["workshop_reserved_other_qty"] == pytest.approx(52.0)
    assert component["workshop_free_qty"] == pytest.approx(0.0)
    assert component["workshop_overheld_qty"] == pytest.approx(0.0)
    assert component["workshop_coverage_status"] == "shortage"
    assert before["workshop_coverage_status"] == "shortage"
    # Global stock still permits a transfer, but is not local readiness.
    assert component["launch_coverage"] == "ok"
    journal_row = list_journal(
        db,
        product_id=current.product_id,
        _material_coverage_by_product={current.product_id: before},
    )["rows"][0]
    # §60: the journal launch pill uses global free stock; the detail card
    # still exposes workshop shortage and prevents physical consumption.
    assert journal_row["coverage_status"] == "ready"
    assert journal_row["coverage_label"] == "Обеспечен"

    overheld_row = (
        db.query(ProductionMaterialCustodyProjection)
        .filter(
            ProductionMaterialCustodyProjection.product_id == workshop_owner.product_id,
            ProductionMaterialCustodyProjection.component_item_id == comp.item_id,
        )
        .one()
    )
    overheld_row.reserved_qty = 35.0
    db.commit()
    db.expire_all()
    overheld = preview_materials(db, current.product_id)["components"][0]
    assert overheld["workshop_stock_qty"] == pytest.approx(52.0)
    assert overheld["workshop_free_qty"] == pytest.approx(0.0)
    assert overheld["workshop_overheld_qty"] == pytest.approx(10.0)
    assert overheld["workshop_coverage_status"] == "unavailable"
    assert overheld["workshop_availability_reason"] == "custody_exceeds_physical"
    overheld_row = db.get(ProductionMaterialCustodyProjection, overheld_row.id)
    overheld_row.reserved_qty = 25.0
    db.commit()
    db.expire_all()

    result = create_material_issues(
        db, [current.product_id], warehouse_ref1c=WORKSHOP_WH
    )

    assert result["already_on_destination"] == []
    assert len(result["created"]) == 1
    issue = db.get(ProductionMaterialIssue, result["created"][0]["issue_id"])
    assert issue.source_warehouse_ref1c == SOURCE_WH
    assert issue.lines[0].required_qty == pytest.approx(9.0)
    current_events = (
        db.query(ProductionMaterialCustodyEvent)
        .filter(ProductionMaterialCustodyEvent.product_id == current.product_id)
        .all()
    )
    assert [(event.location_kind, event.delta_qty) for event in current_events] == [
        ("transit", pytest.approx(9.0))
    ]


def test_material_issue_fails_closed_when_exact_physical_scope_cannot_be_persisted(
    db_session, monkeypatch
):
    db = db_session
    _add_warehouses(db)
    _parent, component, product = _setup(db, order_qty=1.0, suffix="SCOPE")
    original = material_issues._components_for_product

    def scoped_components(session, selected_product, **kwargs):
        spec_id, components = original(session, selected_product, **kwargs)
        components[0]["characteristic_ref"] = "characteristic-1c-ref"
        return spec_id, components

    monkeypatch.setattr(material_issues, "_components_for_product", scoped_components)
    monkeypatch.setattr(material_availability, "_components_for_product", scoped_components)
    workshop = ProductionResource(resource_name="Scope workshop")
    db.add(workshop)
    db.flush()
    db.add(
        WorkshopWarehouseBinding(
            workshop_id=int(workshop.resource_id),
            warehouse_ref1c=WORKSHOP_WH,
            feeder_warehouse_ref1c=SOURCE_WH,
        )
    )
    product.control_state.workshop_id = int(workshop.resource_id)
    product.control_state.workshop_id_source = "manual"
    db.commit()

    preview = preview_materials(db, product.product_id)
    scoped = preview["components"][0]
    assert scoped["workshop_coverage_status"] == "unavailable"
    assert scoped["workshop_availability_reason"] == "unsupported_exact_custody_scope"

    result = create_material_issues(
        db, [product.product_id], warehouse_ref1c=WORKSHOP_WH
    )

    assert result["created"] == []
    assert result["errors"] == [
        f"product_id={product.product_id}: component_item_id={component.item_id}: "
        "заявка перемещения не сохраняет characteristic/organization exact physical key"
    ]
    assert db.query(ProductionMaterialIssue).count() == 0


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
    assert len(second["created"]) == 1
    assert second["feeder_shortages"][0]["components"][0]["pending_qty"] == pytest.approx(6)
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
