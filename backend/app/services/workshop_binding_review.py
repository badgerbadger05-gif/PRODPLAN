"""Manual-review list of parts whose workshop binding does not resolve.

Since routing goes exclusively through the spec's production kind
(workshop_resolution.py), parts with an unfilled kind / unbound kind /
missing workshop warehouse no longer get a workshop silently. This service
feeds the "Разбор привязок" page: which parts are affected, why, what to do,
and a stage-chain suggestion for the manual fix.

Scopes:
* "active"  — parts present on non-terminal production journal lines (plus
  lines fixed by a manual workshop assignment are checked for the warehouse
  binding only).
* "catalog" — every item that has a default specification, regardless of
  orders. Items without a default spec are not listed here: purchased items
  legitimately have no spec, so NO_SPEC is only reported for items that
  actually appear in production (active scope).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..models import (
    DefaultSpecification,
    Item,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionResource,
)
from .production_control_common import to_float as _to_float
from .production_output_truth import accepted_product_output
from .workshop_resolution import (
    PROBLEM_REASON_CODES,
    REASON_NO_WAREHOUSE_BINDING,
    REASON_OK,
    WorkshopDiagnosis,
    diagnose_specs,
    no_spec_diagnosis,
    reason_text_for,
    recommendation_for,
    warehouse_binding_for_workshop,
)

# Mirrors production_control_journal._TERMINAL_LINE_STATUSES without importing
# the heavy journal module.
_TERMINAL_LINE_STATUSES = ("completed", "cancelled")
_DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"


def _diagnosis_payload(diagnosis: WorkshopDiagnosis) -> Dict[str, Any]:
    return {
        "reason_code": diagnosis.reason_code,
        "reason_text": diagnosis.reason_text,
        "recommendation": diagnosis.recommendation,
        "workshop_id": diagnosis.workshop_id,
        "spec_id": diagnosis.spec_id,
        "spec_name": diagnosis.spec_name,
        "production_kind_id": diagnosis.production_kind_id,
        "production_kind_name": diagnosis.production_kind_name,
        "suggested_resource_id": diagnosis.suggested_resource_id,
        "suggested_resource_name": diagnosis.suggested_resource_name,
        "suggested_stage_id": diagnosis.suggested_stage_id,
        "suggested_stage_name": diagnosis.suggested_stage_name,
    }


def _items_by_id(db: Session, item_ids: List[int]) -> Dict[int, Item]:
    if not item_ids:
        return {}
    return {
        int(item.item_id): item
        for item in db.query(Item).filter(Item.item_id.in_(item_ids)).all()
    }


def _manual_binding_diagnoses(
    db: Session, workshop_ids: List[int]
) -> Dict[int, Optional[WorkshopDiagnosis]]:
    """For manually assigned lines only the warehouse binding can be missing.
    Returns {workshop_id: diagnosis or None when the binding is fine}."""
    result: Dict[int, Optional[WorkshopDiagnosis]] = {}
    for workshop_id in sorted({int(w) for w in workshop_ids if w}):
        binding = warehouse_binding_for_workshop(db, workshop_id)
        if binding:
            result[workshop_id] = None
            continue
        resource = (
            db.query(ProductionResource.resource_name)
            .filter(ProductionResource.resource_id == workshop_id)
            .first()
        )
        workshop_name = str(resource[0] or "") if resource else ""
        result[workshop_id] = WorkshopDiagnosis(
            status="problem",
            reason_code=REASON_NO_WAREHOUSE_BINDING,
            reason_text=reason_text_for(REASON_NO_WAREHOUSE_BINDING, workshop_name=workshop_name),
            recommendation=recommendation_for(REASON_NO_WAREHOUSE_BINDING),
            workshop_id=workshop_id,
            workshop_source="state",
        )
    return result


def _catalog_problem_rows(db: Session) -> List[Dict[str, Any]]:
    from .workshop_resolution import default_spec_ids_for_items

    item_ids = [
        int(item_id)
        for (item_id,) in db.query(DefaultSpecification.item_id).distinct().all()
    ]
    pairs = default_spec_ids_for_items(db, item_ids)
    if not pairs:
        return []

    diagnoses = diagnose_specs(db, list(set(pairs.values())))
    problem_items: Dict[int, WorkshopDiagnosis] = {}
    for item_id, spec_id in pairs.items():
        diagnosis = diagnoses.get(spec_id)
        if diagnosis is not None and diagnosis.status == "problem":
            problem_items[item_id] = diagnosis

    items = _items_by_id(db, list(problem_items.keys()))
    rows: List[Dict[str, Any]] = []
    for item_id, diagnosis in problem_items.items():
        item = items.get(item_id)
        if item is None:
            continue
        rows.append(
            {
                "item_id": item_id,
                "item_code": str(item.item_code or ""),
                "item_name": str(item.item_name or ""),
                "item_article": str(item.item_article or ""),
                "active_lines": 0,
                **_diagnosis_payload(diagnosis),
            }
        )
    return rows


def _active_lines_query(db: Session):
    return (
        db.query(ProductionProduct, ProductionOrder, ProductionOrderLineState)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .outerjoin(
            ProductionOrderLineState,
            ProductionOrderLineState.product_id == ProductionProduct.product_id,
        )
        .filter(ProductionOrder.deletion_mark == False)  # noqa: E712
        .filter(
            or_(
                ProductionOrder.order_state_key.is_(None),
                func.lower(ProductionOrder.order_state_key) != _DONE_STATE_KEY,
            )
        )
        .filter(
            func.coalesce(ProductionOrderLineState.status, "shortage").notin_(
                _TERMINAL_LINE_STATUSES
            )
        )
    )


def _active_problem_rows(db: Session) -> List[Dict[str, Any]]:
    lines = _active_lines_query(db).all()
    if not lines:
        return []

    item_ids = sorted({int(product.item_id) for product, _o, _s in lines})
    from .workshop_resolution import default_spec_ids_for_items

    default_specs = default_spec_ids_for_items(db, item_ids)

    spec_ids = set()
    manual_workshops = set()
    for product, _order, state in lines:
        if state is not None and state.workshop_id and str(getattr(state, "workshop_id_source", "") or "") not in {"auto", "legacy"}:
            manual_workshops.add(int(state.workshop_id))
            continue
        spec_id = int(product.spec_id) if product.spec_id else default_specs.get(int(product.item_id))
        if spec_id:
            spec_ids.add(spec_id)

    spec_diagnoses = diagnose_specs(db, list(spec_ids))
    manual_diagnoses = _manual_binding_diagnoses(db, list(manual_workshops))

    per_item: Dict[int, Dict[str, Any]] = {}
    for product, _order, state in lines:
        item_id = int(product.item_id)
        if state is not None and state.workshop_id and str(getattr(state, "workshop_id_source", "") or "") not in {"auto", "legacy"}:
            diagnosis = manual_diagnoses.get(int(state.workshop_id))
        else:
            spec_id = int(product.spec_id) if product.spec_id else default_specs.get(item_id)
            if not spec_id:
                diagnosis = no_spec_diagnosis()
            else:
                diagnosis = spec_diagnoses.get(spec_id) or no_spec_diagnosis()
            if diagnosis.status != "problem":
                diagnosis = None
        if diagnosis is None:
            continue
        entry = per_item.setdefault(item_id, {"diagnosis": diagnosis, "lines": 0})
        entry["lines"] += 1

    items = _items_by_id(db, list(per_item.keys()))
    rows: List[Dict[str, Any]] = []
    for item_id, entry in per_item.items():
        item = items.get(item_id)
        if item is None:
            continue
        diagnosis: WorkshopDiagnosis = entry["diagnosis"]
        rows.append(
            {
                "item_id": item_id,
                "item_code": str(item.item_code or ""),
                "item_name": str(item.item_name or ""),
                "item_article": str(item.item_article or ""),
                "active_lines": int(entry["lines"]),
                **_diagnosis_payload(diagnosis),
            }
        )
    return rows


def list_review_items(
    db: Session,
    *,
    scope: str = "active",
    search: Optional[str] = None,
    reason_code: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    scope = (scope or "active").strip().lower()
    if scope not in ("active", "catalog"):
        raise ValueError("scope должен быть 'active' или 'catalog'")
    if reason_code and reason_code not in PROBLEM_REASON_CODES:
        raise ValueError(f"Недопустимый reason_code: {reason_code}")

    rows = _active_problem_rows(db) if scope == "active" else _catalog_problem_rows(db)

    counts_by_reason: Dict[str, int] = {}
    for row in rows:
        counts_by_reason[row["reason_code"]] = counts_by_reason.get(row["reason_code"], 0) + 1

    needle = (search or "").strip().lower()
    if needle:
        rows = [
            row
            for row in rows
            if needle in row["item_name"].lower()
            or needle in row["item_article"].lower()
            or needle in row["item_code"].lower()
        ]
    if reason_code:
        rows = [row for row in rows if row["reason_code"] == reason_code]

    rows.sort(key=lambda row: (-int(row["active_lines"]), row["item_name"]))
    total = len(rows)
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 500))
    page = rows[offset : offset + limit]

    return {
        "items": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "scope": scope,
        "counts_by_reason": counts_by_reason,
    }


def list_item_lines(db: Session, item_id: int) -> Dict[str, Any]:
    """Active journal lines of one item — targets for manual assignment."""
    lines = (
        _active_lines_query(db)
        .filter(ProductionProduct.item_id == int(item_id))
        .order_by(ProductionProduct.product_id.asc())
        .all()
    )
    rows: List[Dict[str, Any]] = []
    for product, order, state in lines:
        rows.append(
            {
                "product_id": int(product.product_id),
                "order_id": int(order.order_id),
                "order_number": str(order.order_number or ""),
                "quantity": _to_float(product.quantity),
                "remaining_qty": float(
                    accepted_product_output(product).remaining_qty
                ),
                "status": str(state.status) if state else "shortage",
                "workshop_id": int(state.workshop_id) if state and state.workshop_id else None,
                "planned_start_date": (
                    state.planned_start_date.isoformat()
                    if state and state.planned_start_date
                    else None
                ),
            }
        )
    return {"item_id": int(item_id), "rows": rows, "total": len(rows)}
