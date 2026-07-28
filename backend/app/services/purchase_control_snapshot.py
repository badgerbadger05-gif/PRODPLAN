"""Immutable Ledger-native read boundary for the purchase control journal."""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app import models
from app.services.item_ledger.reservation import replenishment_remaining
from app.services.planning_truth import (
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_PLANNING_SNAPSHOTS,
    CAPABILITY_PURCHASE_CONTROL_JOURNAL,
    CAPABILITY_RESERVATION_REPLAY,
    PlanningTruthUnavailable,
    get_latest_read_snapshot,
    get_truth_state,
)


CONSUMER = "purchase_control_journal"
SNAPSHOT_KEY = "journal:v1"
REQUIRED = (
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_RESERVATION_REPLAY,
    CAPABILITY_PLANNING_SNAPSHOTS,
    CAPABILITY_PURCHASE_CONTROL_JOURNAL,
)

_BUY_MODE = "buy"
_BUY_ROW_PREFIX = "buy:"
_BUY_ROW_GENERATOR = "mrp_reservation"
_EPS_FLOAT = 1e-9

_RU_MONTHS = {
    1: "Январь",
    2: "Февраль",
    3: "Март",
    4: "Апрель",
    5: "Май",
    6: "Июнь",
    7: "Июль",
    8: "Август",
    9: "Сентябрь",
    10: "Октябрь",
    11: "Ноябрь",
    12: "Декабрь",
}


class PurchaseJournalSnapshotUnavailable(RuntimeError):
    def __init__(self, detail: dict[str, Any]):
        self.detail = detail
        super().__init__(detail["reason"])

    def as_dict(self):
        return dict(self.detail)


def _unavailable(db: Session, reason: str, truth: dict[str, Any] | None = None):
    state = get_truth_state(db)
    detail = {
        "code": "purchase_control_snapshot_unavailable",
        "consumer": CONSUMER,
        "status": "unavailable",
        "truth_status": state.status,
        "ledger_generation": state.generation_id,
        "cutoff": state.cutoff.isoformat() if state.cutoff else None,
        "reason": reason,
    }
    if truth:
        detail["truth"] = jsonable_encoder(truth)
    return PurchaseJournalSnapshotUnavailable(detail)


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        raise ValueError("numeric field is malformed")


def validate_purchase_control_journal_buy_row(row: Any) -> None:
    if not isinstance(row, dict):
        raise ValueError("purchase control buy row is malformed")
    if str(row.get("row_generator") or "") != _BUY_ROW_GENERATOR:
        raise ValueError("purchase control buy row has unsupported generator")
    if not str(row.get("row_key") or "").startswith(_BUY_ROW_PREFIX):
        raise ValueError("purchase control buy row key is malformed")
    if "received_qty" not in row:
        raise ValueError("purchase control buy row received_qty is required")

    required_qty = _to_float(row["required_qty"])
    realized_qty = _to_float(row["realized_qty"])
    open_order_covered_qty = _to_float(row["open_order_covered_qty"])
    to_order_qty = _to_float(row["to_order_qty"])
    quantity = _to_float(row["quantity"])
    remaining_qty = _to_float(row["remaining_qty"])
    received_qty = _to_float(row["received_qty"])
    if required_qty < 0 or realized_qty < 0 or open_order_covered_qty < 0:
        raise ValueError("purchase control buy row has invalid quantities")
    if to_order_qty < 0:
        raise ValueError("purchase control buy row has invalid quantities")
    if quantity < 0 or remaining_qty < 0:
        raise ValueError("purchase control buy row has invalid quantities")
    if received_qty < 0 or received_qty > required_qty + _EPS_FLOAT:
        raise ValueError("purchase control buy row has invalid quantities")
    if not math.isclose(quantity, required_qty, abs_tol=_EPS_FLOAT):
        raise ValueError("purchase control buy row quantity is inconsistent")
    if not math.isclose(received_qty, realized_qty, abs_tol=_EPS_FLOAT):
        raise ValueError("purchase control buy row quantity is inconsistent")
    if not math.isclose(remaining_qty, to_order_qty, abs_tol=_EPS_FLOAT):
        raise ValueError("purchase control buy row quantity is inconsistent")
    if not math.isclose(
        realized_qty + open_order_covered_qty + to_order_qty,
        required_qty,
        abs_tol=_EPS_FLOAT,
    ):
        raise ValueError("purchase control buy row quantity is inconsistent")
    reservation_ids = row.get("reservation_ids")
    requirement_ids = row.get("requirement_ids")
    if (
        not isinstance(reservation_ids, list)
        or not reservation_ids
        or not isinstance(requirement_ids, list)
        or not requirement_ids
    ):
        raise ValueError("purchase control buy row lineage is malformed")
    run_id = row.get("run_id")
    run_ids = row.get("run_ids")
    if run_id is not None:
        if not isinstance(run_ids, list):
            raise ValueError("purchase control buy row lineage is malformed")
        if int(run_id) not in [int(v) for v in run_ids]:
            raise ValueError("purchase control buy row lineage is malformed")
    elif run_ids is not None:
        if not isinstance(row.get("run_ids"), list):
            raise ValueError("purchase control buy row lineage is malformed")
        if not run_ids:
            raise ValueError("purchase control buy row lineage is malformed")


