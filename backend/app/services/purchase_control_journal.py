"""
Purchase control journal ("Журнал закупок").

Read model over supplier orders synced from 1C (Document_ЗаказПоставщику)
plus not-yet-ordered MRP purchase needs (planned_purchase rows of the latest
FIXED_SNAPSHOT run without a successful SyncLink). Ordering itself reuses
POST /v1/plan/results/{run_id}/purchases/export-to-1c; receipts come from 1C
via supplier order sync. See .docs/purchase_journal_plan.md.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models import (
    Item,
    PlannedPurchase,
    PlanningRun,
    Supplier,
    SupplierOrder,
    SupplierOrderItem,
    SyncLink,
)
from .one_c_purchase_order_export import PURCHASE_ORDER_ENTITY
from .planning_service import SUPPLIER_ORDER_EXCLUDED_STATE_NAMES

_EPS = 1e-9

# line_status values, in display priority order
LINE_STATUSES = ("to_order", "overdue", "no_date", "expected", "partial", "received", "closed")


def _normalize_state(name: Optional[str]) -> str:
    return (name or "").strip().lower().replace("ё", "е")


def _order_is_active(order: SupplierOrder) -> bool:
    if bool(order.deletion_mark):
        return False
    return _normalize_state(order.order_state_name) not in SUPPLIER_ORDER_EXCLUDED_STATE_NAMES


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _date_to_iso(value: Any) -> Optional[str]:
    d = _to_date(value)
    return d.isoformat() if d else None


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _line_status(
    *,
    remaining_qty: float,
    received_qty: float,
    delivery_date: Optional[date],
    today: date,
    order_active: bool,
) -> str:
    if not order_active:
        return "closed"
    if remaining_qty <= _EPS:
        return "received"
    if delivery_date is not None and delivery_date < today:
        return "overdue"
    if received_qty > _EPS:
        return "partial"
    if delivery_date is None:
        return "no_date"
    return "expected"


def latest_fixed_run_id(db: Session) -> Optional[int]:
    row = (
        db.query(PlanningRun.run_id)
        .filter(PlanningRun.status == "FIXED_SNAPSHOT")
        .order_by(PlanningRun.run_id.desc())
        .first()
    )
    return int(row[0]) if row else None


def _exported_purchase_ids(db: Session) -> set:
    rows = (
        db.query(SyncLink.source_id)
        .filter(
            SyncLink.source_doctype == "planned_purchase",
            SyncLink.target_entity == PURCHASE_ORDER_ENTITY,
            SyncLink.status == "success",
        )
        .all()
    )
    return {int(r[0]) for r in rows}


def _mrp_origin_order_refs(db: Session) -> set:
    rows = (
        db.query(SyncLink.target_ref_key)
        .filter(
            SyncLink.source_doctype == "planned_purchase",
            SyncLink.target_entity == PURCHASE_ORDER_ENTITY,
            SyncLink.target_ref_key.isnot(None),
        )
        .all()
    )
    return {str(r[0]).lower() for r in rows if r[0]}


def _suppliers_by_ref(db: Session) -> Dict[str, str]:
    return {
        str(s.supplier_ref1c).lower(): str(s.supplier_name or "")
        for s in db.query(Supplier).all()
        if s.supplier_ref1c
    }


def _supplier_order_rows(
    db: Session,
    *,
    order_id: Optional[int],
    supplier_id: Optional[int],
    search: Optional[str],
    active_only: bool,
    today: date,
) -> List[Dict[str, Any]]:
    query = (
        db.query(SupplierOrderItem, SupplierOrder, Item, Supplier)
        .join(SupplierOrder, SupplierOrder.order_id == SupplierOrderItem.order_id)
        .join(Item, Item.item_id == SupplierOrderItem.item_id_ref)
        .outerjoin(Supplier, Supplier.supplier_id == SupplierOrder.supplier_id)
        # удалённые в 1С заказы не показываем даже при active_only=False
        .filter(SupplierOrder.deletion_mark == False)  # noqa: E712
    )
    if order_id is not None:
        query = query.filter(SupplierOrder.order_id == int(order_id))
    if supplier_id is not None:
        query = query.filter(SupplierOrder.supplier_id == int(supplier_id))
    if search:
        like = f"%{search.strip()}%"
        query = query.filter(
            or_(
                SupplierOrder.order_number.ilike(like),
                Item.item_name.ilike(like),
                Item.item_article.ilike(like),
                Item.item_code.ilike(like),
                Supplier.supplier_name.ilike(like),
            )
        )

    mrp_refs = _mrp_origin_order_refs(db)
    rows: List[Dict[str, Any]] = []
    for line, order, item, supplier in query.all():
        order_active = _order_is_active(order)
        if active_only and not order_active:
            continue
        remaining = _to_float(line.remaining_qty)
        received = _to_float(line.received_qty)
        delivery = _to_date(line.delivery_date)
        status = _line_status(
            remaining_qty=remaining,
            received_qty=received,
            delivery_date=delivery,
            today=today,
            order_active=order_active,
        )
        overdue_days = (today - delivery).days if status == "overdue" and delivery else 0
        rows.append(
            {
                "row_key": f"line:{int(line.item_id)}",
                "line_id": int(line.item_id),
                "purchase_id": None,
                "order_id": int(order.order_id),
                "order_number": str(order.order_number or ""),
                "order_date": _date_to_iso(order.order_date),
                "order_ref1c": order.order_ref1c,
                "order_state_name": order.order_state_name,
                "source": "mrp" if (order.order_ref1c or "").lower() in mrp_refs else "1c",
                "supplier_id": int(order.supplier_id) if order.supplier_id is not None else None,
                "supplier_name": str(supplier.supplier_name or "") if supplier else "",
                "item_id": int(item.item_id),
                "item_code": str(item.item_code or ""),
                "item_article": item.item_article,
                "item_name": str(item.item_name or ""),
                "unit": item.unit,
                "quantity": _to_float(line.quantity),
                "received_qty": received,
                "remaining_qty": remaining,
                "delivery_date": delivery.isoformat() if delivery else None,
                "need_date": None,
                "overdue_days": overdue_days,
                "line_status": status,
                "price": _to_float(line.price),
                "amount": _to_float(line.amount),
                "run_id": None,
            }
        )
    return rows


def _to_order_rows(
    db: Session,
    *,
    run_id: Optional[int],
    supplier_id: Optional[int],
    search: Optional[str],
    today: date,
) -> List[Dict[str, Any]]:
    if not run_id:
        return []
    exported = _exported_purchase_ids(db)
    suppliers_by_ref = _suppliers_by_ref(db)
    supplier_ids_by_ref: Dict[str, int] = {
        str(s.supplier_ref1c).lower(): int(s.supplier_id)
        for s in db.query(Supplier).all()
        if s.supplier_ref1c
    }
    needle = (search or "").strip().lower()

    query = (
        db.query(PlannedPurchase, Item)
        .join(Item, Item.item_id == PlannedPurchase.item_id)
        .filter(PlannedPurchase.run_id == int(run_id))
        .filter(PlannedPurchase.qty > 0)
    )
    rows: List[Dict[str, Any]] = []
    for purchase, item in query.all():
        if int(purchase.purchase_id) in exported:
            continue
        supplier_ref = str(purchase.supplier_ref1c or item.supplier_ref1c or "").lower()
        row_supplier_id = supplier_ids_by_ref.get(supplier_ref)
        if supplier_id is not None and row_supplier_id != int(supplier_id):
            continue
        supplier_name = suppliers_by_ref.get(supplier_ref, "")
        if needle:
            haystack = " ".join(
                str(v or "").lower()
                for v in (item.item_name, item.item_article, item.item_code, supplier_name)
            )
            if needle not in haystack:
                continue
        need = _to_date(purchase.need_date)
        qty = _to_float(purchase.qty)
        rows.append(
            {
                "row_key": f"purchase:{int(purchase.purchase_id)}",
                "line_id": None,
                "purchase_id": int(purchase.purchase_id),
                "order_id": None,
                "order_number": "",
                "order_date": _date_to_iso(purchase.order_date),
                "order_ref1c": None,
                "order_state_name": None,
                "source": "mrp",
                "supplier_id": row_supplier_id,
                "supplier_name": supplier_name,
                "item_id": int(item.item_id),
                "item_code": str(item.item_code or ""),
                "item_article": item.item_article,
                "item_name": str(item.item_name or ""),
                "unit": item.unit,
                "quantity": qty,
                "received_qty": 0.0,
                "remaining_qty": qty,
                "delivery_date": None,
                "need_date": need.isoformat() if need else None,
                "overdue_days": (today - need).days if need and need < today else 0,
                "line_status": "to_order",
                "price": 0.0,
                "amount": 0.0,
                "run_id": int(run_id),
            }
        )
    return rows


def _sort_key_date(row: Dict[str, Any]) -> tuple:
    raw = row.get("delivery_date") or row.get("need_date")
    return (raw is None, raw or "", row.get("order_number") or "", row.get("item_name") or "")


def _summary(rows: List[Dict[str, Any]], today: date) -> Dict[str, Any]:
    week_ahead = (today + timedelta(days=7)).isoformat()
    today_iso = today.isoformat()
    by_status: Dict[str, int] = {}
    in_transit_amount = 0.0
    expected_7d = 0
    for row in rows:
        status = str(row.get("line_status"))
        by_status[status] = by_status.get(status, 0) + 1
        if status in ("expected", "partial", "overdue", "no_date"):
            in_transit_amount += _to_float(row.get("remaining_qty")) * _to_float(row.get("price"))
        delivery = row.get("delivery_date")
        if status in ("expected", "partial") and delivery and today_iso <= delivery <= week_ahead:
            expected_7d += 1
    return {
        "total_rows": len(rows),
        "by_status": by_status,
        "to_order": by_status.get("to_order", 0),
        "overdue": by_status.get("overdue", 0),
        "expected_7d": expected_7d,
        "in_transit_amount": round(in_transit_amount, 2),
    }


def list_journal(
    db: Session,
    *,
    order_id: Optional[int] = None,
    supplier_id: Optional[int] = None,
    state: Optional[str] = None,
    line_status: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    active_only: bool = True,
    include_to_order: bool = True,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    today = today or date.today()
    run_id = latest_fixed_run_id(db)

    rows = _supplier_order_rows(
        db,
        order_id=order_id,
        supplier_id=supplier_id,
        search=search,
        active_only=active_only,
        today=today,
    )
    if include_to_order and order_id is None:
        rows.extend(
            _to_order_rows(db, run_id=run_id, supplier_id=supplier_id, search=search, today=today)
        )

    if state:
        state_norm = _normalize_state(state)
        rows = [r for r in rows if _normalize_state(r.get("order_state_name")) == state_norm]
    start = _parse_date(date_from)
    finish = _parse_date(date_to)
    if start or finish:
        def _in_range(row: Dict[str, Any]) -> bool:
            raw = row.get("delivery_date") or row.get("need_date")
            d = _parse_date(raw)
            if d is None:
                return False
            if start and d < start:
                return False
            if finish and d > finish:
                return False
            return True

        rows = [r for r in rows if _in_range(r)]

    summary = _summary(rows, today)

    if line_status:
        rows = [r for r in rows if r.get("line_status") == str(line_status)]

    reverse = (sort_dir or "").strip().lower() == "desc"
    sort_field = (sort_by or "").strip().lower()
    if sort_field == "order_date":
        rows.sort(key=lambda r: (r.get("order_date") is None, r.get("order_date") or "", r.get("order_number") or ""), reverse=reverse)
    else:
        rows.sort(key=_sort_key_date, reverse=reverse)

    total = len(rows)
    effective_limit = max(1, min(int(limit or 100), 500))
    effective_offset = max(0, int(offset or 0))
    page = rows[effective_offset : effective_offset + effective_limit]

    return {
        "rows": page,
        "total": total,
        "limit": effective_limit,
        "offset": effective_offset,
        "run_id": run_id,
        "summary": summary,
    }


def get_order_card(db: Session, order_id: int, *, today: Optional[date] = None) -> Dict[str, Any]:
    today = today or date.today()
    order = db.query(SupplierOrder).filter(SupplierOrder.order_id == int(order_id)).first()
    if order is None:
        raise ValueError(f"Supplier order {order_id} not found")
    supplier = (
        db.query(Supplier).filter(Supplier.supplier_id == order.supplier_id).first()
        if order.supplier_id is not None
        else None
    )
    lines = _supplier_order_rows(
        db,
        order_id=int(order_id),
        supplier_id=None,
        search=None,
        active_only=False,
        today=today,
    )
    mrp_refs = _mrp_origin_order_refs(db)
    return {
        "order": {
            "order_id": int(order.order_id),
            "order_number": str(order.order_number or ""),
            "order_date": _date_to_iso(order.order_date),
            "order_ref1c": order.order_ref1c,
            "order_state_name": order.order_state_name,
            "deletion_mark": bool(order.deletion_mark),
            "is_posted": bool(order.is_posted),
            "document_amount": _to_float(order.document_amount),
            "active": _order_is_active(order),
            "source": "mrp" if (order.order_ref1c or "").lower() in mrp_refs else "1c",
            "supplier_id": int(order.supplier_id) if order.supplier_id is not None else None,
            "supplier_name": str(supplier.supplier_name or "") if supplier else "",
        },
        "lines": lines,
    }


def list_filters(db: Session) -> Dict[str, Any]:
    suppliers = [
        {"supplier_id": int(s.supplier_id), "supplier_name": str(s.supplier_name or "")}
        for s in (
            db.query(Supplier)
            .join(SupplierOrder, SupplierOrder.supplier_id == Supplier.supplier_id)
            .filter(SupplierOrder.deletion_mark == False)  # noqa: E712
            .distinct()
            .order_by(Supplier.supplier_name.asc())
            .all()
        )
    ]
    state_rows = (
        db.query(SupplierOrder.order_state_name)
        .filter(SupplierOrder.deletion_mark == False)  # noqa: E712
        .filter(SupplierOrder.order_state_name.isnot(None))
        .distinct()
        .all()
    )
    states = sorted({str(r[0]) for r in state_rows if r[0]})
    return {"suppliers": suppliers, "states": states}
