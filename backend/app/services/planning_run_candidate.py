"""Create a new planning-run header for an obligation-refresh generation.

This is intentionally only the header hand-off.  It neither materializes MRP
rows nor freezes/publishes any result, so the caller can put all subsequent
work in one outer transaction.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app import models


class PlanningRunCandidateError(RuntimeError):
    """A candidate cannot be safely derived from the supplied parent run."""


def _as_utc(value: datetime | None, field: str) -> datetime:
    if value is None:
        raise PlanningRunCandidateError(f"{field} is required")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _require_parent_and_target(
    db: Session,
    *,
    parent_run_id: int,
    target_generation_id: int,
) -> tuple[models.PlanningRun, models.LedgerGeneration]:
    parent = db.get(models.PlanningRun, int(parent_run_id))
    if parent is None:
        raise PlanningRunCandidateError(f"PlanningRun {parent_run_id} not found")
    if str(parent.status) != "FIXED_SNAPSHOT":
        raise PlanningRunCandidateError("parent run must be FIXED_SNAPSHOT")
    if parent.source_plan_id is None:
        raise PlanningRunCandidateError("parent run must have source_plan_id")
    if parent.ledger_generation_id is None:
        raise PlanningRunCandidateError("parent run has no Ledger generation")

    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None or pointer.current_generation_id is None:
        raise PlanningRunCandidateError("accepted Ledger pointer is not set")
    if int(pointer.current_generation_id) != int(parent.ledger_generation_id):
        raise PlanningRunCandidateError(
            "parent run is not bound to the current accepted Ledger generation"
        )
    accepted = db.get(models.LedgerGeneration, int(parent.ledger_generation_id))
    if accepted is None or str(accepted.status) != "accepted":
        raise PlanningRunCandidateError("parent Ledger generation is not accepted")

    target = db.get(models.LedgerGeneration, int(target_generation_id))
    if target is None:
        raise PlanningRunCandidateError(
            f"target LedgerGeneration {target_generation_id} not found"
        )
    if str(target.status) != "building":
        raise PlanningRunCandidateError("target Ledger generation must be BUILDING")
    if (target.source_watermarks or {}).get("generation_kind") != "obligation_refresh":
        raise PlanningRunCandidateError("target generation is not an obligation_refresh")
    if (target.source_watermarks or {}).get("parent_generation_id") != int(accepted.id):
        raise PlanningRunCandidateError(
            "target generation does not descend from parent Ledger generation"
        )
    if _as_utc(target.cutoff, "target cutoff") != _as_utc(accepted.cutoff, "parent cutoff"):
        raise PlanningRunCandidateError("target generation cutoff differs from parent")
    return parent, target


def _matches_parent(candidate: models.PlanningRun, parent: models.PlanningRun) -> bool:
    """Check immutable header lineage, not lifecycle timestamps or actor."""
    return (
        str(candidate.status) == "BUILDING_SNAPSHOT"
        and candidate.prior_run_id == parent.run_id
        and candidate.source_plan_id == parent.source_plan_id
        and candidate.period_from == parent.period_from
        and candidate.period_to == parent.period_to
        and candidate.horizon_days == parent.horizon_days
        and candidate.config_version_id == parent.config_version_id
        and candidate.config_snapshot == parent.config_snapshot
        and candidate.finished_at is None
        and candidate.fixed_at is None
        and candidate.active_freeze_version is None
        and candidate.pinned is False
        and (candidate.warnings or {}) == {}
        and (candidate.kpi or {}) == {}
    )


def create_candidate_run(
    db: Session,
    parent_run_id: int,
    target_generation_id: int,
    started_by: str | None,
) -> models.PlanningRun:
    """Return the unique BUILDING_SNAPSHOT header for a target generation.

    ``flush`` is intentional: it exposes the id and uniqueness errors to the
    caller while preserving ownership of commit/rollback by the outer workflow.
    Candidate lifecycle values are fresh (actor/time, no ``fixed_at``): a
    fixed timestamp must describe this candidate's own completed snapshot, not
    the parent snapshot it was derived from.
    """
    parent, target = _require_parent_and_target(
        db,
        parent_run_id=parent_run_id,
        target_generation_id=target_generation_id,
    )

    existing = db.query(models.PlanningRun).filter(
        models.PlanningRun.ledger_generation_id == int(target.id),
        models.PlanningRun.source_plan_id == int(parent.source_plan_id),
        models.PlanningRun.status == "BUILDING_SNAPSHOT",
    ).one_or_none()
    if existing is not None:
        if not _matches_parent(existing, parent):
            raise PlanningRunCandidateError(
                "candidate identity already exists with conflicting lineage"
            )
        return existing

    candidate = models.PlanningRun(
        status="BUILDING_SNAPSHOT",
        prior_run_id=int(parent.run_id),
        ledger_generation_id=int(target.id),
        source_plan_id=int(parent.source_plan_id),
        period_from=parent.period_from,
        period_to=parent.period_to,
        horizon_days=parent.horizon_days,
        config_version_id=parent.config_version_id,
        config_snapshot=deepcopy(parent.config_snapshot or {}),
        started_by=started_by,
        started_at=datetime.now(timezone.utc),
        finished_at=None,
        fixed_at=None,
        warnings={},
        kpi={},
        active_freeze_version=None,
        pinned=False,
    )
    db.add(candidate)
    db.flush()
    return candidate
