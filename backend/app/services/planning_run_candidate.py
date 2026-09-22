"""Create a new planning-run header for an obligation-refresh generation.

This is intentionally only the header hand-off.  It neither materializes MRP
rows nor freezes/publishes any result, so the caller can put all subsequent
work in one outer transaction.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

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


def current_live_plan_run_ids(
    db: Session,
    current: models.LedgerGeneration,
) -> frozenset[int]:
    """The one answer to "which runs are live for this accepted generation".

    Canon §27 and ``.docs/planning-truth-contract.md`` give that question a
    single owner: the sealed ``parent_generation_id`` chain resolved in
    ``live_plan_scope.py``.  Deciding it anywhere else by comparing a run's or
    a reservation's ``ledger_generation_id`` with the accepted pointer is the
    defect the contract names explicitly - a fact-only fork never re-anchors
    an obligation, so such a reader goes silent after the first physical
    refresh.

    A broken lineage raises: an unreadable scope is unavailable, never an
    empty one.  ``DuplicateLivePlanRunError`` is the one failure a caller may
    catch, because the duplicate-snapshot repair has to work on exactly that
    state.
    """
    from .item_ledger.live_plan_scope import live_plan_run_ids

    return frozenset(int(value) for value in live_plan_run_ids(db, current))


def _resolve_parent_generation_id(
    db: Session,
    parent: models.PlanningRun,
    *,
    current_generation_id: int | None = None,
    live_run_ids: frozenset[int] | None = None,
) -> int | None:
    """Resolve the accepted generation whose live scope contains ``parent``.

    Returns the accepted generation id when the run is live for it, otherwise
    the run's own sealed anchor (it is an obligation of an older generation,
    not of this one), otherwise ``None``.

    Liveness is not decided here: it is read from the sealed lineage through
    :func:`current_live_plan_run_ids`.  This function used to answer it from
    ``PlanningRun.ledger_generation_id`` and ``ReservationEntry`` replay rows,
    which is a second formula for the same entity.  After the compact-owner
    cutover the two disagreed in production: the owners keep the generation of
    their last publication while the pointer advances with every physical
    refresh, so this gate reported "no current snapshot" for a plan whose
    refresh manifest then refused with "add plan already has current
    FIXED_SNAPSHOT" - a 400 on every MRP recalculation.

    ``live_run_ids`` lets a caller resolve many runs against one lineage walk.
    """
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

    if live_run_ids is None:
        live_run_ids = current_live_plan_run_ids(db, current)
    if run_id in live_run_ids:
        return current_generation_id

    # Anchored, but outside this generation's live scope: the run is the
    # obligation of the generation that sealed it.
    if parent.ledger_generation_id is not None:
        return int(parent.ledger_generation_id)
    # An unanchored FIXED_SNAPSHOT cannot be live in any sealed lineage.  It is
    # a pre-anchor row shape the contract does not allow, and
    # ``_current_parents`` rejects it by name before any refresh is built.
    return None


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
    # generation is a refresh, never another add.  "Already has" is the sealed
    # live scope, the same source the refresh manifest uses; matching
    # ``ledger_generation_id`` against the pointer made this gate and that one
    # disagree after every physical refresh.
    live_run_ids = current_live_plan_run_ids(db, accepted)
    fixed_runs = db.query(models.PlanningRun).filter(
        models.PlanningRun.source_plan_id == int(plan.id),
        models.PlanningRun.status == "FIXED_SNAPSHOT",
    ).all()
    if any(int(row.run_id) in live_run_ids for row in fixed_runs):
        raise PlanningRunCandidateError(
            "source production plan already has a FIXED_SNAPSHOT on current Ledger generation"
        )
    # An unanchored fixed run cannot be placed in any sealed lineage, so it can
    # neither be proved live nor proved dead.  That is unavailable, not
    # permission to add a second snapshot for the same plan.
    unanchored = [
        int(row.run_id) for row in fixed_runs if row.ledger_generation_id is None
    ]
    if unanchored:
        raise PlanningRunCandidateError(
            "source production plan already has a FIXED_SNAPSHOT without a "
            f"Ledger generation anchor: run {min(unanchored)}"
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
        and candidate.prior_run_id == plan.predecessor_run_id
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
        # A first MRP run for a successor production plan is still an ``add``
        # to the live set, but it retains immutable historical lineage to the
        # run whose remaining roots it inherited.
        prior_run_id=(
            int(plan.predecessor_run_id)
            if plan.predecessor_run_id is not None
            else None
        ),
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


def create_replacement_candidate_run(
    db: Session,
    parent_run_id: int,
    target_generation_id: int,
    started_by: str | None,
    *,
    horizon_days: int | None,
    config_version_id: int | None,
    config_snapshot: dict,
) -> models.PlanningRun:
    """Create the next MRP run for the same immutable production plan."""
    parent = db.get(models.PlanningRun, int(parent_run_id))
    target = db.get(models.LedgerGeneration, int(target_generation_id))
    if (
        parent is None
        or str(parent.status) != "FIXED_SNAPSHOT"
        or parent.source_plan_id is None
    ):
        raise PlanningRunCandidateError("replacement parent must be a live fixed MRP")
    plan = db.get(models.ProductionPlanHeader, int(parent.source_plan_id))
    if plan is None or str(plan.status) != "fixed":
        raise PlanningRunCandidateError("replacement source plan must remain fixed")
    if target is None or str(target.status) != "building" or target.cutoff is None:
        raise PlanningRunCandidateError("replacement target must be a dated BUILDING generation")

    existing = db.query(models.PlanningRun).filter(
        models.PlanningRun.ledger_generation_id == int(target.id),
        models.PlanningRun.source_plan_id == int(plan.id),
        models.PlanningRun.status == "BUILDING_SNAPSHOT",
    ).one_or_none()
    if existing is not None:
        if int(existing.prior_run_id or -1) != int(parent.run_id):
            raise PlanningRunCandidateError("replacement candidate lineage conflicts")
        return existing

    started_at = _as_utc(target.cutoff, "target cutoff") + timedelta(microseconds=1)
    candidate = models.PlanningRun(
        status="BUILDING_SNAPSHOT",
        prior_run_id=int(parent.run_id),
        ledger_generation_id=int(target.id),
        source_plan_id=int(plan.id),
        period_from=plan.period_from,
        period_to=plan.period_to,
        horizon_days=horizon_days,
        config_version_id=config_version_id,
        config_snapshot=deepcopy(config_snapshot),
        started_by=started_by,
        started_at=started_at,
        finished_at=None,
        fixed_at=None,
        warnings={},
        kpi={},
        active_freeze_version=None,
        pinned=False,
    )
    db.add(candidate)
    db.flush()

    lines = db.query(models.ProductionPlanLine).filter(
        models.ProductionPlanLine.plan_id == int(plan.id)
    ).order_by(models.ProductionPlanLine.id).all()
    persisted_root_count = 0
    for line in lines:
        if line.remaining_output_qty is None:
            raise PlanningRunCandidateError(
                f"plan line {int(line.id)} has no persisted execution remainder"
            )
        planned = line.qty or 0
        accepted = line.accepted_output_qty or 0
        remaining = line.remaining_output_qty
        if planned < 0 or accepted < 0 or remaining < 0 or planned != accepted + remaining:
            raise PlanningRunCandidateError(
                f"plan line {int(line.id)} violates output conservation"
            )
        if remaining <= 0:
            continue
        db.add(models.MrpRunRoot(
            run_id=int(candidate.run_id),
            plan_line_id=int(line.id),
            planned_qty=remaining,
            accepted_qty=0,
            remaining_qty=remaining,
        ))
        persisted_root_count += 1
    if persisted_root_count == 0:
        raise PlanningRunCandidateError(
            "replacement source plan has no persisted positive execution remainder"
        )
    db.flush()
    return candidate
