"""Борд давальческого контура переработки (питатель №3, фаза 4).

Read-only мониторинг для снабжения/ПДО (дока §5 питатель-3-гальваника):
по каждой processing-позиции — зона/NFP с разложением (полка, труба
переработчика, цепочечное слагаемое голой), открытые заказы переработчику с
возрастом и алертом просроченного кругорейса (старше
settings.processing_roundtrip_days).

Дата отправки давальческого сырья в 1С не синхронизируется (регистр
ЗапасыПереданные вне синка), поэтому возраст партии у подрядчика считается от
даты заказа переработчику — консервативная верхняя оценка кругорейса.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session, joinedload

from ...models import (
    DbrSupermarketPosition,
    Item,
    SupplierOrder,
    SupplierOrderItem,
)
from . import feeder_nfp_service
from .settings_service import get_or_create_settings


def _age_days(order_date: Any, today: date) -> int | None:
    if order_date is None:
        return None
    value = order_date.date() if isinstance(order_date, datetime) else order_date
    return (today - value).days


def processing_board(db: Session, *, today: date | None = None) -> dict[str, Any]:
    settings = get_or_create_settings(db)
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
    orders_by_item: dict[int, list[dict[str, Any]]] = {}
    if item_ids:
        for line, order in (
            db.query(SupplierOrderItem, SupplierOrder)
            .join(SupplierOrder, SupplierOrder.order_id == SupplierOrderItem.order_id)
            .filter(
                SupplierOrderItem.item_id_ref.in_(item_ids),
                SupplierOrderItem.remaining_qty > 0,
                SupplierOrder.deletion_mark.is_(False),
            )
            .order_by(SupplierOrder.order_date.asc())
            .all()
        ):
            age = _age_days(order.order_date, today)
            orders_by_item.setdefault(int(line.item_id_ref), []).append(
                {
                    "order_number": str(order.order_number or ""),
                    "order_date": order.order_date.isoformat() if order.order_date else None,
                    "remaining_qty": float(line.remaining_qty or 0),
                    "age_days": age,
                    "overdue": bool(age is not None and age > limit_days),
                }
            )

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
                "is_complete": nfp.get("is_complete"),
                "missing_reasons": nfp.get("missing_reasons") or [],
                "open_orders": orders,
                "has_overdue": has_overdue,
            }
        )

    return {
        "roundtrip_limit_days": limit_days,
        "positions": rows,
        "positions_total": len(rows),
        "overdue_positions": overdue_positions,
        "generated_at": datetime.now().isoformat(),
    }
