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
from typing import Any, Iterable, Mapping

from sqlalchemy import func, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.services.purchase_control_projection import validate_purchase_control_journal_row
from app.services.production_control_journal_projection import (
    validate_candidate_payload as validate_production_journal_payload,
)
from app.services.production_material_custody_projection import (
    MaterialCustodySnapshotUnavailable,
    publish_current_material_custody,
    require_published_current_material_custody,
    validate_material_custody_projection,
)
from app.services.mrp_freeze import MRP_LEDGER_LOCK_KEY
from app.services.obligation_refresh_manifest import (
    MANIFEST_HASH_KEY,
    MANIFEST_KEY,
    ObligationRefreshManifestError,
    _current_parents,
)
from app.services.item_ledger.future_supply_capture import (
    FutureSupplyCaptureError,
    publish_current_future_supply,
    verify_future_supply_capture,
)
from app.services.planning_truth import move_pointer_to_generation
from app.services.item_ledger.r3_contract import (
    record_successor,
    retire_live_pointer,
    set_live_pointer,
)


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


def _sync_business_mrp_pointers(
    db: Session,
    *,
    additions: list[models.PlanningRun],
    replacements: list[models.PlanningRun],
    retained: list[models.PlanningRun],
    retired: list[models.PlanningRun],
) -> None:
    """Persist live MRP pointer/successor history in the publish transaction."""
    for run in [*retained, *additions, *replacements]:
        if run.source_plan_id is not None:
            set_live_pointer(db, int(run.source_plan_id), int(run.run_id))
    for run in replacements:
        if run.source_plan_id is not None and run.prior_run_id is not None:
            record_successor(
                db, int(run.source_plan_id), int(run.prior_run_id),
                int(run.run_id), reason="obligation-rebase",
            )
    for run in retired:
        if run.source_plan_id is not None:
            retire_live_pointer(db, int(run.source_plan_id))


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
    replacements: list[models.PlanningRun] = []
    retained: list[models.PlanningRun] = []
    retired: list[models.PlanningRun] = []
    successor_predecessor_ids: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ObligationRefreshPublishError("obligation_refresh_manifest entry is malformed")
        try:
            action = str(entry["action"])
            plan_id = int(entry["plan_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ObligationRefreshPublishError("obligation_refresh_manifest entry identity is malformed") from exc
        if action not in {"add", "replace", "retain", "retire"} or plan_id <= 0:
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

        if action == "add" and entry.get("parent_run_id") is not None:
            raise ObligationRefreshPublishError("add manifest must not claim a current parent run")
        if action == "add" and plan_id in parent_by_plan:
            raise ObligationRefreshPublishError("add manifest repeats a current parent plan")
        plan = db.get(models.ProductionPlanHeader, plan_id)
        if plan is None or str(plan.status) != "fixed":
            raise ObligationRefreshPublishError("add manifest plan must be fixed")
        if action == "add" and candidate.prior_run_id != plan.predecessor_run_id:
            raise ObligationRefreshPublishError(
                "add candidate predecessor conflicts with source plan lineage"
            )
        if action == "add" and plan.predecessor_run_id is not None:
            predecessor = parent_by_id.get(int(plan.predecessor_run_id))
            if predecessor is None:
                raise ObligationRefreshPublishError(
                    "successor candidate predecessor is not a current parent"
                )
            successor_predecessor_ids.add(int(predecessor.run_id))
        if action == "replace":
            try:
                parent_id = int(entry["parent_run_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ObligationRefreshPublishError(
                    "replace manifest entry lacks parent run"
                ) from exc
            parent = parent_by_id.get(parent_id)
            expected_parent_status = (
                "CLOSED"
                if candidate_status == "FIXED_SNAPSHOT"
                else "FIXED_SNAPSHOT"
            )
            if (
                parent is None
                or parent_by_plan.get(plan_id) is not parent
                or str(parent.status) != expected_parent_status
                or int(candidate.prior_run_id or -1) != int(parent.run_id)
            ):
                raise ObligationRefreshPublishError(
                    "replace manifest changes current parent lineage"
                )
            replacements.append(candidate)
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
        if action == "add":
            additions.append(candidate)

    if declared_candidate_ids != set(candidate_by_id):
        raise ObligationRefreshPublishError("obligation_refresh_manifest has missing or extra candidates")
    covered_parent_ids = {
        int(parent.run_id)
        for parent in [
            *retained,
            *retired,
            *(parent_by_id[int(row.prior_run_id)] for row in replacements),
        ]
    }
    retired_ids = {int(parent.run_id) for parent in retired}
    if not successor_predecessor_ids.issubset(retired_ids):
        raise ObligationRefreshPublishError(
            "successor candidate predecessor must be retired in the same build"
        )
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
    try:
        request_replace_ids = [int(value) for value in add_request["replace_plan_ids"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise ObligationRefreshPublishError(
            "obligation_refresh_manifest replace request is malformed"
        ) from exc
    if (
        request_replace_ids != sorted(request_replace_ids)
        or len(request_replace_ids) != len(set(request_replace_ids))
        or set(request_replace_ids)
        != {int(row.source_plan_id) for row in replacements}
    ):
        raise ObligationRefreshPublishError(
            "obligation_refresh_manifest replace request conflicts"
        )
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
    return additions, replacements, retained, retired


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
    required_mrp_run_ids: Iterable[int],
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


def _require_period_rows_for_remaining_basis(
    db: Session, payloads: Mapping[str, Mapping[str, Any]]
) -> None:
    """Refuse a live run with a remaining basis but an empty journal.

    A run whose roots still have a remaining quantity and which has
    requirements owns execution rows; publishing it with none would show an
    empty execution journal until some later refresh (item 35).  An
    ``unavailable`` payload is no exception: fail closed.
    """
    run_ids = sorted({int(payload["run_id"]) for payload in payloads.values()})
    if not run_ids:
        return
    remaining = {
        int(run_id): Decimal(str(qty or 0))
        for run_id, qty in db.query(
            models.MrpRunRoot.run_id, func.sum(models.MrpRunRoot.remaining_qty),
        ).filter(models.MrpRunRoot.run_id.in_(run_ids)).group_by(models.MrpRunRoot.run_id)
    }
    with_requirements = {
        int(run_id)
        for (run_id,) in db.query(models.MrpRequirement.run_id)
        .filter(models.MrpRequirement.run_id.in_(run_ids))
        .distinct()
    }
    for key, payload in sorted(payloads.items()):
        run_id = int(payload["run_id"])
        if (
            remaining.get(run_id, Decimal("0")) > 0
            and run_id in with_requirements
            and not list(payload.get("rows") or [])
        ):
            raise ObligationRefreshPublishError(
                f"period payload {key} has a remaining root basis but no journal rows "
                f"(truth_status={payload.get('truth_status')!r})"
            )


def _require_mrp_current_payloads(
    raw_payloads: Any,
    *,
    required_run_ids: Iterable[int],
) -> dict[str, Mapping[str, Any]]:
    """Validate the direct MRP boundary before any publication DML."""
    if not isinstance(raw_payloads, Mapping):
        raise ObligationRefreshPublishError(
            "snapshot_build lacks direct mrp_result_payloads"
        )
    result: dict[str, Mapping[str, Any]] = {}
    for marker, raw in raw_payloads.items():
        if not isinstance(raw, Mapping):
            raise ObligationRefreshPublishError("MRP current payload is malformed")
        try:
            run_id = int(raw.get("run_id", marker))
        except (TypeError, ValueError) as exc:
            raise ObligationRefreshPublishError("MRP current payload run identity is malformed") from exc
        key = str(run_id)
        if key in result:
            raise ObligationRefreshPublishError("MRP current payload has duplicate run")
        rows = raw.get("rows")
        if not isinstance(rows, list):
            raise ObligationRefreshPublishError("MRP current payload rows are missing")
        identities: set[str] = set()
        counts = {kind: 0 for kind in ("production", "purchase", "rework", "capacity")}
        for row in rows:
            if not isinstance(row, Mapping):
                raise ObligationRefreshPublishError("MRP current payload row is malformed")
            payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else row
            kind = str(payload.get("row_kind") or row.get("row_kind") or "").strip().lower()
            if kind not in counts:
                raise ObligationRefreshPublishError("MRP current payload row kind is malformed")
            identity = str(row.get("current_identity") or payload.get("current_identity") or "").strip()
            if not identity or identity in identities:
                raise ObligationRefreshPublishError("MRP current payload contains duplicate identity")
            identities.add(identity)
            counts[kind] += 1
            roots = payload.get("root_item_ids")
            if roots is not None and (not isinstance(roots, (list, tuple)) or any(
                not isinstance(value, int) for value in roots
            )):
                raise ObligationRefreshPublishError("MRP current payload root membership is malformed")
        declared_counts = raw.get("row_counts")
        if declared_counts is None or (
            not isinstance(declared_counts, Mapping)
            or set(declared_counts) != set(counts)
            or any(int(declared_counts.get(kind, -1)) != count for kind, count in counts.items())
        ):
            raise ObligationRefreshPublishError("MRP current payload row counts are malformed")
        result[key] = raw
    required = {str(int(value)) for value in required_run_ids}
    actual = set(result)
    if actual != required:
        missing = ",".join(sorted(required - actual, key=int)) or "none"
        extra = ",".join(sorted(actual - required, key=int)) or "none"
        raise ObligationRefreshPublishError(
            f"MRP current payload run set mismatch (missing: {missing}; extra: {extra})"
        )
    return result


def _validate_purchase_candidate_payload(
    target: models.LedgerGeneration,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate the direct purchase candidate without creating a snapshot row."""
    if not isinstance(payload, Mapping):
        raise ObligationRefreshPublishError(
            "snapshot_build lacks direct purchase control journal payload"
        )
    result = dict(payload)
    meta = result.get("meta")
    rows = result.get("rows")
    cards = result.get("cards")
    if (
        not isinstance(meta, Mapping)
        or meta.get("read_only") is not True
        or meta.get("fact_source") != "ledger"
        or int(meta.get("ledger_generation_id") or -1) != int(target.id)
        or not isinstance(rows, list)
        or not isinstance(cards, Mapping)
    ):
        raise ObligationRefreshPublishError(
            "purchase control journal direct candidate is missing or stale"
        )
    seen: set[str] = set()
    for row in rows:
        try:
            validate_purchase_control_journal_row(row)
            key = str(row["row_key"])
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise ObligationRefreshPublishError(
                "purchase control journal row is malformed"
            ) from exc
        except ValueError as exc:
            if "malformed" in str(exc):
                raise ObligationRefreshPublishError(
                    "purchase control journal row is malformed"
                ) from exc
            raise ObligationRefreshPublishError(
                "purchase control journal row violates Ledger fact contract"
            ) from exc
        if key in seen:
            raise ObligationRefreshPublishError(
                "purchase control journal row violates Ledger fact contract"
            )
        seen.add(key)
    return result


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
        additions, replacements, retained, retired = _require_manifest(
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
        # Successful publication removes bounded generation staging.  An
        # exact retry must therefore accept the compact current owner as the
        # proof, while still rejecting a target whose current owner belongs to
        # another generation or whose declared capture was non-empty but is
        # now absent.
        current_rows = db.query(models.LedgerFutureSupplyCurrent).all()
        expected_rows = int(snapshot_metrics.get("rows", -1))
        if current_rows:
            if any(
                int(row.source_generation_id) != int(target.id)
                for row in current_rows
            ):
                return None
        elif expected_rows != 0:
            return None
    try:
        journal_payload = _validate_purchase_candidate_payload(
            target,
            snapshot_metrics.get("purchase_control_journal_payload"),
        )
    except ObligationRefreshPublishError:
        return None
    journal_rows = journal_payload.get("rows")
    seen_journal_rows: set[str] = set()
    for row in journal_rows:
        try:
            validate_purchase_control_journal_row(row)
            key = str(row["row_key"])
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return None
        if key in seen_journal_rows:
            return None
        seen_journal_rows.add(key)
    try:
        production_payload = snapshot_metrics.get("production_control_journal_payload")
        validate_production_journal_payload(production_payload, target)
    except (TypeError, ValueError, RuntimeError):
        return None
    # Retained runs intentionally remain anchored to ``parent``.  The
    # manifest above is the complete plan scope; requiring every fixed run to
    # resolve to the new generation would reintroduce the forbidden
    # retargeting/copy semantics and make an exact retry fail after a valid
    # no-copy publication.
    candidate_ids = [int(row.run_id) for row in candidates]
    try:
        _require_mrp_current_payloads(
            snapshot_metrics.get("mrp_result_payloads"),
            required_run_ids=[
                *candidate_ids,
                *(int(row.run_id) for row in retained),
            ],
        )
    except ObligationRefreshPublishError:
        return None
    try:
        from .item_ledger.current_execution import _require_period_current_payloads
        _require_period_current_payloads(
            snapshot_metrics.get("period_plan_execution_payloads"),
            required_run_ids=[
                *candidate_ids,
                *(int(row.run_id) for row in retained),
            ],
        )
    except (ObligationRefreshPublishError, ValueError):
        return None
    for candidate in [*additions, *replacements]:
        source_plan = _lock(db.query(models.ProductionPlanHeader)).filter(
            models.ProductionPlanHeader.id == int(candidate.source_plan_id),
        ).one_or_none()
        if (
            source_plan is None
            or str(source_plan.status) != "fixed"
            or source_plan.fixed_at is None
        ):
            raise ObligationRefreshPublishError(
                "add source plan must be fixed with historical fixation time"
            )
        if candidate.fixed_at != source_plan.fixed_at:
            return None
        locked_rows = _lock(db.query(models.ProductionPlanLine)).filter(
            models.ProductionPlanLine.plan_id == int(candidate.source_plan_id),
            models.ProductionPlanLine.locked_by_run_id.is_not(None),
        ).all()
        if any(int(row.locked_by_run_id) != int(candidate.run_id) for row in locked_rows):
            return None
    _sync_business_mrp_pointers(
        db, additions=additions, replacements=replacements,
        retained=retained, retired=retired,
    )
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
    purchase_payload: Mapping[str, Any] | None = None,
    production_payload: Mapping[str, Any] | None = None,
    mrp_payloads: Mapping[str, Any] | None = None,
    period_payloads: Mapping[str, Any] | None = None,
) -> ObligationRefreshPublishResult:
    """Publish every active source plan together, using only ``flush``.

    The caller owns the surrounding transaction.  In particular this helper
    never commits or rolls back, so a later failed step restores pointer, runs,
    locks and generation as one unit.
    """
    accepted_at = _utc(accepted_at, "accepted_at")
    if not isinstance(capabilities, Mapping):
        raise TypeError("capabilities must be a mapping")
    if not isinstance(purchase_payload, Mapping):
        raise ObligationRefreshPublishError(
            "obligation refresh publication requires an explicit purchase payload"
        )
    if not isinstance(production_payload, Mapping):
        raise ObligationRefreshPublishError(
            "obligation refresh publication requires an explicit production payload"
        )
    if not isinstance(mrp_payloads, Mapping):
        raise ObligationRefreshPublishError(
            "obligation refresh publication requires explicit mrp_payloads"
        )
    if not isinstance(period_payloads, Mapping):
        raise ObligationRefreshPublishError(
            "obligation refresh publication requires explicit period_payloads"
        )
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
    try:
        current_parents = _current_parents(db, int(parent.id))
    except ObligationRefreshManifestError as exc:
        raise ObligationRefreshPublishError(str(exc)) from exc
    parents = [
        _lock(db.query(models.PlanningRun)).filter_by(run_id=int(row.run_id)).one()
        for row in current_parents
    ]
    if any(row.source_plan_id is None for row in parents):
        raise ObligationRefreshPublishError("active parent snapshot lacks source plan lineage")

    candidates = _lock(db.query(models.PlanningRun)).filter(
        models.PlanningRun.ledger_generation_id == int(target.id),
        models.PlanningRun.status == "BUILDING_SNAPSHOT",
    ).all()
    additions, replacements, retained, retired = _require_manifest(
        db, target=target, parents=parents, candidates=candidates,
        candidate_status="BUILDING_SNAPSHOT",
    )
    candidate_ids = sorted(int(row.run_id) for row in candidates)
    _require_sealed_build(
        db,
        target=target,
        candidate_ids=candidate_ids,
        required_mrp_run_ids=[
            *(int(row.run_id) for row in additions),
            *(int(row.run_id) for row in replacements),
            *(int(row.run_id) for row in retained),
        ],
        capabilities=capability_snapshot,
    )
    from .item_ledger.current_execution import _require_period_current_payloads
    direct_period_payloads = _require_period_current_payloads(
        period_payloads,
        required_run_ids=[
            *(int(row.run_id) for row in additions),
            *(int(row.run_id) for row in replacements),
            *(int(row.run_id) for row in retained),
        ],
    )
    _require_period_rows_for_remaining_basis(db, direct_period_payloads)
    snapshot_batch = _lock(db.query(models.LedgerBuildBatch)).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(target.id),
        models.LedgerBuildBatch.stage == "snapshot_build",
    ).one()
    direct_mrp_payloads = _require_mrp_current_payloads(
        mrp_payloads,
        required_run_ids=[*(int(row.run_id) for row in additions), *(int(row.run_id) for row in replacements), *(int(row.run_id) for row in retained)],
    )
    direct_purchase_payload = purchase_payload
    journal_payload = _validate_purchase_candidate_payload(
        target,
        direct_purchase_payload,
    )
    journal_rows = journal_payload["rows"]
    seen_supply_rows: set[str] = set()
    for row in journal_rows:
        try:
            validate_purchase_control_journal_row(row)
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
    direct_production_payload = production_payload
    try:
        validate_material_custody_projection(
            db, ledger_generation_id=int(target.id)
        )
        validate_production_journal_payload(direct_production_payload, target)
    except RuntimeError as exc:
        raise ObligationRefreshPublishError(str(exc)) from exc
    # Invariants 2-3 (planning-truth-contract): one physical fact is counted
    # once and its current allocations never exceed it.  The owners this
    # refresh closed have had their claims retired by now, so an offender
    # here is a real double count and the refresh must not become visible.
    from .item_ledger.current_replenishment import (
        CurrentReplenishmentError,
        require_facts_not_over_allocated,
    )

    try:
        require_facts_not_over_allocated(db)
    except CurrentReplenishmentError as exc:
        raise ObligationRefreshPublishError(str(exc)) from exc
    if _source_export_links_exist(db, candidate_ids):
        raise ObligationRefreshPublishError("candidate has external export links")

    # A source plan must not be half-transferred by an earlier/manual mutation.
    addition_fixed_at: dict[int, datetime] = {}
    for candidate in additions:
        source_plan = _lock(db.query(models.ProductionPlanHeader)).filter(
            models.ProductionPlanHeader.id == int(candidate.source_plan_id),
        ).one_or_none()
        if (
            source_plan is None
            or str(source_plan.status) != "fixed"
            or source_plan.fixed_at is None
        ):
            raise ObligationRefreshPublishError(
                "add source plan must be fixed with historical fixation time"
            )
        addition_fixed_at[int(candidate.run_id)] = source_plan.fixed_at
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
    for candidate in replacements:
        parent_run_id = int(candidate.prior_run_id)
        locked_rows = _lock(db.query(models.ProductionPlanLine)).filter(
            models.ProductionPlanLine.plan_id == int(candidate.source_plan_id),
        ).all()
        if any(
            int(row.locked_by_run_id or -1) != parent_run_id
            for row in locked_rows
        ):
            raise ObligationRefreshPublishError(
                "replacement source plan locks conflict with predecessor run"
            )
        addition_fixed_at[int(candidate.run_id)] = candidate.started_at
        for row in locked_rows:
            row.locked_by_run_id = int(candidate.run_id)
        parent_run = next(
            row for row in parents if int(row.run_id) == parent_run_id
        )
        parent_run.status = "CLOSED"
        parent_run.finished_at = accepted_at
        db.query(models.MrpRequirement).filter(
            models.MrpRequirement.run_id == parent_run_id,
            models.MrpRequirement.status == "open",
        ).update(
            {"status": "closed", "closed_at": accepted_at},
            synchronize_session=False,
        )
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
        db.query(models.MrpRequirement).filter(
            models.MrpRequirement.run_id == int(retired_run.run_id),
            models.MrpRequirement.status == "open",
        ).update(
            {"status": "closed", "closed_at": accepted_at},
            synchronize_session=False,
        )
        plan.status = "closed"
        if plan.closed_at is None:
            plan.closed_at = accepted_at

    target.status = "accepted"
    target.accepted_at = accepted_at
    target.capabilities = capability_snapshot
    # The obligation successor reuses the parent's physical batch and cutoff,
    # so the §57 verification of the parent proves it too; the single pointer
    # writer decides that, not this publisher.
    move_pointer_to_generation(db, pointer, target)
    from .item_ledger.current_execution import publish_current_execution_from_generation
    publish_current_execution_from_generation(
        db, generation_id=int(target.id)
    )
    # One current custody owner, promoted by every publication that moves the
    # pointer - the same function and the same place relative to the current
    # execution scopes as ``accept_generation_build``.  Without it the target
    # built its custody rows and left them non-current, so the compact owner
    # stayed at the physical generation that last promoted it and the next
    # bounded physical refresh was refused ("compact current custody
    # provenance is stale or ambiguous").
    try:
        publish_current_material_custody(
            db, ledger_generation_id=int(target.id)
        )
        require_published_current_material_custody(
            db, ledger_generation_id=int(target.id)
        )
    except MaterialCustodySnapshotUnavailable as exc:
        raise ObligationRefreshPublishError(
            f"custody current publication failed: {exc}"
        ) from exc
    # Future supply is captured as immutable generation evidence but exposed
    # through one compact current projection, alongside the truth-pointer
    # switch.  No reader should select a historical generation copy.
    try:
        publish_current_future_supply(db, int(target.id))
    except FutureSupplyCaptureError as exc:
        raise ObligationRefreshPublishError(
            f"future supply current publication failed: {exc}"
        ) from exc
    # Retained runs remain anchored to their original accepted generation.
    # Stable live-scope/lineage readers expose them without retargeting or
    # copying their obligations into the replacement generation.
    for candidate in [*additions, *replacements]:
        candidate.status = "FIXED_SNAPSHOT"
        candidate.pinned = True
        # The run is the immutable projection of the plan obligation.  Its
        # fixation time is the business event recorded on the source plan,
        # not the wall-clock time of this technical Ledger publication.  This
        # distinction is essential for historical replay: the next physical
        # cutoff must see and carry every plan fixed before that cutoff.
        candidate.fixed_at = addition_fixed_at[int(candidate.run_id)]
        candidate.finished_at = accepted_at
    _sync_business_mrp_pointers(
        db, additions=additions, replacements=replacements,
        retained=retained, retired=retired,
    )
    # Current user-facing obligation/result views are promoted from the
    # validated direct payloads; no historical read-model rows cross publication.
    from .item_ledger.current_execution import publish_current_obligation_views_from_generation
    publish_current_obligation_views_from_generation(
        db,
        int(target.id),
        purchase_payload=journal_payload,
        production_payload=direct_production_payload,
        mrp_payloads=direct_mrp_payloads,
        period_payloads=direct_period_payloads,
    )
    # The accepted current owner is fully published before retired projection
    # copies are removed.  Cleanup is in this same transaction and therefore
    # cannot leave a partially published refresh visible.
    from .item_ledger.execution_projection_retention import (
        prune_retired_execution_projections,
    )
    prune_retired_execution_projections(db, int(target.id))
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
