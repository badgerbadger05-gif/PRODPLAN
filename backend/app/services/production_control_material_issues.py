from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session, joinedload

from ..models import (
    IgnoredWarehouse,
    Item,
    ItemWarehouseStock,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrderLineState,
    ProductionProduct,
    StockWarehouse,
    SyncLink,
    WorkshopWarehouseBinding,
)
from ..schemas import ODataSyncRequest
from .production_control_common import date_to_iso as _date_to_iso, to_float as _to_float
from .production_control_domain import ensure_state as _ensure_state, unit_display as _unit_display
from .production_control_material_availability import _components_for_product
from .one_c_export_common import (
    clean_ref1c as _clean_ref1c,
    create_odata_client as _create_odata_client,
    post_document_operational as _post_document_operational,
)
from .odata_config import load_odata_config as _load_odata_config
from .odata_client import OData1CClient
from .one_c_stock_transfer_export import STOCK_TRANSFER_ENTITY


def _auto_select_source_warehouse(
    db: Session,
    component_item_ids: List[int],
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """
    Given a list of component item_ids, find the best source warehouse.

    Returns (selected_ref1c, candidates) where:
    - selected_ref1c is the auto-chosen warehouse ref (or None if ambiguous/none)
    - candidates is a list of dicts with {ref1c, name, components_covered, total_components}

    Excludes ignored_warehouses. Ignores warehouses with is_selected=False
    (those are deliberately excluded from stock accounting).

    A warehouse is "best" if it covers the most distinct components with qty>0.
    If exactly one warehouse covers the maximum — it is auto-selected.
    If there's a tie — returns None + all tied candidates so UI can ask.
    """
    if not component_item_ids:
        return None, []

    total = len(component_item_ids)

    ignored_refs = {
        str(r[0])
        for r in db.query(IgnoredWarehouse.warehouse_ref1c).all()
    }
    selected_refs = {
        str(r[0])
        for r in db.query(StockWarehouse.warehouse_ref1c)
        .filter(StockWarehouse.is_selected.is_(True))
        .all()
    }

    rows = (
        db.query(ItemWarehouseStock.warehouse_ref1c, ItemWarehouseStock.item_id)
        .filter(
            ItemWarehouseStock.item_id.in_(component_item_ids),
            ItemWarehouseStock.qty > 0,
        )
        .all()
    )

    coverage: Dict[str, set] = {}
    for wh_ref, item_id in rows:
        wh_ref = str(wh_ref)
        if wh_ref in ignored_refs:
            continue
        if selected_refs and wh_ref not in selected_refs:
            continue
        coverage.setdefault(wh_ref, set()).add(int(item_id))

    if not coverage:
        return None, []

    wh_names: Dict[str, str] = {
        str(r[0]): str(r[1] or r[0])
        for r in db.query(StockWarehouse.warehouse_ref1c, StockWarehouse.warehouse_name).all()
    }

    max_covered = max(len(v) for v in coverage.values())
    candidates = [
        {
            "ref1c": ref,
            "name": wh_names.get(ref, ref),
            "components_covered": len(covered),
            "total_components": total,
        }
        for ref, covered in coverage.items()
        if len(covered) == max_covered
    ]

    if len(candidates) == 1:
        return candidates[0]["ref1c"], candidates

    return None, candidates


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
    source_warehouse_ref1c: Optional[str] = None,
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

        # Auto-select source warehouse from per-warehouse stock breakdown.
        # Caller may override by passing source_warehouse_ref1c explicitly
        # (e.g. after showing the user a picker when ambiguous).
        component_item_ids = [int(c["component_item_id"]) for c in components]
        auto_source_wh, source_candidates = _auto_select_source_warehouse(db, component_item_ids)
        resolved_source_wh = source_warehouse_ref1c or auto_source_wh

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
            source_warehouse_ref1c=resolved_source_wh,
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
        entry: Dict[str, Any] = {
            "issue_id": int(issue.issue_id),
            "document_number": issue.document_number,
            "product_id": int(product.product_id),
            "order_number": str(product.order.order_number or ""),
            "item_name": str(product.item.item_name or ""),
            "lines_count": len(components),
            "source_warehouse_ref1c": resolved_source_wh,
        }
        if len(source_candidates) > 1:
            entry["warehouse_candidates"] = source_candidates
        created.append(entry)
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


def _issue_header(db: Session, issue: ProductionMaterialIssue) -> Dict[str, Any]:
    product = issue.product
    item = product.item if product and product.item else None
    state = (
        getattr(product, "control_state", None)
        if product is not None
        else None
    )
    return {
        "issue_id": int(issue.issue_id),
        "document_number": str(issue.document_number),
        "status": str(issue.status or ""),
        "direction": str(issue.direction or "issue"),
        "product_id": int(issue.product_id),
        "order_id": int(issue.order_id),
        "order_number": str(issue.order.order_number or "") if issue.order else "",
        "order_ref1c": str(issue.order.order_ref1c or "") if issue.order and issue.order.order_ref1c else None,
        "item_id": int(product.item_id) if product else None,
        "item_name": str(item.item_name or "") if item else "",
        "item_article": str(item.item_article or "") if item else "",
        "item_code": str(item.item_code or "") if item else "",
        "quantity": _to_float(product.quantity) if product else 0.0,
        "remaining_qty": _to_float(product.remaining_qty) if product else 0.0,
        "unit": _unit_display(db, item.unit) if item else "",
        "warehouse_ref1c": str(issue.warehouse_ref1c or ""),
        "source_warehouse_ref1c": str(issue.source_warehouse_ref1c or ""),
        "exported_ref1c": str(issue.exported_ref1c or ""),
        "exported_at": _date_to_iso(issue.exported_at),
        "created_at": _date_to_iso(issue.created_at),
        "export_error": str(issue.export_error or ""),
        "line_status": str(state.status if state else ""),
        "issue_status": str(state.issue_status if state else ""),
        "lines_count": len(issue.lines or []),
    }


def list_material_issues(
    db: Session,
    *,
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    query = (
        db.query(ProductionMaterialIssue)
        .options(
            joinedload(ProductionMaterialIssue.order),
            joinedload(ProductionMaterialIssue.product)
            .joinedload(ProductionProduct.item),
            joinedload(ProductionMaterialIssue.product)
            .joinedload(ProductionProduct.control_state),
            joinedload(ProductionMaterialIssue.lines),
        )
        .filter(ProductionMaterialIssue.direction == "issue")
    )
    if status:
        query = query.filter(ProductionMaterialIssue.status == status)
    if search:
        like = f"%{search.strip()}%"
        query = (
            query.join(ProductionProduct, ProductionProduct.product_id == ProductionMaterialIssue.product_id)
            .join(ProductionProduct.item)
            .filter(
                (ProductionMaterialIssue.document_number.ilike(like))
                | (ProductionProduct.item.has(Item.item_name.ilike(like)))
                | (ProductionProduct.item.has(Item.item_article.ilike(like)))
                | (ProductionProduct.item.has(Item.item_code.ilike(like)))
            )
        )

    total = query.count()
    effective_limit = max(1, min(int(limit or 100), 500))
    effective_offset = max(0, int(offset or 0))
    rows = (
        query.order_by(ProductionMaterialIssue.created_at.desc(), ProductionMaterialIssue.issue_id.desc())
        .offset(effective_offset)
        .limit(effective_limit)
        .all()
    )
    return {
        "rows": [_issue_header(db, issue) for issue in rows],
        "total": int(total),
        "limit": effective_limit,
        "offset": effective_offset,
    }


def assemble_material_issue(
    db: Session,
    issue_id: int,
    *,
    allow_production: bool = False,
) -> Dict[str, Any]:
    issue = (
        db.query(ProductionMaterialIssue)
        .options(
            joinedload(ProductionMaterialIssue.order),
            joinedload(ProductionMaterialIssue.product).joinedload(ProductionProduct.item),
            joinedload(ProductionMaterialIssue.lines),
        )
        .filter(ProductionMaterialIssue.issue_id == int(issue_id))
        .one_or_none()
    )
    if issue is None:
        raise ValueError("Заявка на перемещение не найдена")
    ref_key = _clean_ref1c(issue.exported_ref1c)
    if not ref_key:
        raise ValueError("Перемещение ещё не выгружено в 1С")
    if str(issue.status or "") == "posted":
        return {"status": "ok", "issue_id": int(issue.issue_id), "already_posted": True}

    client = _create_odata_client(
        _load_odata_config(),
        OData1CClient,
        allow_production=allow_production,
        require_demo_base=True,
    )
    _post_document_operational(
        client,
        entity=STOCK_TRANSFER_ENTITY,
        ref_key=ref_key,
        unpost_first=True,
    )

    issue.status = "posted"
    link = (
        db.query(SyncLink)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "material_issue",
            SyncLink.source_id == int(issue.issue_id),
            SyncLink.target_entity == STOCK_TRANSFER_ENTITY,
        )
        .one_or_none()
    )
    if link is not None:
        link.status = "posted"
        link.last_synced_at = datetime.utcnow()
    for line in issue.lines or []:
        line.issued_qty = line.required_qty
        line.line_status = "issued"
    state = (
        db.query(ProductionOrderLineState)
        .filter(ProductionOrderLineState.product_id == issue.product_id)
        .one_or_none()
    )
    if state is not None:
        if state.status in {"shortage", "partial", "ready", "to_move"}:
            state.status = "assembled"
        state.issue_status = "posted"
    db.commit()
    return {
        "status": "ok",
        "issue_id": int(issue.issue_id),
        "product_id": int(issue.product_id),
        "target_ref_key": ref_key,
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
