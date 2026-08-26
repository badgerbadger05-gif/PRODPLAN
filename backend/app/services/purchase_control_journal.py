"""Read-only facade over the immutable purchase-control snapshot."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.services.item_ledger.reservation import replenishment_execution_pct
from .purchase_control_snapshot import read_snapshot

_EPS = 1e-9
_BUY_ROW_GENERATOR = "mrp_reservation"


def _to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _horizon_filter_inclusive(value: Any, horizon_iso: Optional[str]) -> bool:
    if horizon_iso is None:
        return True
    if value is None:
        return False
    return str(value) <= horizon_iso


def _materialization_action(row: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    if row.get("row_generator") != _BUY_ROW_GENERATOR:
        return False, "Строка не является MRP-снабжением"
    if row.get("line_status") != "to_order":
        return False, "Строка не требует нового заказа"
    if _to_float(row.get("to_order_qty")) <= _EPS:
        return False, "Количество к заказу отсутствует"
    return True, None


def _reconcile_buy_row_for_horizon(row: Dict[str, Any], horizon_iso: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return BUY-row copy projected to selected horizon slices.

    For grouped BUY rows we recompute all quantities and percentages only from
    slices that lie in the selected horizon. Legacy run-pinning is intentionally
    avoided: all included slices participate, even when an item/pool row spans
    multiple plans.
    """
    if row.get("row_generator") != _BUY_ROW_GENERATOR:
        return dict(row)
    if horizon_iso is None:
        return dict(row)

    slices = [dict(s) for s in row.get("slices") or [] if _horizon_filter_inclusive(s.get("plan_period_to"), horizon_iso)]
    if not slices:
        return None

    required = round(sum(_to_float(s.get("required_qty")) for s in slices), 3)
    realized = round(sum(_to_float(s.get("realized_qty")) for s in slices), 3)
    open_order_covered = round(sum(_to_float(s.get("open_order_covered_qty")) for s in slices), 3)
    to_order = round(sum(_to_float(s.get("to_order_qty")) for s in slices), 3)

    if required <= _EPS and to_order <= _EPS:
        return None

    run_ids = sorted({int(s["run_id"]) for s in slices if s.get("run_id") is not None})
    requirement_ids = sorted({int(s["requirement_id"]) for s in slices if s.get("requirement_id") is not None})
    reservation_ids = sorted({int(s["reservation_id"]) for s in slices if s.get("reservation_id") is not None})

    bucket_slices = [dict(s) for s in slices]
    ordered_period_tos = [str(s.get("plan_period_to") or "") for s in bucket_slices]
    ordered_period_froms = [str(s.get("plan_period_from") or "") for s in bucket_slices]
    ordered_labels = [s.get("period_label") for s in bucket_slices if s.get("period_label") is not None]
    bucket_period_to = min(ordered_period_tos)
    last_period_to = max(ordered_period_tos)
    first_period_from = min(ordered_period_froms)
    period_label = ordered_labels[0] if ordered_labels else _period_label(_parse_date(last_period_to))

    projected = dict(row)
    to_order_pct = replenishment_execution_pct(required, to_order)
    open_order_covered_pct = replenishment_execution_pct(
        required,
        open_order_covered,
    )

    projected.update(
        {
            "run_id": run_ids[0] if len(run_ids) == 1 else None,
            "run_ids": run_ids,
            "requirement_ids": requirement_ids,
            "reservation_ids": reservation_ids,
            "slices": bucket_slices,
            "horizon_buckets": bucket_slices,
            "horizon_bucket_count": len(bucket_slices),
            "required_qty": required,
            "realized_qty": realized,
            "open_order_covered_qty": open_order_covered,
            "to_order_qty": to_order,
            "to_order_pct": float(to_order_pct) if to_order_pct is not None else None,
            "open_order_covered_pct": (
                float(open_order_covered_pct)
                if open_order_covered_pct is not None
                else None
            ),
            "plan_period_from": first_period_from,
            "plan_period_to": last_period_to,
            "period_label": period_label,
            "delivery_date": last_period_to,
            "quantity": required,
            "remaining_qty": to_order,
            "received_qty": realized,
            "amount": (
                round(to_order * _to_float(row.get("price")), 2)
                if row.get("price") is not None
                else None
            ),
            "line_status": (
                "to_order"
                if to_order > _EPS
                else "expected"
                if open_order_covered > _EPS
                else "received"
            ),
        }
    )
    return projected


