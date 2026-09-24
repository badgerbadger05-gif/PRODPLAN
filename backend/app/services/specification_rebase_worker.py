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
from app.services.replenishment import (
    REPLENISHMENT_FLOW_PRODUCTION,
    classify_replenishment_flow,
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


#: A run whose rebase fails this many times in a row stops blocking the queue:
#: the worker moves on to the next affected run.  The counter is the run's own
#: (``SpecificationRebaseRunState``), so a run without any request is covered
#: and a request shared with another run does not skip that other run.
MAX_REBASE_ATTEMPTS = 3


def _live_runs(db: Session) -> list[models.PlanningRun]:
    return (
        db.query(models.PlanningRun)
        .filter(
            models.PlanningRun.status == "FIXED_SNAPSHOT",
            models.PlanningRun.active_freeze_version.isnot(None),
        )
        .order_by(
            models.PlanningRun.period_from,
            models.PlanningRun.period_to,
            models.PlanningRun.run_id,
        )
        .all()
    )


def _current_hash_by_ref(db: Session) -> dict[str, str]:
    return {
        str(ref).strip(): str(content_hash)
        for ref, content_hash in db.query(
            models.Specification.spec_ref1c, models.Specification.content_hash
        ).all()
        if str(ref or "").strip() and content_hash
    }


def _frozen_nodes(
    db: Session, run: models.PlanningRun
) -> list[tuple[int | None, str, str | None]]:
    """``(root, spec_ref, frozen version)`` of every node the freeze selected.

    The structural BOM nodes are the freeze's own record of the selected
    branch; the component edges add the child specification of each edge
    (``child_spec_version`` is part of the drift check).  Legacy freezes
    without node rows are covered by their component edges.
    """
    version = int(run.active_freeze_version)
    nodes: list[tuple[int | None, str, str | None]] = []
    for root, ref, spec_version in db.query(
        models.MrpFreezeBomNode.root_item_id,
        models.MrpFreezeBomNode.spec_ref,
        models.MrpFreezeBomNode.spec_version,
    ).filter(
        models.MrpFreezeBomNode.run_id == int(run.run_id),
        models.MrpFreezeBomNode.freeze_version == version,
    ):
        nodes.append((root, str(ref or "").strip(), spec_version))
    for root, ref, spec_version, child_ref, child_version in db.query(
        models.MrpFreezeComponent.root_item_id,
        models.MrpFreezeComponent.spec_ref,
        models.MrpFreezeComponent.spec_version,
        models.MrpFreezeComponent.child_spec_ref,
        models.MrpFreezeComponent.child_spec_version,
    ).filter(
        models.MrpFreezeComponent.run_id == int(run.run_id),
        models.MrpFreezeComponent.freeze_version == version,
    ):
        nodes.append((root, str(ref or "").strip(), spec_version))
        nodes.append((root, str(child_ref or "").strip(), child_version))
    return [node for node in nodes if node[1]]


def _remaining_production_roots(db: Session, run: models.PlanningRun) -> set[int]:
    """Remaining roots the freeze expands: production roots with a remainder.

    The remainder comes from the canonical plan-line source; only production
    roots are expanded by the freeze, so only they can need a specification.
    """
    if run.source_plan_id is None:
        return set()
    rows, _audit = _remaining_root_rows(
        db, plan_id=int(run.source_plan_id), successor_period_from=date.min,
    )
    item_ids = {int(row["item_id"]) for row in rows}
    if not item_ids:
        return set()
    return {
        int(item_id)
        for item_id, method in db.query(
            models.Item.item_id, models.Item.replenishment_method
        ).filter(models.Item.item_id.in_(sorted(item_ids)))
        if classify_replenishment_flow(method) == REPLENISHMENT_FLOW_PRODUCTION
    }


def _is_drift(version: str | None, ref: str, hash_by_ref: dict[str, str]) -> bool:
    current = hash_by_ref.get(ref)
    return current is not None and (version is None or str(version) != current)


def _rebase_refs(
    db: Session,
    run: models.PlanningRun,
    resolver: BomSpecificationResolver,
    hash_by_ref: dict[str, str],
) -> tuple[str, ...]:
    """Decision §50: why this run's remainder must be rebased, as spec refs.

    ``needed-now`` is the set of specifications the current expansion of the
    remaining roots selects (the resolver walk, the same one the freeze
    records); ``needed-frozen`` is the set the freeze selected for those
    roots.  The run rebases when the two differ - a main specification
    switched to another document, a node added or dropped - or when a
    specification both need drifted.  A node only an accepted root uses, or
    one the remainder no longer needs, is not a reason; a legacy node without
    a recorded version counts as drift only when the remainder needs it.

    When the remaining expansion cannot be resolved, every drifted frozen
    node is returned, so the rebase fails visibly instead of being skipped.
    """
    nodes = _frozen_nodes(db, run)
    try:
        roots = _remaining_production_roots(db, run)
        needed_now = {
            ref
            for spec_ids in resolver.spec_ids_by_root(roots).values()
            for ref in (resolver.spec_ref(spec_id) for spec_id in spec_ids)
            if ref
        } if roots else set()
    except (BomSpecificationResolutionError, ValueError):
        return tuple(sorted({
            ref for _root, ref, version in nodes if _is_drift(version, ref, hash_by_ref)
        }))
    in_remainder = [
        (ref, version)
        for root, ref, version in nodes
        if (root is not None and int(root) in roots)
        or (root is None and ref in needed_now)
    ]
    needed_frozen = {ref for ref, _version in in_remainder}
    changed = needed_now ^ needed_frozen if nodes else set()
    shared = needed_now & needed_frozen
    drift = {
        ref
        for ref, version in in_remainder
        if ref in shared and _is_drift(version, ref, hash_by_ref)
    }
    return tuple(sorted(changed | drift))


def _affected(
    db: Session, resolver: BomSpecificationResolver
) -> list[tuple[models.PlanningRun, tuple[str, ...]]]:
    """Live runs whose remainder must be rebased, with their reasons (§50)."""
    hash_by_ref = _current_hash_by_ref(db)
    result = []
    for run in _live_runs(db):
        refs = _rebase_refs(db, run, resolver, hash_by_ref)
        if refs:
            result.append((run, refs))
    return result


def _any_frozen_drift(db: Session) -> bool:
    """Whether any live freeze carries an outdated revision at all."""
    hash_by_ref = _current_hash_by_ref(db)
    return any(
        _is_drift(version, ref, hash_by_ref)
        for run in _live_runs(db)
        for _root, ref, version in _frozen_nodes(db, run)
    )


def _request_ref(db: Session, request: models.SpecificationRebaseQueue) -> str:
    spec = db.get(models.Specification, int(request.spec_id))
    return str(spec.spec_ref1c or "").strip() if spec is not None else ""


def _run_failures(db: Session, run_id: int) -> int:
    state = db.get(models.SpecificationRebaseRunState, int(run_id))
    return int(state.consecutive_failures or 0) if state is not None else 0


def _record_run_failure(db: Session, run_id: int, error: str) -> int:
    state = db.get(models.SpecificationRebaseRunState, int(run_id))
    if state is None:
        state = models.SpecificationRebaseRunState(run_id=int(run_id), consecutive_failures=0)
        db.add(state)
    state.consecutive_failures = int(state.consecutive_failures or 0) + 1
    state.last_error = str(error)[:4000]
    state.last_failed_at = datetime.now(timezone.utc)
    return int(state.consecutive_failures)


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
    affected = _affected(db, resolver)

    run = None
    refs: tuple[str, ...] = ()
    selected_requests: list[models.SpecificationRebaseQueue] = []
    skipped_run_ids: list[int] = []
    held_refs: set[str] = set()
    for candidate, candidate_refs in affected:
        candidate_requests = [
            request for request in requests if _request_ref(db, request) in candidate_refs
        ]
        # A run that failed MAX_REBASE_ATTEMPTS times in a row must not starve
        # the rest of the queue; its state keeps the reason until an operator
        # or a new revision changes something.  Its requests are not judged
        # by that: another run sharing them is still processed.
        if _run_failures(db, int(candidate.run_id)) >= MAX_REBASE_ATTEMPTS:
            skipped_run_ids.append(int(candidate.run_id))
            held_refs.update(candidate_refs)
            continue
        run, refs, selected_requests = candidate, candidate_refs, candidate_requests
        break

    if run is None:
        now = datetime.now(timezone.utc)
        # Drift that no remaining root needs (§50) closes with its own reason,
        # so "nothing to rebase" is distinguishable from "nothing drifted".
        outside = _any_frozen_drift(db)
        for request in requests:
            if _request_ref(db, request) in held_refs:
                # Still needed by a run that keeps failing: stays failed.
                request.status = "failed"
                request.result = {
                    "status": "failed_max_attempts",
                    "run_ids": skipped_run_ids,
                }
                continue
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
            "status": "blocked" if skipped_run_ids else "idle",
            "pending_specifications": len(requests),
            "affected_runs": len(affected),
            "failing_run_ids": skipped_run_ids,
            "dry_run": bool(dry_run),
        }

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
            failures = _record_run_failure(db, int(run.run_id), str(exc))
            for request_id in [int(row.id) for row in selected_requests]:
                request = db.get(models.SpecificationRebaseQueue, request_id)
                if request is not None:
                    request.status = "failed"
                    request.last_error = str(exc)[:4000]
                    if failures >= MAX_REBASE_ATTEMPTS:
                        request.result = {
                            "status": "failed_max_attempts",
                            "run_id": int(run.run_id),
                            "reason": str(exc)[:500],
                        }
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
    # requests remain pending while another live run still needs their spec.
    db.expire_all()
    state = db.get(models.SpecificationRebaseRunState, int(run.run_id))
    if state is not None:
        db.delete(state)
    resolver = BomSpecificationResolver(db)
    remaining = _affected(db, resolver)
    remaining_ids = sorted(int(candidate.run_id) for candidate, _refs in remaining)
    now = datetime.now(timezone.utc)
    for request_id in [int(row.id) for row in selected_requests]:
        request = db.get(models.SpecificationRebaseQueue, request_id)
        if request is None:
            continue
        ref = _request_ref(db, request)
        still_used = any(ref in candidate_refs for _candidate, candidate_refs in remaining)
        request.status = "pending" if still_used else "completed"
        request.completed_at = None if still_used else now
        request.result = {
            "status": "more_runs_pending" if still_used else "completed",
            "last_rebased_run_id": int(run.run_id),
            "remaining_run_ids": remaining_ids,
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
