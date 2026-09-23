"""Consume durable specification-change work one successor MRP at a time."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import models
from app.services.item_ledger.physical_refresh_candidacy import (
    has_live_physical_refresh_candidate,
)
from app.services.bom_specification_resolver import (
    BomSpecificationResolutionError,
    BomSpecificationResolver,
)
from app.services.specification_mrp_rebase import (
    _remaining_root_rows,
    rebase_fixed_plan_remaining_roots,
)

DRIFT_OUTSIDE_REMAINING_ROOTS = "drift_outside_remaining_roots"


def _current_requests(db: Session) -> list[models.SpecificationRebaseQueue]:
    stale_running_before = datetime.now(timezone.utc) - timedelta(hours=4)
    rows = (
        db.query(models.SpecificationRebaseQueue)
        .filter(
            or_(
                models.SpecificationRebaseQueue.status.in_(("pending", "failed")),
                (
                    (models.SpecificationRebaseQueue.status == "running")
                    & (
                        models.SpecificationRebaseQueue.started_at
                        < stale_running_before
                    )
                ),
            )
        )
        .order_by(
            models.SpecificationRebaseQueue.spec_id,
            models.SpecificationRebaseQueue.detected_at.desc(),
            models.SpecificationRebaseQueue.id.desc(),
        )
        .all()
    )
    current: list[models.SpecificationRebaseQueue] = []
    seen: set[int] = set()
    now = datetime.now(timezone.utc)
    for row in rows:
        spec_id = int(row.spec_id)
        spec = db.get(models.Specification, spec_id)
        if (
            spec is None
            or not spec.content_hash
            or str(row.new_content_hash) != str(spec.content_hash)
        ):
            row.status = "completed"
            row.completed_at = now
            row.result = {"status": "superseded_by_newer_revision"}
            continue
        if spec_id in seen:
            row.status = "completed"
            row.completed_at = now
            row.result = {"status": "coalesced"}
            continue
        seen.add(spec_id)
        current.append(row)
    return current


def _affected_runs(
    db: Session,
    requests: list[models.SpecificationRebaseQueue],
) -> list[models.PlanningRun]:
    hash_by_ref: dict[str, str] = {}
    for request in requests:
        spec = db.get(models.Specification, int(request.spec_id))
        ref = str(spec.spec_ref1c or "").strip() if spec is not None else ""
        if ref and spec.content_hash:
            hash_by_ref[ref] = str(spec.content_hash)
    if not hash_by_ref:
        return []

    stale_predicates = [
        (
            (models.MrpFreezeComponent.spec_ref == ref)
            & or_(
                models.MrpFreezeComponent.spec_version.is_(None),
                models.MrpFreezeComponent.spec_version != content_hash,
            )
        )
        for ref, content_hash in sorted(hash_by_ref.items())
    ]
    rows = (
        db.query(models.PlanningRun)
        .join(
            models.MrpFreezeComponent,
            models.MrpFreezeComponent.run_id == models.PlanningRun.run_id,
        )
        .filter(
            models.PlanningRun.status == "FIXED_SNAPSHOT",
            models.PlanningRun.active_freeze_version.isnot(None),
            models.MrpFreezeComponent.freeze_version
            == models.PlanningRun.active_freeze_version,
            or_(*stale_predicates),
        )
        .distinct()
        .order_by(
            models.PlanningRun.period_from,
            models.PlanningRun.period_to,
            models.PlanningRun.run_id,
        )
        .all()
    )
    return rows


def _remaining_root_item_ids(db: Session, run: models.PlanningRun) -> set[int]:
    """Roots with an unaccepted remainder - the canonical remaining source."""
    if run.source_plan_id is None:
        return set()
    rows, _audit = _remaining_root_rows(
        db, plan_id=int(run.source_plan_id), successor_period_from=date.min,
    )
    return {int(row["item_id"]) for row in rows}


def _refs_needed_by_remaining_roots(
    db: Session,
    run: models.PlanningRun,
    resolver: BomSpecificationResolver,
) -> set[str] | None:
    """Specification refs the current expansion of the remaining roots uses.

    ``None`` when the expansion cannot be resolved: the run then stays in
    scope, and the rebase fails visibly instead of the drift being skipped.
    """
    try:
        roots = _remaining_root_item_ids(db, run)
        if not roots:
            return set()
        spec_ids = {
            spec_id
            for values in resolver.spec_ids_by_root(roots).values()
            for spec_id in values
        }
    except (BomSpecificationResolutionError, ValueError):
        return None
    return {ref for ref in (resolver.spec_ref(spec_id) for spec_id in spec_ids) if ref}


def _live_drift_refs(
    db: Session,
    run: models.PlanningRun,
    drifted_refs: tuple[str, ...],
    resolver: BomSpecificationResolver,
) -> tuple[str, ...]:
    """Decision §50: the drifted refs the run's remainder really needs."""
    if not drifted_refs:
        return ()
    needed = _refs_needed_by_remaining_roots(db, run, resolver)
    if needed is None:
        return drifted_refs
    return tuple(ref for ref in drifted_refs if ref in needed)


