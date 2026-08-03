"""Atomically publish a complete obligation-refresh snapshot batch.

This is deliberately the *last* lifecycle step.  Builders may create a new
Ledger generation and one fresh MRP candidate per source plan, but neither is
planning truth until this service switches all of them in one caller-owned
transaction.  A partially prepared batch is not publishable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any, Mapping

from sqlalchemy import or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.services.purchase_control_snapshot import validate_purchase_control_journal_buy_row
from app.services.production_control_journal_snapshot import (
    CONSUMER as _PRODUCTION_JOURNAL_CONSUMER,
    SNAPSHOT_KEY as _PRODUCTION_JOURNAL_SNAPSHOT_KEY,
    validate_candidate_snapshot as validate_production_journal_candidate,
)
from app.services.production_material_custody_projection import (
    validate_material_custody_projection,
)
from app.services.mrp_freeze import MRP_LEDGER_LOCK_KEY
from app.services.obligation_refresh_manifest import (
    MANIFEST_HASH_KEY,
    MANIFEST_KEY,
    _current_parents,
)
from app.services.item_ledger.future_supply_capture import verify_future_supply_capture
from app.services.planning_run_candidate import _resolve_parent_generation_id


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
    "execution_allocation",
    "replenishment_work_item",
    "reservation_replay",
    "assembly_output_allocation",
    "drum_schedule",
    "shelf_projection",
    "future_supply_capture",
    "snapshot_build",
)

_MRP_RESULT_CONSUMER = "mrp_result"
_MRP_ROW_KINDS = frozenset({"production", "purchase", "rework", "capacity"})
_REQUIRED_PUBLISHED_CAPABILITIES = frozenset({
    "physical_ledger",
    "reservation_replay",
    "execution_allocations",
    "reservation_consumption_allocation",
    "replenishment_work_item",
    "supplier_receipt_coverage",
    "planning_snapshots",
    "assembly_output_allocation",
    "assembly_queue",
    "drum_schedule",
    "shelf_projection",
    # An obligation refresh always captures future supply; a target which does
    # not carry it would publish a purchase journal with zero ordered/in-transit.
    "future_supply",
    "production_control_journal",
})


def _utc(value: datetime | None, field: str) -> datetime:
    if value is None:
        raise ObligationRefreshPublishError(f"{field} is required")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _lock(query):
    """Use row locks where supported; SQLite intentionally treats this as a no-op."""
    return query.with_for_update()


def _manifest_hash(value: Any) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _require_manifest(
    db: Session,
    *,
    target: models.LedgerGeneration,
    parents: list[models.PlanningRun],
    candidates: list[models.PlanningRun],
    candidate_status: str,
) -> tuple[
    list[models.PlanningRun],
    list[models.PlanningRun],
    list[models.PlanningRun],
]:
    """Validate the sealed refresh/add set against the actual run rows.

    The source-watermark manifest is the batch boundary.  In particular, a
    target generation is not publishable merely because it happens to contain
    plausible candidates: it must contain *exactly* the candidates sealed in
    the manifest, including first-plan ``add`` candidates with no parent.
    """
    watermarks = dict(target.source_watermarks or {})
    payload = watermarks.get(MANIFEST_KEY)
    content_hash = watermarks.get(MANIFEST_HASH_KEY)
    if not isinstance(payload, dict) or not isinstance(content_hash, str):
        raise ObligationRefreshPublishError("target lacks a sealed obligation_refresh_manifest")
    if _manifest_hash(payload) != content_hash:
        raise ObligationRefreshPublishError("obligation_refresh_manifest hash conflicts")
    entries = payload.get("entries")
    add_request = payload.get("add_request")
    if not isinstance(entries, list) or not isinstance(add_request, dict):
        raise ObligationRefreshPublishError("obligation_refresh_manifest is malformed")

    candidate_by_id = {int(row.run_id): row for row in candidates}
    parent_by_id = {int(row.run_id): row for row in parents}
    parent_by_plan = {int(row.source_plan_id): row for row in parents}
    if len(parent_by_plan) != len(parents):
        raise ObligationRefreshPublishError("current parent snapshots have duplicate source plans")

    declared_candidate_ids: set[int] = set()
    declared_plans: set[int] = set()
    additions: list[models.PlanningRun] = []
    retained: list[models.PlanningRun] = []
    retired: list[models.PlanningRun] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ObligationRefreshPublishError("obligation_refresh_manifest entry is malformed")
        try:
            action = str(entry["action"])
            plan_id = int(entry["plan_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ObligationRefreshPublishError("obligation_refresh_manifest entry identity is malformed") from exc
        if action not in {"add", "retain", "retire"} or plan_id <= 0:
            raise ObligationRefreshPublishError("obligation_refresh_manifest contains unsupported action")
        if plan_id in declared_plans:
            raise ObligationRefreshPublishError("obligation_refresh_manifest has duplicate candidate or plan")
        declared_plans.add(plan_id)
        if action == "retain":
            try:
                parent_id = int(entry["parent_run_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ObligationRefreshPublishError(
                    "retain manifest entry lacks parent run"
                ) from exc
            parent = parent_by_id.get(parent_id)
            if (
                entry.get("candidate_run_id") is not None
                or parent is None
                or parent_by_plan.get(plan_id) is not parent
                or str(parent.status) != "FIXED_SNAPSHOT"
            ):
                raise ObligationRefreshPublishError(
                    "retain manifest omits or changes current parent"
                )
            retained.append(parent)
            continue
        if action == "retire":
            try:
                parent_id = int(entry["parent_run_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ObligationRefreshPublishError(
                    "retire manifest entry lacks parent run"
                ) from exc
            parent = parent_by_id.get(parent_id)
            expected_parent_status = (
                "CLOSED"
                if candidate_status == "FIXED_SNAPSHOT"
                else "FIXED_SNAPSHOT"
            )
            if (
                entry.get("candidate_run_id") is not None
                or parent is None
                or parent_by_plan.get(plan_id) is not parent
                or str(parent.status) != expected_parent_status
            ):
                raise ObligationRefreshPublishError(
                    "retire manifest omits or changes current parent"
                )
            retired.append(parent)
            continue
        try:
            candidate_id = int(entry["candidate_run_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ObligationRefreshPublishError(
                "obligation_refresh_manifest candidate identity is malformed"
            ) from exc
        if candidate_id in declared_candidate_ids:
            raise ObligationRefreshPublishError("obligation_refresh_manifest has duplicate candidate or plan")
        candidate = candidate_by_id.get(candidate_id)
        if (
            candidate is None
            or str(candidate.status) != candidate_status
            or int(candidate.ledger_generation_id or -1) != int(target.id)
            or int(candidate.source_plan_id or -1) != plan_id
        ):
            raise ObligationRefreshPublishError("obligation_refresh_manifest has missing or extra candidates")
        declared_candidate_ids.add(candidate_id)

        if entry.get("parent_run_id") is not None or candidate.prior_run_id is not None:
            raise ObligationRefreshPublishError("add candidate must not have a parent run")
        if plan_id in parent_by_plan:
            raise ObligationRefreshPublishError("add manifest repeats a current parent plan")
        plan = db.get(models.ProductionPlanHeader, plan_id)
        if plan is None or str(plan.status) != "fixed":
            raise ObligationRefreshPublishError("add manifest plan must be fixed")
        if candidate.period_from != plan.period_from or candidate.period_to != plan.period_to:
            raise ObligationRefreshPublishError("add candidate period conflicts with fixed plan")
        if candidate_status == "BUILDING_SNAPSHOT":
            if candidate.fixed_at is not None or candidate.finished_at is not None or candidate.pinned is not False:
                raise ObligationRefreshPublishError("add candidate has terminal lifecycle before publication")
        elif candidate_status == "FIXED_SNAPSHOT":
            if candidate.fixed_at is None or candidate.finished_at is None or candidate.pinned is not True:
                raise ObligationRefreshPublishError("published add candidate has incomplete lifecycle")
        else:
            raise ObligationRefreshPublishError("unsupported add candidate lifecycle phase")
        additions.append(candidate)

    if declared_candidate_ids != set(candidate_by_id):
        raise ObligationRefreshPublishError("obligation_refresh_manifest has missing or extra candidates")
    covered_parent_ids = {
        int(parent.run_id) for parent in [*retained, *retired]
    }
    if covered_parent_ids != set(parent_by_id):
        raise ObligationRefreshPublishError("obligation_refresh_manifest omits or adds refresh parents")

    try:
        request_plan_ids = [int(value) for value in add_request["plan_ids"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise ObligationRefreshPublishError("obligation_refresh_manifest add_request is malformed") from exc
    if request_plan_ids != sorted(request_plan_ids) or len(request_plan_ids) != len(set(request_plan_ids)):
        raise ObligationRefreshPublishError("obligation_refresh_manifest add_request plan_ids are malformed")
    if set(request_plan_ids) != {int(row.source_plan_id) for row in additions}:
        raise ObligationRefreshPublishError("obligation_refresh_manifest add_request conflicts with candidates")
    try:
        request_retire_ids = [int(value) for value in add_request["retire_plan_ids"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise ObligationRefreshPublishError("obligation_refresh_manifest retire request is malformed") from exc
    if (
        request_retire_ids != sorted(request_retire_ids)
        or len(request_retire_ids) != len(set(request_retire_ids))
        or set(request_retire_ids) != {int(row.source_plan_id) for row in retired}
    ):
        raise ObligationRefreshPublishError("obligation_refresh_manifest retire request conflicts")
    if not isinstance(add_request.get("config_snapshot"), dict):
        raise ObligationRefreshPublishError("obligation_refresh_manifest add config is malformed")
    pool_mapping = add_request.get("planning_pool_by_warehouse")
    if (
        not isinstance(pool_mapping, dict)
        or any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
            for key, value in pool_mapping.items()
        )
        or list(pool_mapping) != sorted(pool_mapping)
    ):
        raise ObligationRefreshPublishError(
            "obligation_refresh_manifest planning pool mapping is malformed"
        )
    for candidate in additions:
        if (
            candidate.horizon_days != add_request.get("horizon_days")
            or candidate.config_version_id != add_request.get("config_version_id")
            or candidate.config_snapshot != add_request["config_snapshot"]
        ):
            raise ObligationRefreshPublishError("add candidate config conflicts with manifest")
    return additions, retained, retired


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


def _require_future_supply_capture(
    db: Session,
    target: models.LedgerGeneration,
    snapshot_metrics: Mapping[str, Any],
) -> None:
    batch_id = snapshot_metrics.get("future_supply_capture_batch_id")
    try:
        capture_batch_id = int(batch_id)
    except (TypeError, ValueError) as exc:
        raise ObligationRefreshPublishError(
            "snapshot_build lacks future_supply_capture_batch_id"
        ) from exc
    try:
        verify_future_supply_capture(
            db,
            int(target.id),
            capture_batch_id=capture_batch_id,
        )
    except Exception as exc:
        raise ObligationRefreshPublishError(
            "snapshot_build future-supply proof is incomplete or malformed"
        ) from exc


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
    missing_capabilities = sorted(
        name
        for name in _REQUIRED_PUBLISHED_CAPABILITIES
        if capabilities.get(name) is not True
    )
    if missing_capabilities:
        raise ObligationRefreshPublishError(
            "target capabilities are incomplete: " + ", ".join(missing_capabilities)
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
    _require_future_supply_capture(
        db,
        target=target,
        snapshot_metrics=snapshot_metrics,
    )
    _require_candidate_read_snapshots(
        db,
        target=target,
        candidate_ids=candidate_ids,
        snapshot_metrics=snapshot_metrics,
        truth_status="building",
        accepted_at=None,
    )


def _require_candidate_read_snapshots(
    db: Session,
    *,
    target: models.LedgerGeneration,
    candidate_ids: list[int],
    snapshot_metrics: Mapping[str, Any],
    truth_status: str,
    accepted_at: datetime | None,
) -> list[models.PlanningReadSnapshot]:
    """Validate the sealed MRP read side before making a generation visible.

    The snapshot builder is deliberately separate from publication, so its
    persisted output is part of the publication manifest.  A candidate run
    without an exact persisted read snapshot is not a usable MRP result.
    """
    raw_ids = snapshot_metrics.get("candidate_read_snapshot_ids")
    if not isinstance(raw_ids, Mapping):
        raise ObligationRefreshPublishError(
            "snapshot_build lacks candidate_read_snapshot_ids"
        )
    try:
        declared = {int(run_id): int(snapshot_id) for run_id, snapshot_id in raw_ids.items()}
    except (TypeError, ValueError) as exc:
        raise ObligationRefreshPublishError(
            "snapshot_build candidate_read_snapshot_ids is malformed"
        ) from exc
    if set(declared) != set(candidate_ids) or len(set(declared.values())) != len(declared):
        raise ObligationRefreshPublishError(
            "snapshot_build candidate read snapshots conflict with candidates"
        )

    snapshots = _lock(db.query(models.PlanningReadSnapshot)).filter(
        models.PlanningReadSnapshot.ledger_generation_id == int(target.id),
        models.PlanningReadSnapshot.consumer == _MRP_RESULT_CONSUMER,
    ).all()
    by_id = {int(row.id): row for row in snapshots}
    if set(by_id) != set(declared.values()):
        raise ObligationRefreshPublishError(
            "target has foreign or extra mrp_result snapshots"
        )
    expected_cutoff = _utc(target.cutoff, "target cutoff")
    for run_id, snapshot_id in declared.items():
        snapshot = by_id.get(snapshot_id)
        if (
            snapshot is None
            or snapshot.snapshot_key != f"run:{run_id}"
            or str(snapshot.truth_status) != truth_status
            or _utc(snapshot.cutoff, "candidate snapshot cutoff") != expected_cutoff
        ):
            raise ObligationRefreshPublishError(
                "candidate read snapshot identity or truth state conflicts"
            )
        if accepted_at is None:
            if snapshot.reason is None:
                raise ObligationRefreshPublishError("building candidate snapshot lacks unpublished reason")
        elif snapshot.reason is not None or _utc(snapshot.published_at, "candidate snapshot published_at") != accepted_at:
            raise ObligationRefreshPublishError(
                "accepted candidate read snapshot publication conflicts"
            )
        row_counts = dict(snapshot.payload or {}).get("row_counts")
        if not isinstance(row_counts, Mapping) or set(row_counts) != _MRP_ROW_KINDS:
            raise ObligationRefreshPublishError("candidate read snapshot row_counts are incomplete")
        try:
            expected_counts = {kind: int(row_counts[kind]) for kind in _MRP_ROW_KINDS}
        except (TypeError, ValueError) as exc:
            raise ObligationRefreshPublishError("candidate read snapshot row_counts are malformed") from exc
        if any(count < 0 for count in expected_counts.values()):
            raise ObligationRefreshPublishError("candidate read snapshot row_counts are malformed")
        actual_counts = {kind: 0 for kind in _MRP_ROW_KINDS}
        rows = db.query(models.PlanningReadRow.row_kind).filter(
            models.PlanningReadRow.snapshot_id == int(snapshot.id)
        ).all()
        for (kind,) in rows:
            if kind not in _MRP_ROW_KINDS:
                raise ObligationRefreshPublishError("candidate read snapshot has unsupported row kind")
            actual_counts[str(kind)] += 1
        if actual_counts != expected_counts:
            raise ObligationRefreshPublishError(
                "candidate read snapshot persisted rows conflict with row_counts"
            )
    return [by_id[declared[run_id]] for run_id in sorted(declared)]


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
    manifest = dict(target.source_watermarks or {}).get(MANIFEST_KEY)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("entries"), list):
        return None
    candidate_manifest_ids: list[int] = []
    parent_ids: list[int] = []
    for entry in manifest["entries"]:
        if not isinstance(entry, dict):
            return None
        try:
            if entry.get("action") in {"retain", "retire"}:
                parent_ids.append(int(entry["parent_run_id"]))
            else:
                candidate_manifest_ids.append(int(entry["candidate_run_id"]))
        except (KeyError, TypeError, ValueError):
            return None
    candidates = (
        _lock(db.query(models.PlanningRun))
        .filter(
            models.PlanningRun.run_id.in_(candidate_manifest_ids),
            models.PlanningRun.ledger_generation_id == int(target.id),
            models.PlanningRun.status == "FIXED_SNAPSHOT",
        )
        .all()
        if candidate_manifest_ids
        else []
    )
    if {int(row.run_id) for row in candidates} != set(candidate_manifest_ids):
        return None
    parent_ids.extend(
        int(row.prior_run_id)
        for row in candidates
        if row.prior_run_id is not None
    )
    parent_ids = sorted(set(parent_ids))
    if parent_ids:
        superseded = _lock(db.query(models.PlanningRun)).filter(
            models.PlanningRun.run_id.in_(parent_ids),
            models.PlanningRun.source_plan_id.isnot(None),
        ).all()
        parents = superseded
    else:
        parents = []
    try:
        additions, retained, retired = _require_manifest(
            db, target=target, parents=parents, candidates=candidates,
            candidate_status="FIXED_SNAPSHOT",
        )
    except ObligationRefreshPublishError:
        return None
    snapshot_batch = _lock(db.query(models.LedgerBuildBatch)).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(target.id),
        models.LedgerBuildBatch.stage == "snapshot_build",
        models.LedgerBuildBatch.status == "completed",
    ).one_or_none()
    if snapshot_batch is None:
        return None
    snapshot_metrics = dict(snapshot_batch.metrics or {})
    try:
        _require_future_supply_capture(
            db,
            target=target,
            snapshot_metrics=snapshot_metrics,
        )
    except ObligationRefreshPublishError:
        return None
    try:
        journal_id = int(snapshot_metrics["purchase_control_journal_snapshot_id"])
    except (KeyError, TypeError, ValueError):
        return None
    journal = db.get(models.PlanningReadSnapshot, journal_id)
    if (journal is None or journal.consumer != "purchase_control_journal" or journal.snapshot_key != "journal:v1"
            or journal.ledger_generation_id != target.id or journal.truth_status != "accepted"
            or journal.reason is not None
            or _utc(journal.published_at, "purchase journal published_at") != accepted_at):
        return None
    journal_payload = journal.payload if isinstance(journal.payload, dict) else None
    journal_meta = journal_payload.get("meta") if journal_payload else None
    journal_rows = journal_payload.get("rows") if journal_payload else None
    journal_cards = journal_payload.get("cards") if journal_payload else None
    if (not isinstance(journal_meta, dict) or journal_meta.get("read_only") is not True
            or journal_meta.get("fact_source") != "ledger"
            or int(journal_meta.get("ledger_generation_id") or -1) != int(target.id)
            or not isinstance(journal_rows, list) or not isinstance(journal_cards, dict)):
        return None
    seen_journal_rows: set[str] = set()
    for row in journal_rows:
        try:
            validate_purchase_control_journal_buy_row(row)
            key = str(row["row_key"])
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return None
        if key in seen_journal_rows:
            return None
        seen_journal_rows.add(key)
    try:
        production_journal_id = int(
            dict(snapshot_batch.metrics or {})[
                "production_control_journal_snapshot_id"
            ]
        )
    except (KeyError, TypeError, ValueError):
        return None
    production_journal = db.get(
        models.PlanningReadSnapshot,
        production_journal_id,
    )
    if (
        production_journal is None
        or production_journal.consumer != _PRODUCTION_JOURNAL_CONSUMER
        or production_journal.snapshot_key != _PRODUCTION_JOURNAL_SNAPSHOT_KEY
        or production_journal.ledger_generation_id != target.id
        or production_journal.truth_status != "accepted"
        or production_journal.reason is not None
        or _utc(
            production_journal.published_at,
            "production journal published_at",
        )
        != accepted_at
    ):
        return None
    production_payload = (
        production_journal.payload
        if isinstance(production_journal.payload, dict)
        else None
    )
    production_meta = production_payload.get("meta") if production_payload else None
    if (
        not isinstance(production_meta, dict)
        or production_meta.get("read_only") is not True
        or int(production_meta.get("ledger_generation_id") or -1)
        != int(target.id)
    ):
        return None
    try:
        production_row_count = int(production_meta["row_count"])
    except (KeyError, TypeError, ValueError):
        return None
    if production_row_count != db.query(models.PlanningReadRow.id).filter(
        models.PlanningReadRow.snapshot_id == int(production_journal.id),
        models.PlanningReadRow.row_kind == "production_order",
    ).count():
        return None
    fixed_parents = _lock(db.query(models.PlanningRun)).filter(
        models.PlanningRun.status == "FIXED_SNAPSHOT",
        models.PlanningRun.source_plan_id.isnot(None),
    ).all()
    if any(
        _resolve_parent_generation_id(
            db, row, current_generation_id=int(parent.id)
        ) == int(parent.id) for row in fixed_parents
    ):
        return None
    candidate_ids = [int(row.run_id) for row in candidates]
    try:
        _require_candidate_read_snapshots(
            db,
            target=target,
            candidate_ids=candidate_ids,
            snapshot_metrics=dict(snapshot_batch.metrics or {}),
            truth_status="accepted",
            accepted_at=accepted_at,
        )
    except ObligationRefreshPublishError:
        return None
    for candidate in additions:
        locked_rows = _lock(db.query(models.ProductionPlanLine)).filter(
            models.ProductionPlanLine.plan_id == int(candidate.source_plan_id),
            models.ProductionPlanLine.locked_by_run_id.is_not(None),
        ).all()
        if any(int(row.locked_by_run_id) != int(candidate.run_id) for row in locked_rows):
            return None
    return ObligationRefreshPublishResult(
        parent_generation_id=int(parent.id), target_generation_id=int(target.id),
        parent_run_ids=tuple(sorted(
            [int(row.run_id) for row in retained]
            + [int(row.run_id) for row in retired]
        )),
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
    parents = [
        _lock(db.query(models.PlanningRun)).filter_by(run_id=int(row.run_id)).one()
        for row in _current_parents(db, int(parent.id))
    ]
    if any(row.source_plan_id is None for row in parents):
        raise ObligationRefreshPublishError("active parent snapshot lacks source plan lineage")

    candidates = _lock(db.query(models.PlanningRun)).filter(
        models.PlanningRun.ledger_generation_id == int(target.id),
        models.PlanningRun.status == "BUILDING_SNAPSHOT",
    ).all()
    additions, retained, retired = _require_manifest(
        db, target=target, parents=parents, candidates=candidates,
        candidate_status="BUILDING_SNAPSHOT",
    )
    candidate_ids = sorted(int(row.run_id) for row in candidates)
    _require_sealed_build(
        db, target=target, candidate_ids=candidate_ids,
        capabilities=capability_snapshot,
    )
    snapshot_batch = _lock(db.query(models.LedgerBuildBatch)).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(target.id),
        models.LedgerBuildBatch.stage == "snapshot_build",
    ).one()
    candidate_read_snapshots = _require_candidate_read_snapshots(
        db,
        target=target,
        candidate_ids=candidate_ids,
        snapshot_metrics=dict(snapshot_batch.metrics or {}),
        truth_status="building",
        accepted_at=None,
    )
    try:
        purchase_journal_id = int(dict(snapshot_batch.metrics or {})["purchase_control_journal_snapshot_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ObligationRefreshPublishError("snapshot_build lacks purchase control journal snapshot") from exc
    candidate_purchase_journal = _lock(db.query(models.PlanningReadSnapshot)).filter(
        models.PlanningReadSnapshot.id == purchase_journal_id,
        models.PlanningReadSnapshot.consumer == "purchase_control_journal",
        models.PlanningReadSnapshot.snapshot_key == "journal:v1",
        models.PlanningReadSnapshot.ledger_generation_id == int(target.id),
        models.PlanningReadSnapshot.truth_status == "building",
        models.PlanningReadSnapshot.cutoff == target.cutoff,
    ).one_or_none()
    journal_payload = candidate_purchase_journal.payload if candidate_purchase_journal is not None else None
    journal_meta = journal_payload.get("meta") if isinstance(journal_payload, dict) else None
    journal_rows = journal_payload.get("rows") if isinstance(journal_payload, dict) else None
    journal_cards = journal_payload.get("cards") if isinstance(journal_payload, dict) else None
    if (candidate_purchase_journal is None or not isinstance(journal_meta, dict)
            or journal_meta.get("read_only") is not True or journal_meta.get("fact_source") != "ledger"
            or int(journal_meta.get("ledger_generation_id") or -1) != int(target.id)
            or not isinstance(journal_rows, list) or not isinstance(journal_cards, dict)):
        raise ObligationRefreshPublishError("purchase control journal candidate is missing or stale")
    seen_supply_rows: set[str] = set()
    for row in journal_rows:
        try:
            validate_purchase_control_journal_buy_row(row)
            key = str(row["row_key"])
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise ObligationRefreshPublishError("purchase control journal row is malformed") from exc
        except ValueError as exc:
            if "malformed" in str(exc):
                raise ObligationRefreshPublishError("purchase control journal row is malformed") from exc
            raise ObligationRefreshPublishError("purchase control journal row violates Ledger fact contract") from exc
        if key in seen_supply_rows:
            raise ObligationRefreshPublishError("purchase control journal row violates Ledger fact contract")
        seen_supply_rows.add(key)
    try:
        production_journal_id = int(
            dict(snapshot_batch.metrics or {})[
                "production_control_journal_snapshot_id"
            ]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ObligationRefreshPublishError(
            "snapshot_build lacks production control journal snapshot"
        ) from exc
    candidate_production_journal = _lock(
        db.query(models.PlanningReadSnapshot)
    ).filter(
        models.PlanningReadSnapshot.id == production_journal_id,
        models.PlanningReadSnapshot.consumer == _PRODUCTION_JOURNAL_CONSUMER,
        models.PlanningReadSnapshot.snapshot_key
        == _PRODUCTION_JOURNAL_SNAPSHOT_KEY,
        models.PlanningReadSnapshot.ledger_generation_id == int(target.id),
        models.PlanningReadSnapshot.truth_status == "building",
        models.PlanningReadSnapshot.cutoff == target.cutoff,
    ).one_or_none()
    if candidate_production_journal is None:
        raise ObligationRefreshPublishError(
            "production control journal candidate is missing or stale"
        )
    try:
        validate_material_custody_projection(
            db, ledger_generation_id=int(target.id)
        )
        validate_production_journal_candidate(
            db,
            candidate_production_journal,
            target,
        )
    except RuntimeError as exc:
        raise ObligationRefreshPublishError(str(exc)) from exc
    if _source_export_links_exist(db, candidate_ids):
        raise ObligationRefreshPublishError("candidate has external export links")

    # A source plan must not be half-transferred by an earlier/manual mutation.
    for candidate in additions:
        locked_rows = _lock(db.query(models.ProductionPlanLine)).filter(
            models.ProductionPlanLine.plan_id == int(candidate.source_plan_id),
            models.ProductionPlanLine.locked_by_run_id.is_not(None),
        ).all()
        if locked_rows:
            raise ObligationRefreshPublishError("add source plan line is already locked")
        # An add has no parent lock to transfer.  Its previously editable plan
        # becomes immutable only at this atomic publication point.
        all_rows = _lock(db.query(models.ProductionPlanLine)).filter(
            models.ProductionPlanLine.plan_id == int(candidate.source_plan_id),
        ).all()
        for row in all_rows:
            row.locked_by_run_id = int(candidate.run_id)
    for retired_run in retired:
        plan = _lock(db.query(models.ProductionPlanHeader)).filter(
            models.ProductionPlanHeader.id == int(retired_run.source_plan_id),
        ).one_or_none()
        if plan is None or str(plan.status) != "fixed":
            raise ObligationRefreshPublishError(
                "retire manifest plan must be fixed"
            )
        retired_run.status = "CLOSED"
        retired_run.finished_at = accepted_at
        plan.status = "closed"

    target.status = "accepted"
    target.accepted_at = accepted_at
    target.capabilities = capability_snapshot
    pointer.current_generation_id = int(target.id)
    for retained_run in retained:
        # The frozen obligation rows stay untouched; only the run's accepted
        # truth projection advances to the new generation.
        retained_run.ledger_generation_id = int(target.id)
        retained_run.ledger_cutoff = target.cutoff
    for candidate in additions:
        candidate.status = "FIXED_SNAPSHOT"
        candidate.pinned = True
        candidate.fixed_at = accepted_at
        candidate.finished_at = accepted_at
    for snapshot in [
        *candidate_read_snapshots,
        candidate_purchase_journal,
        candidate_production_journal,
    ]:
        snapshot.truth_status = "accepted"
        snapshot.reason = None
        snapshot.published_at = accepted_at
    try:
        db.flush()
    except IntegrityError as exc:
        message = str(exc.orig).lower() if getattr(exc, "orig", None) else str(exc).lower()
        if (
            "uq_planning_run_fixed_snapshot_source_plan" in message
            or "unique constraint failed: planning_run.source_plan_id" in message
        ):
            raise ObligationRefreshPublishError(
                "publish failed: plan already has a FIXED_SNAPSHOT planning run"
            ) from exc
        raise
    db.expire(pointer, ["current_generation"])
    return ObligationRefreshPublishResult(
        parent_generation_id=int(parent.id), target_generation_id=int(target.id),
        parent_run_ids=tuple(sorted(
            [int(row.run_id) for row in retained]
        )),
        candidate_run_ids=tuple(candidate_ids), published=True,
    )
