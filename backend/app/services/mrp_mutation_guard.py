"""Fail-closed lineage checks for MRP materialization and 1C exports.

These checks live in the service layer because routers, workers and tests may
all call the mutation services directly.  Dry-runs intentionally use the same
gate as live writes: an unsafe preview is still an unsafe promise.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from app import models
from app.services.item_ledger.live_plan_scope import (
    RunAnchorError,
    sealed_generation_lineage_ids,
    sealed_run_anchor,
)
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


def _accepted_lineage(db: Session, generation_id: int) -> tuple[int, ...]:
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None:
        raise MrpMutationLineageError(
            f"accepted Ledger generation {int(generation_id)} not found"
        )
    try:
        return sealed_generation_lineage_ids(db, generation)
    except ValueError as exc:
        raise MrpMutationLineageError(str(exc)) from exc


def require_current_run(
    db: Session,
    run_id: int,
    *,
    consumer: str,
) -> tuple[models.PlanningRun, int]:
    """Require a live obligation of the accepted truth, not a re-anchored run.

    The run must be anchored anywhere in the accepted generation's sealed
    lineage and carry the cutoff of that anchor.  Demanding the accepted
    generation id here blocked every materialization and 1C export from the
    first physical refresh onwards, because a fact-only fork deliberately
    leaves obligations where they were frozen.
    """
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
    generation = db.get(models.LedgerGeneration, generation_id)
    if generation is None:
        raise MrpMutationLineageError(
            f"accepted Ledger generation {generation_id} not found"
        )
    try:
        sealed_run_anchor(db, run, generation)
    except (RunAnchorError, ValueError) as exc:
        raise MrpMutationLineageError(str(exc)) from exc
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
    """Require every selected proposal to belong to this run's frozen obligation.

    A proposal row stays anchored to the generation that froze it, so its
    lineage is checked against the accepted generation's sealed chain.  Run
    identity and the active freeze version below are what pin the row to the
    *current* obligation; the generation only proves it is not from a foreign
    branch of truth.
    """
    del consumer  # retained for uniform, explicit call sites and future telemetry
    allowed_generation_ids = set(_accepted_lineage(db, generation_id))
    for row in rows:
        row_id = getattr(row, "purchase_id", getattr(row, "order_id", "?"))
        if int(getattr(row, "run_id")) != int(run.run_id):
            raise MrpMutationLineageError(
                f"proposal {row_id} belongs to another planning run"
            )
        row_generation = getattr(row, "ledger_generation_id", None)
        if row_generation is None or int(row_generation) not in allowed_generation_ids:
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
    # A materialized product is stamped with the generation accepted when the
    # operator materialized it.  Fact-only forks advance the pointer past that
    # stamp without touching the obligation, so provenance is proved against
    # the accepted generation's sealed lineage.
    allowed_generation_ids = set(_accepted_lineage(db, generation_id))
    for order in orders:
        products = list(order.products)
        if not products:
            raise MrpMutationLineageError(f"production order {order.order_id} has no products")
        for product in products:
            if product.source_planned_order_id is not None:
                if (
                    product.ledger_generation_id is None
                    or int(product.ledger_generation_id) not in allowed_generation_ids
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