def get_selection_summary(
    db: Session,
    *,
    snapshot_id: int,
    row_keys: List[str],
    horizon_period_to: Optional[date] = None,
) -> Dict[str, Any]:
    """Aggregate a selected set using only the immutable purchase snapshot."""
    snapshot = read_snapshot(db)
    meta = dict(snapshot.get("meta") or {})
    if int(meta.get("snapshot_id") or 0) != int(snapshot_id):
        raise ValueError("Снимок журнала изменился; обновите страницу и повторите выбор")

    unique_keys = list(dict.fromkeys(str(key or "").strip() for key in row_keys))
    if not unique_keys or any(not key for key in unique_keys):
        raise ValueError("Не выбраны строки журнала закупок")

    rows_by_key = {
        str(row.get("row_key")): dict(row)
        for row in snapshot.get("rows") or []
        if row.get("row_key") is not None
    }
    unknown = [key for key in unique_keys if key not in rows_by_key]
    if unknown:
        raise ValueError("Выбранные строки отсутствуют в текущем снимке")

    horizon_iso = horizon_period_to.isoformat() if horizon_period_to else None
    rows: List[Dict[str, Any]] = []
    for key in unique_keys:
        projected = _reconcile_buy_row_for_horizon(rows_by_key[key], horizon_iso)
        if projected is None or not _materialization_action(projected)[0]:
            raise ValueError("Выбор содержит строку, недоступную для формирования заказа")
        rows.append(projected)

    priced_rows = [row for row in rows if row.get("price") is not None]
    known_amount = sum(
        (Decimal(str(row.get("amount") or 0)) for row in priced_rows),
        start=Decimal("0"),
    )
    unpriced_rows = len(rows) - len(priced_rows)
    amount_status = (
        "complete"
        if unpriced_rows == 0
        else "unavailable"
        if not priced_rows
        else "partial"
    )
    return {
        "snapshot_id": int(snapshot_id),
        "selected_rows": len(rows),
        "priced_rows": len(priced_rows),
        "unpriced_rows": unpriced_rows,
        "known_amount": round(float(known_amount), 2),
        "total_amount": round(float(known_amount), 2) if amount_status == "complete" else None,
        "amount_status": amount_status,
    }


