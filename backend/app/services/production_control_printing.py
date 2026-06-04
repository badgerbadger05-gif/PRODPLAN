from __future__ import annotations

import html
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy.orm import Session, joinedload

from ..models import (
    DefaultSpecification,
    Item,
    MrpRequirement,
    Operation,
    PlanningRun,
    ProductionPlanHeader,
    ProductionPlanLine,
    ProductionMaterialIssue,
    ProductionProduct,
    ProductionStage,
    StockWarehouse,
    SpecComponent,
    SpecOperation,
    SyncLink,
)
from .production_control_common import to_float as _to_float
from .production_control_domain import ensure_state as _ensure_state, unit_display as _unit_display
from .production_control_material_availability import _components_for_product
from .one_c_production_order_export import PRODUCTION_ORDER_ENTITY


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


def _bom_descendant_ids(db: Session, root_item_id: int) -> set[int]:
    result = {int(root_item_id)}
    spec_by_item: Dict[int, int] = {
        int(row.item_id): int(row.spec_id)
        for row in db.query(DefaultSpecification.item_id, DefaultSpecification.spec_id)
        .filter(DefaultSpecification.item_id == int(root_item_id))
        .all()
    }

    def visit(item_id: int, seen_specs: set[int]) -> None:
        spec_id = spec_by_item.get(int(item_id))
        if not spec_id or spec_id in seen_specs:
            return
        next_seen = set(seen_specs)
        next_seen.add(int(spec_id))
        for row in db.query(SpecComponent.item_id).filter(SpecComponent.spec_id == int(spec_id)).all():
            child_id = int(row.item_id)
            result.add(child_id)
            if child_id not in spec_by_item:
                ds = db.query(DefaultSpecification.spec_id).filter(DefaultSpecification.item_id == child_id).first()
                if ds:
                    spec_by_item[child_id] = int(ds.spec_id)
            visit(child_id, next_seen)

    visit(int(root_item_id), set())
    return result


def _route_context(db: Session, product: ProductionProduct) -> Dict[str, str]:
    order = product.order
    run: Optional[PlanningRun] = None
    if order and order.source_run_id:
        run = db.query(PlanningRun).filter(PlanningRun.run_id == int(order.source_run_id)).first()
    if run is None and product.source_mrp_requirement_id:
        req = db.query(MrpRequirement).filter(MrpRequirement.id == int(product.source_mrp_requirement_id)).first()
        if req:
            run = db.query(PlanningRun).filter(PlanningRun.run_id == int(req.run_id)).first()

    plan: Optional[ProductionPlanHeader] = None
    if run and run.source_plan_id:
        plan = db.query(ProductionPlanHeader).filter(ProductionPlanHeader.id == int(run.source_plan_id)).first()

    root_item: Optional[Item] = None
    if plan:
        root_ids = [
            int(row.item_id)
            for row in (
                db.query(ProductionPlanLine.item_id)
                .filter(ProductionPlanLine.plan_id == int(plan.id), ProductionPlanLine.qty > 0)
                .distinct()
                .all()
            )
        ]
        root_rows = (
            db.query(Item)
            .filter(Item.item_id.in_(root_ids))
            .order_by(Item.item_article.asc(), Item.item_name.asc())
            .all()
        )
        for item in root_rows:
            if int(product.item_id) in _bom_descendant_ids(db, int(item.item_id)):
                root_item = item
                break

    one_c_number = ""
    if order:
        link = (
            db.query(SyncLink)
            .filter(
                SyncLink.source_doctype == "production_order",
                SyncLink.source_id == int(order.order_id),
                SyncLink.target_entity == PRODUCTION_ORDER_ENTITY,
            )
            .one_or_none()
        )
        one_c_number = str(link.target_number or "") if link else ""

    plan_period = ""
    if plan:
        plan_period = f"{_date_ru(plan.period_from)} - {_date_ru(plan.period_to)}"
    elif run and (run.period_from or run.period_to):
        plan_period = f"{_date_ru(run.period_from)} - {_date_ru(run.period_to)}"

    return {
        "plan_name": str(plan.name or "") if plan else "",
        "plan_period": plan_period.strip(" -"),
        "root_item": _item_label(root_item),
        "one_c_number": one_c_number,
    }


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


