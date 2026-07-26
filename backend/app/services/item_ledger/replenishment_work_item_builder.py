"""Build unified replenishment work items from frozen reservations.

The builder is intentionally deterministic and generation-scoped:
- reads only active reservations with positive replenishment need
- deletes and recreates rows for the target generation
- guarantees one work item per reservation and stable summary metrics
"""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

from sqlalchemy.orm import Session

from app import models
from .reservation import replenishment_remaining

_STAGE = "replenishment_work_item"


class ReplenishmentWorkItemBuilderError(RuntimeError):
    """The replenishment work-item pass cannot be executed for target generation."""


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _run_plan_ids(db: Session, run_ids: set[int]) -> dict[int, int]:
    if not run_ids:
        return {}
    runs = (
        db.query(models.PlanningRun.run_id, models.PlanningRun.source_plan_id)
        .filter(
            models.PlanningRun.run_id.in_(sorted(run_ids)),
            models.PlanningRun.status.in_(
                ("FIXED_SNAPSHOT", "BUILDING_SNAPSHOT")
            ),
            models.PlanningRun.source_plan_id.isnot(None),
        )
        .all()
    )
    if not runs:
        return {}
    return {int(run_id): int(plan_id) for run_id, plan_id in runs}


def _build_rows(
    db: Session,
    generation: models.LedgerGeneration,
    plan_by_run: dict[int, int],
) -> tuple[dict[str, dict[str, Any]], int]:
    reservations = (
        db.query(models.ReservationEntry)
        .filter(
            models.ReservationEntry.ledger_generation_id == int(generation.id),
            models.ReservationEntry.lifecycle_status == "active",
            models.ReservationEntry.run_id.isnot(None),
            models.ReservationEntry.replenishment_required_qty > 0,
        )
        .order_by(models.ReservationEntry.id.asc())
        .all()
    )

    work_items: list[dict[str, Any]] = []
    method_counts = {"make": 0, "buy": 0}
    for row in reservations:
        run_id = int(row.run_id)
        plan_id = plan_by_run.get(run_id)
        if plan_id is None:
            raise ReplenishmentWorkItemBuilderError(
                f"reservation {int(row.id)} has a missing plan lineage"
            )
        method = _text(row.realization_mode)
        required = _decimal(row.replenishment_required_qty)
        fulfilled = _decimal(row.replenishment_received_qty)
        if required <= 0:
            continue
        if fulfilled < 0 or fulfilled > required:
            raise ReplenishmentWorkItemBuilderError(
                f"reservation {int(row.id)} has invalid replenishment execution"
            )
        if method not in method_counts:
            raise ReplenishmentWorkItemBuilderError(
                f"reservation {int(row.id)} has unsupported realization mode {method!r}"
            )
        method_counts[method] += 1
        remaining = replenishment_remaining(required, fulfilled)
        work_items.append({
            "reservation_id": int(row.id),
            "plan_id": int(plan_id),
            "run_id": run_id,
            "requirement_id": int(row.requirement_id),
            "item_id": int(row.item_id),
            "replenishment_method": method,
            "replenishment_required_qty": required,
            "replenishment_fulfilled_qty": fulfilled,
            "replenishment_remaining_qty": remaining,
        })

    return {
        "rows": work_items,
        "method_counts": method_counts,
    }, len(reservations)


def materialize_replenishment_work_items(
    db: Session,
    ledger_generation_id: int,
    build_batch_id: int,
) -> dict[str, Any]:
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None or str(generation.status) != "building":
        raise ReplenishmentWorkItemBuilderError("target generation must be BUILDING")

    batch = db.get(models.LedgerBuildBatch, int(build_batch_id))
    if (
        batch is None
        or int(batch.ledger_generation_id) != int(generation.id)
        or str(batch.stage) != _STAGE
        or str(batch.status) != "building"
    ):
        raise ReplenishmentWorkItemBuilderError("work-item build batch must be BUILDING")

    run_by_plan = _run_plan_ids(
        db,
        {
            int(value[0])
            for value in db.query(models.ReservationEntry.run_id)
            .filter(
                models.ReservationEntry.ledger_generation_id == int(generation.id),
                models.ReservationEntry.lifecycle_status == "active",
                models.ReservationEntry.run_id.isnot(None),
            )
            .all()
        },
    )

    payload, source_rows = _build_rows(db, generation, run_by_plan)
    rows = payload["rows"]
    method_counts = payload["method_counts"]
    required_sum = sum((row["replenishment_required_qty"] for row in rows), Decimal("0"))
    fulfilled_sum = sum((row["replenishment_fulfilled_qty"] for row in rows), Decimal("0"))
    remaining_sum = sum((row["replenishment_remaining_qty"] for row in rows), Decimal("0"))
    checksum_payload = {
        "rows": rows,
        "batch_algorithm_version": str(batch.algorithm_version),
        "generation_id": int(generation.id),
    }

    with db.begin_nested():
        db.query(models.ReplenishmentWorkItem).filter(
            models.ReplenishmentWorkItem.ledger_generation_id == int(generation.id)
        ).delete(synchronize_session=False)
        for row in rows:
            db.add(models.ReplenishmentWorkItem(
                ledger_generation_id=int(generation.id),
                reservation_id=int(row["reservation_id"]),
                plan_id=int(row["plan_id"]),
                run_id=int(row["run_id"]),
                requirement_id=int(row["requirement_id"]),
                item_id=int(row["item_id"]),
                replenishment_method=str(row["replenishment_method"]),
                replenishment_required_qty=row["replenishment_required_qty"],
                replenishment_fulfilled_qty=row["replenishment_fulfilled_qty"],
                replenishment_remaining_qty=row["replenishment_remaining_qty"],
            ))
        db.flush()

    metrics = {
        "source_reservation_rows": int(source_rows),
        "replenishment_work_items": len(rows),
        "replenishment_work_item_methods": method_counts,
        "replenishment_required_total": str(required_sum),
        "replenishment_fulfilled_total": str(fulfilled_sum),
        "replenishment_remaining_total": str(remaining_sum),
        "rows_checksum": sha256(_canonical(checksum_payload).encode("utf-8")).hexdigest(),
    }
    return metrics
