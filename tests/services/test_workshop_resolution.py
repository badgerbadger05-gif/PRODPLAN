"""Workshop resolution: production kind only, stage chain is advisory."""
from __future__ import annotations

from datetime import datetime

from app.models import (
    DefaultSpecification,
    Item,
    ProductionKind,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionResource,
    ProductionStage,
    ResourceProductionKind,
    ResourceStage,
    SpecComponent,
    SpecOperation,
    Specification,
    WorkshopWarehouseBinding,
)
from app.services.workshop_resolution import (
    REASON_KIND_NOT_BOUND,
    REASON_NO_PRODUCTION_KIND,
    REASON_NO_SPEC,
    REASON_NO_WAREHOUSE_BINDING,
    REASON_OK,
    diagnose_product,
    diagnose_specs,
    main_stages_for_specs,
    resolve_workshop_for_product,
    resolve_workshop_for_specs,
    suggest_workshops_by_stage,
)


def _mk_item(db, code: str) -> Item:
    item = Item(
        item_code=code,
        item_name=f"Item {code}",
        item_article=code,
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db.add(item)
    db.flush()
    return item


def _mk_spec(db, item: Item, *, name: str, kind: ProductionKind | None = None) -> Specification:
    spec = Specification(
        spec_name=name,
        spec_ref1c=f"sr-{name}",
        production_kind_id=kind.id if kind else None,
    )
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    return spec


def _mk_kind(db, name: str) -> ProductionKind:
    kind = ProductionKind(ref_1c=f"kind-{name}", name=name)
    db.add(kind)
    db.flush()
    return kind


def _mk_resource(db, name: str) -> ProductionResource:
    resource = ProductionResource(resource_name=name)
    db.add(resource)
    db.flush()
    return resource


def _mk_operation(db, name: str):
    from app.models import Operation

    op = Operation(operation_ref1c=f"op-{name}", operation_name=name)
    db.add(op)
    db.flush()
    return op


def _mk_product(db, item: Item, *, spec_id: int | None = None) -> ProductionProduct:
    order = ProductionOrder(
        order_number=f"O-{item.item_code}",
        order_date=datetime(2026, 6, 1),
        is_posted=True,
        deletion_mark=False,
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=1,
        produced_qty=0,
        remaining_qty=1,
        spec_id=spec_id,
    )
    db.add(product)
    db.flush()
    return product


def test_resolves_workshop_via_production_kind(db_session):
    db = db_session
    item = _mk_item(db, "RK-1")
    kind = _mk_kind(db, "Сварка")
    resource = _mk_resource(db, "Сварочный участок")
    spec = _mk_spec(db, item, name="Spec RK-1", kind=kind)
    db.add(ResourceProductionKind(resource_id=resource.resource_id, production_kind_id=kind.id))
    db.commit()

    assert resolve_workshop_for_specs(db, [spec.spec_id]) == {spec.spec_id: resource.resource_id}


def test_stage_chain_does_not_resolve_workshop(db_session):
    """The legacy fallback: a ResourceStage binding alone must NOT route."""
    db = db_session
    item = _mk_item(db, "RK-2")
    spec = _mk_spec(db, item, name="Spec RK-2", kind=None)
    stage = ProductionStage(stage_name="Сварка", stage_ref1c="st-rk2")
    resource = _mk_resource(db, "Участок по этапу")
    db.add(stage)
    db.flush()
    db.add(SpecOperation(spec_id=spec.spec_id, operation_id=_mk_operation(db, "op-rk2").operation_id, stage_id=stage.stage_id, time_norm=2))
    db.add(ResourceStage(resource_id=resource.resource_id, stage_id=stage.stage_id))
    db.commit()

    assert resolve_workshop_for_specs(db, [spec.spec_id]) == {}
    product = _mk_product(db, item)
    db.commit()
    assert resolve_workshop_for_product(db, product) is None

    # ... but the chain is available as a suggestion.
    suggestions = suggest_workshops_by_stage(db, [spec.spec_id])
    assert suggestions[spec.spec_id][0] == resource.resource_id
    assert suggestions[spec.spec_id][3] == "Сварка"


def test_manual_state_assignment_wins(db_session):
    db = db_session
    item = _mk_item(db, "RK-3")
    kind = _mk_kind(db, "Гибка")
    auto_resource = _mk_resource(db, "Авто-участок")
    manual_resource = _mk_resource(db, "Ручной участок")
    spec = _mk_spec(db, item, name="Spec RK-3", kind=kind)
    db.add(ResourceProductionKind(resource_id=auto_resource.resource_id, production_kind_id=kind.id))
    product = _mk_product(db, item, spec_id=spec.spec_id)
    db.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="ready",
            issue_status="not_requested",
            workshop_id=manual_resource.resource_id,
            workshop_id_source="manual",
        )
    )
    db.commit()

    assert resolve_workshop_for_product(db, product) == manual_resource.resource_id


