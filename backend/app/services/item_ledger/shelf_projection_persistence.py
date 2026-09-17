"""Persist shelf timing projections from frozen norms and persisted drum slots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Mapping

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app import models

from .reservation import replenishment_remaining
from .shelf_projection_core import ShelfDemand, ShelfReceipt, project_shelf
from .future_supply_read import future_supply_model


STAGE = "shelf_projection"
ALGORITHM_VERSION = "shelf-projection/2"


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def _checksum(value: Any) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _demands_by_policy(
    db: Session,
    generation_id: int,
    policies: list[models.ShelfPolicy],
) -> dict[int, list[dict[str, Any]]]:
    by_item = {int(row.item_id): int(row.id) for row in policies}
    result: dict[int, list[dict[str, Any]]] = {int(row.id): [] for row in policies}
    if not by_item:
        return result
    rows = (
        db.query(
            models.DrumSlot,
            models.AssemblyQueueLine,
            models.MrpFreezeComponentCumulative,
        )
        .join(
            models.DrumSchedule,
            models.DrumSchedule.id == models.DrumSlot.drum_schedule_id,
        )
        .join(
            models.AssemblyQueueLine,
            models.AssemblyQueueLine.id == models.DrumSlot.assembly_queue_line_id,
        )
        .join(
            models.PlanningRun,
            models.PlanningRun.run_id == models.AssemblyQueueLine.planning_run_id,
        )
        .join(
            models.MrpFreezeComponentCumulative,
            and_(
                models.MrpFreezeComponentCumulative.run_id
                == models.AssemblyQueueLine.planning_run_id,
                # Refreezing one run keeps every older frozen norm row alive.
                # Without the active version the same component demand would be
                # summed once per freeze version and inflate the shelf target.
                models.MrpFreezeComponentCumulative.freeze_version
                == models.PlanningRun.active_freeze_version,
                models.MrpFreezeComponentCumulative.root_item_id == models.DrumSlot.item_id,
                models.MrpFreezeComponentCumulative.component_item_id.in_(sorted(by_item)),
            ),
        )
        .filter(models.DrumSchedule.ledger_generation_id == int(generation_id))
        .order_by(
            models.DrumSlot.slot_date,
            models.AssemblyQueueLine.sort_key,
            models.DrumSlot.id,
            models.MrpFreezeComponentCumulative.id,
        )
        .all()
    )
    for slot, queue, component in rows:
        policy_id = by_item[int(component.component_item_id)]
        qty = _d(slot.slot_qty) * _d(component.cumulative_norm_qty_per_root_unit)
        if qty <= 0:
            continue
        result[policy_id].append(
            {
                "need_date": slot.slot_date,
                "qty": qty,
                "priority": tuple(queue.original_priority or ()),
                "planning_run_id": int(queue.planning_run_id),
                "plan_id": int(queue.plan_id),
                "plan_line_id": int(queue.plan_line_id),
                "drum_slot_id": int(slot.id),
                "freeze_component_id": int(component.id),
                "root_item_id": int(component.root_item_id),
                "component_cumulative_norm": str(_d(component.cumulative_norm_qty_per_root_unit)),
            }
        )
    return result


def _open_mrp(
    db: Session, generation_id: int, item_id: int
) -> tuple[Decimal, list[int]]:
    rows = (
        db.query(models.ReservationEntry)
        .filter(
            models.ReservationEntry.ledger_generation_id == int(generation_id),
            models.ReservationEntry.item_id == int(item_id),
            models.ReservationEntry.lifecycle_status == "active",
            models.ReservationEntry.realization_mode == "make",
        )
        .all()
    )
    return (
        sum(
            (
                replenishment_remaining(
                    row.replenishment_required_qty,
                    row.replenishment_received_qty,
                )
                for row in rows
            ),
            Decimal("0"),
        ),
        [int(row.requirement_id) for row in rows],
    )


def _ignored_warehouses(db: Session) -> set[str]:
    """Warehouses whose stock must never propose a transfer to the shelf."""
    return {
        str(ref)
        for (ref,) in db.query(models.IgnoredWarehouse.warehouse_ref1c).all()
        if ref
    }


def _stock(
    db: Session,
    generation_id: int,
    item_id: int,
    shelf_warehouse: str,
    ignored_warehouses: set[str],
) -> tuple[Decimal, Decimal]:
    rows = (
        db.query(models.StockBin.warehouse_ref1c, func.sum(models.StockBin.on_hand))
        .filter(
            models.StockBin.is_current.is_(True),
            models.StockBin.item_id == int(item_id),
        )
        .group_by(models.StockBin.warehouse_ref1c)
        .all()
    )
    shelf = sum(
        (_d(qty) for warehouse, qty in rows if str(warehouse) == shelf_warehouse),
        Decimal("0"),
    )
    # Ignored warehouses (tolling stock, scrap isolator, WIP) hold quantity that
    # is not ours to move, so it must not turn into a transfer proposal.
    other = sum(
        (
            max(_d(qty), Decimal("0"))
            for warehouse, qty in rows
            if str(warehouse) != shelf_warehouse
            and str(warehouse) not in ignored_warehouses
        ),
        Decimal("0"),
    )
    return shelf, other


def _confirmed_receipts(
    db: Session,
    generation_id: int,
    item_id: int,
    requirement_ids: list[int],
    warehouse_ref1c: str,
) -> tuple[ShelfReceipt, ...]:
    """Read dated WIP receipts only from this generation's sealed capture."""
    if not requirement_ids:
        return ()
    future_supply = future_supply_model(db, int(generation_id))
    rows = (
        db.query(
            future_supply.eta_date,
            future_supply.open_qty_at_cutoff,
        )
        .filter(
            future_supply.ledger_generation_id == int(generation_id),
            future_supply.supply_kind == "wip_order",
            future_supply.evidence_status == "exact",
            future_supply.item_id == int(item_id),
            future_supply.source_requirement_id.in_(requirement_ids),
            future_supply.destination_warehouse_ref1c
            == str(warehouse_ref1c),
            future_supply.open_qty_at_cutoff > 0,
            future_supply.eta_date.is_not(None),
        )
        .order_by(future_supply.eta_date, future_supply.id)
        .all()
    )
    return tuple(
        ShelfReceipt(available_from=eta_date, qty=_d(open_qty))
        for eta_date, open_qty in rows
    )


