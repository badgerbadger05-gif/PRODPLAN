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
from app.services.item_ledger.candidate_realization_replay import replay_candidate_realizations
from app.services.item_ledger.obligation_generation import fork_obligation_generation
from app.services.dbr.cockpit_candidate import (
    DbrCockpitCandidateError,
    build_cockpit_candidate_snapshot,
)
from app.services.dbr.policy_snapshot import (
    DbrPolicySnapshotError,
    build_policy_candidate_snapshot,
)
from app.services.dbr.purchase_candidate import (
    DbrPurchaseCandidateError,
    build_purchase_candidate_snapshot,
)
from app.services.purchase_control_snapshot import build_candidate_snapshot as build_purchase_journal_candidate
from app.services.mrp_freeze import MRP_LEDGER_LOCK_KEY, freeze_candidate_snapshots
from app.services.mrp_result_snapshot import build_mrp_result_candidate_snapshot
from app.services.obligation_refresh_manifest import (
    MANIFEST_HASH_KEY,
    MANIFEST_KEY,
    create_obligation_refresh_manifest,
)
from app.services.obligation_refresh_publish import publish_obligation_refresh_batch


_VERSION = "obligation-refresh-orchestrator/1"
_CORE_CAPABILITIES = {
    "physical_ledger": True,
    "reservation_replay": True,
    "execution_allocations": True,
    "planning_snapshots": True,
    "purchase_control_journal": True,
}
_DBR_CAPABILITY = "dbr_feeder_cockpit"
_DBR_PURCHASE_CAPABILITY = "dbr_purchase_cockpit"


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
    rows = db.query(models.LedgerBuildBatch).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(target_id),
        models.LedgerBuildBatch.stage == stage,
    ).all()
    if len(rows) > 1:
        raise ObligationRefreshOrchestratorError(f"target has duplicate {stage} batches")
    if rows:
        row = rows[0]
        if str(row.batch_key) != key or str(row.algorithm_version) != _VERSION:
            raise ObligationRefreshOrchestratorError(f"target {stage} batch conflicts")
        return row
    row = models.LedgerBuildBatch(
        ledger_generation_id=int(target_id), stage=stage, batch_key=key,
        status="building", algorithm_version=_VERSION, metrics={},
    )
    db.add(row)
    db.flush()
    return row


def _complete(batch: models.LedgerBuildBatch, metrics: Mapping[str, Any]) -> None:
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
    *, add_plan_ids: Iterable[int], horizon_days: int | None,
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


