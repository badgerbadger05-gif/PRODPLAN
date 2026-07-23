"""Atomically publish a complete obligation-refresh snapshot batch.

This is deliberately the *last* lifecycle step.  Builders may create a new
Ledger generation and one fresh MRP candidate per source plan, but neither is
planning truth until this service switches all of them in one caller-owned
transaction.  A partially prepared batch is not publishable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from app import models
from app.services.mrp_freeze import MRP_LEDGER_LOCK_KEY


class ObligationRefreshPublishError(RuntimeError):
    """The requested refresh cannot be made visible safely."""


@dataclass(frozen=True)
class ObligationRefreshPublishResult:
    parent_generation_id: int
    target_generation_id: int
    parent_run_ids: tuple[int, ...]
    candidate_run_ids: tuple[int, ...]
    published: bool


_REQUIRED_BUILD_STAGES = (
    "physical_import",
    "reservation_materialize",
    "reservation_replay",
    "snapshot_build",
)


def _utc(value: datetime | None, field: str) -> datetime:
    if value is None:
        raise ObligationRefreshPublishError(f"{field} is required")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _lock(query):
    """Use row locks where supported; SQLite intentionally treats this as a no-op."""
    return query.with_for_update()


def _candidate_matches(parent: models.PlanningRun, candidate: models.PlanningRun, target_id: int) -> bool:
    return (
        str(candidate.status) == "BUILDING_SNAPSHOT"
        and int(candidate.ledger_generation_id or -1) == int(target_id)
        and int(candidate.prior_run_id or -1) == int(parent.run_id)
        and candidate.source_plan_id == parent.source_plan_id
        and candidate.period_from == parent.period_from
        and candidate.period_to == parent.period_to
        and candidate.horizon_days == parent.horizon_days
        and candidate.config_version_id == parent.config_version_id
        and candidate.config_snapshot == parent.config_snapshot
        and candidate.fixed_at is None
        and candidate.finished_at is None
        and candidate.pinned is False
    )


def _source_export_links_exist(db: Session, candidate_ids: list[int]) -> bool:
    """Candidates must not have crossed the external 1C boundary yet."""
    if not candidate_ids:
        return False
    if db.query(models.ProductionOrder.order_id).filter(
        models.ProductionOrder.source_run_id.in_(candidate_ids)
    ).first() is not None:
        return True
    purchase_ids = [
        int(row[0]) for row in db.query(models.PlannedPurchase.purchase_id).filter(
            models.PlannedPurchase.run_id.in_(candidate_ids)
        ).all()
    ]
    order_ids = [
        int(row[0]) for row in db.query(models.PlannedOrder.order_id).filter(
            models.PlannedOrder.run_id.in_(candidate_ids)
        ).all()
    ]
    if purchase_ids and db.query(models.PurchaseExportLineAllocation.id).filter(
        models.PurchaseExportLineAllocation.planned_purchase_id.in_(purchase_ids)
    ).first() is not None:
        return True
    filters = []
    if purchase_ids:
        filters.append(
            (models.SyncLink.source_doctype == "planned_purchase")
            & (models.SyncLink.source_id.in_(purchase_ids))
        )
    if order_ids:
        filters.append(
            (models.SyncLink.source_doctype == "planned_order")
            & (models.SyncLink.source_id.in_(order_ids))
        )
    return bool(
        filters
        and db.query(models.SyncLink.link_id).filter(
            filters[0] if len(filters) == 1 else or_(*filters)
        ).first()
    )


def _require_refresh_lineage(
    db: Session, parent_generation_id: int, target_generation_id: int
) -> tuple[models.PlanningTruthState, models.LedgerGeneration, models.LedgerGeneration]:
    pointer = _lock(db.query(models.PlanningTruthState)).filter_by(id=1).one_or_none()
    parent = _lock(db.query(models.LedgerGeneration)).filter_by(id=int(parent_generation_id)).one_or_none()
    target = _lock(db.query(models.LedgerGeneration)).filter_by(id=int(target_generation_id)).one_or_none()
    if pointer is None or parent is None or target is None:
        raise ObligationRefreshPublishError("planning truth pointer or generation is missing")
    if str(parent.status) != "accepted":
        raise ObligationRefreshPublishError("parent generation must be accepted")
    if str(target.status) != "building":
        raise ObligationRefreshPublishError("target generation must be BUILDING")
    if int(pointer.current_generation_id or -1) != int(parent.id):
        raise ObligationRefreshPublishError("current truth pointer is not the accepted parent")
    marks = dict(target.source_watermarks or {})
    if marks.get("generation_kind") != "obligation_refresh" or marks.get("parent_generation_id") != int(parent.id):
        raise ObligationRefreshPublishError("target is not an obligation refresh of parent")
    if target.physical_import_batch_id != parent.physical_import_batch_id:
        raise ObligationRefreshPublishError("target does not reuse parent physical batch")
    if _utc(target.cutoff, "target cutoff") != _utc(parent.cutoff, "parent cutoff"):
        raise ObligationRefreshPublishError("target cutoff differs from parent")
    return pointer, parent, target


def _require_sealed_build(
    db: Session,
    *,
    target: models.LedgerGeneration,
    candidate_ids: list[int],
    capabilities: dict[str, Any],
) -> None:
    """A caller cannot turn a merely BUILDING generation into truth by fiat."""
    if not capabilities or dict(target.capabilities or {}) != capabilities:
        raise ObligationRefreshPublishError(
            "target capabilities must be a non-empty pre-sealed snapshot"
        )
    rows = _lock(db.query(models.LedgerBuildBatch)).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(target.id),
        models.LedgerBuildBatch.stage.in_(_REQUIRED_BUILD_STAGES),
    ).all()
    for stage in _REQUIRED_BUILD_STAGES:
        stage_rows = [row for row in rows if row.stage == stage]
        if len(stage_rows) != 1 or str(stage_rows[0].status) != "completed":
            raise ObligationRefreshPublishError(
                f"target build stage {stage} is incomplete or partial"
            )
    snapshot_metrics = dict(
        next(row for row in rows if row.stage == "snapshot_build").metrics or {}
    )
    declared_ids = snapshot_metrics.get("candidate_run_ids")
    if (
        snapshot_metrics.get("future_supply_captured") is not True
        or not isinstance(declared_ids, list)
        or sorted(int(value) for value in declared_ids) != sorted(candidate_ids)
        or len(declared_ids) != len(set(int(value) for value in declared_ids))
    ):
        raise ObligationRefreshPublishError(
            "snapshot_build lacks a complete future-supply candidate manifest"
        )


def _exact_retry(
    db: Session, *, parent: models.LedgerGeneration, target: models.LedgerGeneration,
    pointer: models.PlanningTruthState, accepted_at: datetime, capabilities: dict[str, Any],
) -> ObligationRefreshPublishResult | None:
    if (
        str(parent.status) != "accepted"
        or
        str(target.status) != "accepted"
        or int(pointer.current_generation_id or -1) != int(target.id)
        or _utc(target.accepted_at, "target accepted_at") != accepted_at
        or dict(target.capabilities or {}) != capabilities
        or target.physical_import_batch_id != parent.physical_import_batch_id
        or _utc(target.cutoff, "target cutoff") != _utc(parent.cutoff, "parent cutoff")
        or dict(target.source_watermarks or {}).get("generation_kind") != "obligation_refresh"
        or dict(target.source_watermarks or {}).get("parent_generation_id") != int(parent.id)
    ):
        return None
    candidates = _lock(db.query(models.PlanningRun)).filter(
        models.PlanningRun.ledger_generation_id == int(target.id),
        models.PlanningRun.status == "FIXED_SNAPSHOT",
    ).all()
    if not candidates:
        return None
    by_parent: dict[int, models.PlanningRun] = {}
    parents: list[models.PlanningRun] = []
    for candidate in candidates:
        parent_run = _lock(db.query(models.PlanningRun)).filter_by(run_id=candidate.prior_run_id).one_or_none()
        if parent_run is None or str(parent_run.status) != "SUPERSEDED" or int(parent_run.ledger_generation_id or -1) != int(parent.id):
            return None
        if (
            candidate.source_plan_id != parent_run.source_plan_id
            or candidate.period_from != parent_run.period_from
            or candidate.period_to != parent_run.period_to
            or candidate.horizon_days != parent_run.horizon_days
            or candidate.config_version_id != parent_run.config_version_id
            or candidate.config_snapshot != parent_run.config_snapshot
            or candidate.pinned is not True
            or candidate.fixed_at is None
            or candidate.finished_at is None
        ):
            return None
        parent_id = int(parent_run.run_id)
        if parent_id in by_parent:
            return None
        by_parent[parent_id] = candidate
        parents.append(parent_run)
    if len(by_parent) != len(candidates):
        return None
    if db.query(models.PlanningRun.run_id).filter(
        models.PlanningRun.ledger_generation_id == int(parent.id),
        models.PlanningRun.status == "FIXED_SNAPSHOT",
    ).first() is not None:
        return None
    candidate_ids = [int(row.run_id) for row in candidates]
    for parent_run in parents:
        locked_rows = _lock(db.query(models.ProductionPlanLine)).filter(
            models.ProductionPlanLine.plan_id == int(parent_run.source_plan_id),
            models.ProductionPlanLine.locked_by_run_id.is_not(None),
        ).all()
        if any(int(row.locked_by_run_id) != int(by_parent[int(parent_run.run_id)].run_id) for row in locked_rows):
            return None
    return ObligationRefreshPublishResult(
        parent_generation_id=int(parent.id), target_generation_id=int(target.id),
        parent_run_ids=tuple(sorted(int(row.run_id) for row in parents)),
        candidate_run_ids=tuple(sorted(candidate_ids)), published=False,
    )


def publish_obligation_refresh_batch(
    db: Session,
    *,
    parent_generation_id: int,
    target_generation_id: int,
    accepted_at: datetime,
    capabilities: Mapping[str, Any],
) -> ObligationRefreshPublishResult:
    """Publish every active source plan together, using only ``flush``.

    The caller owns the surrounding transaction.  In particular this helper
    never commits or rolls back, so a later failed step restores pointer, runs,
    locks and generation as one unit.
    """
    accepted_at = _utc(accepted_at, "accepted_at")
    if not isinstance(capabilities, Mapping):
        raise TypeError("capabilities must be a mapping")
    capability_snapshot = dict(capabilities)
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": MRP_LEDGER_LOCK_KEY})

    # Read the locked terminal state first: _require_refresh_lineage intentionally
    # admits BUILDING only, while an exact completed retry is a no-op.
    retry_pointer = _lock(db.query(models.PlanningTruthState)).filter_by(id=1).one_or_none()
    retry_parent = _lock(db.query(models.LedgerGeneration)).filter_by(id=int(parent_generation_id)).one_or_none()
    retry_target = _lock(db.query(models.LedgerGeneration)).filter_by(id=int(target_generation_id)).one_or_none()
    if retry_pointer is None or retry_parent is None or retry_target is None:
        raise ObligationRefreshPublishError("planning truth pointer or generation is missing")
    if str(retry_target.status) == "accepted" or int(retry_pointer.current_generation_id or -1) == int(retry_target.id):
        exact = _exact_retry(
            db, parent=retry_parent, target=retry_target, pointer=retry_pointer,
            accepted_at=accepted_at, capabilities=capability_snapshot,
        )
        if exact is not None:
            return exact
        raise ObligationRefreshPublishError("mixed or partial obligation-refresh publish state")

    pointer, parent, target = _require_refresh_lineage(
        db, int(parent_generation_id), int(target_generation_id)
    )
    parents = _lock(db.query(models.PlanningRun)).filter(
        models.PlanningRun.ledger_generation_id == int(parent.id),
        models.PlanningRun.status == "FIXED_SNAPSHOT",
    ).order_by(models.PlanningRun.run_id).all()
    if not parents:
        raise ObligationRefreshPublishError("no active parent snapshots to publish")
    if any(row.source_plan_id is None for row in parents):
        raise ObligationRefreshPublishError("active parent snapshot lacks source plan lineage")

    candidates = _lock(db.query(models.PlanningRun)).filter(
        models.PlanningRun.ledger_generation_id == int(target.id),
        models.PlanningRun.status == "BUILDING_SNAPSHOT",
    ).all()
    by_parent = {int(row.prior_run_id): row for row in candidates if row.prior_run_id is not None}
    if len(by_parent) != len(candidates) or set(by_parent) != {int(row.run_id) for row in parents}:
        raise ObligationRefreshPublishError("complete candidate batch is required for every active parent snapshot")
    for parent_run in parents:
        if not _candidate_matches(parent_run, by_parent[int(parent_run.run_id)], int(target.id)):
            raise ObligationRefreshPublishError("candidate has conflicting parent/config/period lineage")

    candidate_ids = [int(by_parent[int(row.run_id)].run_id) for row in parents]
    _require_sealed_build(
        db, target=target, candidate_ids=candidate_ids,
        capabilities=capability_snapshot,
    )
    if _source_export_links_exist(db, candidate_ids):
        raise ObligationRefreshPublishError("candidate has external export links")

    # A source plan must not be half-transferred by an earlier/manual mutation.
    for parent_run in parents:
        rows = _lock(db.query(models.ProductionPlanLine)).filter(
            models.ProductionPlanLine.plan_id == int(parent_run.source_plan_id),
            models.ProductionPlanLine.locked_by_run_id.is_not(None),
        ).all()
        if any(int(row.locked_by_run_id) != int(parent_run.run_id) for row in rows):
            raise ObligationRefreshPublishError("source plan line lock is not held by its parent snapshot")
        for row in rows:
            row.locked_by_run_id = int(by_parent[int(parent_run.run_id)].run_id)

    target.status = "accepted"
    target.accepted_at = accepted_at
    target.capabilities = capability_snapshot
    pointer.current_generation_id = int(target.id)
    for parent_run in parents:
        parent_run.status = "SUPERSEDED"
        candidate = by_parent[int(parent_run.run_id)]
        candidate.status = "FIXED_SNAPSHOT"
        candidate.pinned = True
        candidate.fixed_at = accepted_at
        candidate.finished_at = accepted_at
    db.flush()
    return ObligationRefreshPublishResult(
        parent_generation_id=int(parent.id), target_generation_id=int(target.id),
        parent_run_ids=tuple(int(row.run_id) for row in parents),
        candidate_run_ids=tuple(candidate_ids), published=True,
    )
