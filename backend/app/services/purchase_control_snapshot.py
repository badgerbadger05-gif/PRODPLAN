"""Immutable Ledger-native read boundary for the purchase control journal."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app import models
from app.services.planning_truth import (
    CAPABILITY_PHYSICAL_LEDGER, CAPABILITY_PLANNING_SNAPSHOTS,
    CAPABILITY_RESERVATION_REPLAY, PlanningTruthUnavailable,
    get_latest_read_snapshot, get_truth_state,
)

CONSUMER = "purchase_control_journal"
SNAPSHOT_KEY = "journal:v1"
CAPABILITY = "purchase_control_journal"
REQUIRED = (CAPABILITY_PHYSICAL_LEDGER, CAPABILITY_RESERVATION_REPLAY,
            CAPABILITY_PLANNING_SNAPSHOTS, CAPABILITY)


class PurchaseJournalSnapshotUnavailable(RuntimeError):
    def __init__(self, detail: dict[str, Any]): self.detail = detail; super().__init__(detail["reason"])
    def as_dict(self): return dict(self.detail)


def _unavailable(db: Session, reason: str, truth: dict[str, Any] | None = None):
    state = get_truth_state(db)
    detail = {"code": "purchase_control_snapshot_unavailable", "consumer": CONSUMER,
              "status": "unavailable", "truth_status": state.status,
              "ledger_generation": state.generation_id,
              "cutoff": state.cutoff.isoformat() if state.cutoff else None, "reason": reason}
    if truth: detail["truth"] = jsonable_encoder(truth)
    return PurchaseJournalSnapshotUnavailable(detail)


def build_candidate_snapshot(db: Session, generation_id: int) -> models.PlanningReadSnapshot:
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or generation.status != "building" or generation.cutoff is None:
        raise ValueError("purchase journal candidate requires BUILDING Ledger generation")
    rows: list[dict[str, Any]] = []
    supplies = db.query(models.LedgerFutureSupply, models.Item).join(
        models.Item, models.Item.item_id == models.LedgerFutureSupply.item_id
    ).filter(models.LedgerFutureSupply.ledger_generation_id == generation.id,
             models.LedgerFutureSupply.supply_kind == "supplier_order").all()
    seen_source_lines: set[tuple[str, str]] = set()
    for supply, item in supplies:
        if supply.evidence_status != "exact" or not supply.source_ref or not supply.source_line_ref:
            continue
        source_identity = (str(supply.source_ref).strip(), str(supply.source_line_ref).strip())
        if not all(source_identity) or source_identity in seen_source_lines:
            raise ValueError("LedgerFutureSupply supplier-order source line is duplicated")
        seen_source_lines.add(source_identity)
        order = db.query(models.SupplierOrder).filter(models.SupplierOrder.order_ref1c == supply.source_ref).one_or_none()
        supplier = db.get(models.Supplier, order.supplier_id) if order and order.supplier_id else None
        try:
            ordered, open_qty = float(supply.ordered_qty_at_cutoff), float(supply.open_qty_at_cutoff)
        except (TypeError, ValueError) as exc:
            raise ValueError("LedgerFutureSupply supplier-order quantities are missing or invalid") from exc
        if ordered < 0 or open_qty < 0 or open_qty > ordered:
            raise ValueError("LedgerFutureSupply supplier-order quantities violate ordered/open invariant")
        if not str(item.item_code or "").strip():
            raise ValueError("LedgerFutureSupply supplier-order item has no code")
        rows.append({"row_key": f"ledger-supply:{supply.id}", "line_id": None, "purchase_id": None,
            "source_purchase_ids": [], "order_id": order.order_id if order else None,
            "order_number": str(order.order_number or "") if order else str(supply.source_ref),
            "order_date": order.order_date.isoformat() if order and order.order_date else None,
            "order_ref1c": supply.source_ref, "order_state_name": supply.source_state_key,
            "supply_phase": None, "counts_in_mrp": None, "source": "ledger",
            "supplier_id": order.supplier_id if order else None, "supplier_name": str(supplier.supplier_name or "") if supplier else "",
            "item_id": item.item_id, "item_code": item.item_code, "item_article": item.item_article, "item_name": item.item_name,
            "unit": item.unit, "quantity": ordered,
            "received_qty": None, "remaining_qty": open_qty,
            "delivery_date": supply.eta_date.isoformat() if supply.eta_date else None, "need_date": None,
            "overdue_days": None, "line_status": "unavailable",
            "price": None, "amount": None, "run_id": None,
            "fact_status": "unavailable", "fact_source": "ledger_future_supply"})
    cards: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["order_id"] is None: continue
        key = str(row["order_id"])
        header = {k: row.get(k) for k in ("order_id", "order_number", "order_date", "order_ref1c", "order_state_name", "supplier_id", "supplier_name")}
        card = cards.setdefault(key, {"order": header, "lines": []})
        if card["order"] != header: raise ValueError("conflicting frozen supplier-order header")
        card["lines"].append(row)
    for card in cards.values():
        card["lines"].sort(key=lambda row: (str(row["item_code"]), str(row["row_key"])))
    payload = {"meta": {"ledger_generation": generation.id, "ledger_generation_id": generation.id,
               "cutoff": generation.cutoff.isoformat(), "truth_status": "building", "fact_source": "ledger",
               "received_qty_status": "unavailable", "read_only": True}, "rows": sorted(rows, key=lambda r: (r["order_number"], r["item_code"], r["row_key"])),
               "cards": cards}
    existing = db.query(models.PlanningReadSnapshot).filter_by(consumer=CONSUMER, snapshot_key=SNAPSHOT_KEY, ledger_generation_id=generation.id).one_or_none()
    if existing:
        if existing.payload != payload or existing.truth_status != "building": raise ValueError("purchase journal candidate conflict")
        return existing
    snapshot = models.PlanningReadSnapshot(consumer=CONSUMER, snapshot_key=SNAPSHOT_KEY, ledger_generation_id=generation.id,
        cutoff=generation.cutoff, truth_status="building", reason="unpublished Ledger-native purchase journal", payload=payload, published_at=datetime.now(timezone.utc))
    db.add(snapshot); db.flush(); return snapshot


def read_snapshot(db: Session) -> dict[str, Any]:
    try: snapshot = get_latest_read_snapshot(db, consumer=CONSUMER, snapshot_key=SNAPSHOT_KEY, required_capabilities=REQUIRED)
    except PlanningTruthUnavailable as exc: raise _unavailable(db, str(exc), exc.as_dict()) from exc
    if snapshot is None or not isinstance(snapshot.payload, dict) or not isinstance(snapshot.payload.get("rows"), list):
        raise _unavailable(db, "No purchase control journal snapshot for current accepted Ledger")
    result = dict(snapshot.payload); meta = dict(result.get("meta") or {})
    meta.update({"snapshot_id": snapshot.id, "ledger_generation": snapshot.ledger_generation_id, "cutoff": snapshot.cutoff.isoformat(), "truth_status": snapshot.truth_status, "truth_reason": snapshot.reason}); result["meta"] = meta
    return result
