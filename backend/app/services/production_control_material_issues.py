from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from ..models import (
    IgnoredWarehouse,
    Item,
    ItemWarehouseStock,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ResourceStage,
    SpecComponent,
    SpecOperation,
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
from .one_c_document_numbers import material_issue_number

PRODUCTION_ORDER_ENTITY = "Document_ЗаказНаПроизводство"


def _clean_odata_error_message(error: Exception) -> str:
    raw = str(error or "")
    match = re.search(r"Details:\s*(\{.*\})", raw, flags=re.S)
    if match:
        try:
            data = json.loads(match.group(1))
            value = (
                data.get("odata.error", {})
                .get("message", {})
                .get("value")
            )
            if value:
                return str(value)
        except Exception:
            pass
    if "URL:" in raw:
        raw = raw.split("URL:", 1)[0].strip()
    return raw.strip("<> ") or "1С отказала в проведении перемещения"


def _auto_select_source_warehouse(
    db: Session,
    component_item_ids: List[int],
    *,
    excluded_refs: Optional[set[str]] = None,
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
    excluded_refs = {str(ref) for ref in (excluded_refs or set()) if str(ref)}
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
        if wh_ref in excluded_refs:
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


def _source_warehouse_options(
    db: Session,
    component_item_ids: List[int],
    *,
    excluded_refs: Optional[set[str]] = None,
) -> Dict[int, List[Dict[str, Any]]]:
    if not component_item_ids:
        return {}

    ignored_refs = {
        str(r[0])
        for r in db.query(IgnoredWarehouse.warehouse_ref1c).all()
    }
    excluded_refs = {str(ref) for ref in (excluded_refs or set()) if str(ref)}
    selected_refs = {
        str(r[0])
        for r in db.query(StockWarehouse.warehouse_ref1c)
        .filter(StockWarehouse.is_selected.is_(True))
        .all()
    }
    wh_names: Dict[str, str] = {
        str(r[0]): str(r[1] or r[0])
        for r in db.query(StockWarehouse.warehouse_ref1c, StockWarehouse.warehouse_name).all()
    }

    rows = (
        db.query(
            ItemWarehouseStock.item_id,
            ItemWarehouseStock.warehouse_ref1c,
            func.sum(ItemWarehouseStock.qty),
        )
        .filter(
            ItemWarehouseStock.item_id.in_(component_item_ids),
            ItemWarehouseStock.qty > 0,
        )
        .group_by(ItemWarehouseStock.item_id, ItemWarehouseStock.warehouse_ref1c)
        .all()
    )

    result: Dict[int, List[Dict[str, Any]]] = {}
    for item_id, wh_ref, qty in rows:
        ref = str(wh_ref)
        if ref in ignored_refs or ref in excluded_refs:
            continue
        if selected_refs and ref not in selected_refs:
            continue
        result.setdefault(int(item_id), []).append(
            {
                "ref1c": ref,
                "name": wh_names.get(ref, ref),
                "qty": _to_float(qty),
            }
        )
    for options in result.values():
        options.sort(key=lambda row: (-float(row.get("qty") or 0.0), str(row.get("name") or "")))
    return result


def _allocate_components_by_source_warehouse(
    db: Session,
    components: List[Dict[str, Any]],
    *,
    destination_warehouse_ref1c: Optional[str] = None,
    selected_source_warehouse_ref1c: Optional[str] = None,
) -> Tuple[Dict[Optional[str], List[Dict[str, Any]]], List[Dict[str, Any]]]:
    component_item_ids = [int(c["component_item_id"]) for c in components]
    excluded = {str(destination_warehouse_ref1c)} if destination_warehouse_ref1c else set()
    options_by_item = _source_warehouse_options(
        db,
        component_item_ids,
        excluded_refs=excluded,
    )
    selected_ref = _clean_ref1c(selected_source_warehouse_ref1c) or None

    groups: Dict[Optional[str], List[Dict[str, Any]]] = {}
    selection_required: List[Dict[str, Any]] = []
    for comp in components:
        component_id = int(comp["component_item_id"])
        options = options_by_item.get(component_id, [])
        if not options:
            groups.setdefault(None, []).append(comp)
            continue
        if len(options) == 1:
            groups.setdefault(str(options[0]["ref1c"]), []).append(comp)
            continue
        option_refs = {str(row["ref1c"]) for row in options}
        if not selected_ref or selected_ref not in option_refs:
            selection_required.append(
                {
                    "component_item_id": component_id,
                    "item_name": str(comp.get("item_name") or ""),
                    "item_article": str(comp.get("item_article") or ""),
                    "required_qty": _to_float(comp.get("required_qty")),
                    "warehouse_candidates": [
                        {
                            "ref1c": str(row["ref1c"]),
                            "name": str(row["name"]),
                            "qty": _to_float(row.get("qty")),
                        }
                        for row in options
                    ],
                }
            )
            continue
        groups.setdefault(selected_ref, []).append(comp)

    if selection_required:
        return {}, selection_required
    return groups, []


def _next_issue_number(db: Session) -> str:
    today = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"MI-{today}-"
    count = db.query(ProductionMaterialIssue).filter(ProductionMaterialIssue.document_number.like(f"{prefix}%")).count()
    return f"{prefix}{count + 1:04d}"


def _issue_reuse_payload(
    issue: ProductionMaterialIssue,
    product: ProductionProduct,
) -> Dict[str, Any]:
    return {
        "issue_id": int(issue.issue_id),
        "document_number": str(issue.document_number),
        "product_id": int(product.product_id),
        "order_number": str(product.order.order_number or ""),
        "item_name": str(product.item.item_name or ""),
        "status": str(issue.status),
        "source_warehouse_ref1c": str(issue.source_warehouse_ref1c or ""),
    }


def _prodplan_order_display_number(product: Optional[ProductionProduct], order: Optional[ProductionOrder]) -> str:
    if order is None:
        return ""
    order_source = str(order.source or "1c")
    if order_source != "mrp" or product is None:
        return str(order.order_number or "")

    run_id = int(order.source_run_id) if order.source_run_id is not None else None
    planned_order_id = (
        int(product.source_planned_order_id)
        if getattr(product, "source_planned_order_id", None) is not None
        else None
    )
    if run_id is not None and planned_order_id is not None:
        return f"MRP-{run_id}-{planned_order_id}"

    requirement_id = (
        int(product.source_mrp_requirement_id)
        if getattr(product, "source_mrp_requirement_id", None) is not None
        else None
    )
    allocation_key = str(product.source_mrp_allocation_key or "")
    if requirement_id is not None and allocation_key.startswith(f"mrp_requirement:{requirement_id}:order:"):
        try:
            seq = int(allocation_key.rsplit(":", 1)[-1])
        except Exception:
            seq = 1
        return f"MRP-R-{requirement_id}" if seq <= 1 else f"MRP-R-{requirement_id}-{seq}"

    if run_id is not None and getattr(product, "item_id", None) is not None:
        return f"MRP-RC-{run_id}-{int(product.item_id)}"

    return str(order.order_number or "")


def _order_one_c_number(db: Session, order: Optional[ProductionOrder]) -> str:
    if order is None:
        return ""
    link = (
        db.query(SyncLink)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "production_order",
            SyncLink.source_id == int(order.order_id),
            SyncLink.target_entity == PRODUCTION_ORDER_ENTITY,
        )
        .one_or_none()
    )
    if link and link.target_number:
        return str(link.target_number)
    if order.order_ref1c and str(order.source or "1c") == "1c":
        return str(order.order_number or "")
    return ""


def _sync_existing_issue_lines(
    db: Session,
    issue: ProductionMaterialIssue,
    components: List[Dict[str, Any]],
    *,
    spec_id: Optional[int],
    replace_missing: bool = False,
) -> None:
    """
    Keep a non-posted local transfer aligned with the current order quantity.

    Re-clicking "Запустить в 1С" should update the existing 1C document, not
    create a duplicate local transfer. That only works if the local transfer
    lines also reflect the edited order quantity.
    """
    by_component = {int(comp["component_item_id"]): comp for comp in components}
    existing_by_component = {
        int(line.component_item_id): line
        for line in (issue.lines or [])
    }

    for component_id, line in list(existing_by_component.items()):
        comp = by_component.get(component_id)
        if comp is None:
            if replace_missing:
                db.delete(line)
            continue
        line.required_qty = float(comp["required_qty"])
        line.unit = comp.get("unit")
        line.source_spec_id = spec_id
        if str(line.line_status or "") == "planned":
            line.issued_qty = 0.0

    if not replace_missing:
        return

    for comp in components:
        component_id = int(comp["component_item_id"])
        if component_id in existing_by_component:
            continue
        db.add(
            ProductionMaterialIssueLine(
                issue_id=int(issue.issue_id),
                component_item_id=component_id,
                required_qty=float(comp["required_qty"]),
                issued_qty=0.0,
                unit=comp.get("unit"),
                source_spec_id=spec_id,
                line_status="planned",
            )
        )


def _workshop_id_for_product(
    db: Session,
    product: ProductionProduct,
    spec_id: Optional[int],
) -> Optional[int]:
    state_obj = (
        db.query(ProductionOrderLineState)
        .filter(ProductionOrderLineState.product_id == int(product.product_id))
        .first()
    )
    if state_obj and state_obj.workshop_id:
        return int(state_obj.workshop_id)
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
            .first()
        )
        stage_id = int(comp_stage[0]) if comp_stage else None
    if stage_id is None:
        return None
    resource = (
        db.query(ResourceStage.resource_id)
        .filter(ResourceStage.stage_id == int(stage_id))
        .order_by(ResourceStage.id.asc())
        .first()
    )
    return int(resource[0]) if resource else None