def _all_affected_runs(
    db: Session, *, resolver: BomSpecificationResolver | None = None
) -> list[models.PlanningRun]:
    """Find live runs whose remainder needs a changed specification (§50).

    The durable queue is an event log and a scheduling hint, not the source of
    truth for specification drift.  A specification may return to an existing
    historical revision, whose revision id already has a completed queue row.
    Comparing the live freeze directly with the current specification keeps
    that transition observable and recoverable.

    A frozen matrix that differs from the current specification only in the
    part already produced, or in nodes the remaining roots no longer use, is
    not a reason to rebase: the successor would be equivalent.
    """
    resolver = resolver or BomSpecificationResolver(db)
    return [
        run
        for run in _drifted_runs(db)
        if _live_drift_refs(db, run, _all_refs_for_run(db, run), resolver)
    ]


def _drifted_runs(db: Session) -> list[models.PlanningRun]:
    """Live runs with any frozen component on a non-current revision."""
    return (
        db.query(models.PlanningRun)
        .join(
            models.MrpFreezeComponent,
            models.MrpFreezeComponent.run_id == models.PlanningRun.run_id,
        )
        .join(
            models.Specification,
            models.Specification.spec_ref1c
            == models.MrpFreezeComponent.spec_ref,
        )
        .filter(
            models.PlanningRun.status == "FIXED_SNAPSHOT",
            models.PlanningRun.active_freeze_version.isnot(None),
            models.MrpFreezeComponent.freeze_version
            == models.PlanningRun.active_freeze_version,
            models.Specification.content_hash.isnot(None),
            or_(
                models.MrpFreezeComponent.spec_version.is_(None),
                models.MrpFreezeComponent.spec_version
                != models.Specification.content_hash,
            ),
        )
        .distinct()
        .order_by(
            models.PlanningRun.period_from,
            models.PlanningRun.period_to,
            models.PlanningRun.run_id,
        )
        .all()
    )


def _refs_for_run(
    db: Session,
    run: models.PlanningRun,
    requests: list[models.SpecificationRebaseQueue],
) -> tuple[str, ...]:
    hash_by_ref: dict[str, str] = {}
    for request in requests:
        spec = db.get(models.Specification, int(request.spec_id))
        ref = str(spec.spec_ref1c or "").strip() if spec is not None else ""
        if ref and spec.content_hash:
            hash_by_ref[ref] = str(spec.content_hash)
    if not hash_by_ref or run.active_freeze_version is None:
        return ()
    stale_predicates = [
        (
            (models.MrpFreezeComponent.spec_ref == ref)
            & or_(
                models.MrpFreezeComponent.spec_version.is_(None),
                models.MrpFreezeComponent.spec_version != content_hash,
            )
        )
        for ref, content_hash in sorted(hash_by_ref.items())
    ]
    frozen = (
        db.query(models.MrpFreezeComponent.spec_ref)
        .filter(
            models.MrpFreezeComponent.run_id == int(run.run_id),
            models.MrpFreezeComponent.freeze_version
            == int(run.active_freeze_version),
            or_(*stale_predicates),
        )
        .distinct()
        .all()
    )
    return tuple(sorted(str(ref) for (ref,) in frozen))


def _all_refs_for_run(
    db: Session,
    run: models.PlanningRun,
) -> tuple[str, ...]:
    if run.active_freeze_version is None:
        return ()
    frozen = (
        db.query(models.MrpFreezeComponent.spec_ref)
        .join(
            models.Specification,
            models.Specification.spec_ref1c
            == models.MrpFreezeComponent.spec_ref,
        )
        .filter(
            models.MrpFreezeComponent.run_id == int(run.run_id),
            models.MrpFreezeComponent.freeze_version
            == int(run.active_freeze_version),
            models.Specification.content_hash.isnot(None),
            or_(
                models.MrpFreezeComponent.spec_version.is_(None),
                models.MrpFreezeComponent.spec_version
                != models.Specification.content_hash,
            ),
        )
        .distinct()
        .all()
    )
    return tuple(sorted(str(ref) for (ref,) in frozen))


