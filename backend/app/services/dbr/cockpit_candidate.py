"""Build the unpublished, Ledger-native DBR feeder cockpit snapshot.

This is deliberately a candidate-side writer.  It is called inside the
obligation-refresh transaction, while the Ledger generation and its MRP runs
are still ``building``.  The normal HTTP reader never calls this module: it
only reads the payload after the publisher has atomically promoted it.

No compatibility calculator is imported here.  In particular, mutable
``ItemWarehouseStock`` and the old production/purchase counters are not a
source of stock, supply, demand, or completion in this projection.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

from sqlalchemy.orm import Session

from app import models
from app.services.dbr.core.feeder import zones
from app.services.dbr.ledger_projection import LedgerProjectionKey, build_generation_projection
from app.services.dbr.policy_snapshot import (
    CONSUMER as POLICY_CONSUMER,
    SNAPSHOT_KEY as POLICY_SNAPSHOT_KEY,
)
from app.services.replenishment import (
    REPLENISHMENT_FLOW_PRODUCTION,
    REPLENISHMENT_FLOW_PURCHASE,
    REPLENISHMENT_FLOW_REWORK,
    classify_replenishment_flow,
)


CONSUMER = "dbr_feeder_cockpit"
SNAPSHOT_KEY = "cockpit:v1"
ZERO = Decimal("0")


class DbrCockpitCandidateError(RuntimeError):
    """A candidate cannot produce one exact, immutable cockpit view."""


def _decimal(value: object | None) -> Decimal:
    return ZERO if value is None else Decimal(str(value))


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
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
    if snapshot is None:
        raise DbrCockpitCandidateError("building generation has no DBR policy snapshot")
    if snapshot.truth_status != "building" or snapshot.cutoff != generation.cutoff:
        raise DbrCockpitCandidateError("DBR policy snapshot is not bound to this building generation")
    if not isinstance(snapshot.payload, dict):
        raise DbrCockpitCandidateError("DBR policy snapshot payload is invalid")
    return snapshot


def _candidate_runs(policy: dict[str, Any]) -> dict[int, int]:
    result: dict[int, int] = {}
    for row in policy.get("runs", []):
        try:
            run_id, freeze = int(row["run_id"]), int(row["freeze_version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DbrCockpitCandidateError("DBR policy has malformed candidate run lineage") from exc
        if run_id in result and result[run_id] != freeze:
            raise DbrCockpitCandidateError("DBR policy has duplicate candidate run lineage")
        result[run_id] = freeze
    if not result:
        raise DbrCockpitCandidateError("DBR policy has no candidate runs")
    return result


def _warehouse_for_axis(
    pool: str,
    *,
    flow: str,
    item: dict[str, Any],
    category: dict[str, Any] | None,
    pools: dict[str, str],
    risks: dict[str, dict[str, Any]],
) -> str:
    """Resolve a captured warehouse without guessing among a shared pool."""
    if flow == REPLENISHMENT_FLOW_PURCHASE and category:
        risk = risks.get(str(category.get("category_name") or ""))
        candidate = str((risk or {}).get("receipt_warehouse_ref1c") or "").strip()
        if candidate:
            if pools.get(candidate) != pool:
                raise DbrCockpitCandidateError("purchase receipt warehouse conflicts with reservation pool")
            return candidate
    captured = str(item.get("boundary_warehouse_ref1c") or "").strip()
    if captured:
        if pools.get(captured) != pool:
            raise DbrCockpitCandidateError(
                "captured item boundary warehouse conflicts with reservation pool"
            )
        return captured
    choices = sorted(warehouse for warehouse, mapped_pool in pools.items() if mapped_pool == pool)
    if len(choices) != 1:
        raise DbrCockpitCandidateError(
            "reservation pool has no unique DBR warehouse; policy must resolve the boundary"
        )
    return choices[0]


def _position_shape(
    *,
    item: dict[str, Any],
    warehouse: str,
    adu: Decimal,
    commonality: int,
    settings: dict[str, Any],
    risks: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    flow = classify_replenishment_flow(item.get("replenishment_method"))
    category = item.get("category") or {}
    risk = risks.get(str(category.get("category_name") or "")) or {}
    risk_pct = float(risk.get("supply_risk_pct") or 0)
    k_var = 0.25 if commonality >= 2 else 0.5
    if flow == REPLENISHMENT_FLOW_REWORK:
        supply_type, route_class, rt_source = "processing", None, "chain"
        rt, batch = float(settings.get("rt_processing_days") or 0), float(settings.get("processing_trip_interval_days") or 0)
        calculated = zones.compute_purchase_zones(float(adu), rt, batch, k_var=k_var, supply_risk_pct=risk_pct)
    elif flow == REPLENISHMENT_FLOW_PURCHASE:
        supply_type, route_class, rt_source = "purchase", None, "lead_time"
        rt, batch = float(item.get("replenishment_time") or 0), float(settings.get("batch_days_turning") or 0)
        calculated = zones.compute_purchase_zones(float(adu), rt, batch, k_var=k_var, supply_risk_pct=risk_pct)
    else:
        route_text = str(item.get("route_text") or "").casefold()
        boundary_kind = str(item.get("boundary_kind") or "")
        if boundary_kind == "w3" or any(
            token in route_text for token in ("окрас", "покрас", "порош")
        ):
            route_class = "painting"
            rt = float(settings.get("rt_painting_days") or 0)
            batch = float(settings.get("batch_days_paint_black") or 0)
        elif "свар" in route_text:
            route_class = "welding"
            rt = float(settings.get("rt_welding_days") or 0)
            batch = float(settings.get("batch_days_welding") or 0)
        else:
            route_class = "machining"
            rt = float(settings.get("rt_machining_days") or 0)
            if "гиб" in route_text:
                batch = float(settings.get("batch_days_bending") or 0)
            else:
                batch = float(settings.get("batch_days_turning") or 0)
        supply_type, rt_source = "manufacture", "class"
        calculated = zones.compute_zones(float(adu), rt, batch, float(item.get("optimal_batch") or 0), k_var, False, risk_pct)
    mode = (
        "under_schedule"
        if item.get("boundary_kind") == "under_schedule"
        else (
            "shelf"
            if zones.has_shelf(
                float(adu),
                rt,
                float(settings.get("shelf_threshold_qty") or 0),
            )
            else "under_schedule"
        )
    )
    return {
        "item_id": int(item["item_id"]), "item_code": item.get("item_code"),
        "item_name": item.get("item_name"),
        "warehouse_ref1c": warehouse, "supply_type": supply_type,
        "mode": mode,
        "is_active": True, "is_stale": False, "adu": float(adu), "commonality": commonality,
        "route_class": route_class, "rt_days": rt, "rt_source": rt_source, "batch_days": batch,
        "q_batch": calculated.green, "k_var": k_var, "supply_risk_pct": risk_pct,
        "red_qty": calculated.red, "yellow_qty": calculated.yellow, "green_qty": calculated.green,
        "target_qty": calculated.target,
        "data_quality": [],
    }


def build_cockpit_candidate_snapshot(db: Session, generation_id: int) -> models.PlanningReadSnapshot:
    """Persist one idempotent, unpublished cockpit payload for a BUILDING generation."""
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or generation.status != "building" or generation.cutoff is None:
        raise DbrCockpitCandidateError("DBR cockpit candidate requires a BUILDING Ledger generation")
    policy_snapshot = _policy(db, generation)
    policy = dict(policy_snapshot.payload)
    runs = _candidate_runs(policy)
    pools = {str(key): str(value) for key, value in (policy.get("planning_pool_by_warehouse") or {}).items()}
    settings = dict(policy.get("settings") or {})
    if not pools or not settings:
        raise DbrCockpitCandidateError("DBR policy lacks pool mapping or settings")
    items = {int(row["item_id"]): dict(row) for row in policy.get("items", [])}
    risks = {str(row.get("item_group") or ""): dict(row) for row in policy.get("category_supply_risks", [])}
    workdays = [row["date"] for row in policy.get("calendar", []) if bool(row.get("is_workday"))]
    if not workdays:
        raise DbrCockpitCandidateError("DBR policy has no frozen workdays")

    reservations = db.query(models.ReservationEntry).filter(
        models.ReservationEntry.ledger_generation_id == int(generation.id),
        models.ReservationEntry.lifecycle_status == "active",
    ).all()
    axes: dict[tuple[int, str], Decimal] = defaultdict(lambda: ZERO)
    requirements: dict[int, models.MrpRequirement] = {}
    for reservation in reservations:
        outstanding = max(_decimal(reservation.reserved_qty) - _decimal(reservation.realized_qty), ZERO)
        if not outstanding:
            continue
        expected_freeze = runs.get(int(reservation.run_id or -1))
        if expected_freeze is None or int(reservation.freeze_version) != expected_freeze:
            raise DbrCockpitCandidateError("open reservation is outside the exact candidate run/freeze set")
        requirement = db.get(models.MrpRequirement, int(reservation.requirement_id))
        if requirement is None or int(requirement.run_id) != int(reservation.run_id) or int(requirement.freeze_version or -1) != expected_freeze:
            raise DbrCockpitCandidateError("open reservation requirement lineage is not frozen candidate lineage")
        axes[(int(reservation.item_id), str(reservation.planning_stock_pool))] += outstanding
        requirements[int(requirement.id)] = requirement

    # Gross frozen buckets size daily usage.  Unbucketed frozen requirements
    # retain their explicitly frozen total instead of reading mutable counters.
    gross_by_axis: dict[tuple[int, str], Decimal] = defaultdict(lambda: ZERO)
    bucket_req_ids: set[int] = set()
    if requirements:
        for bucket in db.query(models.MrpRequirementBucket).filter(models.MrpRequirementBucket.requirement_id.in_(requirements)).all():
            requirement = requirements.get(int(bucket.requirement_id))
            if requirement is None or int(bucket.run_id) != int(requirement.run_id):
                raise DbrCockpitCandidateError("frozen demand bucket has foreign lineage")
            gross_by_axis[(int(requirement.item_id), str(requirement.planning_stock_pool))] += _decimal(bucket.gross_qty)
            bucket_req_ids.add(int(requirement.id))
        for requirement_id, requirement in requirements.items():
            if requirement_id not in bucket_req_ids:
                gross_by_axis[(int(requirement.item_id), str(requirement.planning_stock_pool))] += _decimal(requirement.total_required_qty)

    # Commonality is the number of candidate roots which carry a boundary item.
    roots_by_item: dict[int, set[tuple[int, int]]] = defaultdict(set)
    for requirement in requirements.values():
        roots_by_item[int(requirement.item_id)].add((int(requirement.run_id), int(requirement.item_id)))
    for run_id, freeze in runs.items():
        for component in db.query(models.MrpFreezeComponent).filter(
            models.MrpFreezeComponent.run_id == run_id,
            models.MrpFreezeComponent.freeze_version == freeze,
        ).all():
            roots_by_item[int(component.component_item_id)].add((run_id, int(component.parent_item_id)))

    position_specs: list[dict[str, Any]] = []
    projection_keys: list[LedgerProjectionKey] = []
    for (item_id, pool), outstanding in sorted(axes.items()):
        item = items.get(item_id)
        if item is None:
            raise DbrCockpitCandidateError("open reservation item is absent from frozen DBR policy")
        flow = classify_replenishment_flow(item.get("replenishment_method"))
        warehouse = _warehouse_for_axis(
            pool,
            flow=flow,
            item=item,
            category=item.get("category"),
            pools=pools,
            risks=risks,
        )
        shape = _position_shape(
            item=item, warehouse=warehouse,
            adu=gross_by_axis[(item_id, pool)] / Decimal(len(workdays)),
            commonality=len(roots_by_item[item_id]), settings=settings, risks=risks,
        )
        shape["outstanding_obligation_qty"] = float(outstanding)
        shape["planning_stock_pool"] = pool
        position_specs.append(shape)
        projection_keys.append(LedgerProjectionKey(str(item["item_code"]), warehouse))

    projection = build_generation_projection(db, int(generation.id), projection_keys, pools, "building")
    projection_by_key = {(row.key.item_code, row.key.warehouse_ref1c): row for row in projection.rows}
    signals: list[dict[str, Any]] = []
    deficits: list[dict[str, Any]] = []
    for position in position_specs:
        row = projection_by_key[(str(position["item_code"]), str(position["warehouse_ref1c"]))]
        nfp = row.on_hand + row.inbound - row.outstanding_obligation_qty
        computed_zones = zones.Zones(position["red_qty"], position["yellow_qty"], position["green_qty"])
        zone = zones.nfp_zone(float(nfp), computed_zones)
        position["live_nfp"] = {
            "stock_qty": float(row.on_hand), "open_supply_qty": float(row.inbound),
            "qualified_demand_qty": float(row.outstanding_obligation_qty), "nfp": float(nfp),
            "zone": zone, "penetration": zones.penetration(float(nfp), computed_zones),
            "is_complete": not bool(row.excluded_future_supply),
            "missing_reasons": (["unqualified_future_supply_excluded"] if row.excluded_future_supply else []),
            "formula": "stock_qty + open_supply_qty - outstanding_reservation_qty",
        }
        uncovered = max(row.uncovered_qty, ZERO)
        if uncovered > ZERO:
            deficits.append({
                "item_id": position["item_id"], "item_code": position["item_code"],
                "warehouse_ref1c": position["warehouse_ref1c"], "planning_stock_pool": position["planning_stock_pool"],
                "obligation_qty": float(row.outstanding_obligation_qty), "deficit_qty": float(uncovered),
            })
        if position["mode"] == "shelf":
            suggested = zones.replenishment_qty(float(nfp), computed_zones, position["q_batch"])
            if suggested > 0:
                key = sha256((f"{generation.id}|shelf|{position['item_id']}|{position['warehouse_ref1c']}").encode()).hexdigest()
                signals.append({
                    "id": None, "dedup_key": key, "ledger_generation_id": int(generation.id),
                    "source_run_id": None, "freeze_version": None, "signal_type": "Пополнение",
                    "item_id": position["item_id"], "item_code": position["item_code"], "item_name": None,
                    "warehouse_ref1c": position["warehouse_ref1c"], "status": "Open", "zone": zone,
                    "suggested_qty": suggested, "priority": zones.penetration(float(nfp), computed_zones),
                    "nfp_snapshot": float(nfp), "target_qty_snapshot": position["target_qty"],
                    "reason_json": {"source": "ledger_candidate", "read_only": True},
                })

    payload = {
        "meta": {
            "policy_snapshot_id": int(policy_snapshot.id), "policy_snapshot_hash": sha256(_canonical(policy).encode()).hexdigest(),
            "runs": [{"run_id": run_id, "freeze_version": freeze} for run_id, freeze in sorted(runs.items())],
            "ledger_generation": int(generation.id), "ledger_generation_id": int(generation.id),
            "cutoff": _json(generation.cutoff), "truth_status": "building",
            "chain_enabled": bool(settings.get("feeder_chain_enabled")),
            "unavailable_sections": ["under_schedule", "processing_board"],
            "read_only": True,
        },
        "positions": sorted(position_specs, key=lambda row: (row["item_code"], row["warehouse_ref1c"])),
        "signals": sorted(signals, key=lambda row: (row["priority"], row["item_code"]), reverse=True),
        "deficits": {"items": sorted(deficits, key=lambda row: (-row["deficit_qty"], row["item_code"])), "source": "open_reservation_obligations"},
        "processing_board": {"status": "unavailable", "reason": "exact target processing inputs are not captured"},
    }
    payload = _json(payload)
    existing = db.query(models.PlanningReadSnapshot).filter(
        models.PlanningReadSnapshot.consumer == CONSUMER,
        models.PlanningReadSnapshot.snapshot_key == SNAPSHOT_KEY,
        models.PlanningReadSnapshot.ledger_generation_id == int(generation.id),
    ).one_or_none()
    if existing is not None:
        if existing.truth_status != "building" or existing.cutoff != generation.cutoff or _canonical(existing.payload) != _canonical(payload):
            raise DbrCockpitCandidateError("candidate DBR cockpit snapshot conflicts with persisted data")
        return existing
    snapshot = models.PlanningReadSnapshot(
        consumer=CONSUMER, snapshot_key=SNAPSHOT_KEY, ledger_generation_id=int(generation.id),
        cutoff=generation.cutoff, truth_status="building", reason="unpublished Ledger-native DBR cockpit",
        payload=payload, published_at=datetime.now(timezone.utc),
    )
    db.add(snapshot)
    db.flush()
    return snapshot