@dataclass(frozen=True)
class CompactShelfProjectionPayload:
    """Full current shelf scope built without ShelfProjection staging rows."""

    target_generation_id: int
    parent_generation_id: int
    rows: tuple[dict[str, Any], ...]
    metrics: dict[str, Any]


def _open_current_mrp(
    db: Session,
    item_id: int,
) -> tuple[Decimal, list[int]]:
    rows = (
        db.query(models.ReservationEntry)
        .filter(
            models.ReservationEntry.item_id == int(item_id),
            models.ReservationEntry.is_current.is_(True),
            models.ReservationEntry.owner_kind == "current",
            models.ReservationEntry.lifecycle_status == "active",
            models.ReservationEntry.realization_mode == "make",
        )
        .order_by(models.ReservationEntry.priority_period_from, models.ReservationEntry.id)
        .all()
    )
    return (
        sum(
            (
                replenishment_remaining(
                    row.replenishment_required_qty,
                    row.replenishment_received_qty,
                )
                for row in rows
            ),
            Decimal("0"),
        ),
        [int(row.requirement_id) for row in rows],
    )


def _confirmed_current_receipts(
    db: Session,
    parent_generation_id: int,
    item_id: int,
    requirement_ids: list[int],
    warehouse_ref1c: str,
) -> tuple[ShelfReceipt, ...]:
    if not requirement_ids:
        return ()
    future_supply = future_supply_model(
        db,
        int(parent_generation_id),
        allow_building_read=False,
    )
    rows = (
        db.query(future_supply.eta_date, future_supply.open_qty_at_cutoff)
        .filter(
            future_supply.supply_kind == "wip_order",
            future_supply.evidence_status == "exact",
            future_supply.item_id == int(item_id),
            future_supply.source_requirement_id.in_(requirement_ids),
            future_supply.destination_warehouse_ref1c == str(warehouse_ref1c),
            future_supply.open_qty_at_cutoff > 0,
            future_supply.eta_date.is_not(None),
        )
        .order_by(future_supply.eta_date, future_supply.id)
        .all()
    )
    return tuple(
        ShelfReceipt(available_from=eta_date, qty=_d(open_qty))
        for eta_date, open_qty in rows
    )


