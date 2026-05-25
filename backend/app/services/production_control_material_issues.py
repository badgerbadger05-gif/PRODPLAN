from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session, joinedload

from ..models import (
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrderLineState,
    ProductionProduct,
    WorkshopWarehouseBinding,
)
from ..schemas import ODataSyncRequest
from .production_control_common import date_to_iso as _date_to_iso, to_float as _to_float
from .production_control_domain import ensure_state as _ensure_state, unit_display as _unit_display
from .production_control_material_availability import _components_for_product


def _next_issue_number(db: Session) -> str:
    today = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"MI-{today}-"
    count = db.query(ProductionMaterialIssue).filter(ProductionMaterialIssue.document_number.like(f"{prefix}%")).count()
    return f"{prefix}{count + 1:04d}"


def create_material_issues(
    db: Session,
    product_ids: Sequence[int],
    *,
    initiated_by: Optional[str] = None,
    warehouse_ref1c: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Idempotent per the plan: a repeated click on "prepare issue" for the same
    production line must not create a duplicate document.

    If an active (draft|requested) ProductionMaterialIssue already exists for
    the product, return its descriptor in `reused` instead of creating a new
    one. Issues already exported to 1C or in error state are treated as
    archived вЂ” a fresh draft can be created in their place.
    """
    created: List[Dict[str, Any]] = []
    reused: List[Dict[str, Any]] = []
    errors: List[str] = []
    for pid in product_ids:
        product = (
            db.query(ProductionProduct)
            .options(joinedload(ProductionProduct.order), joinedload(ProductionProduct.item))
            .filter(ProductionProduct.product_id == int(pid))
            .first()
        )
        if not product:
            errors.append(f"product_id={pid}: строка заказа не найдена")
            continue

        existing = (
            db.query(ProductionMaterialIssue)
            .filter(
                ProductionMaterialIssue.product_id == int(product.product_id),
                ProductionMaterialIssue.status.in_(("draft", "requested")),
            )
            .order_by(ProductionMaterialIssue.issue_id.desc())
            .first()
        )
        if existing is not None:
            reused.append(
                {
                    "issue_id": int(existing.issue_id),
                    "document_number": str(existing.document_number),
                    "product_id": int(product.product_id),
                    "order_number": str(product.order.order_number or ""),
                    "item_name": str(product.item.item_name or ""),
                    "status": str(existing.status),
                }
            )
            continue

        spec_id, components = _components_for_product(db, product)
        if not components:
            errors.append(f"product_id={pid}: не найдена спецификация или материалы")
            continue

        # If the caller did not pin a destination warehouse, fall back to the
        # workshop->warehouse binding from settings. Plan rule:
        # "привязка участок -> склад получатель".
        resolved_warehouse = warehouse_ref1c
        if not resolved_warehouse:
            state_obj = (
                db.query(ProductionOrderLineState)
                .filter(ProductionOrderLineState.product_id == int(product.product_id))
                .first()
            )
            workshop_id_resolved: Optional[int] = (
                int(state_obj.workshop_id) if state_obj and state_obj.workshop_id else None
            )
            if workshop_id_resolved:
                binding = (
                    db.query(WorkshopWarehouseBinding)
                    .filter(WorkshopWarehouseBinding.workshop_id == workshop_id_resolved)
                    .first()
                )
                if binding:
                    resolved_warehouse = str(binding.warehouse_ref1c)

        issue = ProductionMaterialIssue(
            document_number=_next_issue_number(db),
            product_id=int(product.product_id),
            order_id=int(product.order_id),
            status="draft",
            warehouse_ref1c=resolved_warehouse,
            initiated_by=initiated_by,
        )
        db.add(issue)
        db.flush()
        for comp in components:
            db.add(
                ProductionMaterialIssueLine(
                    issue_id=int(issue.issue_id),
                    component_item_id=int(comp["component_item_id"]),
                    required_qty=float(comp["required_qty"]),
                    issued_qty=0.0,
                    unit=comp.get("unit"),
                    source_spec_id=spec_id,
                    line_status="planned",
                )
            )
        state = _ensure_state(db, product)
        state.issue_status = "requested"
        # Once a material-issue draft is open, the line has moved beyond the
        # "no coverage yet" phase. Bump status to 'to_move' (документы созданы,
        # ждём проведения) unless it's already further along.
        if state.status in {"shortage", "partial", "ready"}:
            state.status = "to_move"
        created.append(
            {
                "issue_id": int(issue.issue_id),
                "document_number": issue.document_number,
                "product_id": int(product.product_id),
                "order_number": str(product.order.order_number or ""),
                "item_name": str(product.item.item_name or ""),
                "lines_count": len(components),
            }
        )
    db.commit()
    return {"status": "ok", "created": created, "reused": reused, "errors": errors}


def get_issue(db: Session, issue_id: int) -> Dict[str, Any]:
    issue = (
        db.query(ProductionMaterialIssue)
        .options(
            joinedload(ProductionMaterialIssue.order),
            joinedload(ProductionMaterialIssue.product).joinedload(ProductionProduct.item),
            joinedload(ProductionMaterialIssue.lines).joinedload(ProductionMaterialIssueLine.component_item),
        )
        .filter(ProductionMaterialIssue.issue_id == int(issue_id))
        .first()
    )
    if not issue:
        raise ValueError("Документ выдачи не найден")
    return {
        "issue_id": int(issue.issue_id),
        "document_number": str(issue.document_number),
        "status": str(issue.status),
        "warehouse_ref1c": str(issue.warehouse_ref1c or ""),
        "initiated_by": str(issue.initiated_by or ""),
        "order_number": str(issue.order.order_number or ""),
        "product_id": int(issue.product_id),
        "item_name": str(issue.product.item.item_name or "") if issue.product and issue.product.item else "",
        "item_article": str(issue.product.item.item_article or "") if issue.product and issue.product.item else "",
        "created_at": _date_to_iso(issue.created_at),
        "exported_ref1c": str(issue.exported_ref1c or ""),
        "export_error": str(issue.export_error or ""),
        "lines": [
            {
                "line_id": int(line.line_id),
                "component_item_id": int(line.component_item_id),
                "item_code": str(line.component_item.item_code or ""),
                "item_name": str(line.component_item.item_name or ""),
                "item_article": str(line.component_item.item_article or ""),
                "required_qty": _to_float(line.required_qty),
                "issued_qty": _to_float(line.issued_qty),
                "unit": _unit_display(db, line.unit or line.component_item.unit),
                "line_status": str(line.line_status),
            }
            for line in sorted(issue.lines, key=lambda x: x.line_id)
        ],
    }


def build_issue_1c_payload(db: Session, issue_id: int) -> Dict[str, Any]:
    issue_data = get_issue(db, issue_id)
    issue = (
        db.query(ProductionMaterialIssue)
        .options(
            joinedload(ProductionMaterialIssue.order),
            joinedload(ProductionMaterialIssue.product).joinedload(ProductionProduct.item),
            joinedload(ProductionMaterialIssue.lines).joinedload(ProductionMaterialIssueLine.component_item),
        )
        .filter(ProductionMaterialIssue.issue_id == int(issue_id))
        .first()
    )
    if not issue:
        raise ValueError("Документ выдачи не найден")

    return {
        "Number": str(issue.document_number),
        "Date": datetime.utcnow().replace(microsecond=0).isoformat(),
        "Posted": False,
        "Комментарий": f"PRODPLAN: выдача под заказ {issue.order.order_number}, строка {issue.product.line_number or issue.product_id}",
        "ЗаказНаПроизводство_Key": str(issue.order.order_ref1c or ""),
        "Склад_Key": str(issue.warehouse_ref1c or ""),
        "Продукция_Key": str(issue.product.item.item_ref1c or "") if issue.product and issue.product.item else "",
        "ПродукцияКоличество": _to_float(issue.product.remaining_qty) or _to_float(issue.product.quantity),
        "Материалы": [
            {
                "LineNumber": idx + 1,
                "Номенклатура_Key": str(line.component_item.item_ref1c or ""),
                "Количество": _to_float(line.required_qty),
                "Единица": _unit_display(db, line.unit or line.component_item.unit),
            }
            for idx, line in enumerate(sorted(issue.lines, key=lambda x: x.line_id))
        ],
        "_prodplan": {
            "issue_id": issue_data["issue_id"],
            "document_number": issue_data["document_number"],
            "product_id": issue_data["product_id"],
        },
    }


def export_issue_to_1c(db: Session, issue_id: int, req: ODataSyncRequest) -> Dict[str, Any]:
    issue = db.query(ProductionMaterialIssue).filter(ProductionMaterialIssue.issue_id == int(issue_id)).first()
    if not issue:
        raise ValueError("Документ выдачи не найден")
    payload = build_issue_1c_payload(db, issue_id)

    if req.dry_run:
        return {
            "status": "dry_run",
            "entity_name": req.entity_name,
            "payload": payload,
        }

    from ..services.odata_client import OData1CClient

    client = OData1CClient(req.base_url, req.username, req.password, req.token)
    try:
        response = client.post(req.entity_name, payload, timeout=120)
        ref = str(response.get("Ref_Key") or response.get("ref") or response.get("Ref") or "")
        issue.status = "exported"
        issue.exported_ref1c = ref or None
        issue.exported_at = datetime.utcnow()
        issue.export_error = None
        state = (
            db.query(ProductionOrderLineState)
            .filter(ProductionOrderLineState.product_id == issue.product_id)
            .first()
        )
        if state:
            state.issue_status = "exported"
        db.commit()
        return {
            "status": "ok",
            "issue_id": int(issue.issue_id),
            "document_number": str(issue.document_number),
            "exported_ref1c": ref,
            "response": response,
        }
    except Exception as e:
        issue.status = "error"
        issue.export_error = str(e)
        state = (
            db.query(ProductionOrderLineState)
            .filter(ProductionOrderLineState.product_id == issue.product_id)
            .first()
        )
        if state:
            state.issue_status = "error"
        db.commit()
        raise