def _destination_warehouse_for_product(
    db: Session,
    product: ProductionProduct,
    spec_id: Optional[int],
) -> Optional[str]:
    workshop_id_resolved = _workshop_id_for_product(db, product, spec_id)
    if not workshop_id_resolved:
        return None
    binding = (
        db.query(WorkshopWarehouseBinding)
        .filter(WorkshopWarehouseBinding.workshop_id == int(workshop_id_resolved))
        .first()
    )
    return _clean_ref1c(binding.warehouse_ref1c) if binding else None


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

    If an existing ProductionMaterialIssue already exists for the product,
    return its descriptor in `reused` instead of creating a duplicate. Posted
    transfers are final 1C documents, so re-clicking the action must be a
    no-op unless a separate delta flow explicitly creates another document.
    """
    created: List[Dict[str, Any]] = []
    reused: List[Dict[str, Any]] = []
    selection_required: List[Dict[str, Any]] = []
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

        existing_rows = (
            db.query(ProductionMaterialIssue)
            .options(joinedload(ProductionMaterialIssue.lines))
            .filter(
                ProductionMaterialIssue.product_id == int(product.product_id),
                ProductionMaterialIssue.status.in_(("draft", "requested", "issued", "exported", "posted", "error")),
                ProductionMaterialIssue.direction == "issue",
            )
            .order_by(ProductionMaterialIssue.issue_id.desc())
            .all()
        )

        spec_id, components = _components_for_product(db, product)
        if not components:
            errors.append(f"product_id={pid}: не найдена спецификация или материалы")
            continue

        # If the caller did not pin a destination warehouse, fall back to the
        # workshop->warehouse binding from settings. Plan rule:
        # "привязка участок -> склад получатель".
        resolved_warehouse = warehouse_ref1c
        if not resolved_warehouse:
            resolved_warehouse = _destination_warehouse_for_product(db, product, spec_id)

        if existing_rows and not source_warehouse_ref1c:
            for existing in existing_rows:
                if not existing.warehouse_ref1c:
                    existing.warehouse_ref1c = resolved_warehouse
                if str(existing.status or "") != "posted":
                    _sync_existing_issue_lines(
                        db,
                        existing,
                        components,
                        spec_id=spec_id,
                        replace_missing=False,
                    )
                reused.append(_issue_reuse_payload(existing, product))
            continue

        groups, needed_selection = _allocate_components_by_source_warehouse(
            db,
            components,
            destination_warehouse_ref1c=resolved_warehouse,
            selected_source_warehouse_ref1c=source_warehouse_ref1c,
        )
        if needed_selection:
            selection_required.append(
                {
                    "product_id": int(product.product_id),
                    "order_number": str(product.order.order_number or ""),
                    "item_name": str(product.item.item_name or ""),
                    "components": needed_selection,
                    "warehouse_candidates": needed_selection[0]["warehouse_candidates"],
                }
            )
            continue

        existing_by_source = {
            str(row.source_warehouse_ref1c or ""): row
            for row in existing_rows
        }
        for resolved_source_wh, grouped_components in groups.items():
            source_key = str(resolved_source_wh or "")
            existing = existing_by_source.get(source_key)
            if existing is not None:
                if not existing.warehouse_ref1c:
                    existing.warehouse_ref1c = resolved_warehouse
                if str(existing.status or "") != "posted":
                    _sync_existing_issue_lines(
                        db,
                        existing,
                        grouped_components,
                        spec_id=spec_id,
                        replace_missing=True,
                    )
                reused.append(_issue_reuse_payload(existing, product))
                continue

            issue = ProductionMaterialIssue(
                document_number="",
                product_id=int(product.product_id),
                order_id=int(product.order_id),
                status="draft",
                warehouse_ref1c=resolved_warehouse,
                source_warehouse_ref1c=resolved_source_wh,
                initiated_by=initiated_by,
            )
            db.add(issue)
            db.flush()
            issue.document_number = material_issue_number(db, issue)
            for comp in grouped_components:
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
            entry: Dict[str, Any] = {
                "issue_id": int(issue.issue_id),
                "document_number": issue.document_number,
                "product_id": int(product.product_id),
                "order_number": str(product.order.order_number or ""),
                "item_name": str(product.item.item_name or ""),
                "lines_count": len(grouped_components),
                "source_warehouse_ref1c": resolved_source_wh,
            }
            created.append(entry)

        if groups:
            state = _ensure_state(db, product)
            state.issue_status = "requested"
            # Once material-issue drafts are open, the line has moved beyond
            # the "no coverage yet" phase. Bump status to 'to_move'
            # ("документы созданы, ждём проведения") unless it's already
            # further along.
            if state.status in {"shortage", "partial", "ready"}:
                state.status = "to_move"
    db.commit()
    return {
        "status": "ok",
        "created": created,
        "reused": reused,
        "selection_required": selection_required,
        "errors": errors,
    }


def _warehouse_name_lookup(db: Session, refs: Sequence[str]) -> Dict[str, str]:
    clean_refs = sorted({_clean_ref1c(ref) for ref in refs if _clean_ref1c(ref)})
    if not clean_refs:
        return {}
    return {
        str(row.warehouse_ref1c): str(row.warehouse_name or row.warehouse_ref1c)
        for row in db.query(StockWarehouse)
        .filter(StockWarehouse.warehouse_ref1c.in_(clean_refs))
        .all()
    }


def _warehouse_display_name(warehouse_names: Dict[str, str], ref: Optional[str]) -> str:
    clean_ref = _clean_ref1c(ref)
    if not clean_ref:
        return ""
    return warehouse_names.get(clean_ref, clean_ref)


def _issue_source_warehouse_options(db: Session, query) -> List[Dict[str, str]]:
    refs = [
        _clean_ref1c(row[0])
        for row in query.order_by(None)
        .with_entities(ProductionMaterialIssue.source_warehouse_ref1c)
        .filter(ProductionMaterialIssue.source_warehouse_ref1c.isnot(None))
        .distinct()
        .all()
    ]
    refs = sorted({ref for ref in refs if ref})
    warehouse_names = _warehouse_name_lookup(db, refs)
    return [
        {
            "warehouse_ref1c": ref,
            "warehouse_name": _warehouse_display_name(warehouse_names, ref),
        }
        for ref in sorted(refs, key=lambda value: _warehouse_display_name(warehouse_names, value).casefold())
    ]


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
    warehouse_names = _warehouse_name_lookup(
        db,
        [str(issue.source_warehouse_ref1c or ""), str(issue.warehouse_ref1c or "")],
    )
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
    one_c_number = str(link.target_number or "") if link else ""
    return {
        "issue_id": int(issue.issue_id),
        "document_number": str(issue.document_number),
        "status": str(issue.status),
        "warehouse_ref1c": str(issue.warehouse_ref1c or ""),
        "destination_warehouse_name": _warehouse_display_name(warehouse_names, issue.warehouse_ref1c),
        "source_warehouse_ref1c": str(issue.source_warehouse_ref1c or ""),
        "source_warehouse_name": _warehouse_display_name(warehouse_names, issue.source_warehouse_ref1c),
        "initiated_by": str(issue.initiated_by or ""),
        "order_number": str(issue.order.order_number or ""),
        "order_prodplan_number": _prodplan_order_display_number(issue.product, issue.order),
        "order_one_c_number": _order_one_c_number(db, issue.order),
        "product_id": int(issue.product_id),
        "item_name": str(issue.product.item.item_name or "") if issue.product and issue.product.item else "",
        "item_article": str(issue.product.item.item_article or "") if issue.product and issue.product.item else "",
        "created_at": _date_to_iso(issue.created_at),
        "exported_ref1c": str(issue.exported_ref1c or ""),
        "one_c_number": one_c_number,
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


def _issue_header(
    db: Session,
    issue: ProductionMaterialIssue,
    warehouse_names: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    product = issue.product
    item = product.item if product and product.item else None
    state = (
        getattr(product, "control_state", None)
        if product is not None
        else None
    )
    issue_status = str(issue.status or "")
    exported_ref = _clean_ref1c(issue.exported_ref1c)
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
    one_c_number = str(link.target_number or "") if link else ""
    can_assemble = bool(exported_ref) and issue_status != "posted"
    assemble_disabled_reason = ""
    if issue_status == "posted":
        assemble_disabled_reason = "Перемещение уже собрано"
    elif not exported_ref:
        assemble_disabled_reason = "Сначала выгрузите перемещение в 1С"
    warehouse_names = warehouse_names or _warehouse_name_lookup(
        db,
        [str(issue.source_warehouse_ref1c or ""), str(issue.warehouse_ref1c or "")],
    )
    return {
        "issue_id": int(issue.issue_id),
        "document_number": str(issue.document_number),
        "status": issue_status,
        "direction": str(issue.direction or "issue"),
        "product_id": int(issue.product_id),
        "order_id": int(issue.order_id),
        "order_number": str(issue.order.order_number or "") if issue.order else "",
        "order_prodplan_number": _prodplan_order_display_number(product, issue.order),
        "order_one_c_number": _order_one_c_number(db, issue.order),
        "order_source": str(issue.order.source or "1c") if issue.order else "",
        "order_ref1c": str(issue.order.order_ref1c or "") if issue.order and issue.order.order_ref1c else None,
        "item_id": int(product.item_id) if product else None,
        "item_name": str(item.item_name or "") if item else "",
        "item_article": str(item.item_article or "") if item else "",
        "item_code": str(item.item_code or "") if item else "",
        "quantity": _to_float(product.quantity) if product else 0.0,
        "remaining_qty": _to_float(product.remaining_qty) if product else 0.0,
        "unit": _unit_display(db, item.unit) if item else "",
        "warehouse_ref1c": str(issue.warehouse_ref1c or ""),
        "destination_warehouse_name": _warehouse_display_name(warehouse_names, issue.warehouse_ref1c),
        "source_warehouse_ref1c": str(issue.source_warehouse_ref1c or ""),
        "source_warehouse_name": _warehouse_display_name(warehouse_names, issue.source_warehouse_ref1c),
        "exported_ref1c": exported_ref,
        "one_c_number": one_c_number,
        "exported_at": _date_to_iso(issue.exported_at),
        "created_at": _date_to_iso(issue.created_at),
        "export_error": str(issue.export_error or ""),
        "can_assemble": can_assemble,
        "assemble_disabled_reason": assemble_disabled_reason,
        "line_status": str(state.status if state else ""),
        "issue_status": str(state.issue_status if state else ""),
        "lines_count": len(issue.lines or []),
    }


def list_material_issues(
    db: Session,
    *,
    status: Optional[str] = None,
    search: Optional[str] = None,
    source_warehouse_ref1c: Optional[str] = None,
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
                | (ProductionMaterialIssue.order.has(ProductionOrder.order_number.ilike(like)))
                | (ProductionProduct.item.has(Item.item_name.ilike(like)))
                | (ProductionProduct.item.has(Item.item_article.ilike(like)))
                | (ProductionProduct.item.has(Item.item_code.ilike(like)))
            )
        )
    source_warehouses = _issue_source_warehouse_options(db, query)
    clean_source_ref = _clean_ref1c(source_warehouse_ref1c)
    if clean_source_ref:
        query = query.filter(ProductionMaterialIssue.source_warehouse_ref1c == clean_source_ref)

    total = query.count()
    effective_limit = max(1, min(int(limit or 100), 500))
    effective_offset = max(0, int(offset or 0))
    rows = (
        query.order_by(ProductionMaterialIssue.created_at.desc(), ProductionMaterialIssue.issue_id.desc())
        .offset(effective_offset)
        .limit(effective_limit)
        .all()
    )
    warehouse_names = _warehouse_name_lookup(
        db,
        [
            ref
            for issue in rows
            for ref in (str(issue.source_warehouse_ref1c or ""), str(issue.warehouse_ref1c or ""))
        ],
    )
    return {
        "rows": [_issue_header(db, issue, warehouse_names) for issue in rows],
        "total": int(total),
        "limit": effective_limit,
        "offset": effective_offset,
        "source_warehouses": source_warehouses,
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
    client = _create_odata_client(
        _load_odata_config(),
        OData1CClient,
        allow_production=allow_production,
        require_demo_base=True,
    )
    try:
        from .one_c_stock_transfer_export import (
            add_source_cells_to_payload,
            _build_header_payload,
            _collect_export_entries,
            _export_defaults,
        )

        entries, skipped = _collect_export_entries(db, [int(issue.issue_id)])
        if skipped:
            raise ValueError("; ".join(str(row.get("reason") or row) for row in skipped))
        if entries:
            payload = _build_header_payload(entries[0], _export_defaults(_load_odata_config()))
            if link is not None and link.target_number:
                payload["Number"] = str(link.target_number)
            add_source_cells_to_payload(client, entries[0], payload)
            patch = getattr(client, "patch", None)
            if patch is not None:
                patch(f"{STOCK_TRANSFER_ENTITY}(guid'{ref_key}')", payload)
        _post_document_operational(
            client,
            entity=STOCK_TRANSFER_ENTITY,
            ref_key=ref_key,
            unpost_first=True,
        )
    except Exception as exc:
        message = _clean_odata_error_message(exc)
        issue.export_error = message
        db.commit()
        raise ValueError(message) from exc

    issue.status = "posted"
    issue.export_error = None
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
