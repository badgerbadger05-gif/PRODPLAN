from __future__ import annotations

import html
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session, joinedload

from ..models import (
    DefaultSpecification,
    IgnoredWarehouse,
    Item,
    ItemWarehouseStock,
    MrpRequirement,
    Operation,
    PlanningRun,
    ProductionPlanHeader,
    ProductionPlanLine,
    ProductionMaterialIssue,
    ProductionProduct,
    ProductionOrderLineState,
    ProductionStage,
    StockWarehouse,
    SpecComponent,
    SpecOperation,
    SyncLink,
    Unit,
)
from .production_control_common import looks_like_guid as _looks_like_guid, to_float as _to_float
from .one_c_production_order_export import PRODUCTION_ORDER_ENTITY


def mark_route_sheets_printed(db: Session, product_ids: Iterable[int]) -> int:
    ids = [int(pid) for pid in product_ids]
    if not ids:
        return 0

    unique_ids = sorted(set(ids))
    existing_ids = {
        int(row.product_id)
        for row in db.query(ProductionProduct.product_id)
        .filter(ProductionProduct.product_id.in_(unique_ids))
        .all()
    }
    if not existing_ids:
        return 0

    existing_state_ids = {
        int(row.product_id)
        for row in db.query(ProductionOrderLineState.product_id)
        .filter(ProductionOrderLineState.product_id.in_(existing_ids))
        .all()
    }
    now = datetime.utcnow()
    if existing_state_ids:
        (
            db.query(ProductionOrderLineState)
            .filter(ProductionOrderLineState.product_id.in_(existing_state_ids))
            .update(
                {ProductionOrderLineState.route_sheet_printed_at: now},
                synchronize_session=False,
            )
        )
    for product_id in sorted(existing_ids - existing_state_ids):
        db.add(
            ProductionOrderLineState(
                product_id=product_id,
                status="shortage",
                issue_status="not_requested",
                route_sheet_printed_at=now,
            )
        )
    db.commit()
    return sum(1 for pid in ids if pid in existing_ids)


