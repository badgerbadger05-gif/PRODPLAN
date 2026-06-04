from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session, joinedload

from ..models import (
    ProductionManufacture,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionProduct,
    SpecComponent,
)
from .production_control_common import to_float as _to_float
from .production_control_domain import default_spec_id as _default_spec_id, ensure_state as _ensure_state
from .one_c_document_numbers import material_issue_number


# ---------------------------------------------------------------------------


def produce_line(
    db: Session,
    product_id: int,
    *,
    qty: float,
    executor: Optional[str] = None,
    comment: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Record one production event on a ProductionProduct line.

    Effects:
    - Creates a ProductionManufacture row (qty + executor + comment).
    - Bumps production_products.produced_qty by qty.
    - Decreases production_products.remaining_qty by qty (clamped >=0).
    - Updates ProductionOrderLineState.status:
        remaining > 0 -> 'produced_partial'
        remaining <= 0 -> 'produced'
      The state is sticky past those values: never regresses.

    The 1C-side Document_СборкаЗапасов is created separately via
    one_c_manufacture_export.export_manufactures_to_1c (typically right after
    via the journal "Выгрузить выпуск в 1С" action).
    """
    qty_f = float(qty or 0)
    if qty_f <= 0:
        raise ValueError("qty должен быть положительным")

    product = (
        db.query(ProductionProduct)
        .filter(ProductionProduct.product_id == int(product_id))
        .one_or_none()
    )
    if product is None:
        raise ValueError(f"product_id={product_id}: строка заказа не найдена")

    remaining = _to_float(product.remaining_qty)
    produced_before = _to_float(product.produced_qty)
    overproduced_qty = 0.0
    order_quantity_before = _to_float(product.quantity)
    if remaining <= 1e-9:
        raise ValueError(
            "remaining_qty=0: эта строка уже произведена полностью"
        )
    if qty_f - remaining > 1e-6:
        overproduced_qty = qty_f - remaining
        product.quantity = produced_before + qty_f
        remaining = qty_f

    material_issue = (
        db.query(ProductionMaterialIssue)
        .filter(
            ProductionMaterialIssue.product_id == int(product.product_id),
            ProductionMaterialIssue.direction == "issue",
            ProductionMaterialIssue.status.in_(("posted", "issued")),
        )
        .order_by(ProductionMaterialIssue.issue_id.desc())
        .first()
    )
    if material_issue is None:
        raise ValueError(
            "Нельзя создать выпуск без проведённого перемещения материалов по этой строке"
        )

    manufacture = ProductionManufacture(
        product_id=int(product.product_id),
        order_id=int(product.order_id),
        qty=qty_f,
        executor=(str(executor).strip() if executor else None) or None,
        comment=(str(comment).strip() if comment else None) or None,
        status="draft",
    )
    db.add(manufacture)
    db.flush()

    product.produced_qty = produced_before + qty_f
    new_remaining = max(0.0, remaining - qty_f)
    product.remaining_qty = new_remaining

    state = _ensure_state(db, product)
    if new_remaining <= 1e-9:
        new_state = "produced"
    else:
        new_state = "produced_partial"
    # Stickiness: don't drop from 'produced' back to 'produced_partial'
    # if the user somehow recorded a negative-equivalent.
    if state.status not in {"produced", "cancelled"}:
        state.status = new_state
    elif state.status == "produced_partial" and new_state == "produced":
        # Partial -> full produced (final batch).
        state.status = "produced"

    db.commit()

    return {
        "status": "ok",
        "manufacture_id": int(manufacture.manufacture_id),
        "product_id": int(product.product_id),
        "order_id": int(product.order_id),
        "qty": float(qty_f),
        "produced_qty_total": float(product.produced_qty),
        "remaining_qty": float(product.remaining_qty),
        "overproduced_qty": float(overproduced_qty),
        "order_quantity_before": float(order_quantity_before),
        "order_quantity_after": float(product.quantity),
        "line_status": state.status,
    }


def rollback_local_manufacture(db: Session, manufacture_id: int) -> Dict[str, Any]:
    """
    Undo a local manufacture that was created before its 1C export failed.
    Only non-exported draft rows may be rolled back.
    """
    manufacture = (
        db.query(ProductionManufacture)
        .filter(ProductionManufacture.manufacture_id == int(manufacture_id))
        .one_or_none()
    )
    if manufacture is None:
        raise ValueError(f"manufacture_id={manufacture_id}: выпуск не найден")
    if getattr(manufacture, "exported_ref1c", None):
        raise ValueError("Нельзя откатить выпуск, уже выгруженный в 1С")
    if manufacture.status == "exported":
        raise ValueError("Нельзя откатить выгруженный выпуск")

    product = (
        db.query(ProductionProduct)
        .filter(ProductionProduct.product_id == int(manufacture.product_id))
        .one_or_none()
    )
    if product is None:
        raise ValueError(f"product_id={manufacture.product_id}: строка заказа не найдена")

    qty_f = _to_float(manufacture.qty)
    product.produced_qty = max(0.0, _to_float(product.produced_qty) - qty_f)
    product.remaining_qty = _to_float(product.remaining_qty) + qty_f

    state = _ensure_state(db, product)
    if state.status in {"produced", "produced_partial"}:
        state.status = "assembled"

    db.delete(manufacture)
    db.commit()

    return {
        "status": "rolled_back",
        "manufacture_id": int(manufacture_id),
        "product_id": int(product.product_id),
        "produced_qty_total": float(product.produced_qty),
        "remaining_qty": float(product.remaining_qty),
        "line_status": state.status,
    }


# ---------------------------------------------------------------------------
# Reverse transfer: return leftover components from workshop to source.
# Plan rule (Следующие этапы #6): "При частичном выпуске создавать обратное
# перемещение лишних компонентов на исходные склады."
# ---------------------------------------------------------------------------


def _outgoing_issues_for_product(
    db: Session, product_id: int
) -> List[ProductionMaterialIssue]:
    """
    Outgoing material issues that actually delivered (or are reported as
    delivered) into the workshop. 'cancelled' / 'draft' / 'requested' are
    excluded вЂ” those haven't physically moved yet.
    """
    return (
        db.query(ProductionMaterialIssue)
        .options(joinedload(ProductionMaterialIssue.lines))
        .filter(ProductionMaterialIssue.product_id == int(product_id))
        .filter(ProductionMaterialIssue.direction == "issue")
        .filter(ProductionMaterialIssue.status.in_(("exported", "posted", "issued")))
        .all()
    )


def _next_return_number(db: Session) -> str:
    today = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"MR-{today}-"
    count = (
        db.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.document_number.like(f"{prefix}%"))
        .count()
    )
    return f"{prefix}{count + 1:04d}"


def return_leftover_components(
    db: Session,
    product_id: int,
    *,
    initiated_by: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compute leftover components for a partially-produced line and create a
    return-transfer ProductionMaterialIssue (direction='return') with swapped
    source / destination warehouses.

    Steps:
    1. Find original outgoing material_issues (direction='issue', status in
       exported/posted/issued) for this product.
    2. Sum issued qty per component across them.
    3. Compute consumed = produced_qty * spec_qty_per_unit per component.
    4. leftover = max(0, issued - consumed). Skip components with no leftover.
    5. Create one ProductionMaterialIssue with direction='return' + swapped
       warehouses + leftover-qty lines.

    Idempotency: if an active (draft|requested) return-issue already exists
    for the product, return it under `reused=True`. The unique index on
    production_material_issues was scoped to direction='issue' so a return
    draft does not conflict with an outgoing one.

    Returns:
      {status, return_issue_id?, document_number?, reused?, lines: [...],
       source_warehouse_ref1c?, destination_warehouse_ref1c?, skipped_reason?}
    """
    product = (
        db.query(ProductionProduct)
        .filter(ProductionProduct.product_id == int(product_id))
        .one_or_none()
    )
    if product is None:
        raise ValueError(f"product_id={product_id}: строка заказа не найдена")

    produced_qty = _to_float(product.produced_qty)
    if produced_qty <= 0:
        return {
            "status": "skipped",
            "skipped_reason": "produced_qty=0: нечего возвращать без выпуска",
            "lines": [],
        }

    existing_active = (
        db.query(ProductionMaterialIssue)
        .filter(
            ProductionMaterialIssue.product_id == int(product_id),
            ProductionMaterialIssue.direction == "return",
            ProductionMaterialIssue.status.in_(("draft", "requested")),
        )
        .order_by(ProductionMaterialIssue.issue_id.desc())
        .first()
    )
    if existing_active is not None:
        return {
            "status": "ok",
            "reused": True,
            "return_issue_id": int(existing_active.issue_id),
            "document_number": str(existing_active.document_number),
            "lines": [],
        }

    outgoing = _outgoing_issues_for_product(db, int(product_id))
    if not outgoing:
        return {
            "status": "skipped",
            "skipped_reason": (
                "Нет выгруженных в 1С исходящих перемещений по этой строке"
            ),
            "lines": [],
        }

    issued_by_component: Dict[int, float] = {}
    unit_by_component: Dict[int, str] = {}
    spec_by_component: Dict[int, Optional[int]] = {}
    for issue in outgoing:
        for ln in issue.lines:
            cid = int(ln.component_item_id)
            issued_by_component[cid] = issued_by_component.get(cid, 0.0) + _to_float(
                ln.required_qty
            )
            if ln.unit:
                unit_by_component.setdefault(cid, str(ln.unit))
            if ln.source_spec_id and cid not in spec_by_component:
                spec_by_component[cid] = int(ln.source_spec_id)

    spec_id = _default_spec_id(db, product)
    qty_per_unit: Dict[int, float] = {}
    if spec_id:
        rows = (
            db.query(SpecComponent.item_id, SpecComponent.quantity)
            .filter(SpecComponent.spec_id == spec_id)
            .all()
        )
        qty_per_unit = {int(iid): _to_float(q) for iid, q in rows}

    return_lines: List[Dict[str, Any]] = []
    for cid, issued in issued_by_component.items():
        per_unit = qty_per_unit.get(cid, 0.0)
        consumed = produced_qty * per_unit
        leftover = issued - consumed
        if leftover <= 1e-9:
            continue
        return_lines.append(
            {
                "component_item_id": cid,
                "issued_qty": float(issued),
                "consumed_qty": float(consumed),
                "qty_per_unit": float(per_unit),
                "leftover_qty": float(leftover),
                "unit": unit_by_component.get(cid),
                "source_spec_id": spec_by_component.get(cid),
            }
        )

    if not return_lines:
        return {
            "status": "skipped",
            "skipped_reason": "Нет компонентов с положительным остатком",
            "lines": [],
        }

    latest_outgoing = max(outgoing, key=lambda i: i.issue_id)
    return_source = str(latest_outgoing.warehouse_ref1c or "") or None
    return_dest = str(latest_outgoing.source_warehouse_ref1c or "") or None

    new_issue = ProductionMaterialIssue(
        document_number="",
        product_id=int(product.product_id),
        order_id=int(product.order_id),
        status="draft",
        direction="return",
        warehouse_ref1c=return_dest,
        source_warehouse_ref1c=return_source,
        initiated_by=initiated_by,
    )
    db.add(new_issue)
    db.flush()
    new_issue.document_number = material_issue_number(db, new_issue)
    for ln in return_lines:
        db.add(
            ProductionMaterialIssueLine(
                issue_id=int(new_issue.issue_id),
                component_item_id=int(ln["component_item_id"]),
                required_qty=float(ln["leftover_qty"]),
                issued_qty=0.0,
                unit=ln.get("unit"),
                source_spec_id=ln.get("source_spec_id"),
                line_status="planned",
            )
        )
    db.commit()

    return {
        "status": "ok",
        "reused": False,
        "return_issue_id": int(new_issue.issue_id),
        "document_number": new_issue.document_number,
        "source_warehouse_ref1c": return_source,
        "destination_warehouse_ref1c": return_dest,
        "lines": return_lines,
    }
