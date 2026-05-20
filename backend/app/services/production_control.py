from __future__ import annotations

import html
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from ..models import (
    DefaultSpecification,
    Item,
    Operation,
    PlannedOrder,
    PlanningRun,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionResource,
    ProductionStage,
    ResourceStage,
    SpecComponent,
    SpecOperation,
    Unit,
)
from ..schemas import ODataSyncRequest


DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"
LINE_STATUSES = {"new", "opened", "in_work", "waiting_materials", "done", "cancelled"}
ISSUE_STATUSES = {"not_requested", "requested", "issued", "exported", "error"}


def _norm_guid(val: Any) -> str:
    s = str(val or "").strip().lower()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1].strip()
    if s.startswith("guid'") and s.endswith("'"):
        s = s[len("guid'") : -1].strip()
    return s


def _looks_like_guid(val: Any) -> bool:
    s = _norm_guid(val)
    if len(s) != 36:
        return False
    parts = s.split("-")
    return [len(p) for p in parts] == [8, 4, 4, 4, 12] and all(
        all(ch in "0123456789abcdef" for ch in part) for part in parts
    )


def _unit_display(db: Session, raw_unit: Any) -> str:
    raw = str(raw_unit or "").strip()
    if not raw:
        return ""
    unit = db.query(Unit).filter(Unit.unit_ref1c == raw).first()
    if unit:
        return str(unit.short_name or unit.unit_name or unit.unit_code or "").strip()
    return "" if _looks_like_guid(raw) else raw


def _to_float(val: Any) -> float:
    try:
        return float(val or 0.0)
    except Exception:
        return 0.0


def _date_to_iso(val: Any) -> Optional[str]:
    if not val:
        return None
    if hasattr(val, "date") and not isinstance(val, date):
        val = val.date()
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val).split("T")[0].split(" ")[0]


def _parse_date(val: Any) -> Optional[date]:
    if not val:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    try:
        return date.fromisoformat(str(val)[:10])
    except Exception:
        return None


def _line_number(product: ProductionProduct) -> int:
    try:
        return int(product.line_number or product.product_id or 0)
    except Exception:
        return 0


def _ensure_state(db: Session, product: ProductionProduct) -> ProductionOrderLineState:
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
        status="new",
        issue_status="not_requested",
    )
    db.add(state)
    db.flush()
    return state


def _default_spec_id(db: Session, product: ProductionProduct) -> Optional[int]:
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


def _main_workshop_for_spec(db: Session, spec_id: Optional[int]) -> Tuple[Optional[int], Optional[str], Optional[int], Optional[str]]:
    if not spec_id:
        return (None, None, None, None)

    stage_hours = (
        db.query(SpecOperation.stage_id, func.sum(SpecOperation.time_norm).label("hours"))
        .filter(SpecOperation.spec_id == spec_id, SpecOperation.stage_id.isnot(None))
        .group_by(SpecOperation.stage_id)
        .all()
    )
    stage_id: Optional[int] = None
    if stage_hours:
        stage_id = int(max(stage_hours, key=lambda r: _to_float(r.hours)).stage_id)
    else:
        comp_stage = (
            db.query(SpecComponent.stage_id)
            .filter(SpecComponent.spec_id == spec_id, SpecComponent.stage_id.isnot(None))
            .first()
        )
        if comp_stage:
            stage_id = int(comp_stage.stage_id)

    stage_name: Optional[str] = None
    if stage_id:
        stage = db.query(ProductionStage).filter(ProductionStage.stage_id == stage_id).first()
        stage_name = str(stage.stage_name) if stage else None

    workshop_id: Optional[int] = None
    workshop_name: Optional[str] = None
    if stage_id:
        resource_stage = (
            db.query(ResourceStage)
            .options(joinedload(ResourceStage.resource))
            .filter(ResourceStage.stage_id == stage_id)
            .order_by(ResourceStage.id.asc())
            .first()
        )
        if resource_stage and resource_stage.resource:
            workshop_id = int(resource_stage.resource_id)
            workshop_name = str(resource_stage.resource.resource_name)

    return (workshop_id, workshop_name, stage_id, stage_name)


def _latest_run_id(db: Session) -> Optional[int]:
    row = (
        db.query(PlanningRun)
        .filter(PlanningRun.status.in_(["DONE", "SUCCESS", "FINISHED", "COMPLETED"]))
        .order_by(PlanningRun.finished_at.desc().nullslast(), PlanningRun.run_id.desc())
        .first()
    )
    if not row:
        row = db.query(PlanningRun).order_by(PlanningRun.run_id.desc()).first()
    return int(row.run_id) if row else None