def _retry_published(
    db: Session, target: models.LedgerGeneration, *, parent_generation_id: int,
    add_plan_ids: Iterable[int], horizon_days: int | None,
    config_version_id: int | None, config_snapshot: Mapping[str, Any],
    planning_pool_by_warehouse: Mapping[str, str],
) -> ObligationRefreshOrchestrationResult:
    _manifest_request_matches(target, add_plan_ids=add_plan_ids, horizon_days=horizon_days,
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
    started_by: str | None = None,
    horizon_days: int | None = None,
    config_version_id: int | None = None,
    config_snapshot: Mapping[str, Any] | None = None,
    planning_pool_by_warehouse: Mapping[str, str] | None = None,
    explicit_make_transfer_recorders: set[str] | None = None,
    accepted_at: datetime | None = None,
) -> ObligationRefreshOrchestrationResult:
    """Build and atomically publish every refresh/add candidate.

    The caller must commit this transaction.  PostgreSQL serialisation is an
    xact advisory lock; SQLite retains deterministic semantics for tests.
    """
    key = str(generation_key or "").strip()
    if not key:
        raise ValueError("generation_key is required")
    config = dict(config_snapshot or {})
    add_ids = tuple(sorted(int(v) for v in add_plan_ids))
    if len(add_ids) != len(set(add_ids)) or any(v <= 0 for v in add_ids):
        raise ValueError("add_plan_ids must be unique positive ids")
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
            db, existing, parent_generation_id=original_parent_id, add_plan_ids=add_ids,
            horizon_days=horizon_days, config_version_id=config_version_id, config_snapshot=config,
            planning_pool_by_warehouse=dict(planning_pool_by_warehouse or {}),
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
        db, int(parent_generation_id), target_id, add_ids, started_by=started_by,
        horizon_days=horizon_days, config_version_id=config_version_id, config_snapshot=config,
        planning_pool_by_warehouse=dict(planning_pool_by_warehouse or {}),
    )
    candidate_ids = tuple(sorted(int(entry["candidate_run_id"]) for entry in manifest.entries))
    if not candidate_ids:
        raise ObligationRefreshOrchestratorError("refresh manifest has no candidate runs")

    reservation_batch = _single_stage(db, target_id, "reservation_materialize", key)
    snapshot_batch = _single_stage(db, target_id, "snapshot_build", key)
    capture = capture_candidate_future_supply(
        db, int(parent_generation_id), target_id, int(snapshot_batch.id),
        planning_pool_by_warehouse=dict(planning_pool_by_warehouse or {}),
        explicit_make_transfer_recorders=explicit_make_transfer_recorders,
    )
    freeze = freeze_candidate_snapshots(
        db, parent_generation_id=int(parent_generation_id), target_generation_id=target_id,
        candidate_run_ids=candidate_ids,
    )
    reservation_count = db.query(models.ReservationEntry.id).filter(
        models.ReservationEntry.ledger_generation_id == target_id,
        models.ReservationEntry.run_id.in_(candidate_ids),
    ).count()
    reservation_metrics = {
        "candidate_run_ids": list(candidate_ids), "reservation_entries": int(reservation_count),
        "freeze_summary": _json_value(freeze),
        "input_checksum": sha256(_canonical({"candidate_run_ids": candidate_ids, "freeze": freeze}).encode()).hexdigest(),
    }
    _complete(reservation_batch, reservation_metrics)
    replay = replay_candidate_realizations(db, target_id)
    snapshots = {str(run_id): int(build_mrp_result_candidate_snapshot(db, run_id).id) for run_id in candidate_ids}
    purchase_journal_snapshot = build_purchase_journal_candidate(db, target_id)
    target = db.get(models.LedgerGeneration, target_id)
    if target is None or str(target.status) != "building":
        raise ObligationRefreshOrchestratorError(
            "target generation disappeared during refresh"
        )
    # The candidate projection validates these capabilities even though the
    # pointer is not switched until the publisher's final transaction step.
    target.capabilities = dict(_CORE_CAPABILITIES)
    db.flush()

    dbr_metrics: dict[str, Any]
    capabilities = {
        **_CORE_CAPABILITIES,
        _DBR_CAPABILITY: False,
        _DBR_PURCHASE_CAPABILITY: False,
    }
    dbr_configured = (
        db.get(models.DbrSettings, 1) is not None
        and bool(planning_pool_by_warehouse)
    )
    if dbr_configured:
        try:
            policy_snapshot = build_policy_candidate_snapshot(db, target_id)
            cockpit_snapshot = build_cockpit_candidate_snapshot(db, target_id)
            purchase_snapshot = build_purchase_candidate_snapshot(db, target_id)
        except (DbrPolicySnapshotError, DbrCockpitCandidateError, DbrPurchaseCandidateError) as exc:
            raise ObligationRefreshOrchestratorError(
                f"configured DBR candidate build failed: {exc}"
            ) from exc
        policy_hash = sha256(
            _canonical(policy_snapshot.payload).encode("utf-8")
        ).hexdigest()
        dbr_metrics = {
            "dbr_cockpit_ready": True,
            "dbr_purchase_ready": True,
            "dbr_policy_snapshot_id": int(policy_snapshot.id),
            "dbr_cockpit_snapshot_id": int(cockpit_snapshot.id),
            "dbr_purchase_snapshot_id": int(purchase_snapshot.id),
            "dbr_policy_hash": policy_hash,
        }
        capabilities[_DBR_CAPABILITY] = True
        capabilities[_DBR_PURCHASE_CAPABILITY] = True
    else:
        dbr_metrics = {
            "dbr_cockpit_ready": False,
            "dbr_purchase_ready": False,
            "dbr_unavailable_reason": (
                "DBR settings and an exact planning_pool_by_warehouse mapping "
                "are required"
            ),
        }
    snapshot_metrics = {
        "candidate_run_ids": list(candidate_ids),
        "candidate_read_snapshot_ids": snapshots,
        "future_supply_captured": True,
        "future_supply_capture": _json_value(capture),
        "freeze_summary": _json_value(freeze),
        "replay_summary": _json_value(replay),
        "purchase_control_journal_snapshot_id": int(purchase_journal_snapshot.id),
        **dbr_metrics,
    }
    _complete(snapshot_batch, snapshot_metrics)
    target.capabilities = dict(capabilities)
    db.flush()
    published = publish_obligation_refresh_batch(
        db, parent_generation_id=int(parent_generation_id), target_generation_id=target_id,
        accepted_at=_utc(accepted_at), capabilities=dict(capabilities),
    )
    return ObligationRefreshOrchestrationResult(
        parent_generation_id=int(parent_generation_id), target_generation_id=target_id,
        candidate_run_ids=tuple(published.candidate_run_ids), published=published.published,
    )
