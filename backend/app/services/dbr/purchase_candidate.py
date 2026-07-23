"""Build the unpublished Ledger-native DBR purchase cockpit.

The historical purchase preview explodes live programs and reads mutable stock
mirrors.  This builder is intentionally narrower: every row is an open
purchase obligation in one BUILDING Ledger generation, and its quantity to
order is the Ledger reservation's uncovered quantity.  It is run only by the
refresh transaction; HTTP reads the immutable result after atomic publish.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

from sqlalchemy.orm import Session

from app import models
from app.services.dbr.ledger_projection import LedgerProjectionKey, build_generation_projection
from app.services.dbr.policy_snapshot import (
    CONSUMER as POLICY_CONSUMER,
    SNAPSHOT_KEY as POLICY_SNAPSHOT_KEY,
)
from app.services.replenishment import REPLENISHMENT_FLOW_PURCHASE, classify_replenishment_flow


CONSUMER = "dbr_purchase_cockpit"
SNAPSHOT_KEY = "purchase:v1"
ZERO = Decimal("0")


class DbrPurchaseCandidateError(RuntimeError):
    """A generation cannot yield one exact purchase read model."""


def _decimal(value: object | None) -> Decimal:
    return ZERO if value is None else Decimal(str(value))


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set)):
        return [_json(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _policy(db: Session, generation: models.LedgerGeneration) -> models.PlanningReadSnapshot:
    snapshot = db.query(models.PlanningReadSnapshot).filter(
        models.PlanningReadSnapshot.consumer == POLICY_CONSUMER,
        models.PlanningReadSnapshot.snapshot_key == POLICY_SNAPSHOT_KEY,
        models.PlanningReadSnapshot.ledger_generation_id == int(generation.id),
    ).one_or_none()
    if snapshot is None or str(snapshot.truth_status) != "building" or snapshot.cutoff != generation.cutoff:
        raise DbrPurchaseCandidateError("building generation has no exact DBR policy snapshot")
    if not isinstance(snapshot.payload, dict):
        raise DbrPurchaseCandidateError("DBR policy payload is invalid")
    return snapshot


def _runs(policy: dict[str, Any]) -> dict[int, int]:
    result: dict[int, int] = {}
    for row in policy.get("runs", []):
        try:
            run_id, freeze = int(row["run_id"]), int(row["freeze_version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DbrPurchaseCandidateError("DBR policy has malformed run lineage") from exc
        if result.setdefault(run_id, freeze) != freeze:
            raise DbrPurchaseCandidateError("DBR policy duplicates run lineage")
    if not result:
        raise DbrPurchaseCandidateError("DBR policy has no candidate runs")
    return result


def _warehouse_for_item(item: dict[str, Any], pool: str, pools: dict[str, str], risks: dict[str, dict[str, Any]]) -> str:
    category = item.get("category") or {}
    risk = risks.get(str(category.get("category_name") or "")) or {}
    preferred = str(risk.get("receipt_warehouse_ref1c") or item.get("boundary_warehouse_ref1c") or "").strip()
    if preferred:
        if pools.get(preferred) != pool:
            raise DbrPurchaseCandidateError("captured purchase warehouse conflicts with reservation pool")
        return preferred
    choices = sorted(warehouse for warehouse, mapped_pool in pools.items() if mapped_pool == pool)
    if len(choices) != 1:
        raise DbrPurchaseCandidateError("purchase reservation pool has no unique captured warehouse")
    return choices[0]


def build_purchase_candidate_snapshot(db: Session, generation_id: int) -> models.PlanningReadSnapshot:
    """Persist one deterministic, read-only purchase view for a BUILDING generation."""
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or str(generation.status) != "building" or generation.cutoff is None:
        raise DbrPurchaseCandidateError("purchase cockpit requires a BUILDING Ledger generation with cutoff")
    policy_snapshot = _policy(db, generation)
    policy = dict(policy_snapshot.payload)
    runs = _runs(policy)
    pools = {str(key): str(value) for key, value in dict(policy.get("planning_pool_by_warehouse") or {}).items()}
    if not pools or any(not key or not value for key, value in pools.items()):
        raise DbrPurchaseCandidateError("DBR policy has no exact planning pool mapping")
    items = {int(row["item_id"]): dict(row) for row in policy.get("items", []) if row.get("item_id") is not None}
    risks = {str(row.get("item_group") or ""): dict(row) for row in policy.get("category_supply_risks", [])}

    reservations = db.query(models.ReservationEntry).filter(
        models.ReservationEntry.ledger_generation_id == int(generation.id),
        models.ReservationEntry.lifecycle_status == "active",
    ).all()
    axes: dict[tuple[int, str], list[models.ReservationEntry]] = {}
    for reservation in reservations:
        item = items.get(int(reservation.item_id))
        if item is None:
            raise DbrPurchaseCandidateError("active reservation item is absent from frozen DBR policy")
        if classify_replenishment_flow(item.get("replenishment_method")) != REPLENISHMENT_FLOW_PURCHASE:
            continue
        try:
            run_id = int(reservation.run_id)
            freeze_version = int(reservation.freeze_version)
        except (TypeError, ValueError) as exc:
            raise DbrPurchaseCandidateError("purchase reservation has incomplete frozen lineage") from exc
        if runs.get(run_id) != freeze_version:
            raise DbrPurchaseCandidateError("purchase reservation has foreign or mixed frozen lineage")
        # A fully realized active row is historical evidence, not an open
        # purchase obligation.  Keeping it would make a zero-demand line look
        # actionable and defeats the "open Ledger only" contract.
        if max(_decimal(reservation.reserved_qty) - _decimal(reservation.realized_qty), ZERO) <= ZERO:
            continue
        pool = str(reservation.planning_stock_pool or "").strip()
        if pool not in set(pools.values()):
            raise DbrPurchaseCandidateError("purchase reservation has an unmapped planning pool")
        axes.setdefault((int(reservation.item_id), pool), []).append(reservation)

    specs: list[tuple[dict[str, Any], str, str, list[models.ReservationEntry]]] = []
    keys: list[LedgerProjectionKey] = []
    for (item_id, pool), rows in sorted(axes.items(), key=lambda value: (str(items[value[0][0]].get("item_code") or ""), value[0][1])):
        item = items[item_id]
        warehouse = _warehouse_for_item(item, pool, pools, risks)
        code = str(item.get("item_code") or "").strip()
        if not code:
            raise DbrPurchaseCandidateError("captured purchase item has no item_code")
        specs.append((item, pool, warehouse, sorted(rows, key=lambda row: (row.priority_period_from, row.priority_period_to, row.id))))
        keys.append(LedgerProjectionKey(code, warehouse))
    projection = build_generation_projection(db, int(generation.id), keys, pools, "building")
    projection_by_axis = {(row.key.item_code, row.key.warehouse_ref1c): row for row in projection.rows}

    rows: list[dict[str, Any]] = []
    for item, pool, warehouse, reservations_for_axis in specs:
        projected = projection_by_axis[(str(item["item_code"]), warehouse)]
        obligations = {entry.reservation_id: entry for entry in projected.obligations}
        reservation_ids = [int(row.id) for row in reservations_for_axis]
        if not set(reservation_ids).issubset(obligations):
            raise DbrPurchaseCandidateError("purchase projection reservation identity conflicts")
        uncovered = sum((obligations[reservation_id].uncovered_qty for reservation_id in reservation_ids), ZERO)
        outstanding = sum((obligations[reservation_id].outstanding_qty for reservation_id in reservation_ids), ZERO)
        coverage = []
        for reservation_id in reservation_ids:
            obligation = obligations[reservation_id]
            coverage.append({
                "reservation_id": reservation_id,
                "requirement_id": obligation.requirement_id,
                "priority_period_from": _json(obligation.priority_period_from),
                "priority_period_to": _json(obligation.priority_period_to),
                "outstanding_qty": float(obligation.outstanding_qty),
                "uncovered_qty": float(obligation.uncovered_qty),
                "coverage": [{
                    "source_kind": line.source_kind, "source_ref": line.source_ref,
                    "source_line_ref": line.source_line_ref, "pin_kind": line.pin_kind,
                    "alloc_qty": float(line.alloc_qty), "covered_qty": float(line.covered_qty),
                    "realized_qty": float(line.realized_qty), "evaporated_qty": float(line.evaporated_qty),
                } for line in obligation.coverage],
            })
        rows.append({
            "item_id": int(item["item_id"]), "item_code": item["item_code"], "item_name": item.get("item_name"),
            "item_ref1c": item.get("item_ref1c"), "supplier_ref1c": item.get("supplier_ref1c"),
            "article": item.get("article"), "unit": item.get("unit"),
            "replenishment_time": item.get("replenishment_time"),
            "warehouse_ref1c": warehouse, "planning_stock_pool": pool,
            "need_date": _json(min(
                (row.priority_period_from for row in reservations_for_axis if row.priority_period_from is not None),
                default=None,
            )),
            "reservation_ids": reservation_ids, "obligations": coverage,
            "outstanding_obligation_qty": float(outstanding), "uncovered_qty": float(uncovered),
            "to_order_qty": float(uncovered),
            "stock_qty": float(projected.on_hand), "exact_future_supply_qty": float(projected.inbound),
            "excluded_future_supply": [{
                "supply_kind": line.supply_kind, "source_ref": line.source_ref,
                "source_line_ref": line.source_line_ref, "evidence_status": line.evidence_status,
                "destination_warehouse_ref1c": line.destination_warehouse_ref1c, "reason": line.reason,
            } for line in projected.excluded_future_supply],
        })
    payload = _json({
        "meta": {
            "policy_snapshot_id": int(policy_snapshot.id),
            "policy_snapshot_hash": sha256(_canonical(policy).encode()).hexdigest(),
            "runs": [{"run_id": run_id, "freeze_version": freeze} for run_id, freeze in sorted(runs.items())],
            "ledger_generation": int(generation.id), "ledger_generation_id": int(generation.id),
            "cutoff": generation.cutoff, "truth_status": "building", "read_only": True,
            "formula": "to_order_qty = sum(active purchase reservation uncovered_qty)",
        },
        "rows": sorted(rows, key=lambda row: (str(row["supplier_ref1c"] or ""), str(row["item_code"]), str(row["warehouse_ref1c"]))),
    })
    existing = db.query(models.PlanningReadSnapshot).filter(
        models.PlanningReadSnapshot.consumer == CONSUMER,
        models.PlanningReadSnapshot.snapshot_key == SNAPSHOT_KEY,
        models.PlanningReadSnapshot.ledger_generation_id == int(generation.id),
    ).one_or_none()
    if existing is not None:
        if str(existing.truth_status) != "building" or existing.cutoff != generation.cutoff or _canonical(existing.payload) != _canonical(payload):
            raise DbrPurchaseCandidateError("candidate DBR purchase snapshot conflicts with persisted data")
        return existing
    snapshot = models.PlanningReadSnapshot(
        consumer=CONSUMER, snapshot_key=SNAPSHOT_KEY, ledger_generation_id=int(generation.id),
        cutoff=generation.cutoff, truth_status="building", reason="unpublished Ledger-native DBR purchase cockpit",
        payload=payload, published_at=datetime.now(timezone.utc),
    )
    db.add(snapshot)
    db.flush()
    return snapshot
