"""Immutable DBR policy input for one building obligation-refresh generation.

The DBR cockpit must not re-read mutable settings, calendar, item master data or
warehouse stock after a Ledger generation is published.  This module captures
those *policy* inputs alongside the exact frozen MRP candidates.  It does not
calculate a cockpit; it only makes a deterministic, auditable input snapshot
available to that later builder.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from typing import Any

from sqlalchemy.orm import Session

from app import models
from app.services.dbr import adapters
from app.services.replenishment import (
    REPLENISHMENT_FLOW_PURCHASE,
    REPLENISHMENT_FLOW_REWORK,
    classify_replenishment_flow,
)
from app.services.obligation_refresh_manifest import MANIFEST_KEY


CONSUMER = "dbr_policy_input"
SNAPSHOT_KEY = "policy:v1"
_SETTINGS_FIELDS = (
    "frozen_days", "gate_horizon_workdays", "shelf_threshold_qty",
    "rt_machining_days", "rt_welding_days", "rt_painting_days",
    "batch_days_turning", "batch_days_bending", "batch_days_welding",
    "batch_days_paint_black", "batch_days_paint_color",
    "feeder_chain_enabled", "feeder_load_horizon_weeks",
    "rt_processing_days", "processing_trip_interval_days",
    "processing_roundtrip_days", "w2_warehouse_ref1c",
    "w3_warehouse_ref1c", "w4_warehouse_ref1c", "fastener_categories",
)


class DbrPolicySnapshotError(RuntimeError):
    """The candidate lacks a complete immutable DBR policy input."""


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _candidate_runs(db: Session, generation: models.LedgerGeneration) -> list[models.PlanningRun]:
    manifest = (generation.source_watermarks or {}).get(MANIFEST_KEY)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("entries"), list):
        raise DbrPolicySnapshotError("building generation has no sealed refresh manifest")
    ids: set[int] = set()
    for entry in manifest["entries"]:
        try:
            ids.add(int(entry["candidate_run_id"]))
        except (TypeError, KeyError, ValueError) as exc:
            raise DbrPolicySnapshotError("refresh manifest has malformed candidate identity") from exc
    if not ids:
        raise DbrPolicySnapshotError("refresh manifest has no candidate runs")
    runs = db.query(models.PlanningRun).filter(models.PlanningRun.run_id.in_(ids)).all()
    if {int(run.run_id) for run in runs} != ids:
        raise DbrPolicySnapshotError("refresh manifest references a missing candidate run")
    result = sorted(runs, key=lambda row: int(row.run_id))
    for run in result:
        if (
            str(run.status) != "BUILDING_SNAPSHOT"
            or int(run.ledger_generation_id or -1) != int(generation.id)
            or run.ledger_cutoff != generation.cutoff
            or run.active_freeze_version is None
            or run.period_from is None
            or run.period_to is None
            or run.period_from > run.period_to
        ):
            raise DbrPolicySnapshotError("candidate run lineage, freeze, or period is incomplete")
    actual = {
        int(row[0]) for row in db.query(models.PlanningRun.run_id).filter(
            models.PlanningRun.ledger_generation_id == int(generation.id),
            models.PlanningRun.status == "BUILDING_SNAPSHOT",
        ).all()
    }
    if actual != ids:
        raise DbrPolicySnapshotError("building generation has candidate runs outside its sealed manifest")
    return result


def _pool_mapping(generation: models.LedgerGeneration) -> dict[str, str]:
    manifest = (generation.source_watermarks or {}).get(MANIFEST_KEY) or {}
    value = ((manifest.get("add_request") or {}).get("planning_pool_by_warehouse"))
    if not isinstance(value, dict):
        raise DbrPolicySnapshotError("refresh manifest has no planning_pool_by_warehouse")
    result: dict[str, str] = {}
    for raw_warehouse, raw_pool in value.items():
        warehouse, pool = str(raw_warehouse).strip(), str(raw_pool).strip()
        if not warehouse or not pool or (warehouse in result and result[warehouse] != pool):
            raise DbrPolicySnapshotError("planning_pool_by_warehouse is malformed")
        result[warehouse] = pool
    if not result:
        raise DbrPolicySnapshotError("planning_pool_by_warehouse is empty")
    return {warehouse: result[warehouse] for warehouse in sorted(result)}


def _calendar(db: Session, runs: list[models.PlanningRun]) -> list[dict[str, Any]]:
    first, last = min(run.period_from for run in runs), max(run.period_to for run in runs)
    rows = db.query(models.WorkCalendarDay).filter(
        models.WorkCalendarDay.date >= first, models.WorkCalendarDay.date <= last
    ).all()
    by_date = {row.date: row for row in rows}
    expected: list[date] = []
    current = first
    while current <= last:
        expected.append(current)
        current = date.fromordinal(current.toordinal() + 1)
    missing = [day.isoformat() for day in expected if day not in by_date]
    if missing:
        raise DbrPolicySnapshotError("work calendar is incomplete for candidate periods: " + ", ".join(missing))
    return [
        {"date": day.isoformat(), "is_workday": bool(by_date[day].is_workday), "comment": by_date[day].comment}
        for day in expected
    ]


def _policy_items(
    db: Session,
    generation: models.LedgerGeneration,
    runs: list[models.PlanningRun],
    pools: dict[str, str],
    settings: models.DbrSettings,
) -> list[dict[str, Any]]:
    run_ids = [int(run.run_id) for run in runs]
    versions = {int(run.run_id): int(run.active_freeze_version) for run in runs}
    requirements = db.query(models.MrpRequirement).filter(models.MrpRequirement.run_id.in_(run_ids)).all()
    for row in requirements:
        expected = versions[int(row.run_id)]
        if row.freeze_version is None or int(row.freeze_version) != expected:
            raise DbrPolicySnapshotError("candidate requirements use a mixed or missing freeze version")
        pool = str(row.planning_stock_pool or "").strip()
        if not pool or pool not in set(pools.values()):
            raise DbrPolicySnapshotError("candidate requirement has no exact warehouse-pool mapping")

    # Bucket demand is the frozen schedule input for feeder policy.  A bucket
    # on a missing/non-work calendar day would make ADU depend on a later
    # mutable calendar edit, so reject it now rather than guessing.
    calendar = {
        row.date: bool(row.is_workday)
        for row in db.query(models.WorkCalendarDay).filter(
            models.WorkCalendarDay.date >= min(run.period_from for run in runs),
            models.WorkCalendarDay.date <= max(run.period_to for run in runs),
        ).all()
    }
    buckets = db.query(models.MrpRequirementBucket).filter(
        models.MrpRequirementBucket.run_id.in_(run_ids)
    ).all()
    requirement_run_by_id = {int(row.id): int(row.run_id) for row in requirements}
    for bucket in buckets:
        expected_run_id = requirement_run_by_id.get(int(bucket.requirement_id))
        if expected_run_id is None or int(bucket.run_id) != expected_run_id:
            raise DbrPolicySnapshotError("candidate bucket belongs to a foreign requirement")
        if bucket.bucket_date not in calendar or not calendar[bucket.bucket_date]:
            raise DbrPolicySnapshotError("candidate requirement bucket is outside a work calendar day")

    edges: dict[tuple[int, int], list[int]] = defaultdict(list)
    for run in runs:
        components = db.query(models.MrpFreezeComponent).filter(
            models.MrpFreezeComponent.run_id == int(run.run_id),
            models.MrpFreezeComponent.freeze_version == int(run.active_freeze_version),
        ).all()
        for component in components:
            for pool in (component.parent_planning_stock_pool, component.component_planning_stock_pool):
                if pool is not None and str(pool).strip() not in set(pools.values()):
                    raise DbrPolicySnapshotError("frozen component has no exact warehouse-pool mapping")
            edges[(int(run.run_id), int(component.parent_item_id))].append(int(component.component_item_id))

    reachable: set[int] = set()
    visited_axes: set[tuple[int, int]] = set()
    for run in runs:
        queue: deque[int] = deque(sorted(int(row.item_id) for row in requirements if int(row.run_id) == int(run.run_id)))
        while queue:
            item_id = queue.popleft()
            axis = (int(run.run_id), item_id)
            if axis in visited_axes:
                continue
            visited_axes.add(axis)
            reachable.add(item_id)
            queue.extend(sorted(edges[(int(run.run_id), item_id)]))
    if not reachable:
        return []
    rows = db.query(models.Item, models.ItemCategory).outerjoin(
        models.ItemCategory, models.Item.category_id == models.ItemCategory.category_id
    ).filter(models.Item.item_id.in_(reachable)).all()
    if {int(item.item_id) for item, _ in rows} != reachable:
        raise DbrPolicySnapshotError("frozen requirement/component refers to a missing item")
    default_spec: dict[int, int] = {}
    for row in db.query(models.DefaultSpecification).order_by(
        models.DefaultSpecification.id
    ).all():
        default_spec.setdefault(int(row.item_id), int(row.spec_id))
    spec_kind = {
        int(spec_id): int(kind_id)
        for spec_id, kind_id in db.query(
            models.Specification.spec_id,
            models.Specification.production_kind_id,
        ).all()
        if kind_id is not None
    }
    warehouse_by_resource = {
        int(workshop_id): str(warehouse)
        for workshop_id, warehouse in db.query(
            models.WorkshopWarehouseBinding.workshop_id,
            models.WorkshopWarehouseBinding.production_warehouse_ref1c,
        ).all()
        if warehouse
    }
    warehouses_by_kind: dict[int, set[str]] = defaultdict(set)
    for resource_id, kind_id in db.query(
        models.ResourceProductionKind.resource_id,
        models.ResourceProductionKind.production_kind_id,
    ).all():
        warehouse = warehouse_by_resource.get(int(resource_id))
        if warehouse:
            warehouses_by_kind[int(kind_id)].add(warehouse)
    w3_items = {
        int(item_id)
        for (item_id,) in db.query(models.StockBin.item_id).filter(
            models.StockBin.ledger_generation_id == int(generation.id),
            models.StockBin.warehouse_ref1c
            == str(settings.w3_warehouse_ref1c),
        ).distinct().all()
    }
    route_text = adapters.item_route_text_map(db)
    fastener_categories = set(settings.fastener_categories or [])

    def boundary(item: models.Item, category: models.ItemCategory | None) -> tuple[str, str]:
        if category is not None and category.category_name in fastener_categories:
            return "fastener", ""
        flow = classify_replenishment_flow(item.replenishment_method)
        if flow in {REPLENISHMENT_FLOW_PURCHASE, REPLENISHMENT_FLOW_REWORK}:
            return (
                "processing" if flow == REPLENISHMENT_FLOW_REWORK else "purchase",
                str(settings.w4_warehouse_ref1c),
            )
        spec_id = default_spec.get(int(item.item_id))
        kind_id = spec_kind.get(spec_id) if spec_id is not None else None
        if (
            kind_id is not None
            and str(settings.w2_warehouse_ref1c)
            in warehouses_by_kind.get(kind_id, set())
        ):
            return "w2", str(settings.w2_warehouse_ref1c)
        if spec_id is not None and int(item.item_id) in w3_items:
            return "w3", str(settings.w3_warehouse_ref1c)
        return (
            "w4" if spec_id is not None else "under_schedule",
            str(settings.w4_warehouse_ref1c),
        )

    result: list[dict[str, Any]] = []
    for item, category in sorted(
        rows, key=lambda row: (str(row[0].item_code), int(row[0].item_id))
    ):
        boundary_kind, boundary_warehouse = boundary(item, category)
        if boundary_kind != "fastener" and pools.get(boundary_warehouse) is None:
            raise DbrPolicySnapshotError(
                f"item {item.item_code} boundary warehouse has no exact planning pool"
            )
        result.append(
            {
                "item_id": int(item.item_id),
                "item_code": item.item_code,
                "item_name": item.item_name,
                "replenishment_method": item.replenishment_method,
                "replenishment_time": item.replenishment_time,
                "optimal_batch": _json_value(item.optimal_batch),
                "category": None
                if category is None
                else {
                    "category_id": int(category.category_id),
                    "category_name": category.category_name,
                    "category_ref1c": category.category_ref1c,
                },
                "route_text": route_text.get(str(item.item_code), ""),
                "boundary_kind": boundary_kind,
                "boundary_warehouse_ref1c": boundary_warehouse or None,
                "has_default_spec": int(item.item_id) in default_spec,
            }
        )
    return result


def build_policy_candidate_snapshot(db: Session, generation_id: int) -> models.PlanningReadSnapshot:
    """Capture a deterministic policy snapshot for a BUILDING refresh generation.

    The caller owns the transaction.  Exact retries return the existing row;
    any changed master/policy input is rejected instead of silently overwriting
    a snapshot that may be about to become published truth.
    """
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or str(generation.status) != "building" or generation.cutoff is None:
        raise DbrPolicySnapshotError("DBR policy snapshot requires a BUILDING Ledger generation with cutoff")
    if (generation.source_watermarks or {}).get("generation_kind") != "obligation_refresh":
        raise DbrPolicySnapshotError("DBR policy snapshot requires an obligation_refresh generation")
    runs = _candidate_runs(db, generation)
    pools = _pool_mapping(generation)
    settings = db.get(models.DbrSettings, 1)
    if settings is None:
        raise DbrPolicySnapshotError("DBR settings are not configured")
    settings_payload = {field: _json_value(getattr(settings, field)) for field in _SETTINGS_FIELDS}
    required_roles = (
        settings.w2_warehouse_ref1c,
        settings.w3_warehouse_ref1c,
        settings.w4_warehouse_ref1c,
    )
    if any(not str(warehouse or "").strip() for warehouse in required_roles):
        raise DbrPolicySnapshotError("DBR warehouse roles W2/W3/W4 are incomplete")
    for warehouse in required_roles:
        if str(warehouse).strip() not in pools:
            raise DbrPolicySnapshotError("configured DBR warehouse has no exact planning-pool mapping")
    risks = [
        {"item_group": row.item_group, "receipt_warehouse_ref1c": row.receipt_warehouse_ref1c,
         "supply_risk_pct": _json_value(row.supply_risk_pct)}
        for row in db.query(models.DbrCategorySupplyRisk).order_by(models.DbrCategorySupplyRisk.item_group).all()
    ]
    for risk in risks:
        warehouse = risk["receipt_warehouse_ref1c"]
        if warehouse is not None and str(warehouse).strip() not in pools:
            raise DbrPolicySnapshotError("category supply risk warehouse has no exact planning-pool mapping")
    w3_items: list[int] = []
    if settings.w3_warehouse_ref1c:
        # Physical W3 topology is Ledger-owned.  Never consult ItemWarehouseStock.
        w3_items = sorted(int(item_id) for (item_id,) in db.query(models.StockBin.item_id).filter(
            models.StockBin.ledger_generation_id == int(generation.id),
            models.StockBin.warehouse_ref1c == str(settings.w3_warehouse_ref1c),
        ).distinct().all())
    calendar = _calendar(db, runs)
    payload = {
        "version": 1,
        "ledger_generation_id": int(generation.id), "cutoff": _json_value(generation.cutoff),
        "runs": [{"run_id": int(run.run_id), "freeze_version": int(run.active_freeze_version),
                  "period_from": run.period_from.isoformat(), "period_to": run.period_to.isoformat()} for run in runs],
        "planning_pool_by_warehouse": pools,
        "settings": settings_payload,
        "category_supply_risks": risks,
        "calendar": calendar,
        "items": _policy_items(db, generation, runs, pools, settings),
        "w3_stock_item_ids": w3_items,
    }
    payload = _json_value(payload)
    existing = db.query(models.PlanningReadSnapshot).filter(
        models.PlanningReadSnapshot.consumer == CONSUMER,
        models.PlanningReadSnapshot.snapshot_key == SNAPSHOT_KEY,
        models.PlanningReadSnapshot.ledger_generation_id == int(generation.id),
    ).one_or_none()
    if existing is not None:
        if existing.truth_status != "building" or existing.cutoff != generation.cutoff or _canonical(existing.payload) != _canonical(payload):
            raise DbrPolicySnapshotError("candidate DBR policy snapshot conflicts with persisted data")
        return existing
    snapshot = models.PlanningReadSnapshot(
        consumer=CONSUMER, snapshot_key=SNAPSHOT_KEY, ledger_generation_id=int(generation.id),
        cutoff=generation.cutoff, truth_status="building", reason="unpublished DBR policy input",
        payload=payload, published_at=datetime.now(timezone.utc),
    )
    db.add(snapshot)
    db.flush()
    return snapshot