def test_diagnose_specs_all_codes(db_session):
    db = db_session
    # NO_PRODUCTION_KIND
    item_a = _mk_item(db, "DG-A")
    spec_a = _mk_spec(db, item_a, name="Spec A", kind=None)
    # KIND_NOT_BOUND (kind exists, no resource binding) + stage suggestion
    item_b = _mk_item(db, "DG-B")
    kind_b = _mk_kind(db, "Покраска")
    spec_b = _mk_spec(db, item_b, name="Spec B", kind=kind_b)
    stage = ProductionStage(stage_name="Покраска", stage_ref1c="st-dg-b")
    suggested = _mk_resource(db, "Окрасочный участок")
    db.add(stage)
    db.flush()
    db.add(SpecComponent(spec_id=spec_b.spec_id, item_id=item_a.item_id, quantity=1, stage_id=stage.stage_id))
    db.add(ResourceStage(resource_id=suggested.resource_id, stage_id=stage.stage_id))
    # NO_WAREHOUSE_BINDING (kind bound to workshop, warehouse missing)
    item_c = _mk_item(db, "DG-C")
    kind_c = _mk_kind(db, "Сборка")
    resource_c = _mk_resource(db, "Сборочный участок")
    spec_c = _mk_spec(db, item_c, name="Spec C", kind=kind_c)
    db.add(ResourceProductionKind(resource_id=resource_c.resource_id, production_kind_id=kind_c.id))
    # OK (full chain)
    item_d = _mk_item(db, "DG-D")
    kind_d = _mk_kind(db, "Токарка")
    resource_d = _mk_resource(db, "Токарный участок")
    spec_d = _mk_spec(db, item_d, name="Spec D", kind=kind_d)
    db.add(ResourceProductionKind(resource_id=resource_d.resource_id, production_kind_id=kind_d.id))
    db.add(WorkshopWarehouseBinding(workshop_id=resource_d.resource_id, warehouse_ref1c="wh-d"))
    db.commit()

    result = diagnose_specs(db, [spec_a.spec_id, spec_b.spec_id, spec_c.spec_id, spec_d.spec_id])

    assert result[spec_a.spec_id].reason_code == REASON_NO_PRODUCTION_KIND
    assert "Spec A" in result[spec_a.spec_id].reason_text

    diag_b = result[spec_b.spec_id]
    assert diag_b.reason_code == REASON_KIND_NOT_BOUND
    assert diag_b.production_kind_name == "Покраска"
    assert diag_b.suggested_resource_id == suggested.resource_id
    assert "Окрасочный участок" in diag_b.recommendation

    diag_c = result[spec_c.spec_id]
    assert diag_c.reason_code == REASON_NO_WAREHOUSE_BINDING
    assert diag_c.workshop_id == resource_c.resource_id

    diag_d = result[spec_d.spec_id]
    assert diag_d.reason_code == REASON_OK
    assert diag_d.status == "ok"
    assert diag_d.workshop_id == resource_d.resource_id


def test_diagnose_product_no_spec_and_manual_state(db_session):
    db = db_session
    item = _mk_item(db, "DG-NS")
    product = _mk_product(db, item)
    db.commit()
    assert diagnose_product(db, product).reason_code == REASON_NO_SPEC

    # Manual assignment with a warehouse binding -> OK regardless of spec.
    resource = _mk_resource(db, "Ручной участок 2")
    db.add(WorkshopWarehouseBinding(workshop_id=resource.resource_id, warehouse_ref1c="wh-m"))
    db.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="ready",
            issue_status="not_requested",
            workshop_id=resource.resource_id,
            workshop_id_source="manual",
        )
    )
    db.commit()
    diag = diagnose_product(db, product)
    assert diag.reason_code == REASON_OK
    assert diag.workshop_source == "state"


def test_main_stages_prefers_max_hours_operation(db_session):
    db = db_session
    item = _mk_item(db, "ST-1")
    spec = _mk_spec(db, item, name="Spec ST-1")
    stage_small = ProductionStage(stage_name="Резка", stage_ref1c="st-small")
    stage_big = ProductionStage(stage_name="Сварка", stage_ref1c="st-big")
    db.add_all([stage_small, stage_big])
    db.flush()
    db.add(SpecOperation(spec_id=spec.spec_id, operation_id=_mk_operation(db, "op-small").operation_id, stage_id=stage_small.stage_id, time_norm=1))
    db.add(SpecOperation(spec_id=spec.spec_id, operation_id=_mk_operation(db, "op-big").operation_id, stage_id=stage_big.stage_id, time_norm=5))
    db.commit()

    stages = main_stages_for_specs(db, [spec.spec_id])
    assert stages[spec.spec_id] == (stage_big.stage_id, "Сварка")
