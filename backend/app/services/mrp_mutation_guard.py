"""Fail-closed lineage checks for MRP materialization and 1C exports.

These checks live in the service layer because routers, workers and tests may
all call the mutation services directly.  Dry-runs intentionally use the same
gate as live writes: an unsafe preview is still an unsafe promise.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app import models
from app.services.planning_truth import (
    CAPABILITY_EXECUTION_ALLOCATIONS,
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_RESERVATION_REPLAY,
    require_accepted_truth,
)


REQUIRED_MUTATION_CAPABILITIES = (
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_RESERVATION_REPLAY,
    CAPABILITY_EXECUTION_ALLOCATIONS,
)


class MrpMutationLineageError(ValueError):
    """Selected MRP rows do not belong to the currently accepted truth."""


def _same_instant(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return False
    if left.tzinfo is None:
        left = left.replace(tzinfo=timezone.utc)
    if right.tzinfo is None:
        right = right.replace(tzinfo=timezone.utc)
    return left.astimezone(timezone.utc) == right.astimezone(timezone.utc)


def require_current_run(
    db: Session,
    run_id: int,
    *,
    consumer: str,
) -> tuple[models.PlanningRun, int]:
    truth = require_accepted_truth(
        db,
        consumer,
        required_capabilities=REQUIRED_MUTATION_CAPABILITIES,
    )
    run = db.get(models.PlanningRun, int(run_id))
    if run is None:
        raise MrpMutationLineageError(f"planning run {run_id} not found")
    if str(run.status or "").upper() != "FIXED_SNAPSHOT":
        raise MrpMutationLineageError(
            f"planning run {run_id} is not a FIXED_SNAPSHOT"
        )
    generation_id = int(truth.generation_id)
    if run.ledger_generation_id is None or int(run.ledger_generation_id) != generation_id:
        raise MrpMutationLineageError(
            f"planning run {run_id} is not bound to accepted Ledger generation {generation_id}"
        )
    if not _same_instant(run.ledger_cutoff, truth.cutoff):
        raise MrpMutationLineageError(
            f"planning run {run_id} cutoff does not match accepted Ledger cutoff"
        )
    if run.active_freeze_version is None:
        raise MrpMutationLineageError(f"planning run {run_id} has no active freeze")
    return run, generation_id


def require_selected_proposals(
    db: Session,
    rows: Sequence[object],
    *,
    run: models.PlanningRun,
    generation_id: int,
    consumer: str,
) -> None:
    del consumer  # retained for uniform, explicit call sites and future telemetry
    for row in rows:
        row_id = getattr(row, "purchase_id", getattr(row, "order_id", "?"))
        if int(getattr(row, "run_id")) != int(run.run_id):
            raise MrpMutationLineageError(
                f"proposal {row_id} belongs to another planning run"
            )
        row_generation = getattr(row, "ledger_generation_id", None)
        if row_generation is None or int(row_generation) != generation_id:
            raise MrpMutationLineageError(
                f"proposal {row_id} is null, mixed or stale Ledger lineage"
            )
        requirement_id = getattr(row, "source_mrp_requirement_id", None)
        if requirement_id is not None:
            requirement = db.get(models.MrpRequirement, int(requirement_id))
            if (
                requirement is None
                or int(requirement.run_id) != int(run.run_id)
                or requirement.freeze_version is None
                or int(requirement.freeze_version) != int(run.active_freeze_version)
            ):
                raise MrpMutationLineageError(
                    f"proposal {row_id} is outside the current active freeze"
                )


def require_selected_requirements(
    rows: Sequence[models.MrpRequirement],
    *,
    run: models.PlanningRun,
) -> None:
    for row in rows:
        if (
            int(row.run_id) != int(run.run_id)
            or row.freeze_version is None
            or int(row.freeze_version) != int(run.active_freeze_version)
        ):
            raise MrpMutationLineageError(
                f"requirement {row.id} is outside the current active freeze"
            )


def require_materialized_orders(
    db: Session,
    orders: Sequence[models.ProductionOrder],
    *,
    consumer: str,
) -> int:
    run_ids = {int(order.source_run_id) for order in orders if order.source_run_id is not None}
    if len(run_ids) != 1 or any(order.source_run_id is None for order in orders):
        raise MrpMutationLineageError("selected orders have null or mixed planning runs")
    truth = require_accepted_truth(
        db,
        consumer,
        required_capabilities=REQUIRED_MUTATION_CAPABILITIES,
    )
    generation_id = int(truth.generation_id)
    run = db.get(models.PlanningRun, run_ids.pop())
    if run is None or str(run.status or "").upper() != "FIXED_SNAPSHOT":
        raise MrpMutationLineageError("selected orders do not belong to a fixed MRP run")
    if run.active_freeze_version is None:
        raise MrpMutationLineageError(f"planning run {run.run_id} has no active freeze")
    for order in orders:
        products = list(order.products)
        if not products:
            raise MrpMutationLineageError(f"production order {order.order_id} has no products")
        for product in products:
            if product.source_planned_order_id is not None:
                if (
                    product.ledger_generation_id is None
                    or int(product.ledger_generation_id) != generation_id
                ):
                    raise MrpMutationLineageError(
                        f"production product {product.product_id} is null, mixed or stale Ledger lineage"
                    )
                proposal = db.get(models.PlannedOrder, int(product.source_planned_order_id))
                if proposal is None:
                    raise MrpMutationLineageError("source planned order is missing")
                require_selected_proposals(
                    db, [proposal], run=run, generation_id=generation_id, consumer=consumer
                )
            elif product.source_mrp_requirement_id is not None:
                requirement = db.get(
                    models.MrpRequirement, int(product.source_mrp_requirement_id)
                )
                if requirement is None:
                    raise MrpMutationLineageError("source MRP requirement is missing")
                require_selected_requirements([requirement], run=run)
                if (
                    product.ledger_generation_id is None
                    or int(product.ledger_generation_id) != generation_id
                ):
                    current_work = (
                        db.query(models.ReplenishmentWorkItem)
                        .join(
                            models.ReservationEntry,
                            models.ReservationEntry.id
                            == models.ReplenishmentWorkItem.reservation_id,
                        )
                        .filter(
                            models.ReplenishmentWorkItem.ledger_generation_id
                            == generation_id,
                            models.ReplenishmentWorkItem.run_id == int(run.run_id),
                            models.ReplenishmentWorkItem.requirement_id
                            == int(requirement.id),
                            models.ReplenishmentWorkItem.replenishment_method == "make",
                            models.ReservationEntry.ledger_generation_id
                            == generation_id,
                            models.ReservationEntry.lifecycle_status == "active",
                            models.ReservationEntry.realization_mode == "make",
                        )
                        .one_or_none()
                    )
                    if current_work is None:
                        raise MrpMutationLineageError(
                            f"production product {product.product_id} is null, mixed or stale Ledger lineage"
                        )
            else:
                raise MrpMutationLineageError(
                    f"production product {product.product_id} has no current MRP source"
                )
    return generation_id
