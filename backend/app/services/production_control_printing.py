from __future__ import annotations

import html
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..models import (
    IgnoredWarehouse,
    Item,
    MrpRequirement,
    Operation,
    PaintWeldChainLink,
    PlanningRun,
    StockBin,
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
from . import planning_truth
from .one_c_production_order_export import PRODUCTION_ORDER_ENTITY
from .bom_specification_resolver import BomSpecificationResolver
from .production_output_truth import accepted_product_output


ROUTE_SHEET_SNAPSHOT_VERSION = 1


def mark_route_sheets_printed(db: Session, product_ids: Iterable[int]) -> int:
    # Legacy behavior: keep automatic chain expansion for direct callers that
    # still operate on operational IDs.
    ids = [int(pid) for pid in product_ids]
    if not ids:
        return 0

    # Строки цепочки «окраска↔сварка» штампуются вместе: лист один на оба заказа.
    render_ids, weld_pid_by_paint_pid = _paint_weld_chain_for_ids(db, ids)
    unique_ids = sorted(set(ids) | set(render_ids) | set(weld_pid_by_paint_pid.values()))
    return _persist_route_sheets_printed_by_ids(db, unique_ids, return_ids=ids)


def mark_route_sheets_printed_by_snapshot_members(
    db: Session,
    product_ids: Iterable[int],
) -> int:
    # Exact write path for snapshot-backed prints: caller already owns exact members.
    ids = [int(pid) for pid in product_ids]
    return _persist_route_sheets_printed_by_ids(db, ids, return_ids=ids)


def _persist_route_sheets_printed_by_ids(
    db: Session,
    ids: Sequence[int],
    *,
    return_ids: Sequence[int] | None = None,
) -> int:
    unique_ids = sorted({pid for pid in ids if pid is not None})
    if not unique_ids:
        return 0

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
    now = datetime.now(timezone.utc)
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
    compare_ids = return_ids if return_ids is not None else unique_ids
    requested = [pid for pid in compare_ids if pid is not None]
    return sum(1 for pid in requested if pid in existing_ids)


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
    resolver = BomSpecificationResolver(db)
    return {
        item_id: int(spec_id)
        for item_id in ids
        if (spec_id := resolver.default_spec_id(item_id)) is not None
    }


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


def _multi_stock_warehouse_item_ids(
    db: Session,
    item_ids: Sequence[int],
    *,
    ledger_generation_id: int | None = None,
) -> set[int]:
    ids = sorted({int(item_id) for item_id in item_ids if item_id is not None})
    if not ids:
        return set()

    if ledger_generation_id is None:
        try:
            truth = planning_truth.get_truth_state(db)
        except Exception:
            return set()
        if not truth.generation_id:
            return set()
        ledger_generation_id = int(truth.generation_id)
    else:
        ledger_generation_id = int(ledger_generation_id)

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
        db.query(StockBin.item_id, StockBin.warehouse_ref1c)
        .filter(
            StockBin.ledger_generation_id == ledger_generation_id,
            StockBin.item_id.in_(ids),
            StockBin.on_hand > 0,
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
    return BomSpecificationResolver(db).descendant_ids_by_root(roots)


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


def _paint_weld_chain_for_ids(
    db: Session, product_ids: Sequence[int]
) -> Tuple[List[int], Dict[int, int]]:
    """
    Схлопнуть строки цепочки «окраска↔сварка» (paint_weld_chain_links) в один
    лист: печать с любой стороны цепочки даёт лист окрасочного (родительского)
    заказа, выбор обеих строк не даёт дубля.

    Возвращает (render_ids, weld_pid_by_paint_pid): render_ids — product_id к
    печати в исходном порядке (сварная сторона заменена окрасочным якорем);
    weld_pid_by_paint_pid — {окрасочный product_id: сварочный product_id}.
    """
    ids = [int(pid) for pid in product_ids]
    if not ids:
        return [], {}
    # Один запрос в обычном (нецепочечном) случае: подзапрос по заказам
    # выбранных продуктов вместо отдельной выборки order_id.
    order_subquery = (
        select(ProductionProduct.order_id)
        .where(ProductionProduct.product_id.in_(sorted(set(ids))))
        .scalar_subquery()
    )
    links = (
        db.query(PaintWeldChainLink)
        .filter(
            (PaintWeldChainLink.painted_order_id.in_(order_subquery))
            | (PaintWeldChainLink.welded_order_id.in_(order_subquery))
        )
        .all()
    )
    if not links:
        return ids, {}

    order_by_pid = {
        int(pid): int(oid)
        for pid, oid in db.query(ProductionProduct.product_id, ProductionProduct.order_id)
        .filter(ProductionProduct.product_id.in_(sorted(set(ids))))
        .all()
        if oid is not None
    }

    chain_order_ids = sorted(
        {int(link.painted_order_id) for link in links}
        | {int(link.welded_order_id) for link in links}
    )
    pid_by_order: Dict[int, int] = {}
    for pid, oid in (
        db.query(ProductionProduct.product_id, ProductionProduct.order_id)
        .filter(ProductionProduct.order_id.in_(chain_order_ids))
        .order_by(
            ProductionProduct.order_id.asc(),
            ProductionProduct.line_number.asc(),
            ProductionProduct.product_id.asc(),
        )
        .all()
    ):
        pid_by_order.setdefault(int(oid), int(pid))

    weld_pid_by_paint_pid: Dict[int, int] = {}
    paint_pid_by_order: Dict[int, int] = {}
    for link in links:
        paint_pid = pid_by_order.get(int(link.painted_order_id))
        weld_pid = pid_by_order.get(int(link.welded_order_id))
        if paint_pid is None or weld_pid is None:
            continue
        weld_pid_by_paint_pid[paint_pid] = weld_pid
        paint_pid_by_order[int(link.painted_order_id)] = paint_pid
        paint_pid_by_order[int(link.welded_order_id)] = paint_pid

    render_ids: List[int] = []
    seen_anchors: set[int] = set()
    for pid in ids:
        anchor = paint_pid_by_order.get(order_by_pid.get(pid, -1))
        if anchor is None:
            render_ids.append(pid)  # вне цепочки — прежнее поведение
            continue
        if anchor in seen_anchors:
            continue
        seen_anchors.add(anchor)
        render_ids.append(anchor)
    return render_ids, weld_pid_by_paint_pid


def _paint_weld_anchor_by_product(
    db: Session, product_ids: Sequence[int]
) -> Dict[int, int]:
    ids = [int(product_id) for product_id in product_ids]
    if not ids:
        return {}
    anchors: Dict[int, int] = {product_id: int(product_id) for product_id in ids}

    order_by_product_id = {
        int(product_id): int(order_id)
        for product_id, order_id in (
            db.query(ProductionProduct.product_id, ProductionProduct.order_id)
            .filter(ProductionProduct.product_id.in_(sorted(set(ids))))
            .all()
        )
        if order_id is not None
    }
    if not order_by_product_id:
        return anchors

    links = (
        db.query(PaintWeldChainLink)
        .filter(
            (PaintWeldChainLink.painted_order_id.in_(order_by_product_id.values()))
            | (PaintWeldChainLink.welded_order_id.in_(order_by_product_id.values()))
        )
        .all()
    )
    if not links:
        return anchors

    chain_order_ids = sorted(
        {int(link.painted_order_id) for link in links}
        | {int(link.welded_order_id) for link in links}
    )
    if not chain_order_ids:
        return anchors

    pid_by_order: Dict[int, int] = {}
    for pid, oid in (
        db.query(ProductionProduct.product_id, ProductionProduct.order_id)
        .filter(ProductionProduct.order_id.in_(chain_order_ids))
        .order_by(
            ProductionProduct.order_id.asc(),
            ProductionProduct.line_number.asc(),
            ProductionProduct.product_id.asc(),
        )
        .all()
    ):
        if oid is not None:
            pid_by_order.setdefault(int(oid), int(pid))

    for link in links:
        paint_pid = pid_by_order.get(int(link.painted_order_id))
        weld_pid = pid_by_order.get(int(link.welded_order_id))
        if paint_pid is None or weld_pid is None:
            continue
        anchors[int(weld_pid)] = int(paint_pid)
        anchors.setdefault(int(paint_pid), int(paint_pid))

    return anchors


def _route_sheet_print_data(
    db: Session,
    product_ids: Sequence[int],
    *,
    ledger_generation_id: int | None = None,
) -> Tuple[
    List[ProductionProduct],
    Dict[int, List[Dict[str, Any]]],
    Dict[int, List[Dict[str, Any]]],
    Dict[int, Dict[str, str]],
    Dict[int, List[Dict[str, str]]],
    Dict[str, str],
    Dict[int, Dict[str, Any]],
]:
    ids = [int(product_id) for product_id in product_ids]
    if not ids:
        return [], {}, {}, {}, {}, {}, {}

    render_ids, weld_pid_by_paint_pid = _paint_weld_chain_for_ids(db, ids)
    fetch_ids = sorted(set(render_ids) | set(weld_pid_by_paint_pid.values()))
    products = (
        db.query(ProductionProduct)
        .options(
            joinedload(ProductionProduct.order),
            joinedload(ProductionProduct.item),
            joinedload(ProductionProduct.control_state).joinedload(ProductionOrderLineState.workshop),
        )
        .filter(ProductionProduct.product_id.in_(fetch_ids))
        .all()
    )
    product_map = {int(product.product_id): product for product in products}
    ordered = [product_map[product_id] for product_id in render_ids if product_id in product_map]

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
    multi_stock_item_ids = _multi_stock_warehouse_item_ids(
        db,
        sorted(component_item_ids),
        ledger_generation_id=ledger_generation_id,
    )
    components_by_product_id: Dict[int, List[Dict[str, Any]]] = {}
    for product in products:
        spec_id = spec_id_by_product_id.get(int(product.product_id))
        qty = _to_float(accepted_product_output(product).remaining_qty)
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

    # Цепочка «окраска↔сварка»: лист один, якорь — окрасочный продукт.
    # Состав («Материалы и заготовки») — от сварной детали, перемещения — обоих
    # заказов (сначала сварка), операции — двумя блоками (см. renderer).
    chain_info_by_product_id: Dict[int, Dict[str, Any]] = {}
    for paint_pid, weld_pid in weld_pid_by_paint_pid.items():
        weld_product = product_map.get(weld_pid)
        if paint_pid not in product_map or weld_product is None:
            continue
        components_by_product_id[paint_pid] = components_by_product_id.get(weld_pid, [])
        transfer_rows[paint_pid] = (
            transfer_rows.get(weld_pid, []) + transfer_rows.get(paint_pid, [])
        )
        chain_info_by_product_id[paint_pid] = {
            "weld_product_id": weld_pid,
            "weld_qty": _to_float(accepted_product_output(weld_product).remaining_qty),
            "weld_one_c": (route_contexts.get(weld_pid) or {}).get("one_c_number", ""),
            "weld_order_number": str(weld_product.order.order_number or "") if weld_product.order else "",
        }

    return (
        ordered,
        components_by_product_id,
        operations_by_product_id,
        route_contexts,
        transfer_rows,
        unit_by_raw,
        chain_info_by_product_id,
    )


def _route_sheet_payload_from_data(
    payload_product: ProductionProduct,
    components_by_product_id: Mapping[int, List[Dict[str, Any]]],
    operations_by_product_id: Mapping[int, List[Dict[str, Any]]],
    route_contexts: Mapping[int, Dict[str, str]],
    transfer_rows_by_product_id: Mapping[int, List[Dict[str, str]]],
    unit_by_raw: Mapping[str, str],
    chain_info_by_product_id: Mapping[int, Dict[str, Any]],
) -> Dict[str, Any]:
    product = payload_product
    product_id = int(product.product_id)
    route_ctx = route_contexts.get(product_id, {})
    components = components_by_product_id.get(product_id, [])
    operations = operations_by_product_id.get(product_id, [])
    transfer_rows = transfer_rows_by_product_id.get(product_id, [])
    product_unit = unit_by_raw.get(str(product.item.unit or "").strip(), "")
    chain_info = chain_info_by_product_id.get(product_id)

    weld_operations: List[Dict[str, Any]] = []
    chain_payload: Dict[str, Any] = {}
    if chain_info:
        weld_product_id = int(chain_info.get("weld_product_id") or -1)
        weld_operations = list(operations_by_product_id.get(weld_product_id, []))
        chain_payload = {
            "weld_product_id": weld_product_id,
            "weld_qty": _to_float(chain_info.get("weld_qty")),
            "weld_one_c": str(chain_info.get("weld_one_c") or ""),
            "weld_order_number": str(chain_info.get("weld_order_number") or ""),
        }

    return {
        "product_id": product_id,
        "order_id": int(product.order_id),
        "order_number": str(product.order.order_number or ""),
        "order_date": _datetime_ru(product.order.order_date),
        "one_c_number": str(route_ctx.get("one_c_number") or ""),
        "item_name": str(product.item.item_name or ""),
        "item_article": str(product.item.item_article or ""),
        "item_code": str(product.item.item_code or ""),
        "unit": product_unit,
        "remaining_qty": _to_float(accepted_product_output(product).remaining_qty),
        "components": [
            {
                "component_item_id": int(component.get("component_item_id", -1)),
                "item_code": str(component.get("item_code") or ""),
                "item_name": str(component.get("item_name") or ""),
                "item_article": str(component.get("item_article") or ""),
                "qty_per_unit": _to_float(component.get("qty_per_unit")),
                "required_qty": _to_float(component.get("required_qty")),
                "multi_stock_warning": bool(component.get("multi_stock_warning")),
            }
            for component in components
        ],
        "operations": [
            {
                "number": int(op.get("number") or 0),
                "stage_name": str(op.get("stage_name") or ""),
                "operation_name": str(op.get("operation_name") or ""),
                "time_norm": _to_float(op.get("time_norm")),
            }
            for op in operations
        ],
        "chain": chain_payload,
        "weld_operations": [
            {
                "number": int(op.get("number") or 0),
                "stage_name": str(op.get("stage_name") or ""),
                "operation_name": str(op.get("operation_name") or ""),
                "time_norm": _to_float(op.get("time_norm")),
            }
            for op in weld_operations
        ],
        "transfer_rows": [
            {
                "transfer_number": str(row.get("transfer_number") or ""),
                "workshop_name": str(row.get("workshop_name") or ""),
                "source_warehouse": str(row.get("source_warehouse") or ""),
                "destination_warehouse": str(row.get("destination_warehouse") or ""),
            }
            for row in transfer_rows
        ],
        "route_context": {
            "plan_name": str(route_ctx.get("plan_name") or ""),
            "plan_period": str(route_ctx.get("plan_period") or ""),
            "root_item": str(route_ctx.get("root_item") or ""),
        },
    }


def build_route_sheet_snapshot_payloads(
    db: Session,
    product_ids: Sequence[int],
    *,
    ledger_generation_id: int | None = None,
) -> Dict[int, Dict[str, Any]]:
    ids = [int(product_id) for product_id in product_ids]
    if not ids:
        return {}
    (
        ordered,
        components_by_product_id,
        operations_by_product_id,
        route_contexts,
        transfer_rows_by_product_id,
        unit_by_raw,
        chain_info_by_product_id,
    ) = _route_sheet_print_data(
        db,
        ids,
        ledger_generation_id=ledger_generation_id,
    )

    payload_by_anchor: Dict[int, Dict[str, Any]] = {}
    for product in ordered:
        anchor_payload = _route_sheet_payload_from_data(
            payload_product=product,
            components_by_product_id=components_by_product_id,
            operations_by_product_id=operations_by_product_id,
            route_contexts=route_contexts,
            transfer_rows_by_product_id=transfer_rows_by_product_id,
            unit_by_raw=unit_by_raw,
            chain_info_by_product_id=chain_info_by_product_id,
        )
        payload_by_anchor[int(product.product_id)] = {
            "version": ROUTE_SHEET_SNAPSHOT_VERSION,
            "anchor_product_id": int(product.product_id),
            "sheet": anchor_payload,
        }

    anchors_by_product = _paint_weld_anchor_by_product(db, ids)
    result: Dict[int, Dict[str, Any]] = {}
    for product_id in ids:
        anchor_product_id = int(anchors_by_product.get(product_id, product_id))
        anchor_payload = payload_by_anchor.get(anchor_product_id)
        if anchor_payload is None:
            continue
        result[int(product_id)] = anchor_payload
    return result


def _operation_rows_html(operations: Sequence[Dict[str, Any]]) -> str:
    return "".join(
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
    )


def _operation_block_header_html(label: str) -> str:
    return f"<tr><td colspan='10' class='op-block'><b>{html.escape(label)}</b></td></tr>"


def _render_route_sheets_html_from_payloads(
    payloads: Sequence[Mapping[str, Any]],
    *,
    auto_print: bool = False,
) -> str:
    now = datetime.now().strftime("%d.%m.%Y")
    sheets: List[str] = []
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        raw_sheet = payload.get("sheet")
        if isinstance(raw_sheet, Mapping):
            sheet = dict(raw_sheet)
        else:
            sheet = dict(payload)
        components = sheet.get("components", [])
        operations = sheet.get("operations", [])
        route_ctx = sheet.get("route_context", {})
        transfer_rows_data = sheet.get("transfer_rows", [])
        chain = sheet.get("chain") or {}
        weld_operations = sheet.get("weld_operations", [])
        product_unit = str(sheet.get("unit") or "")
        order_number = html.escape(str(sheet.get("order_number") or ""))
        one_c_number = html.escape(str(sheet.get("one_c_number") or "—"))
        title = f"МАРШРУТНЫЙ ЛИСТ № {order_number} от {now}" if order_number else "МАРШРУТНЫЙ ЛИСТ"
        warehouse_warning_html = ' <strong class="warehouse-warning">проверь склады</strong>'
        transfer_rows = "".join(
            "<tr>"
            f"<td colspan='2' class='text strong-value'>{html.escape(row.get('workshop_name') or '—')}</td>"
            f"<td colspan='2' class='text'>{html.escape(row.get('transfer_number') or '—')}</td>"
            f"<td colspan='3' class='text strong-value'>{html.escape(row.get('source_warehouse') or '—')}</td>"
            f"<td colspan='3' class='text strong-value'>{html.escape(row.get('destination_warehouse') or '—')}</td>"
            "</tr>"
            for row in transfer_rows_data
        ) or "<tr><td colspan='10'>Перемещения материалов не созданы</td></tr>"
        component_rows = "".join(
            "<tr>"
            f"<td colspan='2' class='text'>{html.escape(str(row.get('item_name') or ''))}"
            f"{warehouse_warning_html if row.get('multi_stock_warning') else ''}</td>"
            f"<td class='text strong-value'>{html.escape(str(row.get('item_article') or ''))}</td>"
            f"<td class='num'>{_to_float(row.get('qty_per_unit')):.3f}</td>"
            f"<td colspan='6' class='num'>{_to_float(row.get('required_qty')):.3f}</td>"
            "</tr>"
            for row in components
        ) or "<tr><td colspan='10'>Материалы по спецификации не найдены</td></tr>"
        empty_ops_row = "<tr><td colspan='10'>Операции по спецификации не найдены</td></tr>"
        if chain:
            paint_qty = _to_float(sheet.get("remaining_qty"))
            weld_no = str(chain.get("weld_one_c") or chain.get("weld_order_number") or "—")
            paint_no = str(sheet.get("one_c_number") or order_number or "—")
            op_rows = (
                _operation_block_header_html(
                    f"Сварка — заказ 1С №{weld_no} · {_to_float(chain.get('weld_qty')):g} {product_unit}".rstrip()
                )
                + (_operation_rows_html(weld_operations) or empty_ops_row)
                + _operation_block_header_html(
                    f"Окраска — заказ 1С №{paint_no} · {paint_qty:g} {product_unit}".rstrip()
                )
                + (_operation_rows_html(operations) or empty_ops_row)
            )
        else:
            op_rows = _operation_rows_html(operations) or empty_ops_row
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
                    <b>Дата заказа:</b> {html.escape(str(sheet.get('order_date') or '—'))}
                  </td>
                </tr>
                <tr>
                  <td colspan="5" class="product-name"><b>Наименование:</b><br>{html.escape(str(sheet.get('item_name') or ''))}</td>
                  <td class="product-article"><b>Артикул:</b><br><strong class="strong-value">{html.escape(str(sheet.get('item_article') or ''))}</strong></td>
                  <td colspan="4"><b>Количество:</b><br>{_to_float(sheet.get('remaining_qty')):g} {html.escape(product_unit)}</td>
                </tr>
                <tr>
                  <td colspan="5"><b>План:</b><br>{html.escape(str(route_ctx.get("plan_name") or "—"))}</td>
                  <td><b>Период:</b><br>{html.escape(str(route_ctx.get("plan_period") or "—"))}</td>
                  <td colspan="4"><b>Корневое изделие:</b><br>{html.escape(str(route_ctx.get("root_item") or "—"))}</td>
                </tr>
                <tr><td colspan="10"><b>Маршрут перемещения материалов</b></td></tr>
                <tr><th colspan="2">Участок получатель</th><th colspan="2">№ перемещения</th><th colspan="3">Склад отправитель</th><th colspan="3">Склад получатель</th></tr>
                {transfer_rows}
                <tr><td colspan="10" class="material-signoff"><b>Материал выдан:</b> дата __________ подпись __________</td></tr>
                <tr><td colspan="10" class="material-signoff"><b>Материал получен:</b> дата __________ подпись __________</td></tr>
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
    .material-signoff {{ height: 18px; font-size: 10.5px; vertical-align: middle; }}
    .op-block {{ background: #eee; font-size: 11px; }}
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


def render_route_sheets_from_snapshots(
    route_sheet_payloads: Sequence[Mapping[str, Any]],
    *,
    auto_print: bool = False,
) -> str:
    ordered: List[Mapping[str, Any]] = [payload for payload in route_sheet_payloads if payload]
    return _render_route_sheets_html_from_payloads(ordered, auto_print=auto_print)