def build_compact_current_shelf_payload(
    db: Session,
    *,
    target_generation_id: int,
    parent_generation_id: int,
    drum_payload: Any,
) -> CompactShelfProjectionPayload:
    """Build shelf projection from compact drum DTOs and current owners."""

    target = db.get(models.LedgerGeneration, int(target_generation_id))
    parent = db.get(models.LedgerGeneration, int(parent_generation_id))
    if target is None or str(target.status or "") != "building":
        raise ValueError("compact shelf payload requires a BUILDING target")
    if parent is None or str(parent.status or "") != "accepted":
        raise ValueError("compact shelf payload requires an accepted parent")
    if target.cutoff is None or int(target.id) == int(parent.id):
        raise ValueError("compact shelf payload target boundary is invalid")

    policies = (
        db.query(models.ShelfPolicy)
        .filter(models.ShelfPolicy.active.is_(True))
        .order_by(models.ShelfPolicy.item_id, models.ShelfPolicy.id)
        .all()
    )
    policy_by_item = {int(row.item_id): row for row in policies}
    demand_by_policy: dict[int, list[dict[str, Any]]] = {
        int(row.id): [] for row in policies
    }
    raw_rows = list(getattr(drum_payload, "rows", ()) or ())
    slot_rows = [
        row for row in raw_rows
        if isinstance(row, Mapping) and str(row.get("entity_kind")) == "drum_slot"
    ]
    root_ids = {int((row.get("payload") or {}).get("item_id")) for row in slot_rows}
    run_ids = {int((row.get("payload") or {}).get("run_id")) for row in slot_rows}
    component_item_ids = set(policy_by_item)
    components: dict[tuple[int, int, int], list[Any]] = {}
    if root_ids and run_ids and component_item_ids:
        rows = (
            db.query(models.MrpFreezeComponentCumulative, models.PlanningRun)
            .join(
                models.PlanningRun,
                models.PlanningRun.run_id == models.MrpFreezeComponentCumulative.run_id,
            )
            .filter(
                models.MrpFreezeComponentCumulative.run_id.in_(sorted(run_ids)),
                models.MrpFreezeComponentCumulative.root_item_id.in_(sorted(root_ids)),
                models.MrpFreezeComponentCumulative.component_item_id.in_(sorted(component_item_ids)),
                models.MrpFreezeComponentCumulative.freeze_version
                == models.PlanningRun.active_freeze_version,
            )
            .all()
        )
        for component, run in rows:
            components.setdefault(
                (int(run.run_id), int(component.root_item_id), int(component.component_item_id)),
                [],
            ).append(component)
    for raw in slot_rows:
        payload = raw.get("payload") or {}
        run_id = int(payload.get("run_id") or 0)
        root_item_id = int(payload.get("item_id") or 0)
        slot_qty = _d(payload.get("slot_qty"))
        slot_date = datetime.fromisoformat(str(payload["slot_date"])).date()
        for component_item_id, policy in policy_by_item.items():
            for component in components.get((run_id, root_item_id, component_item_id), []):
                qty = slot_qty * _d(component.cumulative_norm_qty_per_root_unit)
                if qty <= 0:
                    continue
                demand_by_policy[int(policy.id)].append({
                    "need_date": slot_date,
                    "qty": qty,
                    "priority": tuple(payload.get("original_priority") or ()),
                    "planning_run_id": run_id,
                    "plan_id": int(payload.get("plan_id") or 0),
                    "plan_line_id": int(payload.get("plan_line_id") or 0),
                    "drum_slot_identity": str(raw.get("business_identity") or ""),
                    "freeze_component_id": int(component.id),
                    "root_item_id": root_item_id,
                    "component_cumulative_norm": str(_d(component.cumulative_norm_qty_per_root_unit)),
                })

    ignored_warehouses = _ignored_warehouses(db)
    created: list[dict[str, Any]] = []
    for policy in policies:
        manifest = demand_by_policy[int(policy.id)]
        open_qty, requirement_ids = _open_current_mrp(db, int(policy.item_id))
        shelf_qty, other_qty = _stock(
            db, int(parent.id), int(policy.item_id), str(policy.warehouse_ref1c), ignored_warehouses
        )
        result = project_shelf(
            tuple(ShelfDemand(row["need_date"], row["qty"], row["priority"]) for row in manifest),
            as_of=target.cutoff.date(),
            replenishment_time_days=int(policy.replenishment_time_days),
            review_cycle_days=int(policy.review_cycle_days),
            safety_days=int(policy.safety_days),
            batch_multiple=_d(policy.batch_multiple),
            open_mrp_qty=open_qty,
            shelf_physical_qty=shelf_qty,
            other_stock_qty=other_qty,
            confirmed_receipts=_confirmed_current_receipts(
                db, int(parent.id), int(policy.item_id), requirement_ids, str(policy.warehouse_ref1c)
            ),
        )
        created.append({
            "entity_kind": "shelf_projection",
            "business_identity": f"shelf-policy:{int(policy.id)}",
            "scope_key": "shelf:all-live-mrps",
            "payload": {
                "policy_id": int(policy.id), "item_id": int(policy.item_id),
                "warehouse_ref1c": str(policy.warehouse_ref1c),
                "as_of_date": target.cutoff.date().isoformat(),
                "protection_until": result.protection_until.isoformat(),
                "target_qty": str(result.target_qty),
                "shelf_physical_qty": str(result.shelf_physical_qty),
                "other_stock_qty": str(result.other_stock_qty),
                "confirmed_open_production_qty": str(result.confirmed_open_production_qty),
                "projected_qty": str(result.projected_qty), "gap_qty": str(result.gap_qty),
                "transfer_qty": str(result.transfer_qty),
                "unlaunched_mrp_qty": str(result.unlaunched_mrp_qty),
                "pull_qty": str(result.pull_qty), "materialized_qty": str(result.materialized_qty),
                "first_shortage_date": result.first_shortage_date.isoformat() if result.first_shortage_date else None,
                "latest_start_date": result.latest_start_date.isoformat() if result.latest_start_date else None,
                "demand_manifest": [
                    {
                        **{key: value for key, value in demand.items() if key != "priority"},
                        "need_date": demand["need_date"].isoformat(),
                        "qty": str(demand["qty"]), "priority": list(demand["priority"]),
                    }
                    for demand in manifest
                ],
            },
        })
    return CompactShelfProjectionPayload(
        target_generation_id=int(target.id), parent_generation_id=int(parent.id),
        rows=tuple(created), metrics={"projection_rows": len(created)},
    )


