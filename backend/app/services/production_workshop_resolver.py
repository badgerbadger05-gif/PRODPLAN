from __future__ import annotations

from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    DefaultSpecification,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionResource,
    ResourceProductionKind,
    ResourceStage,
    SpecComponent,
    Specification,
    SpecOperation,
    WorkshopWarehouseBinding,
)


def default_spec_id_for_product(
    db: Session,
    product: ProductionProduct,
    spec_id: Optional[int] = None,
) -> Optional[int]:
    if spec_id:
        return int(spec_id)
    if product.spec_id:
        return int(product.spec_id)
    row = (
        db.query(DefaultSpecification.spec_id)
        .filter(DefaultSpecification.item_id == int(product.item_id))
        .order_by(DefaultSpecification.id.asc())
        .first()
    )
    return int(row[0]) if row else None


def inferred_workshop_id_for_spec(db: Session, spec_id: Optional[int]) -> Optional[int]:
    if not spec_id:
        return None

    stage_hours = (
        db.query(SpecOperation.stage_id)
        .filter(SpecOperation.spec_id == int(spec_id), SpecOperation.stage_id.isnot(None))
        .group_by(SpecOperation.stage_id)
        .order_by(func.sum(SpecOperation.time_norm).desc())
        .first()
    )
    stage_id = int(stage_hours[0]) if stage_hours else None
    if stage_id is None:
        comp_stage = (
            db.query(SpecComponent.stage_id)
            .filter(SpecComponent.spec_id == int(spec_id), SpecComponent.stage_id.isnot(None))
            .order_by(SpecComponent.component_id.asc())
            .first()
        )
        stage_id = int(comp_stage[0]) if comp_stage else None

    if stage_id is not None:
        resource = (
            db.query(ResourceStage.resource_id)
            .filter(ResourceStage.stage_id == int(stage_id))
            .order_by(ResourceStage.id.asc())
            .first()
        )
        if resource:
            return int(resource[0])

    resource = (
        db.query(ResourceProductionKind.resource_id)
        .join(Specification, Specification.production_kind_id == ResourceProductionKind.production_kind_id)
        .join(ProductionResource, ProductionResource.resource_id == ResourceProductionKind.resource_id)
        .filter(Specification.spec_id == int(spec_id))
        .filter(Specification.production_kind_id.isnot(None))
        .order_by(ResourceProductionKind.id.asc())
        .first()
    )
    return int(resource[0]) if resource else None


def state_workshop_id_for_product(db: Session, product: ProductionProduct) -> Optional[int]:
    state = getattr(product, "control_state", None)
    if state and state.workshop_id:
        return int(state.workshop_id)
    state = (
        db.query(ProductionOrderLineState.workshop_id)
        .filter(ProductionOrderLineState.product_id == int(product.product_id))
        .first()
    )
    return int(state[0]) if state and state[0] else None


def resolve_workshop_id_for_product(
    db: Session,
    product: ProductionProduct,
    spec_id: Optional[int] = None,
) -> Optional[int]:
    effective_spec_id = default_spec_id_for_product(db, product, spec_id)
    inferred_workshop_id = inferred_workshop_id_for_spec(db, effective_spec_id)
    if inferred_workshop_id is not None:
        return inferred_workshop_id
    return state_workshop_id_for_product(db, product)


def resolve_workshop_binding_for_product(
    db: Session,
    product: ProductionProduct,
    spec_id: Optional[int] = None,
) -> Optional[WorkshopWarehouseBinding]:
    workshop_id = resolve_workshop_id_for_product(db, product, spec_id)
    if workshop_id is None:
        return None
    return (
        db.query(WorkshopWarehouseBinding)
        .filter(WorkshopWarehouseBinding.workshop_id == int(workshop_id))
        .one_or_none()
    )
