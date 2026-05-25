from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from ..models import DefaultSpecification, PlanningRun, ProductionOrderLineState, ProductionProduct, Unit
from .production_control_common import looks_like_guid


def unit_display(db: Session, raw_unit: Any) -> str:
    raw = str(raw_unit or "").strip()
    if not raw:
        return ""
    unit = db.query(Unit).filter(Unit.unit_ref1c == raw).first()
    if unit:
        return str(unit.short_name or unit.unit_name or unit.unit_code or "").strip()
    return "" if looks_like_guid(raw) else raw


def ensure_state(db: Session, product: ProductionProduct) -> ProductionOrderLineState:
    state = getattr(product, "control_state", None)
    if state:
        return state
    state = (
        db.query(ProductionOrderLineState)
        .filter(ProductionOrderLineState.product_id == product.product_id)
        .first()
    )
    if state:
        return state
    state = ProductionOrderLineState(
        product_id=product.product_id,
        status="shortage",
        issue_status="not_requested",
    )
    db.add(state)
    db.flush()
    return state


def default_spec_id(db: Session, product: ProductionProduct) -> Optional[int]:
    if product.spec_id:
        return int(product.spec_id)
    item_id = int(product.item_id)
    default_spec = (
        db.query(DefaultSpecification)
        .filter(DefaultSpecification.item_id == item_id)
        .order_by(DefaultSpecification.id.asc())
        .first()
    )
    return int(default_spec.spec_id) if default_spec else None


def latest_run_id(db: Session) -> Optional[int]:
    row = db.query(PlanningRun.run_id).order_by(PlanningRun.run_id.desc()).first()
    return int(row[0]) if row else None
