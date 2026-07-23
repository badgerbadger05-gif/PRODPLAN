"""Борд давальческого контура переработки (питатель №3, фаза 4).

Read-only мониторинг для снабжения/ПДО (дока §5 питатель-3-гальваника):
по каждой processing-позиции — зона/NFP с разложением (полка, труба
переработчика, цепочечное слагаемое голой), открытые заказы переработчику с
возрастом и алертом просроченного кругорейса (старше
settings.processing_roundtrip_days).

Возраст партии считается от фактической передачи в переработку, а пока её нет
— от даты заказа. Дата отчёта переработчика определяет завершённый этап
документооборота, но открытая строка остаётся видна на борде до закрытия трубы.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session, joinedload

from ...models import (
    DbrSupermarketPosition,
    Item,
)
from . import feeder_nfp_service
from .processing_supplier_orders import processing_history_rows, processing_order_rows
from .settings_service import read_settings
from ..processing_stock_sync import processing_stock_status, processing_stock_totals


def _age_days(order_date: Any, today: date) -> int | None:
    if order_date is None:
        return None
    value = order_date.date() if isinstance(order_date, datetime) else order_date
    return (today - value).days


def _roundtrip_kpi(rows: list[dict[str, Any]], limit_days: int) -> dict[str, Any]:
    """Aggregate the document-date round-trip proxy, weighted by received qty."""
    valid = [row for row in rows if row["duration_days"] is not None]
    completed_qty = sum(float(row["received_qty"]) for row in valid)
    weighted_days = sum(
        float(row["duration_days"]) * float(row["received_qty"]) for row in valid
    )
    order_ids = {int(row["order_id"]) for row in valid}
    within = [row for row in valid if int(row["duration_days"]) <= limit_days]
    return {
        "semantics": "processing_report_date - processing_transfer_date; received_qty weighted",
        "eligible_rows": len(rows),
        "completed_rows": len(valid),
        "completed_orders": len(order_ids),
        "completed_qty": round(completed_qty, 4),
        "weighted_avg_days": (
            round(weighted_days / completed_qty, 2) if completed_qty > 0 else None
        ),
        "max_days": max((int(row["duration_days"]) for row in valid), default=None),
        "within_roundtrip_rows": len(within),
        "within_roundtrip_qty": round(
            sum(float(row["received_qty"]) for row in within), 4
        ),
        "invalid_date_rows": sum(bool(row["invalid_dates"]) for row in rows),
    }


def processing_board(db: Session, *, today: date | None = None) -> dict[str, Any]:
    settings = read_settings(db)
    limit_days = int(settings.processing_roundtrip_days or 14)
    today = today or date.today()

    positions = (
        db.query(DbrSupermarketPosition)
        .options(joinedload(DbrSupermarketPosition.item))
        .filter(DbrSupermarketPosition.supply_type == "processing")
        .filter(DbrSupermarketPosition.is_active.is_(True))
        .order_by(DbrSupermarketPosition.id.asc())
        .all()
    )
    live = feeder_nfp_service.live_nfp_rows(db, positions)

    item_ids = sorted({int(row.item_id) for row in positions})
    at_contractor_by_item = processing_stock_totals(db, set(item_ids))
    orders_by_item: dict[int, list[dict[str, Any]]] = {}
    if item_ids:
        for line, order in processing_order_rows(db, item_ids):
            age = _age_days(
                order.processing_transfer_date or order.order_date,
                today,
            )
            if order.processing_report_date and float(line.received_qty or 0) > 0:
                stage = "reported"
            elif order.processing_transfer_date:
                stage = "transferred"
            else:
                stage = "ordered"
            orders_by_item.setdefault(int(line.item_id_ref), []).append(
                {
                    "order_id": int(order.order_id),
                    "line_id": int(line.item_id),
                    "line_number": int(line.line_number) if line.line_number is not None else None,
                    "order_number": str(order.order_number or ""),
                    "order_date": order.order_date.isoformat() if order.order_date else None,
                    "transfer_date": (
                        order.processing_transfer_date.isoformat()
                        if order.processing_transfer_date
                        else None
                    ),
                    "report_date": (
                        order.processing_report_date.isoformat()
                        if order.processing_report_date
                        else None
                    ),
                    "stage": stage,
                    "remaining_qty": float(line.remaining_qty or 0),
                    "age_days": age,
                    "overdue": bool(age is not None and age > limit_days),
                }
            )

    kpi_rows_by_item: dict[int, list[dict[str, Any]]] = {}
    kpi_rows_by_contractor: dict[int, list[dict[str, Any]]] = {}
    contractor_meta: dict[int, dict[str, Any]] = {}
    for line, order, supplier in processing_history_rows(db, item_ids):
        received_qty = float(line.received_qty or 0)
        transfer = order.processing_transfer_date
        report = order.processing_report_date
        duration = None
        invalid_dates = False
        if received_qty > 0 and transfer is not None and report is not None:
            duration = (report.date() - transfer.date()).days
            if duration < 0:
                duration = None
                invalid_dates = True
        metric_row = {
            "order_id": int(order.order_id),
            "received_qty": received_qty,
            "duration_days": duration,
            "invalid_dates": invalid_dates,
        }
        item_id = int(line.item_id_ref)
        supplier_id = int(supplier.supplier_id)
        kpi_rows_by_item.setdefault(item_id, []).append(metric_row)
        kpi_rows_by_contractor.setdefault(supplier_id, []).append(metric_row)
        contractor_meta[supplier_id] = {
            "supplier_id": supplier_id,
            "supplier_ref1c": str(supplier.supplier_ref1c or ""),
            "supplier_name": str(supplier.supplier_name or ""),
        }

    rows: list[dict[str, Any]] = []
    overdue_positions = 0
    for position in positions:
        item: Item | None = position.item
        nfp = live.get(int(position.id), {})
        orders = orders_by_item.get(int(position.item_id), [])
        has_overdue = any(order["overdue"] for order in orders)
        if has_overdue:
            overdue_positions += 1
        rows.append(
            {
                "position_id": int(position.id),
                "item_id": int(position.item_id),
                "item_code": str(item.item_code or "") if item else "",
                "item_article": str(item.item_article or "") if item else "",
                "item_name": str(item.item_name or "") if item else "",
                "adu": float(position.adu or 0),
                "rt_days": float(position.rt_days or 0),
                "trip_interval_days": float(position.batch_days or 0),
                "red_qty": float(position.red_qty or 0),
                "yellow_qty": float(position.yellow_qty or 0),
                "target_qty": float(position.target_qty or 0),
                "nfp": nfp.get("nfp"),
                "zone": nfp.get("zone"),
                "penetration": nfp.get("penetration"),
                "stock_qty": nfp.get("stock_qty"),
                "open_supply_qty": nfp.get("open_supply_qty"),
                "chain_supply_qty": nfp.get("chain_supply_qty"),
                # Kept separate from NFP/open_supply_qty: supplier-order
                # remaining quantity and the balance register can describe the
                # same processing pipe and must not be added together.
                "at_contractor_qty": at_contractor_by_item.get(
                    int(position.item_id), 0.0
                ),
                "is_complete": nfp.get("is_complete"),
                "missing_reasons": nfp.get("missing_reasons") or [],
                "open_orders": orders,
                "has_overdue": has_overdue,
                "roundtrip_kpi": _roundtrip_kpi(
                    kpi_rows_by_item.get(int(position.item_id), []), limit_days
                ),
            }
        )

    contractor_kpis = []
    for supplier_id in sorted(
        kpi_rows_by_contractor,
        key=lambda value: (
            contractor_meta[value]["supplier_name"].casefold(),
            value,
        ),
    ):
        contractor_kpis.append(
            {
                **contractor_meta[supplier_id],
                "roundtrip_kpi": _roundtrip_kpi(
                    kpi_rows_by_contractor[supplier_id], limit_days
                ),
            }
        )

    return {
        "roundtrip_limit_days": limit_days,
        "roundtrip_kpi_semantics": (
            "Proxy from synced 1C document dates: processing_report_date minus "
            "processing_transfer_date, weighted by positive received_qty"
        ),
        "positions": rows,
        "contractors": contractor_kpis,
        "processing_stock": processing_stock_status(db),
        "positions_total": len(rows),
        "overdue_positions": overdue_positions,
        "generated_at": datetime.now().isoformat(),
    }
