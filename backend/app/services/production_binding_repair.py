"""Repair clean MRP journal rows when 1C bindings drift.

PRODPLAN MRP rows are local working lines until a 1C document is created. For
those clean rows, 1C remains the source of truth: the line spec follows the
current default specification, and the workshop is resolved from that spec.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from ..models import (
    DefaultSpecification,
    ProductionManufacture,
    ProductionMaterialIssue,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SyncLink,
)
from .production_control_common import to_float as _to_float

PRODUCTION_ORDER_ENTITY = "Document_ЗаказНаПроизводство"
STOCK_TRANSFER_ENTITY = "Document_ПеремещениеЗапасов"
MANUFACTURE_ENTITY = "Document_СборкаЗапасов"
DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"

_LOCAL_REBUILDABLE_ISSUE_STATUSES = {"draft", "requested", "error"}
_ONE_C_LINK_STATUSES = {"success", "posted"}


def _default_spec_ids_for_items(db: Session, item_ids: Sequence[int]) -> Dict[int, int]:
    ids = sorted({int(item_id) for item_id in item_ids if item_id is not None})
    if not ids:
        return {}
    result: Dict[int, int] = {}
    for row in (
        db.query(DefaultSpecification.item_id, DefaultSpecification.spec_id)
        .filter(DefaultSpecification.item_id.in_(ids))
        .order_by(DefaultSpecification.id.asc())
        .all()
    ):
        result.setdefault(int(row.item_id), int(row.spec_id))
    return result


def _sync_link_has_1c_document(
    db: Session,
    *,
    source_doctype: str,
    source_id: int,
    target_entity: str,
) -> bool:
    row = (
        db.query(SyncLink.target_ref_key, SyncLink.status)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == source_doctype,
            SyncLink.source_id == int(source_id),
            SyncLink.target_entity == target_entity,
        )
        .first()
    )
    if row is None:
        return False
    if str(row.target_ref_key or "").strip():
        return True
    return str(row.status or "").strip().lower() in _ONE_C_LINK_STATUSES


def _issue_has_1c_document(db: Session, issue: ProductionMaterialIssue) -> bool:
    if str(issue.exported_ref1c or "").strip():
        return True
    if str(issue.status or "").strip().lower() in {"exported", "posted"}:
        return True
    return _sync_link_has_1c_document(
        db,
        source_doctype="material_issue",
        source_id=int(issue.issue_id),
        target_entity=STOCK_TRANSFER_ENTITY,
    )


def _manufacture_has_1c_document(db: Session, manufacture: ProductionManufacture) -> bool:
    if str(manufacture.exported_ref1c or "").strip():
        return True
    if str(manufacture.status or "").strip().lower() in {"exported", "posted"}:
        return True
    return _sync_link_has_1c_document(
        db,
        source_doctype="manufacture",
        source_id=int(manufacture.manufacture_id),
        target_entity=MANUFACTURE_ENTITY,
    )


def _cleanliness_blocker(db: Session, product: ProductionProduct) -> Optional[str]:
    order = product.order
    if order is None:
        return "missing_order"
    if str(order.source or "").lower() != "mrp":
        return "not_mrp"
    if str(order.order_ref1c or "").strip():
        return "order_in_1c"
    if _sync_link_has_1c_document(
        db,
        source_doctype="production_order",
        source_id=int(order.order_id),
        target_entity=PRODUCTION_ORDER_ENTITY,
    ):
        return "order_in_1c"
    if _to_float(product.produced_qty) > 1e-9:
        return "produced_qty"

    issues = (
        db.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.product_id == int(product.product_id))
        .all()
    )
    for issue in issues:
        if _issue_has_1c_document(db, issue):
            return "material_issue_in_1c"
        if str(issue.status or "").strip().lower() not in _LOCAL_REBUILDABLE_ISSUE_STATUSES:
            return f"material_issue_status:{issue.status}"

    manufactures = (
        db.query(ProductionManufacture)
        .filter(ProductionManufacture.product_id == int(product.product_id))
        .all()
    )
    for manufacture in manufactures:
        if _manufacture_has_1c_document(db, manufacture):
            return "manufacture_in_1c"
        if str(manufacture.status or "").strip().lower() not in {"draft", "error", "cancelled"}:
            return f"manufacture_status:{manufacture.status}"
    return None


def _delete_local_rebuildable_issues(db: Session, product_id: int) -> int:
    deleted = 0
    issues = (
        db.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.product_id == int(product_id))
        .all()
    )
    for issue in issues:
        if _issue_has_1c_document(db, issue):
            continue
        if str(issue.status or "").strip().lower() in _LOCAL_REBUILDABLE_ISSUE_STATUSES:
            db.delete(issue)
            deleted += 1
    return deleted


def repair_clean_mrp_bindings(
    db: Session,
    *,
    run_id: Optional[int] = None,
    item_ids: Optional[Sequence[int]] = None,
) -> Dict[str, Any]:
    """Refresh clean MRP rows to current 1C default specs and automatic routing."""
    db.flush()
    query = (
        db.query(ProductionProduct)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .outerjoin(
            ProductionOrderLineState,
            ProductionOrderLineState.product_id == ProductionProduct.product_id,
        )
        .options(joinedload(ProductionProduct.order), joinedload(ProductionProduct.control_state))
        .filter(ProductionOrder.source == "mrp")
        .filter(ProductionOrder.deletion_mark == False)  # noqa: E712
        .filter(or_(ProductionOrder.order_state_key.is_(None), func.lower(ProductionOrder.order_state_key) != DONE_STATE_KEY))
        .filter(func.coalesce(ProductionProduct.remaining_qty, ProductionProduct.quantity) > 0)
        .filter(func.coalesce(ProductionOrderLineState.status, "shortage").notin_(("completed", "cancelled")))
    )
    if run_id is not None:
        query = query.filter(ProductionOrder.source_run_id == int(run_id))
    ids = sorted({int(item_id) for item_id in (item_ids or []) if item_id is not None})
    if ids:
        query = query.filter(ProductionProduct.item_id.in_(ids))

    products = query.all()
    default_specs = _default_spec_ids_for_items(db, [int(p.item_id) for p in products])

    stats: Dict[str, Any] = {
        "checked": len(products),
        "spec_updated": 0,
        "workshop_auto_cleared": 0,
        "local_issues_deleted": 0,
        "blocked": {},
    }
    now = datetime.now(timezone.utc)
    for product in products:
        default_spec_id = default_specs.get(int(product.item_id))
        if not default_spec_id:
            continue

        state = product.control_state
        needs_spec = product.spec_id is not None and int(product.spec_id or 0) != int(default_spec_id)
        stale_auto_workshop = bool(
            state
            and state.workshop_id
            and str(getattr(state, "workshop_id_source", "") or "") in {"auto", "legacy"}
        )
        if not needs_spec and not stale_auto_workshop:
            continue

        blocker = _cleanliness_blocker(db, product)
        if blocker:
            blocked = stats["blocked"]
            blocked[blocker] = int(blocked.get(blocker, 0)) + 1
            continue

        if needs_spec:
            stats["local_issues_deleted"] += _delete_local_rebuildable_issues(db, int(product.product_id))
            product.spec_id = int(default_spec_id)
            stats["spec_updated"] += 1
            if state is not None:
                state.issue_status = "not_requested"
                state.material_coverage_status = None
                state.material_coverage_label = None
                state.material_coverage_calculated_at = None
                state.material_coverage_snapshot = None
                if str(state.status or "") in {"to_move", "ready", "partial"}:
                    state.status = "shortage"

        if stale_auto_workshop and state is not None:
            state.workshop_id = None
            state.workshop_id_source = None
            state.workshop_id_set_at = now
            stats["workshop_auto_cleared"] += 1

    return stats
