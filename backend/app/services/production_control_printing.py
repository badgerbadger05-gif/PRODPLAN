from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy.orm import Session, joinedload

from ..models import Operation, ProductionProduct, ProductionStage, SpecOperation
from .production_control_common import date_to_iso as _date_to_iso, line_number as _line_number, to_float as _to_float
from .production_control_domain import ensure_state as _ensure_state, unit_display as _unit_display
from .production_control_material_availability import _components_for_product


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
