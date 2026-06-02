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
    Specification,
)
from app.services.production_workshop_resolver import resolve_workshop_id_for_product


def _product(db, item):
    order = ProductionOrder(
        order_number=f"WR-{item.item_id}",
        order_date=datetime(2026, 6, 2),
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
    )
    db.add(product)
    db.flush()
    return product


def test_resolver_prefers_inferred_stage_over_saved_state(db_session):
    old_workshop = ProductionResource(resource_name="Old")
    inferred_workshop = ProductionResource(resource_name="Inferred")
    stage = ProductionStage(stage_name="Stage")
    item = Item(item_code="WR-STAGE", item_name="Stage item", unit="шт", stock_qty=0, status="active")
    comp = Item(item_code="WR-COMP", item_name="Component", unit="шт", stock_qty=0, status="active")
    db_session.add_all([old_workshop, inferred_workshop, stage, item, comp])
    db_session.flush()
    spec = Specification(spec_name="Stage spec", spec_ref1c="wr-stage-spec")
    db_session.add(spec)
    db_session.flush()
    db_session.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=1, stage_id=stage.stage_id))
    db_session.add(ResourceStage(resource_id=inferred_workshop.resource_id, stage_id=stage.stage_id))
    product = _product(db_session, item)
    db_session.add(ProductionOrderLineState(product_id=product.product_id, workshop_id=old_workshop.resource_id))
    db_session.commit()

    assert resolve_workshop_id_for_product(db_session, product) == inferred_workshop.resource_id


def test_resolver_falls_back_to_production_kind_resource(db_session):
    workshop = ProductionResource(resource_name="Kind workshop")
    kind = ProductionKind(ref_1c="wr-kind", name="Kind")
    item = Item(item_code="WR-KIND", item_name="Kind item", unit="шт", stock_qty=0, status="active")
    db_session.add_all([workshop, kind, item])
    db_session.flush()
    spec = Specification(spec_name="Kind spec", spec_ref1c="wr-kind-spec", production_kind_id=kind.id)
    db_session.add(spec)
    db_session.flush()
    db_session.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    db_session.add(ResourceProductionKind(resource_id=workshop.resource_id, production_kind_id=kind.id))
    product = _product(db_session, item)
    db_session.commit()

    assert resolve_workshop_id_for_product(db_session, product) == workshop.resource_id


def test_resolver_uses_saved_state_when_spec_has_no_mapping(db_session):
    workshop = ProductionResource(resource_name="Manual")
    item = Item(item_code="WR-STATE", item_name="Manual item", unit="шт", stock_qty=0, status="active")
    db_session.add_all([workshop, item])
    db_session.flush()
    product = _product(db_session, item)
    db_session.add(ProductionOrderLineState(product_id=product.product_id, workshop_id=workshop.resource_id))
    db_session.commit()

    assert resolve_workshop_id_for_product(db_session, product) == workshop.resource_id
