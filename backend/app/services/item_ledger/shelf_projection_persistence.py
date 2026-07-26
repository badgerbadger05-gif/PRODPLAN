"""Persist shelf timing projections from frozen norms and persisted drum slots."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app import models

from .reservation import replenishment_remaining
from .shelf_projection_core import ShelfDemand, project_shelf


STAGE = "shelf_projection"
ALGORITHM_VERSION = "shelf-projection/1"


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
            models.MrpFreezeComponent,
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
            models.MrpFreezeComponent,
            and_(
                models.MrpFreezeComponent.run_id
                == models.AssemblyQueueLine.planning_run_id,
                models.MrpFreezeComponent.parent_item_id == models.DrumSlot.item_id,
                models.MrpFreezeComponent.component_item_id.in_(sorted(by_item)),
            ),
        )
        .filter(models.DrumSchedule.ledger_generation_id == int(generation_id))
        .order_by(
            models.DrumSlot.slot_date,
            models.AssemblyQueueLine.sort_key,
            models.DrumSlot.id,
            models.MrpFreezeComponent.id,
        )
        .all()
    )
    for slot, queue, component in rows:
        policy_id = by_item[int(component.component_item_id)]
        qty = _d(slot.slot_qty) * _d(component.norm_qty_per_unit)
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


def _stock(
    db: Session, generation_id: int, item_id: int, shelf_warehouse: str
) -> tuple[Decimal, Decimal]:
    rows = (
        db.query(models.StockBin.warehouse_ref1c, func.sum(models.StockBin.on_hand))
        .filter(
            models.StockBin.ledger_generation_id == int(generation_id),
            models.StockBin.item_id == int(item_id),
        )
        .group_by(models.StockBin.warehouse_ref1c)
        .all()
    )
    shelf = sum(
        (_d(qty) for warehouse, qty in rows if str(warehouse) == shelf_warehouse),
        Decimal("0"),
    )
    other = sum(
        (
            max(_d(qty), Decimal("0"))
            for warehouse, qty in rows
            if str(warehouse) != shelf_warehouse
        ),
        Decimal("0"),
    )
    return shelf, other


def _confirmed_production(
    db: Session,
    requirement_ids: list[int],
    warehouse_ref1c: str,
    protection_until,
) -> Decimal:
    if not requirement_ids:
        return Decimal("0")
    rows = (
        db.query(models.ProductionProduct, models.ProductionOrderLineState)
        .outerjoin(
            models.ProductionOrderLineState,
            models.ProductionOrderLineState.product_id
            == models.ProductionProduct.product_id,
        )
        .filter(
            models.ProductionProduct.source_mrp_requirement_id.in_(requirement_ids),
            models.ProductionProduct.destination_warehouse_ref1c == warehouse_ref1c,
            models.ProductionProduct.remaining_qty > 0,
        )
        .all()
    )
    return sum(
        (
            _d(product.remaining_qty)
            for product, state in rows
            if state is not None
            and state.planned_finish_date is not None
            and state.planned_finish_date <= protection_until
        ),
        Decimal("0"),
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
        )
        preliminary = project_shelf(
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
            confirmed_open_production_qty=Decimal("0"),
        )
        confirmed = _confirmed_production(
            db,
            requirement_ids,
            str(policy.warehouse_ref1c),
            preliminary.protection_until,
        )
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
            confirmed_open_production_qty=confirmed,
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