def _date_ru(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    return str(value)


def _datetime_ru(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M")
    return _date_ru(value)


def _item_label(item: Optional[Item]) -> str:
    if not item:
        return ""
    article = str(item.item_article or item.item_code or "").strip()
    name = str(item.item_name or "").strip()
    return f"{name} ({article})" if article else name


def _warehouse_names(db: Session, refs: Sequence[str]) -> Dict[str, str]:
    clean_refs = sorted({str(ref or "").strip() for ref in refs if str(ref or "").strip()})
    if not clean_refs:
        return {}
    return {
        str(row.warehouse_ref1c): str(row.warehouse_name or row.warehouse_ref1c)
        for row in db.query(StockWarehouse)
        .filter(StockWarehouse.warehouse_ref1c.in_(clean_refs))
        .all()
    }


def _warehouse_label(names: Dict[str, str], ref: Optional[str]) -> str:
    clean_ref = str(ref or "").strip()
    if not clean_ref:
        return ""
    return names.get(clean_ref, clean_ref)


def _first_default_specs(db: Session, item_ids: Sequence[int]) -> Dict[int, int]:
    ids = sorted({int(item_id) for item_id in item_ids if item_id is not None})
    if not ids:
        return {}

    result: Dict[int, int] = {}
    rows = (
        db.query(DefaultSpecification.item_id, DefaultSpecification.spec_id)
        .filter(DefaultSpecification.item_id.in_(ids))
        .order_by(DefaultSpecification.item_id.asc(), DefaultSpecification.id.asc())
        .all()
    )
    for item_id, spec_id in rows:
        item_id_int = int(item_id)
        if item_id_int not in result:
            result[item_id_int] = int(spec_id)
    return result


def _components_by_spec(db: Session, spec_ids: Sequence[int]) -> Dict[int, List[Tuple[SpecComponent, Item]]]:
    ids = sorted({int(spec_id) for spec_id in spec_ids if spec_id})
    if not ids:
        return {}

    result: Dict[int, List[Tuple[SpecComponent, Item]]] = {spec_id: [] for spec_id in ids}
    rows = (
        db.query(SpecComponent, Item)
        .join(Item, Item.item_id == SpecComponent.item_id)
        .filter(SpecComponent.spec_id.in_(ids))
        .order_by(SpecComponent.spec_id.asc(), Item.item_name.asc())
        .all()
    )
    for comp, item in rows:
        result.setdefault(int(comp.spec_id), []).append((comp, item))
    return result


def _operation_rows_by_spec(db: Session, spec_ids: Sequence[int]) -> Dict[int, List[Dict[str, Any]]]:
    ids = sorted({int(spec_id) for spec_id in spec_ids if spec_id})
    if not ids:
        return {}

    grouped: Dict[int, List[Tuple[SpecOperation, Optional[ProductionStage], Optional[Operation]]]] = {
        spec_id: [] for spec_id in ids
    }
    rows = (
        db.query(SpecOperation, ProductionStage, Operation)
        .outerjoin(ProductionStage, ProductionStage.stage_id == SpecOperation.stage_id)
        .outerjoin(Operation, Operation.operation_id == SpecOperation.operation_id)
        .filter(SpecOperation.spec_id.in_(ids))
        .order_by(SpecOperation.spec_id.asc(), SpecOperation.spec_operation_id.asc())
        .all()
    )
    for op, stage, operation in rows:
        grouped.setdefault(int(op.spec_id), []).append((op, stage, operation))

    result: Dict[int, List[Dict[str, Any]]] = {}
    for spec_id, spec_rows in grouped.items():
        result[spec_id] = [
            {
                "number": idx + 1,
                "stage_name": str(stage.stage_name or "") if stage else "",
                "operation_name": str(operation.operation_name or "") if operation else "",
                "time_norm": _to_float(op.time_norm),
            }
            for idx, (op, stage, operation) in enumerate(spec_rows)
        ]
    return result


def _unit_display_by_raw(db: Session, raw_units: Sequence[Any]) -> Dict[str, str]:
    raw_by_key = {
        str(raw or "").strip(): str(raw or "").strip()
        for raw in raw_units
        if str(raw or "").strip()
    }
    if not raw_by_key:
        return {}

    units_by_ref = {
        str(unit.unit_ref1c): str(unit.short_name or unit.unit_name or unit.unit_code or "").strip()
        for unit in db.query(Unit).filter(Unit.unit_ref1c.in_(sorted(raw_by_key))).all()
    }
    result: Dict[str, str] = {}
    for raw in raw_by_key:
        result[raw] = units_by_ref.get(raw, "" if _looks_like_guid(raw) else raw)
    return result


def _multi_stock_warehouse_item_ids(db: Session, item_ids: Sequence[int]) -> set[int]:
    ids = sorted({int(item_id) for item_id in item_ids if item_id is not None})
    if not ids:
        return set()

    ignored_refs = {
        str(row.warehouse_ref1c or "").strip()
        for row in db.query(IgnoredWarehouse.warehouse_ref1c).all()
        if str(row.warehouse_ref1c or "").strip()
    }
    warehouse_settings = {
        str(row.warehouse_ref1c or "").strip(): bool(row.is_selected)
        for row in db.query(StockWarehouse.warehouse_ref1c, StockWarehouse.is_selected).all()
        if str(row.warehouse_ref1c or "").strip()
    }
    selected_refs = {ref for ref, is_selected in warehouse_settings.items() if is_selected}

    rows = (
        db.query(ItemWarehouseStock.item_id, ItemWarehouseStock.warehouse_ref1c)
        .filter(
            ItemWarehouseStock.item_id.in_(ids),
            ItemWarehouseStock.qty > 0,
        )
        .all()
    )

    refs_by_item_id: Dict[int, set[str]] = {}
    for item_id, warehouse_ref in rows:
        ref = str(warehouse_ref or "").strip()
        if not ref or ref in ignored_refs:
            continue
        if warehouse_settings and ref not in selected_refs:
            continue
        refs_by_item_id.setdefault(int(item_id), set()).add(ref)
    return {item_id for item_id, refs in refs_by_item_id.items() if len(refs) > 1}


def _bom_descendant_ids_by_root(db: Session, root_item_ids: Sequence[int]) -> Dict[int, set[int]]:
    roots = sorted({int(item_id) for item_id in root_item_ids if item_id is not None})
    if not roots:
        return {}

    descendants: Dict[int, set[int]] = {root: {root} for root in roots}
    frontier_by_root: Dict[int, set[int]] = {root: {root} for root in roots}
    seen_specs_by_root: Dict[int, set[int]] = {root: set() for root in roots}
    spec_by_item: Dict[int, Optional[int]] = {}
    components_by_loaded_spec: Dict[int, List[int]] = {}

    while any(frontier_by_root.values()):
        frontier_items = {
            item_id
            for item_ids in frontier_by_root.values()
            for item_id in item_ids
            if item_id not in spec_by_item
        }
        if frontier_items:
            found = _first_default_specs(db, sorted(frontier_items))
            for item_id in frontier_items:
                spec_by_item[item_id] = found.get(item_id)

        active_specs: set[int] = set()
        for root, item_ids in frontier_by_root.items():
            for item_id in item_ids:
                spec_id = spec_by_item.get(item_id)
                if spec_id and spec_id not in seen_specs_by_root[root]:
                    active_specs.add(int(spec_id))

        missing_specs = sorted(active_specs - set(components_by_loaded_spec))
        if missing_specs:
            for spec_id in missing_specs:
                components_by_loaded_spec[spec_id] = []
            rows = (
                db.query(SpecComponent.spec_id, SpecComponent.item_id)
                .filter(SpecComponent.spec_id.in_(missing_specs))
                .all()
            )
            for spec_id, item_id in rows:
                components_by_loaded_spec.setdefault(int(spec_id), []).append(int(item_id))

        next_frontier_by_root: Dict[int, set[int]] = {root: set() for root in roots}
        for root, item_ids in frontier_by_root.items():
            for item_id in item_ids:
                spec_id = spec_by_item.get(item_id)
                if not spec_id or spec_id in seen_specs_by_root[root]:
                    continue
                seen_specs_by_root[root].add(int(spec_id))
                for child_id in components_by_loaded_spec.get(int(spec_id), []):
                    if child_id not in descendants[root]:
                        descendants[root].add(child_id)
                        next_frontier_by_root[root].add(child_id)
        frontier_by_root = next_frontier_by_root

    return descendants


def _route_contexts_for_products(
    db: Session,
    products: Sequence[ProductionProduct],
) -> Dict[int, Dict[str, str]]:
    req_ids = sorted(
        {
            int(product.source_mrp_requirement_id)
            for product in products
            if product.source_mrp_requirement_id
        }
    )
    req_by_id: Dict[int, MrpRequirement] = {}
    if req_ids:
        req_by_id = {
            int(req.id): req
            for req in db.query(MrpRequirement).filter(MrpRequirement.id.in_(req_ids)).all()
        }

    run_ids = {
        int(product.order.source_run_id)
        for product in products
        if product.order and product.order.source_run_id
    }
    run_ids.update(int(req.run_id) for req in req_by_id.values() if req.run_id)
    run_by_id: Dict[int, PlanningRun] = {}
    if run_ids:
        run_by_id = {
            int(run.run_id): run
            for run in db.query(PlanningRun).filter(PlanningRun.run_id.in_(sorted(run_ids))).all()
        }

    plan_ids = {
        int(run.source_plan_id)
        for run in run_by_id.values()
        if run and run.source_plan_id
    }
    plan_by_id: Dict[int, ProductionPlanHeader] = {}
    if plan_ids:
        plan_by_id = {
            int(plan.id): plan
            for plan in db.query(ProductionPlanHeader).filter(ProductionPlanHeader.id.in_(sorted(plan_ids))).all()
        }

    root_item_ids_by_plan: Dict[int, set[int]] = {plan_id: set() for plan_id in plan_ids}
    if plan_ids:
        rows = (
            db.query(ProductionPlanLine.plan_id, ProductionPlanLine.item_id)
            .filter(
                ProductionPlanLine.plan_id.in_(sorted(plan_ids)),
                ProductionPlanLine.qty > 0,
            )
            .distinct()
            .all()
        )
        for plan_id, item_id in rows:
            root_item_ids_by_plan.setdefault(int(plan_id), set()).add(int(item_id))

    all_root_item_ids = {
        item_id
        for item_ids in root_item_ids_by_plan.values()
        for item_id in item_ids
    }
    root_items_by_id: Dict[int, Item] = {}
    if all_root_item_ids:
        root_items_by_id = {
            int(item.item_id): item
            for item in (
                db.query(Item)
                .filter(Item.item_id.in_(sorted(all_root_item_ids)))
                .order_by(Item.item_article.asc(), Item.item_name.asc())
                .all()
            )
        }
    root_descendants = _bom_descendant_ids_by_root(db, sorted(all_root_item_ids))

    order_ids = sorted({int(product.order_id) for product in products if product.order_id})
    one_c_by_order_id: Dict[int, str] = {}
    if order_ids:
        for link in (
            db.query(SyncLink)
            .filter(
                SyncLink.source_doctype == "production_order",
                SyncLink.source_id.in_(order_ids),
                SyncLink.target_entity == PRODUCTION_ORDER_ENTITY,
            )
            .all()
        ):
            one_c_by_order_id.setdefault(int(link.source_id), str(link.target_number or ""))

    contexts: Dict[int, Dict[str, str]] = {}
    for product in products:
        run: Optional[PlanningRun] = None
        if product.order and product.order.source_run_id:
            run = run_by_id.get(int(product.order.source_run_id))
        if run is None and product.source_mrp_requirement_id:
            req = req_by_id.get(int(product.source_mrp_requirement_id))
            if req:
                run = run_by_id.get(int(req.run_id))

        plan: Optional[ProductionPlanHeader] = None
        if run and run.source_plan_id:
            plan = plan_by_id.get(int(run.source_plan_id))

        root_item: Optional[Item] = None
        if plan:
            plan_root_ids = root_item_ids_by_plan.get(int(plan.id), set())
            sorted_roots = sorted(
                (root_items_by_id[item_id] for item_id in plan_root_ids if item_id in root_items_by_id),
                key=lambda item: (str(item.item_article or ""), str(item.item_name or "")),
            )
            for item in sorted_roots:
                if int(product.item_id) in root_descendants.get(int(item.item_id), {int(item.item_id)}):
                    root_item = item
                    break

        plan_period = ""
        if plan:
            plan_period = f"{_date_ru(plan.period_from)} - {_date_ru(plan.period_to)}"
        elif run and (run.period_from or run.period_to):
            plan_period = f"{_date_ru(run.period_from)} - {_date_ru(run.period_to)}"

        contexts[int(product.product_id)] = {
            "plan_name": str(plan.name or "") if plan else "",
            "plan_period": plan_period.strip(" -"),
            "root_item": _item_label(root_item),
            "one_c_number": one_c_by_order_id.get(int(product.order_id), ""),
        }

    return contexts


def _material_transfer_rows_for_products(
    db: Session,
    products: Sequence[ProductionProduct],
) -> Dict[int, List[Dict[str, str]]]:
    product_ids = sorted({int(product.product_id) for product in products})
    if not product_ids:
        return {}

    issues_by_product_id: Dict[int, List[ProductionMaterialIssue]] = {product_id: [] for product_id in product_ids}
    issues = (
        db.query(ProductionMaterialIssue)
        .filter(
            ProductionMaterialIssue.product_id.in_(product_ids),
            ProductionMaterialIssue.direction == "issue",
            ProductionMaterialIssue.status != "cancelled",
        )
        .order_by(ProductionMaterialIssue.product_id.asc(), ProductionMaterialIssue.issue_id.asc())
        .all()
    )
    for issue in issues:
        issues_by_product_id.setdefault(int(issue.product_id), []).append(issue)

    warehouse_names = _warehouse_names(
        db,
        [
            ref
            for issue in issues
            for ref in (str(issue.source_warehouse_ref1c or ""), str(issue.warehouse_ref1c or ""))
        ],
    )

    links_by_issue_id: Dict[int, SyncLink] = {}
    issue_ids = [int(issue.issue_id) for issue in issues]
    if issue_ids:
        links_by_issue_id = {
            int(link.source_id): link
            for link in db.query(SyncLink)
            .filter(
                SyncLink.source_system == "PRODPLAN",
                SyncLink.source_doctype == "material_issue",
                SyncLink.source_id.in_(issue_ids),
            )
            .all()
        }

    result: Dict[int, List[Dict[str, str]]] = {}
    for product in products:
        state = getattr(product, "control_state", None)
        workshop = state.workshop if state and state.workshop else None
        workshop_name = str(workshop.resource_name or "") if workshop else ""
        rows: List[Dict[str, str]] = []
        for issue in issues_by_product_id.get(int(product.product_id), []):
            link = links_by_issue_id.get(int(issue.issue_id))
            one_c_number = str(link.target_number or "") if link else ""
            local_number = str(issue.document_number or "")
            if one_c_number and one_c_number != local_number:
                transfer_number = f"{local_number} / {one_c_number}"
            else:
                transfer_number = one_c_number or local_number
            rows.append(
                {
                    "transfer_number": transfer_number,
                    "workshop_name": workshop_name,
                    "source_warehouse": _warehouse_label(warehouse_names, issue.source_warehouse_ref1c),
                    "destination_warehouse": _warehouse_label(warehouse_names, issue.warehouse_ref1c),
                }
            )
        result[int(product.product_id)] = rows
    return result


def _route_sheet_print_data(
    db: Session,
    product_ids: Sequence[int],
) -> Tuple[
    List[ProductionProduct],
    Dict[int, List[Dict[str, Any]]],
    Dict[int, List[Dict[str, Any]]],
    Dict[int, Dict[str, str]],
    Dict[int, List[Dict[str, str]]],
    Dict[str, str],
]:
    ids = [int(product_id) for product_id in product_ids]
    if not ids:
        return [], {}, {}, {}, {}, {}

    products = (
        db.query(ProductionProduct)
        .options(
            joinedload(ProductionProduct.order),
            joinedload(ProductionProduct.item),
            joinedload(ProductionProduct.control_state).joinedload(ProductionOrderLineState.workshop),
        )
        .filter(ProductionProduct.product_id.in_(sorted(set(ids))))
        .all()
    )
    product_map = {int(product.product_id): product for product in products}
    ordered = [product_map[product_id] for product_id in ids if product_id in product_map]

    default_spec_by_item = _first_default_specs(
        db,
        [
            int(product.item_id)
            for product in products
            if not product.spec_id and product.item_id is not None
        ],
    )
    spec_id_by_product_id: Dict[int, Optional[int]] = {}
    for product in products:
        spec_id_by_product_id[int(product.product_id)] = (
            int(product.spec_id) if product.spec_id else default_spec_by_item.get(int(product.item_id))
        )

    components_by_spec = _components_by_spec(db, [spec_id for spec_id in spec_id_by_product_id.values() if spec_id])
    component_item_ids = {
        int(item.item_id)
        for rows in components_by_spec.values()
        for _comp, item in rows
        if item.item_id is not None
    }
    multi_stock_item_ids = _multi_stock_warehouse_item_ids(db, sorted(component_item_ids))
    components_by_product_id: Dict[int, List[Dict[str, Any]]] = {}
    for product in products:
        spec_id = spec_id_by_product_id.get(int(product.product_id))
        qty = _to_float(product.remaining_qty) or _to_float(product.quantity)
        component_rows: List[Dict[str, Any]] = []
        for comp, item in components_by_spec.get(int(spec_id), []) if spec_id else []:
            required = _to_float(comp.quantity) * qty
            if required <= 0:
                continue
            component_rows.append(
                {
                    "component_item_id": int(item.item_id),
                    "item_code": str(item.item_code or ""),
                    "item_name": str(item.item_name or ""),
                    "item_article": str(item.item_article or ""),
                    "qty_per_unit": _to_float(comp.quantity),
                    "required_qty": required,
                    "source_spec_id": spec_id,
                    "multi_stock_warning": int(item.item_id) in multi_stock_item_ids,
                }
            )
        components_by_product_id[int(product.product_id)] = component_rows

    operations_by_spec = _operation_rows_by_spec(db, [spec_id for spec_id in spec_id_by_product_id.values() if spec_id])
    operations_by_product_id = {
        int(product.product_id): operations_by_spec.get(int(spec_id), []) if spec_id else []
        for product in products
        for spec_id in [spec_id_by_product_id.get(int(product.product_id))]
    }

    route_contexts = _route_contexts_for_products(db, products)
    transfer_rows = _material_transfer_rows_for_products(db, products)
    unit_by_raw = _unit_display_by_raw(db, [product.item.unit for product in products if product.item])

    return ordered, components_by_product_id, operations_by_product_id, route_contexts, transfer_rows, unit_by_raw


def render_route_sheets_html(db: Session, product_ids: Sequence[int], *, auto_print: bool = False) -> str:
    (
        ordered,
        components_by_product_id,
        operations_by_product_id,
        route_contexts,
        transfer_rows_by_product_id,
        unit_by_raw,
    ) = _route_sheet_print_data(db, product_ids)
    now = datetime.now().strftime("%d.%m.%Y")
    sheets: List[str] = []
    for product in ordered:
        components = components_by_product_id.get(int(product.product_id), [])
        operations = operations_by_product_id.get(int(product.product_id), [])
        order_date = _datetime_ru(product.order.order_date)
        route_ctx = route_contexts.get(int(product.product_id), {})
        transfer_rows_data = transfer_rows_by_product_id.get(int(product.product_id), [])
        product_unit = unit_by_raw.get(str(product.item.unit or "").strip(), "")
        order_number = html.escape(str(product.order.order_number or ""))
        one_c_number = html.escape(route_ctx.get("one_c_number") or "—")
        title = f"МАРШРУТНЫЙ ЛИСТ № {order_number} от {now}"
        warehouse_warning_html = ' <strong class="warehouse-warning">проверь склады</strong>'
        transfer_rows = "".join(
            "<tr>"
            f"<td colspan='2' class='text strong-value'>{html.escape(row['workshop_name'] or '—')}</td>"
            f"<td colspan='2' class='text'>{html.escape(row['transfer_number'] or '—')}</td>"
            f"<td colspan='3' class='text strong-value'>{html.escape(row['source_warehouse'] or '—')}</td>"
            f"<td colspan='3' class='text strong-value'>{html.escape(row['destination_warehouse'] or '—')}</td>"
            "</tr>"
            for row in transfer_rows_data
        ) or "<tr><td colspan='10'>Перемещения материалов не созданы</td></tr>"
        component_rows = "".join(
            "<tr>"
            f"<td colspan='2' class='text'>{html.escape(c['item_name'])}"
            f"{warehouse_warning_html if c.get('multi_stock_warning') else ''}</td>"
            f"<td class='text strong-value'>{html.escape(c['item_article'])}</td>"
            f"<td class='num'>{c['qty_per_unit']:.3f}</td>"
            f"<td colspan='6' class='num'>{c['required_qty']:.3f}</td>"
            "</tr>"
            for c in components
        ) or "<tr><td colspan='10'>Материалы по спецификации не найдены</td></tr>"
        op_rows = "".join(
            "<tr>"
            f"<td class='num'>{op['number']}</td>"
            f"<td class='text strong-value'>{html.escape(op['stage_name'])}</td>"
            f"<td colspan='2' class='text'>{html.escape(op['operation_name'] or op['stage_name'] or 'Операция')}</td>"
            f"<td class='num'>{op['time_norm']:.3f}</td>"
            "<td class='signature'>ФИО, подпись, дата</td>"
            "<td></td>"
            "<td></td>"
            "<td></td>"
            "<td class='signature'>Клеймо, ФИО, подпись, дата</td>"
            "</tr>"
            for op in operations
        ) or "<tr><td colspan='10'>Операции по спецификации не найдены</td></tr>"
        sheets.append(
            f"""
            <section class="sheet">
              <table class="route">
                <colgroup>
                  <col class="c-num">
                  <col class="c-material">
                  <col class="c-article">
                  <col class="c-qty">
                  <col class="c-qty">
                  <col class="c-worker">
                  <col class="c-presented">
                  <col class="c-nonconforming">
                  <col class="c-good">
                  <col class="c-otk">
                </colgroup>
                <tr>
                  <td colspan="5" class="title">{title}<br><span>(Изготовление новых)</span></td>
                  <td colspan="5" class="order">
                    <b>Заказ PRODPLAN:</b> №{order_number}<br>
                    <b>Номер 1С:</b> <strong class="strong-value">{one_c_number}</strong><br>
                    <b>Дата заказа:</b> {html.escape(order_date)}
                  </td>
                </tr>
                <tr>
                  <td colspan="5" class="product-name"><b>Наименование:</b><br>{html.escape(str(product.item.item_name or ""))}</td>
                  <td class="product-article"><b>Артикул:</b><br><strong class="strong-value">{html.escape(str(product.item.item_article or ""))}</strong></td>
                  <td colspan="4"><b>Количество:</b><br>{_to_float(product.remaining_qty) or _to_float(product.quantity):g} {html.escape(product_unit)}</td>
                </tr>
                <tr>
                  <td colspan="5"><b>План:</b><br>{html.escape(route_ctx.get("plan_name") or "—")}</td>
                  <td><b>Период:</b><br>{html.escape(route_ctx.get("plan_period") or "—")}</td>
                  <td colspan="4"><b>Корневое изделие:</b><br>{html.escape(route_ctx.get("root_item") or "—")}</td>
                </tr>
                <tr><td colspan="10"><b>Маршрут перемещения материалов</b></td></tr>
                <tr><th colspan="2">Участок получатель</th><th colspan="2">№ перемещения</th><th colspan="3">Склад отправитель</th><th colspan="3">Склад получатель</th></tr>
                {transfer_rows}
                <tr><td colspan="10"><b>Материалы и заготовки</b></td></tr>
                <tr><th colspan="2">Материал</th><th>Артикул</th><th>Кол-во на ед.</th><th colspan="6">Кол-во по заказу</th></tr>
                {component_rows}
                <tr><th>№</th><th>Цех / участок</th><th colspan="2">Операция</th><th>Трудоемкость</th><th>Исполнитель</th><th>Предъявлено</th><th>Несоотв.</th><th>Годн.</th><th>ОТК</th></tr>
                {op_rows}
                <tr><td colspan="10" class="notes"><b>Дополнительная информация:</b><br><br><br><br></td></tr>
              </table>
            </section>
            """
        )
    auto_print_script = (
        "<script>window.addEventListener('load', () => setTimeout(() => window.print(), 250));</script>"
        if auto_print
        else ""
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Маршрутные листы</title>
  <style>
    @page {{ size: A4 portrait; margin: 5mm; }}
    body {{ font-family: "Times New Roman", serif; color: #000; margin: 0; }}
    .toolbar {{ position: sticky; top: 0; padding: 8px; background: #f4f6f8; border-bottom: 1px solid #cfd8dc; font-family: Arial, sans-serif; }}
    .toolbar button {{ padding: 6px 12px; }}
    .sheet {{ page-break-after: always; padding: 3px; }}
    table.route {{ border-collapse: collapse; width: 100%; table-layout: fixed; font-size: 10.5px; line-height: 1.12; }}
    .route td, .route th {{ border: 1px solid #000; padding: 2px 3px; vertical-align: top; overflow-wrap: anywhere; }}
    .c-num {{ width: 4%; }}
    .c-material {{ width: 23%; }}
    .c-article {{ width: 12%; }}
    .c-qty {{ width: 8%; }}
    .c-worker {{ width: 14%; }}
    .c-presented {{ width: 8%; }}
    .c-nonconforming {{ width: 8%; }}
    .c-good {{ width: 7%; }}
    .c-otk {{ width: 8%; }}
    .title {{ font-size: 13px; line-height: 1.15; }}
    .title span {{ font-size: 11px; }}
    .order {{ font-size: 11.5px; line-height: 1.15; }}
    .product-name, .product-article {{ font-size: 11.5px; line-height: 1.15; }}
    th {{ text-align: center; font-weight: bold; }}
    .num {{ text-align: center; white-space: nowrap; }}
    .text {{ text-align: left; }}
    .strong-value, .warehouse-warning {{ font-weight: 700; }}
    .warehouse-warning {{ margin-left: 6px; white-space: nowrap; }}
    .signature {{ height: 20px; color: #555; font-size: 8.5px; line-height: 1.05; text-align: center; vertical-align: bottom; }}
    .notes {{ height: 45px; }}
    @media print {{ .toolbar {{ display: none; }} .sheet {{ padding: 0; }} }}
  </style>
</head>
<body>
  <div class="toolbar"><button onclick="window.print()">Печать</button> <span>Листов: {len(sheets)}</span></div>
  {''.join(sheets)}
  {auto_print_script}
</body>
</html>"""
