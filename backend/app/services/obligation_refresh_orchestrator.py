"""One caller-owned build-and-publish workflow for an obligation refresh.

This is intentionally the only place which is allowed to join the otherwise
small, independently testable refresh steps.  In particular it never commits
or rolls back: a web job can place it inside its own transaction and either
make the complete new truth visible or undo every candidate row and lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models
from app.services.item_ledger.candidate_future_supply import capture_candidate_future_supply
from app.services.planning_pool_resolver import (
    effective_planning_pool_by_warehouse,
)
from app.services.item_ledger.candidate_realization_replay import replay_candidate_realizations
from app.services.item_ledger.generation_lifecycle import (
    GenerationValidationError,
    validate_obligation_refresh_build,
)
from app.services.item_ledger.future_supply_capture import _as_utc
from app.services.item_ledger.obligation_generation import (
    carry_forward_retained_reservations,
    fork_obligation_generation,
)
from app.services.item_ledger.supplier_receipt_allocation import (
    rebuild_supplier_receipt_coverage_from_persisted_provenance,
)
from app.services.purchase_control_snapshot import build_candidate_snapshot as build_purchase_journal_candidate
from app.services.production_control_journal_snapshot import (
    build_candidate_snapshot as build_production_journal_candidate,
)
from app.services.production_material_custody_projection import (
    build_material_custody_projection,
)
from app.services.mrp_freeze import MRP_LEDGER_LOCK_KEY, freeze_candidate_snapshots
from app.services.mrp_result_snapshot import (
    build_mrp_result_candidate_snapshot,
    build_mrp_result_snapshot,
)
from app.services.obligation_refresh_manifest import (
    MANIFEST_HASH_KEY,
    MANIFEST_KEY,
    create_obligation_refresh_manifest,
)
from app.services.obligation_refresh_publish import publish_obligation_refresh_batch
from app.services.item_ledger.assembly_queue_snapshot import (
    build_assembly_queue_snapshot,
)
from app.services.item_ledger.assembly_output_persistence import (
    materialize_assembly_output_allocations,
)
from app.services.item_ledger.drum_schedule_persistence import (
    materialize_drum_schedule,
)
from app.services.item_ledger.replenishment_work_item_builder import (
    materialize_replenishment_work_items,
)
from app.services.item_ledger.reservation_consumption_persistence import (
    ALGORITHM_VERSION as RESERVATION_CONSUMPTION_ALGORITHM_VERSION,
    materialize_reservation_consumption_allocations,
)
from app.services.item_ledger.future_supply_capture import (
    FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION,
)
from app.services.item_ledger.shelf_projection_persistence import (
    materialize_shelf_projections,
)


_VERSION = "obligation-refresh-orchestrator/2"
_CORE_CAPABILITIES = {
    "physical_ledger": True,
    "reservation_replay": True,
    "replenishment_work_item": True,
    "execution_allocations": True,
    "reservation_consumption_allocation": True,
    "supplier_receipt_coverage": True,
    "planning_snapshots": True,
    "assembly_output_allocation": True,
    "assembly_queue": True,
    "assembly_readiness": True,
    "drum_schedule": True,
    "shelf_projection": True,
    "purchase_control_journal": True,
    "production_control_journal": True,
    "future_supply": True,
}


class ObligationRefreshOrchestratorError(RuntimeError):
    """The complete refresh cannot be safely constructed or retried."""


@dataclass(frozen=True)
class ObligationRefreshOrchestrationResult:
    parent_generation_id: int
    target_generation_id: int
    candidate_run_ids: tuple[int, ...]
    published: bool


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _json_value(value: Any) -> Any:
    """Metrics are database JSON, while stage adapters may return Decimal."""
    return json.loads(_canonical(value))


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _batch_key(generation_key: str, stage: str) -> str:
    value = f"obligation-refresh:{generation_key}:{stage}"
    if len(value) > 128:
        raise ObligationRefreshOrchestratorError("generation_key is too long")
    return value


def _single_stage(
    db: Session, target_id: int, stage: str, generation_key: str
) -> models.LedgerBuildBatch:
    key = _batch_key(generation_key, stage)
    expected_algorithm_version = (
        RESERVATION_CONSUMPTION_ALGORITHM_VERSION
        if stage == "execution_allocation"
        else FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION
        if stage == "future_supply_capture"
        else _VERSION
    )
    rows = db.query(models.LedgerBuildBatch).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(target_id),
        models.LedgerBuildBatch.stage == stage,
    ).all()
    if len(rows) > 1:
        raise ObligationRefreshOrchestratorError(f"target has duplicate {stage} batches")
    if rows:
        row = rows[0]
        if str(row.batch_key) != key:
            raise ObligationRefreshOrchestratorError(f"target {stage} batch conflicts")
        if str(row.algorithm_version) != expected_algorithm_version:
            row.algorithm_version = expected_algorithm_version
        return row
    row = models.LedgerBuildBatch(
        ledger_generation_id=int(target_id), stage=stage, batch_key=key,
        status="building", algorithm_version=expected_algorithm_version, metrics={},
    )
    db.add(row)
    db.flush()
    return row


def _complete(batch: models.LedgerBuildBatch, metrics: Mapping[str, Any]) -> None:
    if batch.stage == "future_supply_capture":
        generation = getattr(batch, "ledger_generation", None)
        if generation is not None and generation.cutoff is not None:
            metrics = {
                **dict(metrics),
                "generation_id": int(generation.id),
                "cutoff": _as_utc(generation.cutoff).isoformat(),
                "algorithm_version": str(batch.algorithm_version),
            }
    if str(batch.status) == "completed":
        if dict(batch.metrics or {}) != dict(metrics):
            raise ObligationRefreshOrchestratorError(
                f"completed {batch.stage} batch has conflicting metrics"
            )
        return
    if str(batch.status) != "building":
        raise ObligationRefreshOrchestratorError(f"{batch.stage} batch is not building")
    batch.status = "completed"
    batch.metrics = dict(metrics)
    batch.completed_at = datetime.now(timezone.utc)


def _manifest_request_matches(
    target: models.LedgerGeneration,
    *, add_plan_ids: Iterable[int], retire_plan_ids: Iterable[int],
    replace_plan_ids: Iterable[int],
    horizon_days: int | None,
    config_version_id: int | None, config_snapshot: Mapping[str, Any],
    planning_pool_by_warehouse: Mapping[str, str],
) -> None:
    marks = dict(target.source_watermarks or {})
    manifest = marks.get(MANIFEST_KEY)
    content_hash = marks.get(MANIFEST_HASH_KEY)
    if not isinstance(manifest, dict) or not isinstance(content_hash, str):
        raise ObligationRefreshOrchestratorError("published refresh lacks sealed manifest")
    if sha256(_canonical(manifest).encode()).hexdigest() != content_hash:
        raise ObligationRefreshOrchestratorError("published refresh manifest hash conflicts")
    try:
        request = manifest["add_request"]
        expected = {
            "plan_ids": sorted(int(v) for v in add_plan_ids),
            "retire_plan_ids": sorted(int(v) for v in retire_plan_ids),
            "replace_plan_ids": sorted(int(v) for v in replace_plan_ids),
            "horizon_days": horizon_days,
            "config_version_id": config_version_id,
            "config_snapshot": dict(config_snapshot),
            "planning_pool_by_warehouse": {
                str(key).strip(): str(value).strip()
                for key, value in sorted(planning_pool_by_warehouse.items())
            },
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ObligationRefreshOrchestratorError("published refresh manifest request is malformed") from exc
    if request != expected:
        raise ObligationRefreshOrchestratorError("conflicting retry of published refresh request")


def _publish_execution_snapshots(db: Session, generation_id: int) -> None:
    """Journals must never go dark after a refresh: every published generation
    carries its own period-plan execution snapshots, same as genesis accept.
    The builder is idempotent (existing identical snapshot is returned as-is),
    so the exact-retry path is safe to route through here too."""
    from app.services.period_plan_service import (
        build_period_plan_execution_snapshots_for_generation,
    )

    try:
        build_period_plan_execution_snapshots_for_generation(db, int(generation_id))
    except (TypeError, ValueError) as exc:
        raise ObligationRefreshOrchestratorError(
            f"period-plan execution snapshot build failed: {exc}"
        ) from exc


def _publish_retained_mrp_snapshots(
    db: Session,
    generation_id: int,
    retained_run_ids: Iterable[int],
    *,
    allow_stale_truth: bool = False,
) -> None:
    for run_id in sorted({int(value) for value in retained_run_ids}):
        build_mrp_result_snapshot(
            db, run_id, allow_stale_truth=bool(allow_stale_truth)
        )


def _retry_published(
    db: Session, target: models.LedgerGeneration, *, parent_generation_id: int,
    add_plan_ids: Iterable[int], retire_plan_ids: Iterable[int],
    replace_plan_ids: Iterable[int],
    horizon_days: int | None,
    config_version_id: int | None, config_snapshot: Mapping[str, Any],
    planning_pool_by_warehouse: Mapping[str, str],
    allow_stale_parent: bool = False,
) -> ObligationRefreshOrchestrationResult:
    _manifest_request_matches(
        target, add_plan_ids=add_plan_ids, retire_plan_ids=retire_plan_ids,
        replace_plan_ids=replace_plan_ids,
        horizon_days=horizon_days,
                              config_version_id=config_version_id, config_snapshot=config_snapshot,
                              planning_pool_by_warehouse=planning_pool_by_warehouse)
    marks = dict(target.source_watermarks or {})
    if int(marks.get("parent_generation_id") or -1) != int(parent_generation_id):
        raise ObligationRefreshOrchestratorError("published generation belongs to another parent")
    if target.accepted_at is None:
        raise ObligationRefreshOrchestratorError("published generation lacks accepted_at")
    result = publish_obligation_refresh_batch(
        db, parent_generation_id=int(parent_generation_id), target_generation_id=int(target.id),
        accepted_at=target.accepted_at, capabilities=dict(target.capabilities or {}),
    )
    _publish_execution_snapshots(db, int(target.id))
    manifest = marks.get(MANIFEST_KEY)
    retained_ids = (
        [
            int(entry["parent_run_id"])
            for entry in manifest.get("entries", [])
            if isinstance(entry, dict) and entry.get("action") == "retain"
        ]
        if isinstance(manifest, dict)
        else []
    )
    _publish_retained_mrp_snapshots(
        db,
        int(target.id),
        retained_ids,
        allow_stale_truth=bool(allow_stale_parent),
    )
    return ObligationRefreshOrchestrationResult(
        parent_generation_id=int(parent_generation_id), target_generation_id=int(target.id),
        candidate_run_ids=tuple(result.candidate_run_ids), published=result.published,
    )


def run_obligation_refresh(
    db: Session,
    *,
    parent_generation_id: int,
    generation_key: str,
    add_plan_ids: Iterable[int] = (),
    retire_plan_ids: Iterable[int] = (),
    replace_plan_ids: Iterable[int] = (),
    started_by: str | None = None,
    horizon_days: int | None = None,
    config_version_id: int | None = None,
    config_snapshot: Mapping[str, Any] | None = None,
    planning_pool_by_warehouse: Mapping[str, str] | None = None,
    explicit_make_transfer_recorders: set[str] | None = None,
    accepted_at: datetime | None = None,
    allow_stale_parent: bool = False,
) -> ObligationRefreshOrchestrationResult:
    """Build and atomically publish every refresh/add candidate.

    The caller must commit this transaction.  PostgreSQL serialisation is an
    xact advisory lock; SQLite retains deterministic semantics for tests.
    """
    key = str(generation_key or "").strip()
    if not key:
        raise ValueError("generation_key is required")
    pool_mapping = effective_planning_pool_by_warehouse(
        db,
        planning_pool_by_warehouse,
    )
    config = dict(config_snapshot or {})
    add_ids = tuple(sorted(int(v) for v in add_plan_ids))
    retire_ids = tuple(sorted(int(v) for v in retire_plan_ids))
    replace_ids = tuple(sorted(int(v) for v in replace_plan_ids))
    incremental_rebase = bool(replace_ids) and not add_ids and not retire_ids
    if len(add_ids) != len(set(add_ids)) or any(v <= 0 for v in add_ids):
        raise ValueError("add_plan_ids must be unique positive ids")
    if len(retire_ids) != len(set(retire_ids)) or any(v <= 0 for v in retire_ids):
        raise ValueError("retire_plan_ids must be unique positive ids")
    if len(replace_ids) != len(set(replace_ids)) or any(v <= 0 for v in replace_ids):
        raise ValueError("replace_plan_ids must be unique positive ids")
    if (
        set(add_ids).intersection(retire_ids)
        or set(add_ids).intersection(replace_ids)
        or set(retire_ids).intersection(replace_ids)
    ):
        raise ValueError("add, retire and replace plan sets must be disjoint")
    if len(add_ids) > 1:
        # The freeze anchors the shared stock baseline at the earliest
        # period_from of the batch: mixing periods would net a later plan
        # against an earlier boundary (inflated demand). Fail closed.
        period_froms = {
            row.period_from
            for row in db.query(models.ProductionPlanHeader)
            .filter(models.ProductionPlanHeader.id.in_(add_ids))
            .all()
        }
        if len(period_froms) > 1:
            raise ObligationRefreshOrchestratorError(
                "add plans with different period_from must be refreshed separately"
            )
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": MRP_LEDGER_LOCK_KEY})

    existing = db.query(models.LedgerGeneration).filter_by(generation_key=key).one_or_none()
    if existing is not None and str(existing.status) == "accepted":
        # A service-level retry normally resolves ``parent_generation_id`` from
        # today's pointer, which is now this already-published target.  The
        # publisher's exact-retry audit, however, needs the immutable historical
        # parent recorded in the sealed generation manifest.  Accept either
        # spelling and never trust the caller to reconstruct that lineage.
        pointer = db.get(models.PlanningTruthState, 1)
        marks = dict(existing.source_watermarks or {})
        try:
            original_parent_id = int(marks["parent_generation_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ObligationRefreshOrchestratorError(
                "published refresh lacks parent lineage"
            ) from exc
        original_parent = db.get(models.LedgerGeneration, original_parent_id)
        if (
            pointer is None
            or original_parent is None
            or str(original_parent.status) != "accepted"
            or int(pointer.current_generation_id or -1) != int(existing.id)
            or int(parent_generation_id) not in {int(existing.id), original_parent_id}
        ):
            raise ObligationRefreshOrchestratorError(
                "published retry requires target to be current accepted planning truth"
            )
        return _retry_published(
            db, existing, parent_generation_id=original_parent_id,
            add_plan_ids=add_ids, retire_plan_ids=retire_ids,
            replace_plan_ids=replace_ids,
            horizon_days=horizon_days, config_version_id=config_version_id, config_snapshot=config,
            planning_pool_by_warehouse=pool_mapping,
            allow_stale_parent=bool(allow_stale_parent),
        )
    if existing is not None and str(existing.status) != "building":
        raise ObligationRefreshOrchestratorError("generation_key exists in non-retryable state")
    # A build (including a resume of a BUILDING key) must descend from the
    # exact current pointer; no stale worker may mutate an unpublished target.
    pointer = db.get(models.PlanningTruthState, 1)
    parent = db.get(models.LedgerGeneration, int(parent_generation_id))
    if (
        pointer is None or parent is None or str(parent.status) != "accepted"
        or int(pointer.current_generation_id or -1) != int(parent.id)
    ):
        raise ObligationRefreshOrchestratorError(
            "parent generation must be the current accepted planning truth"
        )

    fork = fork_obligation_generation(db, int(parent_generation_id), key)
    target_id = int(fork.ledger_generation_id)
    manifest = create_obligation_refresh_manifest(
        db, int(parent_generation_id), target_id, add_ids,
        retire_plan_ids=retire_ids, replace_plan_ids=replace_ids,
        started_by=started_by,
        horizon_days=horizon_days, config_version_id=config_version_id, config_snapshot=config,
        planning_pool_by_warehouse=pool_mapping,
    )
    candidate_ids = tuple(sorted(
        int(entry["candidate_run_id"])
        for entry in manifest.entries
        if entry.get("candidate_run_id") is not None
    ))
    retained_run_ids = tuple(sorted(
        int(entry["parent_run_id"])
        for entry in manifest.entries
        if entry.get("action") == "retain"
    ))
    retained_reservation_count = carry_forward_retained_reservations(
        db,
        parent_generation_id=int(parent_generation_id),
        target_generation_id=target_id,
        retained_run_ids=retained_run_ids,
        preserve_realization=incremental_rebase,
    )

    reservation_batch = _single_stage(db, target_id, "reservation_materialize", key)
    execution_batch = _single_stage(db, target_id, "execution_allocation", key)
    replenishment_batch = _single_stage(db, target_id, "replenishment_work_item", key)
    future_supply_capture_batch = _single_stage(db, target_id, "future_supply_capture", key)
    snapshot_batch = _single_stage(db, target_id, "snapshot_build", key)
    capture = capture_candidate_future_supply(
        db,
        int(parent_generation_id),
        target_id,
        int(future_supply_capture_batch.id),
        planning_pool_by_warehouse=pool_mapping,
        explicit_make_transfer_recorders=explicit_make_transfer_recorders,
    )
    future_supply_capture = _json_value(capture)
    _complete(
        future_supply_capture_batch,
        {
            "future_supply_capture": future_supply_capture,
            **future_supply_capture,
        },
    )
    reservation_count = db.query(models.ReservationEntry.id).filter(
        models.ReservationEntry.ledger_generation_id == target_id,
        models.ReservationEntry.run_id.in_(candidate_ids),
    ).count()
    reservation_consumption = materialize_reservation_consumption_allocations(
        db,
        target_id,
        int(execution_batch.id),
    )
    _complete(execution_batch, reservation_consumption)
    freeze = (
        freeze_candidate_snapshots(
            db, parent_generation_id=int(parent_generation_id), target_generation_id=target_id,
            candidate_run_ids=candidate_ids,
            allow_stale_parent=bool(allow_stale_parent),
        )
        if candidate_ids
        else {
            "candidate_run_ids": [],
            "retained_run_ids": list(retained_run_ids),
            "frozen": 0,
        }
    )
    reservation_metrics = {
        "candidate_run_ids": list(candidate_ids),
        "retained_run_ids": list(retained_run_ids),
        "retained_reservation_entries": int(retained_reservation_count),
        "reservation_entries": int(reservation_count),
        "freeze_summary": _json_value(freeze),
        "input_checksum": sha256(_canonical({"candidate_run_ids": candidate_ids, "freeze": freeze}).encode()).hexdigest(),
    }
    _complete(reservation_batch, reservation_metrics)
    # Replacement reservations are new live obligations and must see the same
    # accepted physical prefix as every other candidate before publication.
    # Skipping this replay temporarily resurrected already fulfilled purchase
    # needs until the next physical refresh rebuilt the assignments.
    replay = replay_candidate_realizations(db, target_id)
    supplier = rebuild_supplier_receipt_coverage_from_persisted_provenance(
        db,
        ledger_generation_id=target_id,
        cycle_id=f"historical-supplier:g{target_id}:obligation-refresh",
    )
    supplier_summary = {
        "provenance_count": supplier.provenance_count,
        "exact_fact_count": supplier.exact_fact_count,
        "allocation_count": supplier.allocation_count,
        "surplus_qty": supplier.surplus_qty,
    }
    # Work items are a persisted projection of the reservation fold.  Both
    # make and supplier realizations must therefore be applied first; building
    # this projection earlier freezes stale ``fulfilled_qty``/``remaining_qty``
    # and can offer the same replenishment for ordering again.
    replenishment_summary = materialize_replenishment_work_items(
        db,
        target_id,
        int(replenishment_batch.id),
    )
    _complete(replenishment_batch, replenishment_summary)
    assembly_outputs = materialize_assembly_output_allocations(db, target_id)
    custody_projection = build_material_custody_projection(
        db, ledger_generation_id=target_id
    )
    drum_schedule = materialize_drum_schedule(db, target_id)
    shelf_projection = materialize_shelf_projections(db, target_id)
    snapshots = {str(run_id): int(build_mrp_result_candidate_snapshot(db, run_id).id) for run_id in candidate_ids}
    purchase_journal_snapshot = build_purchase_journal_candidate(db, target_id)
    production_journal_snapshot = build_production_journal_candidate(
        db,
        target_id,
        accepted_run_ids=(*candidate_ids, *retained_run_ids),
    )
    assembly_queue_snapshot = build_assembly_queue_snapshot(db, target_id)
    target = db.get(models.LedgerGeneration, target_id)
    if target is None or str(target.status) != "building":
        raise ObligationRefreshOrchestratorError(
            "target generation disappeared during refresh"
        )
    # The candidate projection validates these capabilities even though the
    # pointer is not switched until the publisher's final transaction step.
    target.capabilities = dict(_CORE_CAPABILITIES)
    db.flush()

    capabilities = dict(_CORE_CAPABILITIES)
    snapshot_metrics = {
        "candidate_run_ids": list(candidate_ids),
        "candidate_read_snapshot_ids": snapshots,
        "future_supply_captured": True,
        "future_supply_capture_batch_id": int(future_supply_capture_batch.id),
        "future_supply_capture": future_supply_capture,
        "material_custody_projection": _json_value(custody_projection),
        "freeze_summary": _json_value(freeze),
        "replay_summary": _json_value(replay),
        "reservation_consumption_summary": _json_value(reservation_consumption),
        "assembly_output_summary": _json_value(assembly_outputs),
        "drum_schedule_summary": _json_value(drum_schedule),
        "shelf_projection_summary": _json_value(shelf_projection),
        "supplier_receipt_summary": _json_value(supplier_summary),
        "purchase_control_journal_snapshot_id": int(purchase_journal_snapshot.id),
        "production_control_journal_snapshot_id": int(production_journal_snapshot.id),
        "assembly_queue_snapshot_id": int(assembly_queue_snapshot.id),
    }
    _complete(snapshot_batch, snapshot_metrics)
    target.capabilities = dict(capabilities)
    db.flush()
    # The publisher audits lineage, manifests and read snapshots, but nothing
    # ever proved the *structure* of the candidate itself: that is what the
    # genesis path gets from validate_generation_build.  Run its applicable
    # subset here so an obligation refresh cannot become truth with a broken
    # StockBin fold, a reservation cache that disagrees with its own events, or
    # an event belonging to a foreign generation.
    try:
        validate_obligation_refresh_build(db, target_id)
    except GenerationValidationError as exc:
        raise ObligationRefreshOrchestratorError(
            f"obligation refresh candidate is structurally invalid: {exc}"
        ) from exc
    published = publish_obligation_refresh_batch(
        db, parent_generation_id=int(parent_generation_id), target_generation_id=target_id,
        accepted_at=_utc(accepted_at), capabilities=dict(capabilities),
    )
    _publish_execution_snapshots(db, target_id)
    _publish_retained_mrp_snapshots(
        db,
        target_id,
        retained_run_ids,
        allow_stale_truth=bool(allow_stale_parent),
    )
    return ObligationRefreshOrchestrationResult(
        parent_generation_id=int(parent_generation_id), target_generation_id=target_id,
        candidate_run_ids=tuple(published.candidate_run_ids), published=published.published,
    )
