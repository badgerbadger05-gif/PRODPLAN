"""Bounded retention for generation-scoped execution compatibility owners.

The current execution rows are the stable read owner for queue/readiness/drum,
but a small compatibility surface still needs the exact accepted generation for
manual drum actions, work-item navigation and provenance validation.  This
module removes only retired generation copies after current publication.  An
active BUILDING generation is always retained and the current accepted
generation is never touched.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models


class ExecutionProjectionRetentionError(RuntimeError):
    """The current owner is not proven safe for bounded cleanup."""


# Children must be deleted before their RESTRICT parents.  The statements are
# intentionally set based: one statement per table, with no Python materialized
# row list and no broad UPDATE of the retained current generation.
_RETENTION_STATEMENTS: tuple[str, ...] = (
    """
    DELETE FROM drum_capacity_gap
     WHERE EXISTS (
               SELECT 1 FROM drum_schedule ds
                WHERE ds.id = drum_capacity_gap.drum_schedule_id
                  AND ds.ledger_generation_id <> :current_generation_id
           )
       AND NOT EXISTS (
               SELECT 1 FROM drum_schedule ds
               JOIN ledger_generation lg ON lg.id = ds.ledger_generation_id
                WHERE ds.id = drum_capacity_gap.drum_schedule_id
                  AND lg.status = 'building'
           )
       AND NOT EXISTS (
               SELECT 1 FROM assembly_queue_line q
               JOIN ledger_generation lg ON lg.id = q.ledger_generation_id
                WHERE q.id = drum_capacity_gap.assembly_queue_line_id
                  AND lg.status = 'building'
           )
    """,
    """
    DELETE FROM drum_slot
     WHERE EXISTS (
               SELECT 1 FROM drum_schedule ds
                WHERE ds.id = drum_slot.drum_schedule_id
                  AND ds.ledger_generation_id <> :current_generation_id
           )
       AND NOT EXISTS (
               SELECT 1 FROM drum_schedule ds
               JOIN ledger_generation lg ON lg.id = ds.ledger_generation_id
                WHERE ds.id = drum_slot.drum_schedule_id
                  AND lg.status = 'building'
           )
       AND NOT EXISTS (
               SELECT 1 FROM assembly_queue_line q
               JOIN ledger_generation lg ON lg.id = q.ledger_generation_id
                WHERE q.id = drum_slot.assembly_queue_line_id
                  AND lg.status = 'building'
           )
    """,
    """
    DELETE FROM assembly_readiness
     WHERE ledger_generation_id <> :current_generation_id
       AND NOT EXISTS (
               SELECT 1 FROM ledger_generation lg
                WHERE lg.id = assembly_readiness.ledger_generation_id
                  AND lg.status = 'building'
           )
    """,
    """
    DELETE FROM drum_schedule
     WHERE ledger_generation_id <> :current_generation_id
       AND NOT EXISTS (
               SELECT 1 FROM ledger_generation lg
                WHERE lg.id = drum_schedule.ledger_generation_id
                  AND lg.status = 'building'
           )
    """,
    """
    DELETE FROM assembly_queue_line
     WHERE ledger_generation_id <> :current_generation_id
       AND NOT EXISTS (
               SELECT 1 FROM ledger_generation lg
                WHERE lg.id = assembly_queue_line.ledger_generation_id
                  AND lg.status = 'building'
           )
    """,
    """
    DELETE FROM shelf_projection
     WHERE ledger_generation_id <> :current_generation_id
       AND NOT EXISTS (
               SELECT 1 FROM ledger_generation lg
                WHERE lg.id = shelf_projection.ledger_generation_id
                  AND lg.status = 'building'
           )
    """,
    """
    DELETE FROM assembly_output_allocation
     WHERE ledger_generation_id <> :current_generation_id
       AND NOT EXISTS (
               SELECT 1 FROM ledger_generation lg
                WHERE lg.id = assembly_output_allocation.ledger_generation_id
                  AND lg.status = 'building'
           )
    """,
    """
    DELETE FROM assembly_output_fact_decision
     WHERE ledger_generation_id <> :current_generation_id
       AND NOT EXISTS (
               SELECT 1 FROM ledger_generation lg
                WHERE lg.id = assembly_output_fact_decision.ledger_generation_id
                  AND lg.status = 'building'
           )
    """,
    """
    DELETE FROM stock_ledger_supplier_receipt_provenance
     WHERE ledger_generation_id <> :current_generation_id
       AND NOT EXISTS (
               SELECT 1 FROM ledger_generation lg
                WHERE lg.id = stock_ledger_supplier_receipt_provenance.ledger_generation_id
                  AND lg.status = 'building'
           )
    """,
    """
    DELETE FROM replenishment_work_item
     WHERE ledger_generation_id <> :current_generation_id
       AND NOT EXISTS (
               SELECT 1 FROM ledger_generation lg
                WHERE lg.id = replenishment_work_item.ledger_generation_id
                  AND lg.status = 'building'
           )
    """,
)


_REQUIRED_SCOPES = (
    ("assembly_queue", "assembly:all-live-plans"),
    ("assembly_readiness", "assembly:all-live-plans"),
    ("drum_schedule", "drum:all-live-plans"),
    ("drum_slot", "drum:all-live-plans"),
    ("drum_gap", "drum:all-live-plans"),
    ("drum_excluded", "drum:all-live-plans"),
    ("shelf_projection", "shelf:all-live-mrps"),
)


def _assert_current_execution_coverage(db: Session, generation_id: int) -> None:
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or str(generation.status or "") != "accepted":
        raise ExecutionProjectionRetentionError(
            "execution projection cleanup requires an accepted generation"
        )
    pointer = db.query(models.PlanningTruthState).filter(
        models.PlanningTruthState.id == 1,
    ).one_or_none()
    if pointer is None or int(pointer.current_generation_id or -1) != int(generation_id):
        raise ExecutionProjectionRetentionError(
            "execution projection cleanup requires the exact accepted truth pointer"
        )
    rows = db.execute(text(
        """
        SELECT entity_kind, scope_key
          FROM current_execution_scope
         WHERE source_generation_id = :generation_id
           AND result_ready = true
        """
    ), {"generation_id": int(generation_id)}).all()
    covered = {(str(row[0]), str(row[1])) for row in rows}
    missing = [scope for scope in _REQUIRED_SCOPES if scope not in covered]
    if missing:
        raise ExecutionProjectionRetentionError(
            "current execution coverage is incomplete: "
            + ", ".join(f"{kind}/{scope}" for kind, scope in missing)
        )

def _provenance_prune_is_safe(db: Session, generation_id: int) -> tuple[bool, str]:
    """May the other generations' supplier provenance be removed yet?

    The prune removes every other generation's evidence, which is the only
    copy of anything the pointer does not own.  That is a cleanup once the
    pointer owns its complete typed set; before that it is the step that
    destroys the last record of an accepted receipt, and it ran that way for
    months.

    Only this one statement waits.  Blocking the whole prune would stop the
    projection growth the retention exists to bound - the stand reached 44 GB
    that way - so every other table is still cleaned while the evidence is
    repaired.
    """
    from .physical_refresh_supplier_evidence import (
        lost_supplier_receipt_provenance_sle_ids,
    )
    from app.services.planning_pool_resolver import (
        PlanningPoolConfigurationError,
        resolve_planning_pool_by_warehouse,
    )

    try:
        mapping = resolve_planning_pool_by_warehouse(db)
    except PlanningPoolConfigurationError:
        mapping = None
    lost = lost_supplier_receipt_provenance_sle_ids(
        db,
        ledger_generation_id=int(generation_id),
        planning_pool_by_warehouse=mapping,
        limit=9,
    )
    if not lost:
        return True, ""
    return False, (
        f"generation {int(generation_id)} does not own supplier receipt "
        "provenance for its visible supplier facts "
        f"(first uncovered sle_ids={[int(value) for value in lost[:8]]})"
    )


def prune_retired_execution_projections(
    db: Session,
    current_generation_id: int,
) -> dict[str, int]:
    """Remove retired copies after the current publication boundary.

    This function is called in the caller-owned publication transaction.  Any
    failed statement aborts the transaction, so the previous current owner and
    all staging/legacy rows remain intact.  Only active BUILDING rows and the
    exact accepted current generation survive.
    """

    current_id = int(current_generation_id)
    _assert_current_execution_coverage(db, current_id)
    provenance_safe, provenance_reason = _provenance_prune_is_safe(db, current_id)
    params = {"current_generation_id": current_id}
    removed: dict[str, int] = {}
    for statement in _RETENTION_STATEMENTS:
        table = statement.split("DELETE FROM", 1)[1].split()[0]
        if table == "stock_ledger_supplier_receipt_provenance" and not provenance_safe:
            removed[table] = 0
            removed["stock_ledger_supplier_receipt_provenance_retained_reason"] = (
                provenance_reason
            )
            continue
        result = db.execute(text(statement), params)
        removed[table] = int(result.rowcount or 0)
    return removed


__all__ = [
    "ExecutionProjectionRetentionError",
    "prune_retired_execution_projections",
]