def run_one_pending_specification_rebase(
    db: Session,
    *,
    dry_run: bool = False,
    started_by: str = "specification_rebase_worker",
) -> dict[str, Any]:
    """Process at most one affected run; repeated ticks drain the closure."""
    if not dry_run and has_live_physical_refresh_candidate(db):
        # A rebase retires the very runs a building physical-refresh candidate
        # is carrying forward from the same accepted generation.  Doing that
        # under a live build fails it on a run which is suddenly CLOSED, and the
        # failed build then holds the physical terminal.  The queue is durable:
        # standing aside costs one tick, the collision cost a day of stale
        # planning truth.
        return {
            "status": "deferred",
            "reason": "physical_refresh_building",
            "affected_runs": 0,
            "dry_run": False,
        }
    requests = _current_requests(db)
    resolver = BomSpecificationResolver(db)
    affected = _all_affected_runs(db, resolver=resolver)
    if not affected:
        now = datetime.now(timezone.utc)
        # Drift that no remaining root needs (§50) closes with its own reason,
        # so "nothing to rebase" is distinguishable from "nothing drifted".
        outside = bool(_drifted_runs(db))
        for request in requests:
            request.status = "completed"
            request.completed_at = now
            request.result = {
                "status": DRIFT_OUTSIDE_REMAINING_ROOTS if outside else "no_live_outdated_mrp"
            }
        if dry_run:
            db.rollback()
        else:
            db.commit()
        return {
            "status": "idle",
            "pending_specifications": len(requests),
            "affected_runs": 0,
            "dry_run": bool(dry_run),
        }

    run = affected[0]
    refs = _live_drift_refs(db, run, _all_refs_for_run(db, run), resolver)
    selected_requests = []
    for request in requests:
        spec = db.get(models.Specification, int(request.spec_id))
        if spec is not None and str(spec.spec_ref1c or "").strip() in refs:
            selected_requests.append(request)

    if not dry_run:
        now = datetime.now(timezone.utc)
        for request in selected_requests:
            request.status = "running"
            request.started_at = now
            request.attempt_count = int(request.attempt_count or 0) + 1
            request.last_error = None
        db.commit()

    try:
        result = rebase_fixed_plan_remaining_roots(
            db,
            int(run.run_id),
            changed_spec_refs=refs,
            started_by=started_by,
            dry_run=dry_run,
        )
    except Exception as exc:
        db.rollback()
        if not dry_run:
            for request_id in [int(row.id) for row in selected_requests]:
                request = db.get(models.SpecificationRebaseQueue, request_id)
                if request is not None:
                    request.status = "failed"
                    request.last_error = str(exc)[:4000]
            db.commit()
        raise

    if dry_run:
        return {
            "status": "previewed",
            "affected_runs": len(affected),
            "changed_spec_refs": list(refs),
            "rebase": result,
            "dry_run": True,
        }

    # The rebase commits its generation atomically. Re-scan current truth:
    # requests remain pending while another live run still has an old revision.
    db.expire_all()
    current = _current_requests(db)
    selected_current = [
        db.get(models.SpecificationRebaseQueue, int(row.id))
        for row in selected_requests
    ]
    selected_current = [row for row in selected_current if row is not None]
    resolver = BomSpecificationResolver(db)
    remaining = _all_affected_runs(db, resolver=resolver)
    remaining_live_ids = {int(row.run_id) for row in remaining}
    remaining_for_selected = [
        row for row in _affected_runs(db, selected_current)
        if int(row.run_id) in remaining_live_ids
    ]
    remaining_ids = {int(row.run_id) for row in remaining}
    now = datetime.now(timezone.utc)
    for request_id in [int(row.id) for row in selected_requests]:
        request = db.get(models.SpecificationRebaseQueue, request_id)
        if request is None:
            continue
        spec = db.get(models.Specification, int(request.spec_id))
        ref = str(spec.spec_ref1c or "").strip() if spec is not None else ""
        still_used = any(
            ref in _live_drift_refs(
                db, remaining_run, _refs_for_run(db, remaining_run, [request]), resolver
            )
            for remaining_run in remaining_for_selected
        )
        request.status = "pending" if still_used else "completed"
        request.completed_at = None if still_used else now
        request.result = {
            "status": "more_runs_pending" if still_used else "completed",
            "last_rebased_run_id": int(run.run_id),
            "remaining_run_ids": sorted(remaining_ids),
        }
    db.commit()
    return {
        "status": "rebased",
        "affected_runs_before": len(affected),
        "affected_runs_after": len(remaining),
        "changed_spec_refs": list(refs),
        "rebase": result,
        "dry_run": False,
    }