def _material_transfer_rows(db: Session, product: ProductionProduct) -> List[Dict[str, str]]:
    issues = (
        db.query(ProductionMaterialIssue)
        .filter(
            ProductionMaterialIssue.product_id == int(product.product_id),
            ProductionMaterialIssue.direction == "issue",
            ProductionMaterialIssue.status != "cancelled",
        )
        .order_by(ProductionMaterialIssue.issue_id.asc())
        .all()
    )
    if not issues:
        return []

    warehouse_names = _warehouse_names(
        db,
        [
            ref
            for issue in issues
            for ref in (str(issue.source_warehouse_ref1c or ""), str(issue.warehouse_ref1c or ""))
        ],
    )
    state = getattr(product, "control_state", None)
    workshop = state.workshop if state and state.workshop else None
    workshop_name = str(workshop.resource_name or "") if workshop else ""

    links_by_issue_id: Dict[int, SyncLink] = {
        int(link.source_id): link
        for link in db.query(SyncLink)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "material_issue",
            SyncLink.source_id.in_([int(issue.issue_id) for issue in issues]),
        )
        .all()
    }

    rows: List[Dict[str, str]] = []
    for issue in issues:
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
    return rows


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
        order_date = _datetime_ru(product.order.order_date)
        route_ctx = _route_context(db, product)
        transfer_rows_data = _material_transfer_rows(db, product)
        order_number = html.escape(str(product.order.order_number or ""))
        one_c_number = html.escape(route_ctx["one_c_number"] or "—")
        title = f"МАРШРУТНЫЙ ЛИСТ № {order_number} от {now}"
        transfer_rows = "".join(
            "<tr>"
            f"<td colspan='2' class='text'>{html.escape(row['workshop_name'] or '—')}</td>"
            f"<td colspan='2' class='text'>{html.escape(row['transfer_number'] or '—')}</td>"
            f"<td colspan='2' class='text'>{html.escape(row['source_warehouse'] or '—')}</td>"
            f"<td class='text'>{html.escape(row['destination_warehouse'] or '—')}</td>"
            "</tr>"
            for row in transfer_rows_data
        ) or "<tr><td colspan='7'>Перемещения материалов не созданы</td></tr>"
        component_rows = "".join(
            "<tr>"
            f"<td colspan='2' class='text'>{html.escape(c['item_name'])}</td>"
            f"<td class='text'>{html.escape(c['item_article'])}</td>"
            f"<td class='num'>{c['qty_per_unit']:.3f}</td>"
            f"<td colspan='3' class='num'>{c['required_qty']:.3f}</td>"
            "</tr>"
            for c in components
        ) or "<tr><td colspan='7'>Материалы по спецификации не найдены</td></tr>"
        op_rows = "".join(
            "<tr>"
            f"<td class='num'>{op['number']}</td>"
            f"<td class='text'>{html.escape(op['stage_name'])}</td>"
            f"<td colspan='2' class='text'>{html.escape(op['operation_name'] or op['stage_name'] or 'Операция')}</td>"
            f"<td class='num'>{op['time_norm']:.3f}</td>"
            "<td></td><td></td><td></td>"
            "</tr>"
            for op in operations
        ) or "<tr><td colspan='7'>Операции по спецификации не найдены</td></tr>"
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
                  <col class="c-otk">
                </colgroup>
                <tr>
                  <td colspan="4" class="title">{title}<br><span>(Изготовление новых)</span></td>
                  <td colspan="3" class="order">
                    <b>Заказ PRODPLAN:</b> №{order_number}<br>
                    <b>Номер 1С:</b> {one_c_number}<br>
                    <b>Дата заказа:</b> {html.escape(order_date)}
                  </td>
                </tr>
                <tr>
                  <td colspan="4"><b>Наименование:</b><br>{html.escape(str(product.item.item_name or ""))}</td>
                  <td><b>Артикул:</b><br>{html.escape(str(product.item.item_article or ""))}</td>
                  <td colspan="2"><b>Количество:</b><br>{_to_float(product.remaining_qty) or _to_float(product.quantity):g} {html.escape(_unit_display(db, product.item.unit))}</td>
                </tr>
                <tr>
                  <td colspan="4"><b>План:</b><br>{html.escape(route_ctx["plan_name"] or "—")}</td>
                  <td><b>Период:</b><br>{html.escape(route_ctx["plan_period"] or "—")}</td>
                  <td colspan="2"><b>Корневое изделие:</b><br>{html.escape(route_ctx["root_item"] or "—")}</td>
                </tr>
                <tr><td colspan="7"><b>Маршрут перемещения материалов</b></td></tr>
                <tr><th colspan="2">Участок получатель</th><th colspan="2">№ перемещения</th><th colspan="2">Склад отправитель</th><th>Склад получатель</th></tr>
                {transfer_rows}
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
    @page {{ size: A4 portrait; margin: 8mm; }}
    body {{ font-family: "Times New Roman", serif; color: #000; margin: 0; }}
    .toolbar {{ position: sticky; top: 0; padding: 8px; background: #f4f6f8; border-bottom: 1px solid #cfd8dc; font-family: Arial, sans-serif; }}
    .toolbar button {{ padding: 6px 12px; }}
    .sheet {{ page-break-after: always; padding: 6px; }}
    table.route {{ border-collapse: collapse; width: 100%; table-layout: fixed; font-size: 12px; }}
    .route td, .route th {{ border: 1px solid #000; padding: 3px 4px; vertical-align: top; overflow-wrap: anywhere; }}
    .c-num {{ width: 6%; }}
    .c-material {{ width: 29%; }}
    .c-article {{ width: 16%; }}
    .c-qty {{ width: 11%; }}
    .c-worker {{ width: 17%; }}
    .c-otk {{ width: 10%; }}
    .title {{ font-size: 16px; line-height: 1.2; }}
    .title span {{ font-size: 14px; }}
    .order {{ font-size: 12px; line-height: 1.25; }}
    th {{ text-align: center; font-weight: bold; }}
    .num {{ text-align: center; white-space: nowrap; }}
    .text {{ text-align: left; }}
    .notes {{ height: 90px; }}
    @media print {{ .toolbar {{ display: none; }} .sheet {{ padding: 0; }} }}
  </style>
</head>
<body>
  <div class="toolbar"><button onclick="window.print()">Печать</button> <span>Листов: {len(sheets)}</span></div>
  {''.join(sheets)}
</body>
</html>"""