def _period_label(period_to: Any) -> str | None:
    if period_to is None:
        return None
    return f"{_RU_MONTHS[int(period_to.month)]} {int(period_to.year)}"


def _clean_ref(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _coverage_percent(part: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return round(max(part, 0.0) / total * 100.0, 6)


def _run_horizons(db: Session, run_ids: set[int]) -> dict[int, dict[str, Any]]:
    if not run_ids:
        return {}
    rows = (
        db.query(models.PlanningRun.run_id, models.PlanningRun.period_from, models.PlanningRun.period_to)
        .filter(models.PlanningRun.run_id.in_(sorted(run_ids)))
        .all()
    )
    return {
        int(row[0]): {
            "from": row[1],
            "to": row[2],
        }
        for row in rows
    }


def _build_supplier_card_rows(
    db: Session,
    generation: models.LedgerGeneration,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    supplies = (
        db.query(models.LedgerFutureSupply, models.Item)
        .join(models.Item, models.Item.item_id == models.LedgerFutureSupply.item_id)
        .filter(
            models.LedgerFutureSupply.ledger_generation_id == generation.id,
            models.LedgerFutureSupply.supply_kind == "supplier_order",
        )
        .all()
    )

    cards: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    seen_source_lines: set[tuple[str, str]] = set()

    for supply, item in supplies:
        source_ref = _clean_ref(supply.source_ref)
        source_line_ref = _clean_ref(supply.source_line_ref)
        if source_ref == "" or source_line_ref == "":
            continue

        source_identity = (source_ref, source_line_ref)
        if source_identity in seen_source_lines:
            raise ValueError("LedgerFutureSupply supplier-order source line is duplicated")
        seen_source_lines.add(source_identity)

        if supply.evidence_status != "exact":
            continue

        try:
            ordered = _to_float(supply.ordered_qty_at_cutoff)
            realized = _to_float(supply.realized_qty_at_cutoff)
            open_qty = _to_float(supply.open_qty_at_cutoff)
        except ValueError as exc:
            raise ValueError("LedgerFutureSupply supplier-order quantities are missing or invalid") from exc

        if ordered < 0 or realized < 0 or open_qty < 0 or open_qty > ordered:
            raise ValueError("LedgerFutureSupply supplier-order quantities violate ordered/open invariant")
        if not str(item.item_code or "").strip():
            raise ValueError("LedgerFutureSupply supplier-order item has no code")

        order = db.query(models.SupplierOrder).filter(models.SupplierOrder.order_ref1c == source_ref).one_or_none()
        supplier = (
            db.get(models.Supplier, order.supplier_id)
            if order is not None and order.supplier_id is not None
            else None
        )

        row = {
            "row_key": f"ledger-supply:{int(supply.id)}",
            "line_id": None,
            "purchase_id": None,
            "source_purchase_ids": [],
            "order_id": int(order.order_id) if order else None,
            "order_number": str(order.order_number or "") if order else source_ref,
            "order_date": order.order_date.isoformat() if order and order.order_date else None,
            "order_ref1c": supply.source_ref,
            "order_state_name": supply.source_state_key,
            "supply_phase": None,
            "counts_in_mrp": None,
            "source": "ledger",
            "supplier_id": int(order.supplier_id) if order and order.supplier_id is not None else None,
            "supplier_name": str(supplier.supplier_name or "") if supplier else "",
            "item_id": int(item.item_id),
            "item_code": str(item.item_code or ""),
            "item_article": item.item_article,
            "item_name": str(item.item_name or ""),
            "unit": item.unit,
            "quantity": ordered,
            "received_qty": realized,
            "remaining_qty": open_qty,
            "delivery_date": supply.eta_date.isoformat() if supply.eta_date else None,
            "need_date": None,
            "overdue_days": None,
            "line_status": "unavailable",
            "price": None,
            "amount": None,
            "run_id": None,
            "run_ids": [],
            "row_generator": "ledger_future_supply",
            "fact_status": "available",
            "fact_source": "ledger",
        }
        rows.append(row)

        if order is not None:
            header = {
                k: row.get(k)
                for k in (
                    "order_id",
                    "order_number",
                    "order_date",
                    "order_ref1c",
                    "order_state_name",
                    "supplier_id",
                    "supplier_name",
                )
            }
            card = cards.setdefault(str(int(order.order_id)), {"order": header, "lines": []})
            if card["order"] != header:
                raise ValueError("conflicting frozen supplier-order header")
            card["lines"].append(row)

    for card in cards.values():
        card["lines"].sort(key=lambda row: (str(row["item_code"]), str(row["row_key"])))
    return rows, cards


def _build_buyer_rows(
    db: Session,
    generation_id: int,
    to_order_by_period: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    entries = (
        db.query(
            models.ReplenishmentWorkItem,
            models.ReservationEntry,
            models.Item,
        )
        .join(
            models.ReservationEntry,
            models.ReservationEntry.id
            == models.ReplenishmentWorkItem.reservation_id,
        )
        .join(
            models.Item,
            models.Item.item_id == models.ReplenishmentWorkItem.item_id,
        )
        .filter(
            models.ReplenishmentWorkItem.ledger_generation_id == generation_id,
            models.ReplenishmentWorkItem.replenishment_method == _BUY_MODE,
            models.ReservationEntry.lifecycle_status == "active",
        )
        .order_by(
            models.Item.item_code.asc(),
            models.ReservationEntry.planning_stock_pool.asc(),
            models.ReplenishmentWorkItem.run_id.asc(),
            models.ReplenishmentWorkItem.id.asc(),
        )
        .all()
    )
    if not entries:
        return []

    supplier_refs = {
        _clean_ref(s.supplier_ref1c).lower(): int(s.supplier_id)
        for s in db.query(models.Supplier).all()
        if _clean_ref(s.supplier_ref1c)
    }
    supplier_names = {
        _clean_ref(s.supplier_ref1c).lower(): str(s.supplier_name or "")
        for s in db.query(models.Supplier).all()
        if _clean_ref(s.supplier_ref1c)
    }
    supplier_by_id = {
        int(s.supplier_id): str(s.supplier_name or "")
        for s in db.query(models.Supplier).all()
        if s.supplier_id is not None
    }

    grouped_rows: dict[tuple[int, str], dict[str, Any]] = {}

    run_ids: set[int] = {
        int(work_item.run_id)
        for work_item, _reservation, _item in entries
    }
    horizons = _run_horizons(db, run_ids)

    for work_item, reservation, item in entries:
        run_id = int(work_item.run_id)
        horizon = horizons.get(run_id)
        if horizon is None:
            raise ValueError("buy reservation references planning run without period horizon")
        period_from = horizon.get("from")
        period_to = horizon.get("to")
        if period_from is None or period_to is None:
            raise ValueError("buy reservation run has incomplete plan horizon")

        required = _to_float(work_item.replenishment_required_qty)
        realized = _to_float(work_item.replenishment_fulfilled_qty)
        # Open orders are not physical receipts and cannot mutate the frozen
        # obligation. They are represented by their own saved supply facts.
        open_order_covered = 0.0
        to_order = _to_float(work_item.replenishment_remaining_qty)

        if required < 0 or realized < 0 or to_order < 0:
            raise ValueError("buy reservation has invalid quantities")
        if realized > required + _EPS_FLOAT:
            raise ValueError("buy reservation realized exceeds reserved")
        if not math.isclose(
            to_order,
            float(replenishment_remaining(required, realized)),
            abs_tol=_EPS_FLOAT,
        ):
            raise ValueError("buy reservation has inconsistent uncovered quantity")

        pool = _clean_ref(reservation.planning_stock_pool) or "main"
        item_code = str(item.item_code or "")
        key = (int(item.item_id), pool)
        target = grouped_rows.setdefault(
            key,
            {
                "item_id": int(item.item_id),
                "item_code": item_code,
                "item_article": item.item_article,
                "item_name": str(item.item_name or ""),
                "unit": item.unit,
                "planning_stock_pool": pool,
                "supplier_ref1c": _clean_ref(item.supplier_ref1c).lower(),
                "requirement_ids": set(),
                "reservation_ids": set(),
                "run_ids": set(),
                "required_qty": 0.0,
                "realized_qty": 0.0,
                "open_order_covered_qty": 0.0,
                "to_order_qty": 0.0,
                "slices": [],
                "horizon_buckets": [],
            },
        )

        requirement_id = int(work_item.requirement_id)

        target["requirement_ids"].add(requirement_id)
        target["reservation_ids"].add(int(reservation.id))
        target["run_ids"].add(run_id)
        target["required_qty"] += required
        target["realized_qty"] += realized
        target["open_order_covered_qty"] += open_order_covered
        target["to_order_qty"] += to_order

        target["slices"].append(
            {
                "reservation_id": int(reservation.id),
                "work_item_id": int(work_item.id),
                "requirement_id": requirement_id,
                "run_id": run_id,
                "plan_period_from": period_from.isoformat() if period_from else None,
                "plan_period_to": period_to.isoformat() if period_to else None,
                "period_label": _period_label(period_to),
                "required_qty": required,
                "realized_qty": realized,
                "open_order_covered_qty": open_order_covered,
                "to_order_qty": to_order,
                "to_order_pct": _coverage_percent(to_order, required),
                "open_order_covered_pct": _coverage_percent(open_order_covered, required),
                "coverage_slices": [],
            }
        )
        target["horizon_buckets"].append(target["slices"][-1])

    rows: list[dict[str, Any]] = []
    for _key, payload in grouped_rows.items():
        required_qty = round(float(payload["required_qty"]), 3)
        realized_qty = round(float(payload["realized_qty"]), 3)
        open_order_covered_qty = round(float(payload["open_order_covered_qty"]), 3)
        to_order_qty = round(float(payload["to_order_qty"]), 3)
        if required_qty <= 0 and to_order_qty <= 0:
            continue

        sorted_runs = sorted(int(v) for v in payload["run_ids"])
        first_bucket = min(
            payload["horizon_buckets"],
            key=lambda row: str(row["plan_period_to"] or ""),
        )
        last_bucket = max(
            payload["horizon_buckets"],
            key=lambda row: str(row["plan_period_to"] or ""),
        )

        coverage_by_period: dict[str, dict[str, Any]] = {}
        for bucket in payload["horizon_buckets"]:
            pto = bucket["plan_period_to"]
            key = str(pto or "")
            holder = coverage_by_period.setdefault(
                key,
                {
                    "plan_period_to": pto,
                    "period_label": bucket["period_label"],
                    "run_id": int(bucket["run_id"]),
                    "required_qty": 0.0,
                    "to_order_qty": 0.0,
                    "open_order_covered_qty": 0.0,
                },
            )
            holder["required_qty"] += float(bucket["required_qty"])
            holder["to_order_qty"] += float(bucket["to_order_qty"])
            holder["open_order_covered_qty"] += float(bucket["open_order_covered_qty"])

        supplier_ref = payload["supplier_ref1c"]
        supplier_id = supplier_refs.get(supplier_ref)
        supplier_name = supplier_names.get(supplier_ref)
        if supplier_name is None and supplier_id is not None:
            supplier_name = supplier_by_id.get(supplier_id, "")

        for b in coverage_by_period.values():
            to_order_by_period.append(
                {
                    "plan_period_to": b["plan_period_to"],
                    "period_label": b["period_label"],
                    "item_count": 1,
                    "total_qty": round(float(b["to_order_qty"]), 3),
                }
            )

        row = {
            "row_key": f"buy:{payload['item_id']}:{payload['planning_stock_pool']}",
            "line_id": None,
            "purchase_id": None,
            "source_purchase_ids": [],
            "order_id": None,
            "order_number": "",
            "order_date": None,
            "order_ref1c": None,
            "order_state_name": None,
            "supply_phase": "no_goods",
            "counts_in_mrp": None,
            "source": "mrp",
            "supplier_id": supplier_id,
            "supplier_name": supplier_name,
            "item_id": payload["item_id"],
            "item_code": payload["item_code"],
            "item_article": payload["item_article"],
            "item_name": payload["item_name"],
            "unit": payload["unit"],
            "quantity": required_qty,
            "received_qty": realized_qty,
            "remaining_qty": to_order_qty,
            "delivery_date": last_bucket["plan_period_to"],
            "need_date": first_bucket["plan_period_from"],
            "overdue_days": 0,
            "line_status": "to_order" if to_order_qty > _EPS_FLOAT else "received",
            "price": 0.0,
            "amount": 0.0,
            "run_id": sorted_runs[0] if len(sorted_runs) == 1 else None,
            "run_ids": sorted_runs,
            "requirement_ids": sorted(payload["requirement_ids"]),
            "reservation_ids": sorted(payload["reservation_ids"]),
            "planning_stock_pool": payload["planning_stock_pool"],
            "required_qty": required_qty,
            "realized_qty": realized_qty,
            "open_order_covered_qty": open_order_covered_qty,
            "to_order_qty": to_order_qty,
            "to_order_pct": _coverage_percent(to_order_qty, required_qty),
            "open_order_covered_pct": _coverage_percent(open_order_covered_qty, required_qty),
            "plan_period_from": first_bucket["plan_period_from"],
            "plan_period_to": last_bucket["plan_period_to"],
            "period_label": first_bucket["period_label"],
            "horizon_bucket_count": len(payload["horizon_buckets"]),
            "horizon_buckets": payload["horizon_buckets"],
            "slices": payload["slices"],
            "row_generator": _BUY_ROW_GENERATOR,
            "fact_status": "available",
            "fact_source": "ledger",
        }
        validate_purchase_control_journal_buy_row(row)
        rows.append(row)
    rows.sort(
        key=lambda row: (
            str(row["supplier_id"] or ""),
            str(row["item_code"]),
            str(row["row_key"]),
            str(row["planning_stock_pool"]),
        )
    )
    return rows


def build_candidate_snapshot(db: Session, generation_id: int) -> models.PlanningReadSnapshot:
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or generation.status != "building" or generation.cutoff is None:
        raise ValueError("purchase journal candidate requires BUILDING Ledger generation")

    to_order_buckets: list[dict[str, Any]] = []
    _, cards = _build_supplier_card_rows(db, generation)
    merged_rows = [
        dict(row)
        for row in _build_buyer_rows(db, int(generation.id), to_order_buckets)
        if float(row.get("remaining_qty") or 0) >= 0
    ]

    by_bucket: dict[str, dict[str, Any]] = {}
    for bucket in to_order_buckets:
        key = str(bucket.get("plan_period_to") or "")
        target = by_bucket.setdefault(
            key,
            {
                "plan_period_to": bucket.get("plan_period_to"),
                "period_label": bucket.get("period_label"),
                "item_count": 0,
                "total_qty": 0.0,
            },
        )
        target["item_count"] += 1
        target["total_qty"] += float(bucket.get("total_qty") or 0.0)

    to_order_by_period = [
        {
            "plan_period_to": value["plan_period_to"],
            "period_label": value["period_label"],
            "item_count": value["item_count"],
            "total_qty": round(float(value["total_qty"]), 3),
        }
        for value in by_bucket.values()
    ]
    to_order_by_period.sort(key=lambda bucket: (bucket["plan_period_to"] is None, str(bucket["plan_period_to"] or "")))

    rows = sorted(
        merged_rows,
        key=lambda row: (
            str(row.get("order_number") or ""),
            str(row.get("item_code") or ""),
            str(row.get("row_key") or ""),
            str(row.get("order_ref1c") or ""),
        ),
    )

    row_keys: set[str] = set()
    for row in rows:
        row_key = row.get("row_key")
        if isinstance(row_key, str):
            if row_key in row_keys:
                raise ValueError("purchase control snapshot row keys are duplicated")
            row_keys.add(row_key)

    run_ids = sorted({
        int(v)
        for row in rows
        for v in ([row.get("run_id")] if row.get("run_id") is not None else row.get("run_ids", []) or [])
    })
    supplier_states = sorted(
        {
            str(row.get("order_state_name") or "")
            for row in rows
            if row.get("order_state_name") is not None
        }
    )

    payload = {
        "meta": {
            "ledger_generation": generation.id,
            "ledger_generation_id": generation.id,
            "cutoff": generation.cutoff.isoformat(),
            "truth_status": "building",
            "read_only": True,
            "fact_source": "ledger",
            "received_qty_status": "available",
            "run_ids": run_ids,
            "to_order_by_period": to_order_by_period,
            "states": supplier_states,
        },
        "rows": rows,
        "cards": cards,
    }

    existing = db.query(models.PlanningReadSnapshot).filter_by(
        consumer=CONSUMER,
        snapshot_key=SNAPSHOT_KEY,
        ledger_generation_id=generation.id,
    ).one_or_none()

    if existing is not None:
        if existing.payload != payload or existing.truth_status != "building":
            raise ValueError("purchase journal candidate conflict")
        return existing

    snapshot = models.PlanningReadSnapshot(
        consumer=CONSUMER,
        snapshot_key=SNAPSHOT_KEY,
        ledger_generation_id=generation.id,
        cutoff=generation.cutoff,
        truth_status="building",
        reason="unpublished Ledger-native purchase journal",
        payload=payload,
        published_at=datetime.now(timezone.utc),
    )
    db.add(snapshot)
    db.flush()
    return snapshot


class PurchaseJournalPromotionError(RuntimeError):
    """The journal candidate is not fit to become readable truth."""


def promote_candidate_snapshot(
    db: Session,
    *,
    generation: models.LedgerGeneration,
    accepted_at: datetime,
) -> models.PlanningReadSnapshot | None:
    """Turn this generation's BUILDING journal candidate into accepted truth.

    ``build_candidate_snapshot`` always writes a candidate, and readers only
    accept ``truth_status='accepted'``, so a path that accepts a generation
    without promoting leaves the purchase journal permanently unavailable.  The
    obligation refresh publisher does this inline; the physical refresh path
    needs the same step, so the guards live here rather than being written twice
    from memory.  Returns ``None`` when the generation has no candidate.
    """
    candidate = db.query(models.PlanningReadSnapshot).filter(
        models.PlanningReadSnapshot.consumer == CONSUMER,
        models.PlanningReadSnapshot.snapshot_key == SNAPSHOT_KEY,
        models.PlanningReadSnapshot.ledger_generation_id == int(generation.id),
        models.PlanningReadSnapshot.truth_status == "building",
        models.PlanningReadSnapshot.cutoff == generation.cutoff,
    ).one_or_none()
    if candidate is None:
        return None

    payload = candidate.payload if isinstance(candidate.payload, dict) else None
    meta = payload.get("meta") if isinstance(payload, dict) else None
    rows = payload.get("rows") if isinstance(payload, dict) else None
    cards = payload.get("cards") if isinstance(payload, dict) else None
    if (
        not isinstance(meta, dict)
        or meta.get("read_only") is not True
        or meta.get("fact_source") != "ledger"
        or int(meta.get("ledger_generation_id") or -1) != int(generation.id)
        or not isinstance(rows, list)
        or not isinstance(cards, dict)
    ):
        raise PurchaseJournalPromotionError(
            "purchase control journal candidate is missing or stale"
        )

    seen: set[str] = set()
    for row in rows:
        try:
            validate_purchase_control_journal_buy_row(row)
            key = str(row["row_key"])
        except (KeyError, TypeError) as exc:
            raise PurchaseJournalPromotionError(
                "purchase control journal row is malformed"
            ) from exc
        except ValueError as exc:
            raise PurchaseJournalPromotionError(
                "purchase control journal row violates the Ledger fact contract"
            ) from exc
        if key in seen:
            raise PurchaseJournalPromotionError(
                "purchase control journal row violates the Ledger fact contract"
            )
        seen.add(key)

    candidate.truth_status = "accepted"
    candidate.reason = None
    candidate.published_at = accepted_at
    db.flush()
    return candidate


def read_snapshot(db: Session) -> dict[str, Any]:
    try:
        snapshot = get_latest_read_snapshot(
            db,
            consumer=CONSUMER,
            snapshot_key=SNAPSHOT_KEY,
            required_capabilities=REQUIRED,
        )
    except PlanningTruthUnavailable as exc:
        raise _unavailable(db, str(exc), exc.as_dict()) from exc

    if snapshot is None or not isinstance(snapshot.payload, dict) or not isinstance(snapshot.payload.get("rows"), list):
        raise _unavailable(db, "No purchase control journal snapshot for current accepted Ledger")

    result = dict(snapshot.payload)
    meta = dict(result.get("meta") or {})
    meta.update(
        {
            "snapshot_id": snapshot.id,
            "ledger_generation": snapshot.ledger_generation_id,
            "cutoff": snapshot.cutoff.isoformat(),
            "truth_status": snapshot.truth_status,
            "truth_reason": snapshot.reason,
        }
    )
    result["meta"] = meta
    return result