def _planned_dates_by_item(db: Session, run_id: Optional[int]) -> Dict[int, Tuple[Optional[date], Optional[date]]]:
    if not run_id:
        return {}
    rows = (
        db.query(
            PlannedOrder.item_id,
            func.min(PlannedOrder.start_date).label("start_date"),
            func.max(PlannedOrder.finish_date).label("finish_date"),
        )
        .filter(PlannedOrder.run_id == run_id)
        .group_by(PlannedOrder.item_id)
        .all()
    )
    return {int(r.item_id): (r.start_date, r.finish_date) for r in rows}


def list_journal(
    db: Session,
    *,
    workshop_id: Optional[int] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    run_id = _latest_run_id(db)
    plan_dates = _planned_dates_by_item(db, run_id)

    query = (
        db.query(ProductionProduct)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .join(Item, Item.item_id == ProductionProduct.item_id)
        .outerjoin(ProductionOrderLineState, ProductionOrderLineState.product_id == ProductionProduct.product_id)
        .filter(ProductionOrder.deletion_mark == False)
        .filter(or_(ProductionOrder.order_state_key.is_(None), func.lower(ProductionOrder.order_state_key) != DONE_STATE_KEY))
        .filter(func.coalesce(ProductionProduct.remaining_qty, ProductionProduct.quantity) > 0)
        .options(
            joinedload(ProductionProduct.order),
            joinedload(ProductionProduct.item),
            joinedload(ProductionProduct.control_state).joinedload(ProductionOrderLineState.workshop),
        )
    )

    if status:
        query = query.filter(func.coalesce(ProductionOrderLineState.status, "new") == status)
    if search:
        like = f"%{search.strip()}%"
        query = query.filter(
            or_(
                ProductionOrder.order_number.ilike(like),
                Item.item_name.ilike(like),
                Item.item_article.ilike(like),
                Item.item_code.ilike(like),
            )
        )
    start = _parse_date(date_from)
    finish = _parse_date(date_to)
    if start:
        query = query.filter(ProductionOrder.order_date >= datetime.combine(start, datetime.min.time()))
    if finish:
        query = query.filter(ProductionOrder.order_date < datetime.combine(finish, datetime.max.time()))

    rows = query.order_by(ProductionOrder.order_date.desc(), ProductionOrder.order_number.asc(), ProductionProduct.line_number.asc()).all()

    result: List[Dict[str, Any]] = []
    for product in rows:
        state = getattr(product, "control_state", None)
        spec_id = _default_spec_id(db, product)
        inferred_workshop_id, inferred_workshop_name, stage_id, stage_name = _main_workshop_for_spec(db, spec_id)
        state_workshop_id = int(state.workshop_id) if state and state.workshop_id else None
        resolved_workshop_id = state_workshop_id or inferred_workshop_id
        if workshop_id and resolved_workshop_id != int(workshop_id):
            continue

        planned_start, planned_finish = plan_dates.get(int(product.item_id), (None, None))
        if state and state.planned_start_date:
            planned_start = state.planned_start_date
        if state and state.planned_finish_date:
            planned_finish = state.planned_finish_date

        issue_count = db.query(ProductionMaterialIssue).filter(ProductionMaterialIssue.product_id == product.product_id).count()
        result.append(
            {
                "product_id": int(product.product_id),
                "order_id": int(product.order_id),
                "order_number": str(product.order.order_number or ""),
                "order_date": _date_to_iso(product.order.order_date),
                "line_number": product.line_number,
                "item_id": int(product.item_id),
                "item_code": str(product.item.item_code or ""),
                "item_name": str(product.item.item_name or ""),
                "item_article": str(product.item.item_article or ""),
                "unit": _unit_display(db, product.item.unit),
                "quantity": _to_float(product.quantity),
                "produced_qty": _to_float(product.produced_qty),
                "remaining_qty": _to_float(product.remaining_qty),
                "status": str(state.status if state else "new"),
                "issue_status": str(state.issue_status if state else "not_requested"),
                "planned_start_date": _date_to_iso(planned_start),
                "planned_finish_date": _date_to_iso(planned_finish),
                "opened_at": _date_to_iso(state.opened_at) if state else None,
                "workshop_id": resolved_workshop_id,
                "workshop_name": (state.workshop.resource_name if state and state.workshop else inferred_workshop_name),
                "stage_id": stage_id,
                "stage_name": stage_name,
                "spec_id": spec_id,
                "issue_count": int(issue_count),
                "route_sheet_printed_at": _date_to_iso(state.route_sheet_printed_at) if state else None,
                "comment": str(state.comment or "") if state else "",
            }
        )

    total = len(result)
    effective_limit = max(1, min(int(limit or 100), 500))
    effective_offset = max(0, int(offset or 0))
    return {
        "rows": result[effective_offset : effective_offset + effective_limit],
        "total": total,
        "limit": effective_limit,
        "offset": effective_offset,
        "latest_run_id": run_id,
    }


def update_line_state(db: Session, product_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    product = db.query(ProductionProduct).filter(ProductionProduct.product_id == int(product_id)).first()
    if not product:
        raise ValueError("Строка заказа не найдена")

    state = _ensure_state(db, product)
    if "status" in payload and payload.get("status"):
        status = str(payload.get("status")).strip()
        if status not in LINE_STATUSES:
            raise ValueError(f"Недопустимый статус: {status}")
        state.status = status
        if status == "opened" and not state.opened_at:
            state.opened_at = datetime.utcnow()
    if "issue_status" in payload and payload.get("issue_status"):
        issue_status = str(payload.get("issue_status")).strip()
        if issue_status not in ISSUE_STATUSES:
            raise ValueError(f"Недопустимый статус выдачи: {issue_status}")
        state.issue_status = issue_status
    if "workshop_id" in payload:
        state.workshop_id = int(payload["workshop_id"]) if payload.get("workshop_id") else None
    if "planned_start_date" in payload:
        state.planned_start_date = _parse_date(payload.get("planned_start_date"))
    if "planned_finish_date" in payload:
        state.planned_finish_date = _parse_date(payload.get("planned_finish_date"))
    if "comment" in payload:
        state.comment = str(payload.get("comment") or "")

    db.commit()
    return {"status": "ok", "product_id": int(product_id)}


def _components_for_product(db: Session, product: ProductionProduct) -> Tuple[Optional[int], List[Dict[str, Any]]]:
    spec_id = _default_spec_id(db, product)
    if not spec_id:
        return None, []
    rows = (
        db.query(SpecComponent, Item)
        .join(Item, Item.item_id == SpecComponent.item_id)
        .filter(SpecComponent.spec_id == spec_id)
        .order_by(Item.item_name.asc())
        .all()
    )
    qty = _to_float(product.remaining_qty) or _to_float(product.quantity)
    components: List[Dict[str, Any]] = []
    for comp, item in rows:
        required = _to_float(comp.quantity) * qty
        if required <= 0:
            continue
        components.append(
            {
                "component_item_id": int(item.item_id),
                "item_code": str(item.item_code or ""),
                "item_name": str(item.item_name or ""),
                "item_article": str(item.item_article or ""),
                "unit": _unit_display(db, item.unit),
                "qty_per_unit": _to_float(comp.quantity),
                "required_qty": required,
                "source_spec_id": spec_id,
            }
        )
    return spec_id, components


def preview_materials(db: Session, product_id: int) -> Dict[str, Any]:
    product = (
        db.query(ProductionProduct)
        .options(joinedload(ProductionProduct.order), joinedload(ProductionProduct.item))
        .filter(ProductionProduct.product_id == int(product_id))
        .first()
    )
    if not product:
        raise ValueError("Строка заказа не найдена")
    spec_id, components = _components_for_product(db, product)
    return {
        "product_id": int(product.product_id),
        "order_number": str(product.order.order_number or ""),
        "item_name": str(product.item.item_name or ""),
        "item_article": str(product.item.item_article or ""),
        "qty": _to_float(product.remaining_qty) or _to_float(product.quantity),
        "spec_id": spec_id,
        "components": components,
    }


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
    created: List[Dict[str, Any]] = []
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
        spec_id, components = _components_for_product(db, product)
        if not components:
            errors.append(f"product_id={pid}: не найдена спецификация или материалы")
            continue
        issue = ProductionMaterialIssue(
            document_number=_next_issue_number(db),
            product_id=int(product.product_id),
            order_id=int(product.order_id),
            status="draft",
            warehouse_ref1c=warehouse_ref1c,
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
        if state.status == "new":
            state.status = "waiting_materials"
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
    return {"status": "ok", "created": created, "errors": errors}


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


def mark_route_sheets_printed(db: Session, product_ids: Iterable[int]) -> int:
    count = 0
    for pid in product_ids:
        product = db.query(ProductionProduct).filter(ProductionProduct.product_id == int(pid)).first()
        if not product:
            continue
        state = _ensure_state(db, product)
        state.route_sheet_printed_at = datetime.utcnow()
        count += 1
    db.commit()
    return count


def _operation_rows(db: Session, spec_id: Optional[int]) -> List[Dict[str, Any]]:
    if not spec_id:
        return []
    rows = (
        db.query(SpecOperation, ProductionStage, Operation)
        .outerjoin(ProductionStage, ProductionStage.stage_id == SpecOperation.stage_id)
        .outerjoin(Operation, Operation.operation_id == SpecOperation.operation_id)
        .filter(SpecOperation.spec_id == spec_id)
        .order_by(SpecOperation.spec_operation_id.asc())
        .all()
    )
    return [
        {
            "number": idx + 1,
            "stage_name": str(stage.stage_name or "") if stage else "",
            "operation_name": str(operation.operation_name or "") if operation else "",
            "time_norm": _to_float(op.time_norm),
        }
        for idx, (op, stage, operation) in enumerate(rows)
    ]


def render_route_sheets_html(db: Session, product_ids: Sequence[int]) -> str:
    products = (
        db.query(ProductionProduct)
        .options(joinedload(ProductionProduct.order), joinedload(ProductionProduct.item))
        .filter(ProductionProduct.product_id.in_([int(x) for x in product_ids]))
        .all()
    )
    product_map = {int(p.product_id): p for p in products}
    ordered = [product_map[int(pid)] for pid in product_ids if int(pid) in product_map]
    now = datetime.now().strftime("%d.%m.%Y")
    sheets: List[str] = []
    for product in ordered:
        spec_id, components = _components_for_product(db, product)
        operations = _operation_rows(db, spec_id)
        order_date = _date_to_iso(product.order.order_date) or ""
        title = f"МАРШРУТНЫЙ ЛИСТ № {html.escape(str(product.order.order_number or ''))}/{_line_number(product)} от {now}"
        component_rows = "".join(
            "<tr>"
            f"<td>{html.escape(c['item_name'])}</td>"
            f"<td>{html.escape(c['item_article'])}</td>"
            f"<td class='num'>{c['qty_per_unit']:.3f}</td>"
            f"<td class='num'>{c['required_qty']:.3f}</td>"
            "</tr>"
            for c in components
        ) or "<tr><td colspan='4'>Материалы по спецификации не найдены</td></tr>"
        op_rows = "".join(
            "<tr>"
            f"<td class='num'>{op['number']}</td>"
            f"<td>{html.escape(op['stage_name'])}</td>"
            f"<td>{html.escape(op['operation_name'] or op['stage_name'] or 'Операция')}</td>"
            f"<td class='num'>{op['time_norm']:.3f}</td>"
            "<td></td><td></td><td></td>"
            "</tr>"
            for op in operations
        ) or "<tr><td colspan='7'>Операции по спецификации не найдены</td></tr>"
        sheets.append(
            f"""
            <section class="sheet">
              <table class="route">
                <tr>
                  <td colspan="4" class="title">{title}<br><span>(Изготовление новых)</span></td>
                  <td colspan="3" class="order">Заказ на производство №{html.escape(str(product.order.order_number or ""))}<br>Дата заказа: {html.escape(order_date)}</td>
                </tr>
                <tr>
                  <td colspan="3"><b>Наименование:</b><br>{html.escape(str(product.item.item_name or ""))}</td>
                  <td colspan="2"><b>Артикул:</b><br>{html.escape(str(product.item.item_article or ""))}</td>
                  <td colspan="2"><b>Количество:</b><br>{_to_float(product.remaining_qty) or _to_float(product.quantity):g} {html.escape(_unit_display(db, product.item.unit))}</td>
                </tr>
                <tr><td colspan="7"><b>Материалы и заготовки</b></td></tr>
                <tr><th colspan="2">Материал</th><th>Артикул</th><th>Кол-во на ед.</th><th colspan="3">Кол-во по заказу</th></tr>
                {component_rows}
                <tr><th>№</th><th>Цех / участок</th><th colspan="2">Операция</th><th>Трудоемкость</th><th>Исполнитель</th><th>ОТК</th></tr>
                {op_rows}
                <tr><td colspan="7" class="notes"><b>Дополнительная информация:</b><br><br><br><br></td></tr>
              </table>
            </section>
            """
        )
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Маршрутные листы</title>
  <style>
    @page {{ size: A4 landscape; margin: 8mm; }}
    body {{ font-family: "Times New Roman", serif; color: #000; margin: 0; }}
    .toolbar {{ position: sticky; top: 0; padding: 8px; background: #f4f6f8; border-bottom: 1px solid #cfd8dc; font-family: Arial, sans-serif; }}
    .toolbar button {{ padding: 6px 12px; }}
    .sheet {{ page-break-after: always; padding: 6px; }}
    table.route {{ border-collapse: collapse; width: 100%; font-size: 15px; }}
    .route td, .route th {{ border: 1px solid #000; padding: 4px; vertical-align: top; }}
    .title {{ font-size: 22px; line-height: 1.25; }}
    .title span {{ font-size: 20px; }}
    .order {{ font-size: 18px; vertical-align: middle; }}
    th {{ text-align: center; font-weight: bold; }}
    .num {{ text-align: center; white-space: nowrap; }}
    .notes {{ height: 90px; }}
    @media print {{ .toolbar {{ display: none; }} .sheet {{ padding: 0; }} }}
  </style>
</head>
<body>
  <div class="toolbar"><button onclick="window.print()">Печать</button> <span>Листов: {len(sheets)}</span></div>
  {''.join(sheets)}
</body>
</html>"""
