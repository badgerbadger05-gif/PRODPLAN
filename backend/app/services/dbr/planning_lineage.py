"""Fail-closed PlanningRun lineage checks for DBR decisions."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ... import models
from ..mrp_mutation_guard import MrpMutationLineageError, require_current_run


def current_tuple(
    db: Session,
    source_run_id: int | None,
    *,
    consumer: str,
) -> tuple[int, int, int]:
    if source_run_id is None:
        raise MrpMutationLineageError(f"{consumer} requires explicit source_run_id")
    run, generation_id = require_current_run(
        db, int(source_run_id), consumer=consumer
    )
    return int(run.run_id), int(generation_id), int(run.active_freeze_version)


def require_exact(
    db: Session,
    *,
    source_run_id: int | None,
    ledger_generation_id: int | None,
    freeze_version: int | None,
    consumer: str,
) -> tuple[models.PlanningRun, int]:
    run_id, generation_id, current_freeze = current_tuple(
        db, source_run_id, consumer=consumer
    )
    if ledger_generation_id is None or int(ledger_generation_id) != generation_id:
        raise MrpMutationLineageError(
            f"{consumer} has null, mixed or stale Ledger generation"
        )
    if freeze_version is None or int(freeze_version) != current_freeze:
        raise MrpMutationLineageError(
            f"{consumer} is outside the current active freeze"
        )
    run = db.get(models.PlanningRun, run_id)
    assert run is not None
    return run, generation_id


def require_row(db: Session, row: object, *, consumer: str) -> tuple[models.PlanningRun, int]:
    return require_exact(
        db,
        source_run_id=getattr(row, "source_run_id", None),
        ledger_generation_id=getattr(row, "ledger_generation_id", None),
        freeze_version=getattr(row, "freeze_version", None),
        consumer=consumer,
    )