def _payload(row: models.ShelfProjection) -> dict[str, Any]:
    return {
        "policy_id": int(row.shelf_policy_id),
        "item_id": int(row.item_id),
        "warehouse_ref1c": row.warehouse_ref1c,
        "protection_until": row.protection_until.isoformat(),
        "target_qty": str(row.target_qty),
        "projected_qty": str(row.projected_qty),
        "gap_qty": str(row.gap_qty),
        "transfer_qty": str(row.transfer_qty),
        "pull_qty": str(row.pull_qty),
        "materialized_qty": str(row.materialized_qty),
        "first_shortage_date": (
            row.first_shortage_date.isoformat() if row.first_shortage_date else None
        ),
        "latest_start_date": (
            row.latest_start_date.isoformat() if row.latest_start_date else None
        ),
        "demand_manifest": list(row.demand_manifest or []),
    }


def materialize_shelf_projections(
    db: Session, ledger_generation_id: int
) -> dict[str, Any]:
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None:
        raise ValueError(f"LedgerGeneration {ledger_generation_id} not found")
    if generation.status != "building":
        raise ValueError("shelf projection requires a BUILDING generation")
    if generation.cutoff is None:
        raise ValueError("shelf projection requires generation cutoff")
    if (
        db.query(models.DrumSchedule)
        .filter(models.DrumSchedule.ledger_generation_id == generation.id)
        .count()
        != 1
    ):
        raise ValueError("shelf projection requires one persisted drum schedule")

    policies = (
        db.query(models.ShelfPolicy)
        .filter(models.ShelfPolicy.active.is_(True))
        .order_by(models.ShelfPolicy.item_id, models.ShelfPolicy.id)
        .all()
    )
    demands = _demands_by_policy(db, int(generation.id), policies)
    existing = (
        db.query(models.ShelfProjection)
        .filter(models.ShelfProjection.ledger_generation_id == generation.id)
        .order_by(models.ShelfProjection.shelf_policy_id)
        .all()
    )
    batch_key = f"g{generation.id}:{STAGE}:{ALGORITHM_VERSION}"
    batch = (
        db.query(models.LedgerBuildBatch)
        .filter_by(
            ledger_generation_id=generation.id,
            stage=STAGE,
            batch_key=batch_key,
        )
        .one_or_none()
    )
    if existing or batch:
        if not existing and policies or batch is None:
            raise ValueError("partial shelf projection checkpoint exists")
        checksum = _checksum([_payload(row) for row in existing])
        if checksum != str((batch.metrics or {}).get("projection_checksum") or ""):
            raise ValueError("persisted shelf projection drift")
        return {
            "ledger_generation_id": int(generation.id),
            "batch_id": int(batch.id),
            "projection_rows": len(existing),
            "projection_checksum": checksum,
        }

    created: list[models.ShelfProjection] = []
    as_of = generation.cutoff.date()
    ignored_warehouses = _ignored_warehouses(db)
    for policy in policies:
        manifest = demands[int(policy.id)]
        open_qty, requirement_ids = _open_mrp(
            db, int(generation.id), int(policy.item_id)
        )
        shelf_qty, other_qty = _stock(
            db,
            int(generation.id),
            int(policy.item_id),
            str(policy.warehouse_ref1c),
            ignored_warehouses,
        )
        # One pass now: the second projection only ever existed to learn
        # ``protection_until`` for the receipt filter this no longer applies.
        result = project_shelf(
            tuple(
                ShelfDemand(row["need_date"], row["qty"], row["priority"])
                for row in manifest
            ),
            as_of=as_of,
            replenishment_time_days=int(policy.replenishment_time_days),
            review_cycle_days=int(policy.review_cycle_days),
            safety_days=int(policy.safety_days),
            batch_multiple=_d(policy.batch_multiple),
            open_mrp_qty=open_qty,
            shelf_physical_qty=shelf_qty,
            other_stock_qty=other_qty,
            confirmed_receipts=_confirmed_receipts(
                db,
                int(generation.id),
                int(policy.item_id),
                requirement_ids,
                str(policy.warehouse_ref1c),
            ),
        )
        row = models.ShelfProjection(
            ledger_generation_id=int(generation.id),
            shelf_policy_id=int(policy.id),
            item_id=int(policy.item_id),
            warehouse_ref1c=str(policy.warehouse_ref1c),
            as_of_date=as_of,
            protection_until=result.protection_until,
            target_qty=result.target_qty,
            shelf_physical_qty=result.shelf_physical_qty,
            other_stock_qty=result.other_stock_qty,
            confirmed_open_production_qty=result.confirmed_open_production_qty,
            projected_qty=result.projected_qty,
            gap_qty=result.gap_qty,
            transfer_qty=result.transfer_qty,
            unlaunched_mrp_qty=result.unlaunched_mrp_qty,
            pull_qty=result.pull_qty,
            materialized_qty=result.materialized_qty,
            first_shortage_date=result.first_shortage_date,
            latest_start_date=result.latest_start_date,
            demand_manifest=[
                {
                    **{key: value for key, value in demand.items() if key != "priority"},
                    "need_date": demand["need_date"].isoformat(),
                    "qty": str(demand["qty"]),
                    "priority": list(demand["priority"]),
                }
                for demand in manifest
            ],
        )
        db.add(row)
        created.append(row)
    db.flush()
    checksum = _checksum([_payload(row) for row in created])
    batch = models.LedgerBuildBatch(
        ledger_generation_id=int(generation.id),
        stage=STAGE,
        batch_key=batch_key,
        status="completed",
        algorithm_version=ALGORITHM_VERSION,
        metrics={
            "projection_rows": len(created),
            "projection_checksum": checksum,
        },
        completed_at=datetime.now(timezone.utc),
    )
    db.add(batch)
    db.flush()
    return {
        "ledger_generation_id": int(generation.id),
        "batch_id": int(batch.id),
        "projection_rows": len(created),
        "projection_checksum": checksum,
    }
