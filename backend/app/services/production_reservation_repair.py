from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session, joinedload

from ..models import ProductionMaterialIssue, ProductionOrderLineState, ProductionProduct
from .production_control_common import to_float as _to_float
from .production_control_material_issues import (
    _claim_components_in_place,
    _components_for_product,
    _destination_warehouse_for_product,
)
from .planning_truth import require_accepted_truth
from .production_control_reservations import load_reservation_state


def repair_in_place_reservations(
    db: Session,
    product_ids: Sequence[int],
    *,
    initiated_by: Optional[str] = None,
    warehouse_ref1c: Optional[str] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    Create local-only in-place claims for already-moved legacy materials.

    This is an explicit repair tool for rows where 1C was corrected manually
    and PRODPLAN only needs its reservation ledger to catch up. It never
    creates or posts 1C transfer documents.
    """
    truth = require_accepted_truth(db, "production_reservation_repair")
    repaired: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for raw_pid in product_ids:
        pid = int(raw_pid)
        product = (
            db.query(ProductionProduct)
            .options(
                joinedload(ProductionProduct.order),
                joinedload(ProductionProduct.item),
                joinedload(ProductionProduct.control_state),
            )
            .filter(ProductionProduct.product_id == pid)
            .one_or_none()
        )
        if product is None:
            errors.append({"product_id": pid, "error": "Строка заказа не найдена"})
            continue

        spec_id, components = _components_for_product(db, product)
        if not spec_id or not components:
            errors.append({"product_id": pid, "error": "Нет спецификации или материалов"})
            continue

        destination = warehouse_ref1c or _destination_warehouse_for_product(db, product, spec_id)
        if not destination:
            errors.append({"product_id": pid, "error": "Не найден склад участка"})
            continue

        comp_ids = [int(comp["component_item_id"]) for comp in components]
        reservation_state = load_reservation_state(db, item_ids=comp_ids)
        own = reservation_state.for_product(pid)

        claims: List[Dict[str, Any]] = []
        for comp in components:
            cid = int(comp["component_item_id"])
            required = _to_float(comp.get("required_qty"))
            at_workshop = own.at_workshop.get(cid, 0.0)
            missing = max(0.0, required - at_workshop)
            if missing > 1e-9:
                claims.append({**comp, "claim_qty": missing})

        if not claims:
            skipped.append(
                {
                    "product_id": pid,
                    "order_number": str(product.order.order_number or "") if product.order else "",
                    "reason": "already_covered_at_workshop",
                }
            )
            continue

        issue = _claim_components_in_place(
            db,
            product,
            claims,
            spec_id=spec_id,
            destination_warehouse_ref1c=destination,
                initiated_by=initiated_by or "reservation-repair",
                ledger_generation_id=int(truth.generation_id),
        )
        state = (
            product.control_state
            or db.query(ProductionOrderLineState)
            .filter(ProductionOrderLineState.product_id == pid)
            .one_or_none()
        )
        if state is not None:
            state.issue_status = "posted"
            if str(state.status or "") in {"shortage", "partial", "ready", "to_move"}:
                state.status = "assembled"

        repaired.append(
            {
                "product_id": pid,
                "order_number": str(product.order.order_number or "") if product.order else "",
                "issue_id": int(issue.issue_id) if issue else None,
                "document_number": str(issue.document_number or "") if issue else "",
                "direction": "in_place",
                "warehouse_ref1c": str(destination or ""),
                "lines": [
                    {
                        "component_item_id": int(comp["component_item_id"]),
                        "item_name": str(comp.get("item_name") or ""),
                        "claim_qty": _to_float(comp.get("claim_qty")),
                    }
                    for comp in claims
                ],
            }
        )

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return {
        "status": "ok",
        "dry_run": bool(dry_run),
        "repaired": repaired,
        "skipped": skipped,
        "errors": errors,
    }
