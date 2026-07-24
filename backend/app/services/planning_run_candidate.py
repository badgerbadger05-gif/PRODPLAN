"""Create a new planning-run header for an obligation-refresh generation.

This is intentionally only the header hand-off.  It neither materializes MRP
rows nor freezes/publishes any result, so the caller can put all subsequent
work in one outer transaction.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from sqlalchemy import and_
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


def _resolve_parent_generation_id(
    db: Session,
    parent: models.PlanningRun,
    *,
    current_generation_id: int | None = None,
) -> int | None:
    """Resolve a parent generation from direct lineage or reservation replay.

    Legacy historical FIXED_SNAPSHOT rows occasionally arrive with
    ``ledger_generation_id`` cleared; in that case their lineage is inferred
    from reservation replay rows for the same run.
    """
    if parent.ledger_generation_id is not None:
        return int(parent.ledger_generation_id)

    run_id = int(parent.run_id)
    if current_generation_id is None:
        pointer = db.get(models.PlanningTruthState, 1)
        if (
            pointer is None
            or pointer.current_generation_id is None
        ):
            return None
        current_generation_id = int(pointer.current_generation_id)
    else:
        current_generation_id = int(current_generation_id)

    current = db.get(models.LedgerGeneration, current_generation_id)
    if current is None or str(current.status) != "accepted":
        return None

    def _is_matching_run(entry: models.ReservationEntry) -> bool:
        if entry.ledger_generation_id is None:
            return False
        if int(entry.ledger_generation_id) != current_generation_id:
            return False
        if entry.run_id == run_id:
            return True
        if entry.run_id is not None:
            return False
        if entry.requirement is not None and int(entry.requirement.run_id) == run_id:
            return True
        if entry.requirement_id is None:
            return False
        return db.query(models.MrpRequirement.run_id).filter(
            models.MrpRequirement.id == int(entry.requirement_id)
        ).scalar() == run_id

    if any(_is_matching_run(entry) for entry in list(db.new) if isinstance(entry, models.ReservationEntry)):
        return current_generation_id

    pending = db.query(models.ReservationEntry.run_id).filter(
        and_(
            models.ReservationEntry.ledger_generation_id == current_generation_id,
            models.ReservationEntry.run_id == run_id,
        )
    ).limit(1).scalar()
    if pending is not None:
        return current_generation_id

    persisted = (
        db.query(models.ReservationEntry.run_id)
        .join(
            models.MrpRequirement,
            models.ReservationEntry.requirement_id == models.MrpRequirement.id,
        )
        .filter(
            and_(
                models.ReservationEntry.ledger_generation_id == current_generation_id,
                models.MrpRequirement.run_id == run_id,
                models.ReservationEntry.run_id.is_(None),
            )
        )
        .limit(1)
        .scalar()
    )
    if persisted is not None:
        return current_generation_id
    return None


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
    parent_generation_id = _resolve_parent_generation_id(db, parent)
    if parent_generation_id is None:
        raise PlanningRunCandidateError("parent run has no Ledger generation")

    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None or pointer.current_generation_id is None:
        raise PlanningRunCandidateError("accepted Ledger pointer is not set")
    if int(pointer.current_generation_id) != parent_generation_id:
        raise PlanningRunCandidateError(
            "parent run is not bound to the current accepted Ledger generation"
        )
    accepted = db.get(models.LedgerGeneration, parent_generation_id)
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


def _require_added_plan_and_target(
    db: Session,
    *,
    source_plan_id: int,
    target_generation_id: int,
) -> tuple[models.ProductionPlanHeader, models.LedgerGeneration]:
    """Validate the first-run (``add``) lineage without inventing a parent.

    A production plan is an obligation only after it is fixed.  Unlike a
    refresh, an add has no previous ``PlanningRun``; its parent is solely the
    current accepted physical Ledger generation.
    """
    plan = db.get(models.ProductionPlanHeader, int(source_plan_id))
    if plan is None:
        raise PlanningRunCandidateError(f"ProductionPlanHeader {source_plan_id} not found")
    if str(plan.status) != "fixed":
        raise PlanningRunCandidateError("source production plan must be fixed")

    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None or pointer.current_generation_id is None:
        raise PlanningRunCandidateError("accepted Ledger pointer is not set")
    accepted = db.get(models.LedgerGeneration, int(pointer.current_generation_id))
    if accepted is None or str(accepted.status) != "accepted":
        raise PlanningRunCandidateError("current Ledger generation is not accepted")

    # A plan that already has a published snapshot on the current truth
    # generation is a refresh, never another add.  Historical/superseded runs
    # deliberately do not participate in this decision unless lineage proves
    # they belong to the same accepted generation.
    current_fixed = db.query(models.PlanningRun).filter(
        models.PlanningRun.ledger_generation_id == int(accepted.id),
        models.PlanningRun.source_plan_id == int(plan.id),
        models.PlanningRun.status == "FIXED_SNAPSHOT",
    ).all()
    legacy_fixed = db.query(models.PlanningRun).filter(
        models.PlanningRun.source_plan_id == int(plan.id),
        models.PlanningRun.status == "FIXED_SNAPSHOT",
        models.PlanningRun.ledger_generation_id.is_(None),
    ).all()
    if (
        current_fixed
        or any(_resolve_parent_generation_id(db, row) == int(accepted.id) for row in legacy_fixed)
    ):
        raise PlanningRunCandidateError(
            "source production plan already has a FIXED_SNAPSHOT on current Ledger generation"
        )

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
            "target generation does not descend from current Ledger generation"
        )
    if int(target.physical_import_batch_id or -1) != int(
        accepted.physical_import_batch_id or -1
    ):
        raise PlanningRunCandidateError(
            "target generation does not reuse current physical import batch"
        )
    if _as_utc(target.cutoff, "target cutoff") != _as_utc(accepted.cutoff, "accepted cutoff"):
        raise PlanningRunCandidateError("target generation cutoff differs from current Ledger generation")
    return plan, target


def _matches_added_plan(
    candidate: models.PlanningRun,
    plan: models.ProductionPlanHeader,
    *,
    horizon_days: int | None,
    config_version_id: int | None,
    config_snapshot: dict,
) -> bool:
    """Match only an exact retry of a first-plan candidate."""
    return (
        str(candidate.status) == "BUILDING_SNAPSHOT"
        and candidate.prior_run_id is None
        and candidate.source_plan_id == plan.id
        and candidate.period_from == plan.period_from
        and candidate.period_to == plan.period_to
        and candidate.horizon_days == horizon_days
        and candidate.config_version_id == config_version_id
        and candidate.config_snapshot == config_snapshot
        and candidate.finished_at is None
        and candidate.fixed_at is None
        and candidate.active_freeze_version is None
        and candidate.pinned is False
        and (candidate.warnings or {}) == {}
        and (candidate.kpi or {}) == {}
    )


def create_added_candidate_run(
    db: Session,
    source_plan_id: int,
    target_generation_id: int,
    started_by: str | None,
    *,
    horizon_days: int | None,
    config_version_id: int | None,
    config_snapshot: dict,
) -> models.PlanningRun:
    """Create or return the exact first-run candidate for a fixed plan.

    The supplied configuration is a sealed caller snapshot: it is copied into
    the run rather than read from mutable global configuration.  ``flush``
    exposes identity conflicts but transaction ownership remains with the
    caller; this function never commits or rolls back.
    """
    if not isinstance(config_snapshot, dict):
        raise PlanningRunCandidateError("config_snapshot must be a mapping")
    plan, target = _require_added_plan_and_target(
        db,
        source_plan_id=source_plan_id,
        target_generation_id=target_generation_id,
    )
    sealed_snapshot = deepcopy(config_snapshot)

    existing = db.query(models.PlanningRun).filter(
        models.PlanningRun.ledger_generation_id == int(target.id),
        models.PlanningRun.source_plan_id == int(plan.id),
        models.PlanningRun.status == "BUILDING_SNAPSHOT",
    ).one_or_none()
    if existing is not None:
        if not _matches_added_plan(
            existing,
            plan,
            horizon_days=horizon_days,
            config_version_id=config_version_id,
            config_snapshot=sealed_snapshot,
        ):
            raise PlanningRunCandidateError(
                "candidate identity already exists with conflicting add lineage"
            )
        return existing

    candidate = models.PlanningRun(
        status="BUILDING_SNAPSHOT",
        prior_run_id=None,
        ledger_generation_id=int(target.id),
        source_plan_id=int(plan.id),
        period_from=plan.period_from,
        period_to=plan.period_to,
        horizon_days=horizon_days,
        config_version_id=config_version_id,
        config_snapshot=sealed_snapshot,
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