def _sum_to_order_by_period(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if row.get("line_status") != "to_order":
            continue
        if row.get("row_generator") == _BUY_ROW_GENERATOR and row.get("horizon_buckets"):
            slices = row.get("horizon_buckets")
        else:
            slices = [
                {
                    "plan_period_to": row.get("plan_period_to"),
                    "period_label": row.get("period_label"),
                    "to_order_qty": row.get("to_order_qty"),
                }
            ]

        for slice_row in slices:
            period_to = str(slice_row.get("plan_period_to") or "")
            label = slice_row.get("period_label")
            bucket = buckets.setdefault(
                period_to,
                {
                    "plan_period_to": period_to,
                    "period_label": label,
                    "item_count": 0,
                    "total_qty": 0.0,
                },
            )
            bucket["item_count"] += 1
            bucket["total_qty"] += _to_float(slice_row.get("to_order_qty"))

    ordered = sorted(
        buckets.values(),
        key=lambda bucket: (bucket["plan_period_to"] is None, bucket["plan_period_to"] or ""),
    )
    for bucket in ordered:
        bucket["item_count"] = int(bucket["item_count"])
        bucket["total_qty"] = round(float(bucket["total_qty"]), 6)
    return ordered

def list_filters(db: Session) -> Dict[str, Any]:
    snapshot = read_snapshot(db)
    rows = snapshot["rows"]
    cards = snapshot.get("cards") or {}
    suppliers = sorted(
        {
            (int(row["supplier_id"]), str(row.get("supplier_name") or ""))
            for row in rows if row.get("supplier_id") is not None
        },
        key=lambda row: (row[1].casefold(), row[0]),
    )
    supplier_ids_from_cards = {
        (int(line["supplier_id"]), str(line.get("supplier_name") or ""))
        for card in cards.values()
        for line in card.get("lines") or []
        if line.get("supplier_id") is not None
    }
    suppliers = sorted(
        set(suppliers) | set(supplier_ids_from_cards),
        key=lambda row: (row[1].casefold(), row[0]),
    )
    suppliers = [{"supplier_id": supplier_id, "supplier_name": name} for supplier_id, name in suppliers]
    states = {
        str(row["order_state_name"])
        for row in rows
        if row.get("order_state_name")
    }
    for card in cards.values():
        for line in card.get("lines") or []:
            if line.get("order_state_name"):
                states.add(str(line["order_state_name"]))
    states = sorted(states)
    return {"suppliers": suppliers, "states": states}


def list_journal(db: Session, **kwargs: Any) -> Dict[str, Any]:
    """Filter only the current immutable purchase-journal snapshot."""
    snapshot = read_snapshot(db)
    rows = [dict(row) for row in snapshot["rows"]]
    meta = dict(snapshot.get("meta") or {})
    run_ids = [int(v) for v in meta.get("run_ids", []) if v is not None]
    to_order_by_period = [dict(row) for row in meta.get("to_order_by_period", [])]
    if to_order_by_period:
        # Keep legacy key shape (`period_to`) for API consumers while preserving
        # snapshot-native `plan_period_to` in metadata.
        for bucket in to_order_by_period:
            if "period_to" not in bucket and "plan_period_to" in bucket:
                bucket["period_to"] = bucket["plan_period_to"]
    order_id, supplier_id, search, line_status = (kwargs.get(k) for k in ("order_id", "supplier_id", "search", "line_status"))
    if order_id is not None:
        rows = [r for r in rows if r.get("order_id") == int(order_id)]
    if supplier_id is not None:
        rows = [r for r in rows if r.get("supplier_id") == int(supplier_id)]
    if kwargs.get("state"):
        rows = [r for r in rows if r.get("order_state_name") == str(kwargs["state"])]
    if kwargs.get("phase"):
        rows = [r for r in rows if r.get("supply_phase") == str(kwargs["phase"])]

    if not kwargs.get("include_to_order", True):
        rows = [
            r
            for r in rows
            if not (r.get("row_generator") == _BUY_ROW_GENERATOR and r.get("line_status") == "to_order")
        ]

    horizon_iso = kwargs.get("horizon_period_to").isoformat() if kwargs.get("horizon_period_to") else None
    rows = [
        projected
        for row in rows
        for projected in (
            [
                _reconcile_buy_row_for_horizon(row, horizon_iso)
            ]
            if row.get("row_generator") == _BUY_ROW_GENERATOR
            else [dict(row)]
        )
        if projected is not None
    ]
    for row in rows:
        can_materialize, disabled_reason = _materialization_action(row)
        row["can_materialize"] = can_materialize
        row["materialize_disabled_reason"] = disabled_reason

    if line_status:
        rows = [r for r in rows if r.get("line_status") == str(line_status)]

    if kwargs.get("active_only", True):
        rows = [r for r in rows if float(r.get("remaining_qty") or 0) > 0]

    if kwargs.get("date_from"):
        rows = [r for r in rows if r.get("delivery_date") is not None and str(r["delivery_date"]) >= str(kwargs["date_from"])]
    if kwargs.get("date_to"):
        rows = [r for r in rows if r.get("delivery_date") is not None and str(r["delivery_date"]) <= str(kwargs["date_to"])]

    if horizon_period_to := kwargs.get("horizon_period_to"):
        to_order_by_period = _sum_to_order_by_period(rows)
        for bucket in to_order_by_period:
            if "period_to" not in bucket:
                bucket["period_to"] = bucket["plan_period_to"]
    elif to_order_by_period:
        for bucket in to_order_by_period:
            if "period_to" not in bucket and "plan_period_to" in bucket:
                bucket["period_to"] = bucket["plan_period_to"]
    else:
        to_order_by_period = []

    if search:
        needle = str(search).casefold()
        rows = [
            row
            for row in rows
            if needle
            in " ".join(
                str(row.get(key) or "")
                for key in (
                    "order_number",
                    "item_name",
                    "item_code",
                    "item_article",
                    "supplier_name",
                )
            ).casefold()
        ]
    sort_by = str(kwargs.get("sort_by") or "delivery_date")
    if sort_by not in {"delivery_date", "order_date", "order_number", "item_code", "remaining_qty"}:
        sort_by = "delivery_date"
    reverse = str(kwargs.get("sort_dir") or "asc").casefold() == "desc"
    rows.sort(key=lambda r: (r.get(sort_by) is None, r.get(sort_by) if r.get(sort_by) is not None else "", r.get("row_key")), reverse=reverse)
    by_status: dict[str, int] = {}
    by_phase: dict[str, int] = {}
    for row in rows:
        status = str(row.get("line_status") or "unavailable")
        phase = str(row.get("supply_phase") or "unavailable")
        by_status[status] = by_status.get(status, 0) + 1
        by_phase[phase] = by_phase.get(phase, 0) + 1
    summary = {"total_rows": len(rows), "by_status": by_status, "by_phase": by_phase,
               "to_order": by_status.get("to_order", 0), "overdue": by_status.get("overdue", 0),
               "expected_7d": 0, "in_transit_amount": 0.0,
               "fact_status": "available"}
    limit, offset = max(1, min(int(kwargs.get("limit") or 100), 500)), max(0, int(kwargs.get("offset") or 0))
    return {"rows": rows[offset:offset + limit], "total": len(rows), "limit": limit, "offset": offset,
            "run_id": run_ids[0] if len(run_ids) == 1 else None,
            "run_ids": run_ids,
            "truth_status": meta.get("truth_status", snapshot["meta"]["truth_status"]),
            "ledger_generation_id": snapshot["meta"]["ledger_generation"],
            "summary": summary,
            "to_order_by_period": to_order_by_period,
            "meta": snapshot["meta"]}


def get_order_card(db: Session, order_id: int, *, today: Optional[date] = None) -> Dict[str, Any]:
    snapshot = read_snapshot(db); card = (snapshot.get("cards") or {}).get(str(int(order_id)))
    if card is None: raise ValueError(f"Supplier order {order_id} not found in current purchase journal snapshot")
    return {**card, "meta": snapshot["meta"]}
