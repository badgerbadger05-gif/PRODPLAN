"""Build, validate and atomically publish one explicit Ledger generation.

No external reads happen here.  Every input is an already persisted physical
prefix or frozen planning obligation.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re
from typing import Any, Callable

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app import models
from app.services.planning_truth import publish_generation

from .generation_bootstrap import ALGORITHM_VERSION, _utc
from .historical_obligations import (
    ALGORITHM_VERSION as OBLIGATION_ALGORITHM_VERSION,
    materialize_historical_obligations,
)
from .historical_replay_persistence import (
    _ALGORITHM_VERSION as REPLAY_ALGORITHM_VERSION,
    run_historical_replay,
)
from .future_supply_capture import (
    FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION,
    FUTURE_SUPPLY_CAPTURE_STAGE,
    FutureSupplyCaptureError,
    replace_future_supply_capture,
    carry_forward_future_supply,
    verify_future_supply_capture,
)
from .physical import canonical_content_hash
from .physical_visibility import visible_sles_for_generation
from .supplier_receipt_allocation import rebuild_supplier_receipt_coverage
from .supplier_receipt_odata import extract_supplier_document_evidence
from .assembly_queue_snapshot import build_assembly_queue_snapshot
from .assembly_output_persistence import (
    _ALGORITHM_VERSION as ASSEMBLY_OUTPUT_ALGORITHM_VERSION,
    materialize_assembly_output_allocations,
)
from .drum_schedule_persistence import materialize_drum_schedule
from .shelf_projection_persistence import materialize_shelf_projections
from .replenishment_work_item_builder import materialize_replenishment_work_items
from .reservation_consumption_persistence import (
    ALGORITHM_VERSION as RESERVATION_CONSUMPTION_ALGORITHM_VERSION,
    materialize_reservation_consumption_allocations,
)
from .reservation import replenishment_remaining
from app.services.one_c_export_common import DEFAULT_ORGANIZATION_REF1C
from app.services.purchase_control_snapshot import (
    PurchaseJournalPromotionError,
    build_candidate_snapshot as build_purchase_journal_candidate,
    promote_candidate_snapshot as promote_purchase_journal_candidate,
)
from app.services.production_control_journal_snapshot import (
    ProductionControlJournalPromotionError,
    build_candidate_snapshot as build_production_journal_candidate,
    promote_candidate_snapshot as promote_production_journal_candidate,
)
from app.services.production_material_custody_projection import (
    build_material_custody_projection,
)
from app.services.mrp_result_snapshot import build_mrp_result_snapshot


CAPABILITIES = {
    "physical_ledger": True,
    "reservation_replay": True,
    "execution_allocations": True,
    "supplier_receipt_coverage": True,
    "planning_snapshots": True,
    "assembly_output_allocation": True,
    "assembly_queue": True,
    "drum_schedule": True,
    "shelf_projection": True,
    "replenishment_work_item": True,
    "purchase_control_journal": True,
    "production_control_journal": True,
    # Only a generation which actually carries a future-supply capture may
    # advertise this: consumers of "ordered"/"in transit" must fail closed
    # rather than read a fabricated zero out of an empty projection.
    "future_supply": True,
    # §16 requires explicit physical consumption assignment records for each
    # generation before exposing free-S0 semantics to downstream consumers.
    "reservation_consumption_allocation": True,
}
PHYSICAL_REFRESH_KIND = "physical_refresh"
_REPLENISHMENT_WORK_ITEM_ALGORITHM_VERSION = "physical-refresh-replenishment-work-item/1"
_SUPPLIER_DOCUMENT_TYPES = frozenset({
    "Document_ПриходнаяНакладная",
    "Document_КорректировкаПоступления",
    "Document_РасходнаяНакладная",
})
class GenerationValidationError(ValueError):
    pass


def _d(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def _d_int(value: Any, field: str) -> int:
    parsed = _d(value)
    if parsed != parsed.to_integral():
        raise GenerationValidationError(f"execution allocation metric {field} must be an integer")
    return int(parsed)


def _qty_text(value: Any) -> str:
    number = _d(value)
    return "0" if number == 0 else format(number.normalize(), "f")


def _allocation_signature(rows: tuple[models.ReservationConsumptionAllocation, ...]) -> list[dict[str, Any]]:
    return sorted([
        {
            "sle_id": int(row.sle_id),
            "reservation_id": int(row.reservation_id),
            "requirement_id": int(row.requirement_id),
            "qty": _qty_text(row.allocated_qty),
            "match_rule": str(row.match_rule or ""),
            "idempotency_key": str(row.idempotency_key or ""),
        }
        for row in rows
    ], key=lambda row: (row["sle_id"], row["reservation_id"]))


def _allocation_checksum(rows: tuple[models.ReservationConsumptionAllocation, ...]) -> str:
    signature = _allocation_signature(rows)
    raw = json.dumps(signature, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(raw.encode("utf-8")).hexdigest()


def _parse_iso_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, field)
    if not isinstance(value, str):
        raise GenerationValidationError(f"{field} must be ISO timestamp")
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")), field)
    except ValueError as exc:
        raise GenerationValidationError(f"{field} must be ISO timestamp") from exc


def _parse_int_id_list(value: Any, field: str) -> set[int]:
    if value is None:
        return set()
    if not isinstance(value, (list, tuple)):
        raise GenerationValidationError(f"{field} must be a list of integer ids")
    parsed: set[int] = set()
    for raw_id in value:
        try:
            parsed.add(int(raw_id))
        except (TypeError, ValueError) as exc:
            raise GenerationValidationError(
                f"{field} must contain integer ids"
            ) from exc
    return parsed


def _validate_historical_bootstrap_watermarks(
    generation: models.LedgerGeneration,
) -> None:
    if str(generation.algorithm_version or "") != ALGORITHM_VERSION:
        return

    source_watermarks = generation.source_watermarks
    if not isinstance(source_watermarks, dict):
        raise GenerationValidationError(
            "historical bootstrap requires source_watermarks mapping"
        )

    if "opening_balance" not in source_watermarks:
        raise GenerationValidationError("source_watermarks.opening_balance is required")
    if source_watermarks.get("opening_balance") is None:
        raise GenerationValidationError(
            "source_watermarks.opening_balance must be present"
        )

    cutoff = _utc(generation.cutoff, "generation cutoff")
    historical_import_completed_through = _parse_iso_datetime(
        source_watermarks.get("historical_import_completed_through"),
        "source_watermarks.historical_import_completed_through",
    )
    if historical_import_completed_through != cutoff:
        raise GenerationValidationError(
            "historical_import_completed_through must equal generation cutoff"
        )

    convergence = source_watermarks.get("balance_convergence")
    if not isinstance(convergence, dict):
        raise GenerationValidationError(
            "source_watermarks.balance_convergence must be present"
        )
    if convergence.get("valid") is not True:
        raise GenerationValidationError(
            "source_watermarks.balance_convergence.valid must be true"
        )

    convergence_cutoff = _parse_iso_datetime(
        convergence.get("cutoff"),
        "source_watermarks.balance_convergence.cutoff",
    )
    if convergence_cutoff != cutoff:
        raise GenerationValidationError(
            "balance_convergence.cutoff must equal generation cutoff"
        )
    try:
        convergence_batch_id = int(convergence.get("physical_import_batch_id"))
    except (TypeError, ValueError) as exc:
        raise GenerationValidationError(
            "balance_convergence.physical_import_batch_id must match generation physical boundary"
        ) from exc
    if convergence_batch_id != int(generation.physical_import_batch_id):
        raise GenerationValidationError(
            "balance_convergence.physical_import_batch_id must match generation physical boundary"
        )


def _validate_physical_refresh_watermarks(
    generation: models.LedgerGeneration,
) -> None:
    source_watermarks = generation.source_watermarks
    if not isinstance(source_watermarks, dict):
        if source_watermarks is None:
            return
        raise GenerationValidationError(
            "physical refresh requires source_watermarks mapping"
        )
    if str(source_watermarks.get("generation_kind") or "") != PHYSICAL_REFRESH_KIND:
        return

    cutoff = _utc(generation.cutoff, "generation cutoff")
    historical_import_completed_through = _parse_iso_datetime(
        source_watermarks.get("historical_import_completed_through"),
        "source_watermarks.historical_import_completed_through",
    )
    if historical_import_completed_through != cutoff:
        raise GenerationValidationError(
            "historical_import_completed_through must equal generation cutoff"
        )

    convergence = source_watermarks.get("balance_convergence")
    if not isinstance(convergence, dict):
        raise GenerationValidationError(
            "source_watermarks.balance_convergence must be present"
        )
    if convergence.get("valid") is not True:
        raise GenerationValidationError(
            "source_watermarks.balance_convergence.valid must be true"
        )
    convergence_cutoff = _parse_iso_datetime(
        convergence.get("cutoff"),
        "source_watermarks.balance_convergence.cutoff",
    )
    if convergence_cutoff != cutoff:
        raise GenerationValidationError(
            "balance_convergence.cutoff must equal generation cutoff"
        )
    try:
        convergence_batch_id = int(convergence.get("physical_import_batch_id"))
    except (TypeError, ValueError) as exc:
        raise GenerationValidationError(
            "balance_convergence.physical_import_batch_id must match generation physical boundary"
        ) from exc
    if convergence_batch_id != int(generation.physical_import_batch_id):
        raise GenerationValidationError(
            "balance_convergence.physical_import_batch_id must match generation physical boundary"
        )


def _building_generation(db: Session, generation_id: int) -> models.LedgerGeneration:
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None:
        raise GenerationValidationError(f"LedgerGeneration {generation_id} not found")
    if str(generation.status or "") != "building":
        raise GenerationValidationError("generation lifecycle requires explicit BUILDING generation")
    if generation.cutoff is None or generation.physical_import_batch_id is None:
        raise GenerationValidationError("building generation requires cutoff and physical prefix")
    return generation


def materialize_generation_stock_bins(
    db: Session, generation_id: int
) -> dict[str, Any]:
    """Fold the immutable visible prefix without reading SLE.active/qty_after."""
    generation = _building_generation(db, generation_id)
    rows = visible_sles_for_generation(db, int(generation.id))
    grouped: dict[tuple[int, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            int(row.item_id),
            str(row.characteristic_ref or ""),
            str(row.organization_ref or ""),
            str(row.warehouse_ref1c or ""),
        )
        state = grouped.setdefault(key, {"on_hand": Decimal("0"), "last_entry_id": None})
        state["on_hand"] += _d(row.qty)
        state["last_entry_id"] = int(row.id)

    existing = {
        (
            int(row.item_id),
            str(row.characteristic_ref or ""),
            str(row.organization_ref or ""),
            str(row.warehouse_ref1c or ""),
        ): row
        for row in db.query(models.StockBin).filter(
            models.StockBin.ledger_generation_id == int(generation.id)
        ).all()
    }
    for key, state in grouped.items():
        bin_row = existing.pop(key, None)
        if bin_row is None:
            bin_row = models.StockBin(
                ledger_generation_id=int(generation.id),
                item_id=key[0],
                characteristic_ref=key[1],
                organization_ref=key[2],
                warehouse_ref1c=key[3],
            )
            db.add(bin_row)
        bin_row.on_hand = state["on_hand"]
        bin_row.last_entry_id = state["last_entry_id"]
        bin_row.reconcile_pending_qty = Decimal("0")
    for stale in existing.values():
        db.delete(stale)
    db.flush()
    return {
        "physical_facts": len(rows),
        "stock_bins": len(grouped),
        "on_hand_total": str(
            sum((state["on_hand"] for state in grouped.values()), Decimal("0"))
        ),
    }


def _completed_stage(
    db: Session, generation_id: int, stage: str
) -> models.LedgerBuildBatch:
    rows = (
        db.query(models.LedgerBuildBatch)
        .filter(
            models.LedgerBuildBatch.ledger_generation_id == int(generation_id),
            models.LedgerBuildBatch.stage == stage,
        )
        .order_by(models.LedgerBuildBatch.id.asc())
        .all()
    )
    completed = [row for row in rows if str(row.status) == "completed"]
    if len(completed) != 1 or any(str(row.status) != "completed" for row in rows):
        raise GenerationValidationError(
            f"generation requires one completed {stage} checkpoint and no partial checkpoint"
        )
    return completed[0]


def _execution_allocation_checkpoint(
    db: Session,
    generation: models.LedgerGeneration,
) -> dict[str, Any]:
    execution_batch = _completed_stage(
        db, int(generation.id), "execution_allocation"
    )
    if execution_batch.algorithm_version != RESERVATION_CONSUMPTION_ALGORITHM_VERSION:
        raise GenerationValidationError(
            "unexpected reservation consumption allocation algorithm"
        )
    execution_metrics = dict(execution_batch.metrics or {})
    required_execution_metrics = {
        "facts",
        "allocations",
        "fact_qty",
        "allocated_qty",
        "surplus_qty",
        "allocation_checksum",
    }
    missing = sorted(
        key for key in required_execution_metrics if key not in execution_metrics
    )
    if missing:
        raise GenerationValidationError(
            f"execution allocation metrics missing: {', '.join(missing)}"
        )
    execution_facts = _d_int(execution_metrics["facts"], "facts")
    execution_allocations = _d_int(execution_metrics["allocations"], "allocations")
    execution_fact_qty = _d(execution_metrics["fact_qty"])
    execution_allocated_qty = _d(execution_metrics["allocated_qty"])
    execution_surplus_qty = _d(execution_metrics["surplus_qty"])
    if execution_fact_qty != execution_allocated_qty + execution_surplus_qty:
        raise GenerationValidationError(
            "execution allocation conservation failed"
        )

    allocation_rows = tuple(
        row
        for row in db.query(models.ReservationConsumptionAllocation)
        .filter(
            models.ReservationConsumptionAllocation.ledger_generation_id
            == int(generation.id)
        )
        .order_by(models.ReservationConsumptionAllocation.id.asc())
        .all()
    )
    if len(allocation_rows) != execution_allocations:
        raise GenerationValidationError(
            "execution allocation metric does not match allocation row count"
        )
    if _allocation_checksum(allocation_rows) != str(execution_metrics["allocation_checksum"]):
        raise GenerationValidationError("execution allocation checksum mismatch")
    return {
        "facts": execution_facts,
        "allocations": execution_allocations,
        "fact_qty": str(execution_fact_qty),
        "allocated_qty": str(execution_allocated_qty),
        "surplus_qty": str(execution_surplus_qty),
        "allocation_checksum": str(execution_metrics["allocation_checksum"]),
    }


def _supplier_candidates(
    db: Session, generation_id: int
) -> tuple[models.StockLedgerEntry, ...]:
    return tuple(
        row for row in visible_sles_for_generation(db, generation_id)
        if str(row.recorder_type or "") in _SUPPLIER_DOCUMENT_TYPES
        and str(row.organization_ref or "") == DEFAULT_ORGANIZATION_REF1C
    )


def _persist_non_supplier_receipt_rows(
    db: Session,
    *,
    generation_id: int,
    supplier_candidates: tuple[models.StockLedgerEntry, ...],
    ignored_stock_ledger_entries: tuple[tuple[int, str, str], ...] = (),
) -> None:
    if not ignored_stock_ledger_entries:
        return

    candidate_by_id = {
        int(row.id): row
        for row in supplier_candidates
        if row.id is not None
    }

    seen_ids: set[int] = set()
    for ignored_id, operation_key, operation_name in ignored_stock_ledger_entries:
        entry_id = int(ignored_id)
        if entry_id not in candidate_by_id:
            raise GenerationValidationError(
                "ignored supplier stock entry ids must reference supplier candidates"
            )
        if entry_id in seen_ids:
            continue
        row = candidate_by_id[entry_id]
        evidence_payload = {
            "stock_ledger_entry_id": int(row.id),
            "receipt_doc_type": str(row.recorder_type or ""),
            "receipt_doc_ref": str(row.recorder_ref or ""),
            "receipt_doc_line_no": str(row.line_no or ""),
            "operation_key": operation_key,
            "operation_name": operation_name,
        }
        db.add(models.StockLedgerSupplierReceiptProvenance(
            ledger_generation_id=generation_id,
            stock_ledger_entry_id=int(row.id),
            receipt_doc_type=str(row.recorder_type or ""),
            receipt_doc_ref=str(row.recorder_ref or ""),
            receipt_doc_line_no=str(row.line_no or ""),
            supplier_order_ref=None,
            supplier_order_line_no=None,
            operation_kind="non_supplier_expense",
            operation_key=operation_key,
            operation_name=operation_name,
            correction_receipt_ref=None,
            evidence_hash=canonical_content_hash(evidence_payload),
            evidence_payload=evidence_payload,
            match_rule="supplier-receipt-non-supplier-exclusion",
            match_status="excluded_non_supplier",
            ambiguity_count=0,
            reason="non-supplier expense operation",
        ))
        seen_ids.add(entry_id)

    db.flush()


_CYCLE_GENERATION = re.compile(r":g(\d+)(?::|$)")


def _ancestor_generation_ids(
    db: Session, generation: models.LedgerGeneration
) -> set[int]:
    """This generation plus every generation it descends from.

    An obligation refresh carries retained reservations forward verbatim, so
    their events legitimately keep the cycle of the generation which first
    recorded them.  Walking the sealed ``parent_generation_id`` lineage is the
    only structural way to tell such provenance from a genuinely foreign event.
    """
    seen = {int(generation.id)}
    current: models.LedgerGeneration | None = generation
    while current is not None:
        marks = dict(current.source_watermarks or {})
        try:
            parent_id = int(marks["parent_generation_id"])
        except (KeyError, TypeError, ValueError):
            break
        if parent_id in seen:
            break
        seen.add(parent_id)
        current = db.get(models.LedgerGeneration, parent_id)
    return seen


def _cycle_names_foreign_generation(cycle_id: str, allowed_ids: set[int]) -> bool:
    match = _CYCLE_GENERATION.search(cycle_id)
    return match is not None and int(match.group(1)) not in allowed_ids


def _physical_boundary_checkpoint(
    db: Session,
    generation: models.LedgerGeneration,
) -> models.PhysicalImportBatch:
    physical_batch = db.get(
        models.PhysicalImportBatch, int(generation.physical_import_batch_id)
    )
    if physical_batch is None or str(physical_batch.status) != "completed":
        raise GenerationValidationError("physical import boundary is not completed")
    if physical_batch.cutoff and physical_batch.cutoff > generation.cutoff:
        raise GenerationValidationError("physical import boundary exceeds generation cutoff")
    partial_physical = db.query(models.LedgerBuildBatch.id).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(generation.id),
        models.LedgerBuildBatch.stage == "physical_import",
        models.LedgerBuildBatch.status != "completed",
    ).first()
    if partial_physical:
        raise GenerationValidationError("physical import has incomplete checkpoints")
    return physical_batch


def _reservation_fold_checkpoint(
    db: Session,
    generation: models.LedgerGeneration,
    *,
    is_allowed_cycle: Callable[[str], bool],
) -> tuple[
    list[models.ReservationEntry],
    list[models.ReservationEvent],
    dict[int, models.ReservationEntry],
]:
    """Prove the reservation caches are exactly the fold of their own events."""
    entries = db.query(models.ReservationEntry).filter(
        models.ReservationEntry.ledger_generation_id == int(generation.id)
    ).all()
    entry_by_id = {int(row.id): row for row in entries}
    event_sums: dict[int, tuple[Decimal, Decimal]] = {
        entry_id: (Decimal("0"), Decimal("0")) for entry_id in entry_by_id
    }
    events = db.query(models.ReservationEvent).filter(
        models.ReservationEvent.ledger_generation_id == int(generation.id)
    ).all()
    for event in events:
        if int(event.reservation_id) not in entry_by_id:
            raise GenerationValidationError("reservation event escapes generation")
        if not is_allowed_cycle(str(event.cycle_id or "")):
            raise GenerationValidationError("legacy reservation event entered generation build")
        reserved, realized = event_sums[int(event.reservation_id)]
        event_sums[int(event.reservation_id)] = (
            reserved + _d(event.reserved_delta),
            realized + _d(event.realized_delta),
        )
    for entry in entries:
        reserved, realized = event_sums[int(entry.id)]
        if (
            reserved != _d(entry.reserved_qty)
            or realized != _d(entry.realized_qty)
            or realized != _d(entry.replenishment_received_qty)
        ):
            raise GenerationValidationError(
                f"reservation {entry.id} cache differs from event fold"
            )
        covered = _d(entry.covered_from_stock_at_freeze_qty)
        replenishment_required = _d(entry.replenishment_required_qty)
        if (
            reserved < 0
            or covered < 0
            or replenishment_required < 0
            or covered + replenishment_required != reserved
        ):
            raise GenerationValidationError(
                f"reservation {entry.id} frozen quantities must satisfy "
                "reserved >= 0, covered >= 0, replenishment_required >= 0, "
                "and covered + replenishment_required == reserved"
            )
        if realized > replenishment_required:
            raise GenerationValidationError(
                f"reservation {entry.id} replenishment exceeds frozen demand"
            )
    return entries, events, entry_by_id


def _reservation_fact_conservation_checkpoint(
    events: list[models.ReservationEvent],
    visible: list[models.StockLedgerEntry],
) -> int:
    """Prove that realization projections partition, rather than clone, facts."""
    visible_by_id = {int(row.id): row for row in visible}
    realized_by_sle: dict[int, Decimal] = {}
    for event in events:
        delta = _d(event.realized_delta)
        if delta == 0:
            continue
        if event.sle_id is None or int(event.sle_id) not in visible_by_id:
            raise GenerationValidationError(
                "reservation realization references a non-visible physical fact"
            )
        sle_id = int(event.sle_id)
        physical_qty = _d(visible_by_id[sle_id].qty)
        if physical_qty == 0 or (physical_qty > 0) != (delta > 0):
            raise GenerationValidationError(
                f"reservation realization sign conflicts with physical SLE {sle_id}"
            )
        realized_by_sle[sle_id] = realized_by_sle.get(sle_id, Decimal("0")) + delta
    for sle_id, realized_qty in realized_by_sle.items():
        if abs(realized_qty) > abs(_d(visible_by_id[sle_id].qty)):
            raise GenerationValidationError(
                f"reservation realizations exceed physical SLE {sle_id}"
            )
    return len(realized_by_sle)


def _replenishment_work_item_checkpoint(
    db: Session,
    generation: models.LedgerGeneration,
    entries: list[models.ReservationEntry],
) -> int:
    """Prove the journal projection equals the post-replay reservation fold."""
    batch = (
        db.query(models.LedgerBuildBatch)
        .filter(
            models.LedgerBuildBatch.ledger_generation_id == int(generation.id),
            models.LedgerBuildBatch.stage == "replenishment_work_item",
        )
        .one_or_none()
    )
    # Older accepted ancestors may predate this projection. A generation that
    # advertises/builds it, however, must prove exact equality before publish.
    if batch is None:
        return 0
    if str(batch.status) != "completed":
        raise GenerationValidationError(
            "replenishment work-item batch is not completed"
        )
    expected = {
        int(entry.id): entry
        for entry in entries
        if str(entry.lifecycle_status) == "active"
        and entry.run_id is not None
        and _d(entry.replenishment_required_qty) > 0
        and str(entry.realization_mode) in ("make", "buy")
    }
    actual_rows = (
        db.query(models.ReplenishmentWorkItem)
        .filter(
            models.ReplenishmentWorkItem.ledger_generation_id
            == int(generation.id)
        )
        .all()
    )
    actual = {int(row.reservation_id): row for row in actual_rows}
    if set(actual) != set(expected):
        raise GenerationValidationError(
            "replenishment work items differ from active reservation scope"
        )
    for reservation_id, entry in expected.items():
        row = actual[reservation_id]
        required = _d(entry.replenishment_required_qty)
        fulfilled = _d(entry.replenishment_received_qty)
        remaining = replenishment_remaining(required, fulfilled)
        if (
            _d(row.replenishment_required_qty) != required
            or _d(row.replenishment_fulfilled_qty) != fulfilled
            or _d(row.replenishment_remaining_qty) != remaining
            or str(row.replenishment_method) != str(entry.realization_mode)
        ):
            raise GenerationValidationError(
                f"replenishment work item {row.id} differs from reservation fold"
            )
    return len(actual_rows)


def _mrp_quantity_checkpoint(
    db: Session,
    generation: models.LedgerGeneration,
) -> int:
    """Reject a publish when any generation-owned gross/net pair is invalid."""
    run_ids = (
        db.query(models.PlanningRun.run_id)
        .filter(models.PlanningRun.ledger_generation_id == int(generation.id))
        .scalar_subquery()
    )
    bad_requirements = (
        db.query(models.MrpRequirement.id)
        .filter(
            models.MrpRequirement.run_id.in_(run_ids),
            or_(
                models.MrpRequirement.total_required_qty < 0,
                models.MrpRequirement.net_required_qty < 0,
                models.MrpRequirement.net_required_qty
                > models.MrpRequirement.total_required_qty,
            ),
        )
        .count()
    )
    bad_buckets = (
        db.query(models.MrpRequirementBucket.id)
        .filter(
            models.MrpRequirementBucket.run_id.in_(run_ids),
            or_(
                models.MrpRequirementBucket.gross_qty < 0,
                models.MrpRequirementBucket.net_qty < 0,
                models.MrpRequirementBucket.net_qty
                > models.MrpRequirementBucket.gross_qty,
            ),
        )
        .count()
    )
    if bad_requirements or bad_buckets:
        raise GenerationValidationError(
            "MRP gross/net quantities violate 0 <= net <= gross"
        )
    return (
        db.query(models.MrpRequirement.id)
        .filter(models.MrpRequirement.run_id.in_(run_ids))
        .count()
    )


def _stock_bin_fold_checkpoint(
    db: Session,
    generation: models.LedgerGeneration,
    visible: list[models.StockLedgerEntry],
) -> int:
    expected_bins: dict[tuple[int, str, str, str], tuple[Decimal, int]] = {}
    for row in visible:
        key = (
            int(row.item_id),
            str(row.characteristic_ref or ""),
            str(row.organization_ref or ""),
            str(row.warehouse_ref1c or ""),
        )
        qty, _last = expected_bins.get(key, (Decimal("0"), int(row.id)))
        expected_bins[key] = (qty + _d(row.qty), int(row.id))
    bins = db.query(models.StockBin).filter(
        models.StockBin.ledger_generation_id == int(generation.id)
    ).all()
    actual_bins = {
        (
            int(row.item_id),
            str(row.characteristic_ref or ""),
            str(row.organization_ref or ""),
            str(row.warehouse_ref1c or ""),
        ): (_d(row.on_hand), int(row.last_entry_id) if row.last_entry_id else None)
        for row in bins
    }
    if actual_bins != expected_bins:
        raise GenerationValidationError("StockBin differs from immutable physical fold")
    return len(bins)


def _supplier_provenance_checkpoint(
    db: Session,
    generation: models.LedgerGeneration,
    *,
    require_full_coverage: bool,
) -> tuple[
    list[models.StockLedgerSupplierReceiptProvenance],
    dict[int, models.StockLedgerEntry],
    set[int],
    dict[str, int],
]:
    supplier_candidates = _supplier_candidates(db, int(generation.id))
    supplier_physical_ids = {
        int(row.id)
        for row in supplier_candidates
        if row.id is not None
    }
    supplier_candidate_by_id = {
        int(row.id): row
        for row in supplier_candidates
        if row.id is not None
    }
    provenance = db.query(
        models.StockLedgerSupplierReceiptProvenance
    ).filter(
        models.StockLedgerSupplierReceiptProvenance.ledger_generation_id
        == int(generation.id)
    ).all()
    provenance_ids = {int(row.stock_ledger_entry_id) for row in provenance}
    if provenance_ids - supplier_physical_ids:
        raise GenerationValidationError(
            "supplier receipt provenance must reference supplier candidates"
        )
    if require_full_coverage and provenance_ids != supplier_physical_ids:
        raise GenerationValidationError(
            "supplier receipt evidence must cover all supplier candidate rows"
        )
    allowed_supplier_statuses = {
        "exact",
        "unmatched",
        "ambiguous",
        "excluded_non_supplier",
    }
    if any(
        str(row.match_status or "") not in allowed_supplier_statuses
        for row in provenance
    ):
        raise GenerationValidationError(
            "supplier receipt evidence has an invalid classification"
        )
    if any(
        str(row.operation_kind or "") != "non_supplier_expense"
        or not str(row.operation_key or "").strip()
        or not str(row.operation_name or "").strip()
        or str(row.match_rule or "") != "supplier-receipt-non-supplier-exclusion"
        or row.supplier_order_ref is not None
        or row.supplier_order_line_no is not None
        for row in provenance
        if str(row.match_status or "") == "excluded_non_supplier"
    ):
        raise GenerationValidationError(
            "non-supplier exclusion lacks explicit operation evidence"
        )
    excluded_ids = {
        int(row.stock_ledger_entry_id)
        for row in provenance
        if str(row.match_status or "") == "excluded_non_supplier"
    }
    supplier_status_counts = {
        status: sum(
            1 for row in provenance if str(row.match_status or "") == status
        )
        for status in sorted(allowed_supplier_statuses)
    }
    return provenance, supplier_candidate_by_id, excluded_ids, supplier_status_counts


def validate_obligation_refresh_build(
    db: Session,
    generation_id: int,
) -> dict[str, Any]:
    """Structural gate for the obligation-refresh publisher.

    ``validate_generation_build`` is genesis-shaped: it asserts the historical
    bootstrap's own algorithm identities and conservation metrics, and an
    obligation refresh does not run those stages at all (it materializes
    reservations through the freeze executor and replays only its candidates).
    This is the applicable subset — physical boundary, immutable StockBin fold,
    reservation cache/event-fold agreement, event containment and supplier
    provenance sanity — so that nothing reaches the truth pointer unchecked.

    Deliberately excluded, with reasons:

    * the empty-prefix declaration: the prefix is inherited unchanged from an
      already accepted parent, which declared it once;
    * full supplier-provenance coverage: the fork clones the parent's evidence
      under a checksum, so equality with the candidate set is the parent's
      already-proven property, and re-deriving it here would fail generations
      whose parent legitimately predates supplier evidence;
    * the historical obligation/replay metric conservation: those metrics do
      not exist on this path.
    """
    generation = _building_generation(db, generation_id)
    marks = dict(generation.source_watermarks or {})
    if str(marks.get("generation_kind") or "") != "obligation_refresh":
        raise GenerationValidationError(
            "obligation-refresh validation requires an obligation_refresh generation"
        )
    _execution_allocation_checkpoint(db, generation)
    _physical_boundary_checkpoint(db, generation)
    requirement_count = _mrp_quantity_checkpoint(db, generation)
    visible = visible_sles_for_generation(db, int(generation.id))

    allowed_generation_ids = _ancestor_generation_ids(db, generation)

    def _is_allowed_cycle(cycle_id: str) -> bool:
        if not cycle_id.strip():
            return False
        return not _cycle_names_foreign_generation(cycle_id, allowed_generation_ids)

    entries, events, _by_id = _reservation_fold_checkpoint(
        db, generation, is_allowed_cycle=_is_allowed_cycle
    )
    realized_fact_count = _reservation_fact_conservation_checkpoint(events, visible)
    work_item_count = _replenishment_work_item_checkpoint(
        db,
        generation,
        entries,
    )
    bin_count = _stock_bin_fold_checkpoint(db, generation, visible)
    provenance, _candidates, excluded_ids, status_counts = (
        _supplier_provenance_checkpoint(db, generation, require_full_coverage=False)
    )
    return {
        "ledger_generation_id": int(generation.id),
        "physical_facts": len(visible),
        "stock_bins": bin_count,
        "mrp_requirements": requirement_count,
        "reservation_entries": len(entries),
        "reservation_events": len(events),
        "replenishment_work_items": work_item_count,
        "realized_physical_facts": realized_fact_count,
        "carried_forward_generations": sorted(
            allowed_generation_ids - {int(generation.id)}
        ),
        "supplier_receipt_evidence": len(provenance),
        "supplier_receipt_ignored_count": len(excluded_ids),
        "supplier_receipt_status_counts": status_counts,
        "valid": True,
    }


def validate_generation_build(
    db: Session,
    generation_id: int,
    *,
    explicit_empty_physical: bool = False,
) -> dict[str, Any]:
    """Dry, read-only validation.  Raises before truth publication on any gap."""
    generation = _building_generation(db, generation_id)
    physical_batch = _physical_boundary_checkpoint(db, generation)
    _mrp_quantity_checkpoint(db, generation)
    _validate_historical_bootstrap_watermarks(generation)
    _validate_physical_refresh_watermarks(generation)

    visible = visible_sles_for_generation(db, int(generation.id))
    empty_declared = bool(
        explicit_empty_physical
        or (generation.source_watermarks or {}).get("explicit_empty_prefix")
        or (physical_batch.source_watermarks or {}).get("explicit_empty_prefix")
    )
    if not visible and not empty_declared:
        raise GenerationValidationError("empty physical prefix must be explicit")

    obligation_batch = _completed_stage(
        db, int(generation.id), "reservation_materialize"
    )
    replay_batch = _completed_stage(db, int(generation.id), "reservation_replay")
    assembly_output_batch = _completed_stage(
        db, int(generation.id), "assembly_output_allocation"
    )
    execution_allocations = _execution_allocation_checkpoint(db, generation)
    obligation_metrics = dict(obligation_batch.metrics or {})
    replay_metrics = dict(replay_batch.metrics or {})
    if obligation_batch.algorithm_version != OBLIGATION_ALGORITHM_VERSION:
        raise GenerationValidationError("unexpected historical obligation algorithm")
    if replay_batch.algorithm_version != REPLAY_ALGORITHM_VERSION:
        raise GenerationValidationError("unexpected historical replay algorithm")
    if assembly_output_batch.algorithm_version != ASSEMBLY_OUTPUT_ALGORITHM_VERSION:
        raise GenerationValidationError("unexpected assembly output allocation algorithm")
    assembly_metrics = dict(assembly_output_batch.metrics or {})
    assembly_fact_qty = _d(assembly_metrics.get("fact_qty"))
    assembly_allocated_qty = _d(assembly_metrics.get("allocated_qty"))
    assembly_surplus_qty = _d(assembly_metrics.get("surplus_total"))
    if assembly_fact_qty != assembly_allocated_qty + assembly_surplus_qty:
        raise GenerationValidationError(
            "assembly output allocation conservation failed"
        )
    selected_requirement_ids = _parse_int_id_list(
        obligation_metrics.get("selected_requirement_ids"), "selected_requirement_ids"
    )
    allowed_reservation_cycles = {
        f"historical-obligations:g{generation.id}",
        f"historical-replay:g{generation.id}",
    }
    supplier_cycle_prefix = f"historical-supplier:g{generation.id}:"

    def _is_allowed_cycle(cycle_id: str) -> bool:
        return (
            cycle_id in allowed_reservation_cycles
            or cycle_id.startswith(supplier_cycle_prefix)
        )

    entries, events, _entry_by_id = _reservation_fold_checkpoint(
        db, generation, is_allowed_cycle=_is_allowed_cycle
    )
    _reservation_fact_conservation_checkpoint(events, visible)
    _replenishment_work_item_checkpoint(db, generation, entries)
    represented = {int(row.requirement_id) for row in entries}
    missing = sorted(selected_requirement_ids - represented)
    if missing:
        raise GenerationValidationError(
            f"selected requirements lack reservations: {missing[:10]}"
        )

    replay_realized_qty = sum(
        (
            _d(event.realized_delta)
            for event in events
            if str(event.cycle_id or "") == f"historical-replay:g{generation.id}"
        ),
        Decimal("0"),
    )
    supplier_allocated_qty = sum(
        (
            _d(event.realized_delta)
            for event in events
            if str(event.cycle_id or "").startswith(supplier_cycle_prefix)
        ),
        Decimal("0"),
    )

    fact_qty = _d(replay_metrics.get("fact_qty"))
    allocated_qty = _d(replay_metrics.get("allocated_qty"))
    surplus_qty = _d(replay_metrics.get("surplus_qty"))
    if fact_qty != allocated_qty + surplus_qty:
        raise GenerationValidationError("historical replay violates fact conservation")
    if allocated_qty != replay_realized_qty:
        raise GenerationValidationError("replay metrics disagree with reservation events")
    if _d(replay_metrics.get("ambiguous_pool_facts")) != Decimal("0"):
        raise GenerationValidationError("historical replay has unresolved planning-stock pools")

    (
        provenance,
        supplier_candidate_by_id,
        excluded_ids,
        supplier_status_counts,
    ) = _supplier_provenance_checkpoint(
        db, generation, require_full_coverage=True
    )
    supplier_relevant_ids = {
        int(row.stock_ledger_entry_id)
        for row in provenance
        if str(row.match_status or "") != "excluded_non_supplier"
    }
    supplier_physical_qty = sum(
        (_d(supplier_candidate_by_id[row_id].qty) for row_id in supplier_relevant_ids),
        Decimal("0"),
    )
    supplier_ignored_qty = sum(
        (_d(supplier_candidate_by_id[row_id].qty) for row_id in excluded_ids),
        Decimal("0"),
    )
    supplier_surplus_qty = supplier_physical_qty - supplier_allocated_qty

    bin_count = _stock_bin_fold_checkpoint(db, generation, visible)
    return {
        "ledger_generation_id": int(generation.id),
        "physical_facts": len(visible),
        "stock_bins": bin_count,
        "selected_requirements": len(selected_requirement_ids),
        "reservation_entries": len(entries),
        "reservation_events": len(events),
        "execution_allocations": {
            "facts": execution_allocations["facts"],
            "allocations": execution_allocations["allocations"],
            "fact_qty": execution_allocations["fact_qty"],
            "allocated_qty": execution_allocations["allocated_qty"],
            "surplus_qty": execution_allocations["surplus_qty"],
            "allocation_checksum": execution_allocations["allocation_checksum"],
        },
        "supplier_receipt_evidence": len(provenance),
        "supplier_receipt_ignored_count": len(excluded_ids),
        "supplier_receipt_ignored_qty": str(supplier_ignored_qty),
        "supplier_receipt_status_counts": supplier_status_counts,
        "supplier_receipt_surplus_qty": str(supplier_surplus_qty),
        "fact_qty": str(fact_qty),
        "allocated_qty": str(allocated_qty),
        "surplus_qty": str(surplus_qty),
        "valid": True,
    }


def _parent_generation_id(generation: models.LedgerGeneration) -> int | None:
    marks = dict(generation.source_watermarks or {})
    try:
        return int(marks["parent_generation_id"])
    except (KeyError, TypeError, ValueError):
        return None


def _carry_forward_parent_future_supply(
    db: Session, generation: models.LedgerGeneration
) -> dict[str, Any] | None:
    """Inherit the parent's future supply on a refresh; return None if absent.

    A physical refresh forks only the physical prefix, so without this every
    three-hour cycle publishes a generation whose ``ledger_future_supply`` is
    empty and whose purchase journal therefore reports zero ordered and zero in
    transit.  Nothing is recaptured here (that would invent evidence from a
    source this path never read); the parent's capture travels forward as-is,
    and a generation with nothing to inherit simply does not claim the
    ``future_supply`` capability.
    """
    parent_id = _parent_generation_id(generation)
    if parent_id is None:
        return None
    parent = db.get(models.LedgerGeneration, parent_id)
    if parent is None or str(parent.status) != "accepted":
        return None
    if not dict(parent.capabilities or {}).get("future_supply"):
        return None
    return dict(carry_forward_future_supply(
        db,
        parent_generation_id=int(parent.id),
        target_generation_id=int(generation.id),
    ))


def _zero_future_supply_capture(db: Session, generation: models.LedgerGeneration) -> dict[str, Any]:
    """Create a canonical zero-row future-supply proof for no-parent generations."""
    batch_key = f"future-supply-capture:g{int(generation.id)}"
    batch = db.query(models.LedgerBuildBatch).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(generation.id),
        models.LedgerBuildBatch.stage == FUTURE_SUPPLY_CAPTURE_STAGE,
        models.LedgerBuildBatch.batch_key == batch_key,
    ).one_or_none()
    if batch is None:
        batch = models.LedgerBuildBatch(
            ledger_generation_id=int(generation.id),
            stage=FUTURE_SUPPLY_CAPTURE_STAGE,
            batch_key=batch_key,
            status="building",
            algorithm_version=FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION,
            metrics={},
        )
        db.add(batch)
        db.flush()
    if str(batch.algorithm_version) != FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION:
        raise GenerationValidationError(
            "future supply proof requires the canonical capture algorithm"
        )
    if str(batch.status) == "completed":
        try:
            return dict(verify_future_supply_capture(
                db,
                int(generation.id),
                capture_batch_id=int(batch.id),
            ))
        except FutureSupplyCaptureError as exc:
            raise GenerationValidationError(
                f"future supply proof is malformed: {exc}"
            ) from exc

    try:
        metrics = replace_future_supply_capture(
            db,
            int(generation.id),
            int(batch.id),
            (),
        )
    except FutureSupplyCaptureError as exc:
        raise GenerationValidationError(
            f"future supply direct capture failed: {exc}"
        ) from exc

    batch.status = "completed"
    batch.completed_at = datetime.now(timezone.utc)
    return dict(metrics, created=True)


def _promote_accepted_generation_read_snapshots(
    db: Session,
    *,
    generation: models.LedgerGeneration,
    accepted_at: datetime,
    fixed_run_ids: tuple[int, ...],
    expected_parent_id: int | None,
) -> None:
    capabilities = dict(generation.capabilities or {})

    try:
        if capabilities.get("purchase_control_journal"):
            purchase_snapshot = promote_purchase_journal_candidate(
                db,
                generation=generation,
                accepted_at=accepted_at,
            )
            if purchase_snapshot is None:
                raise GenerationValidationError(
                    f"generation {generation.id} claims the purchase_control_journal "
                    "capability but has no journal candidate to publish"
                )
        else:
            promote_purchase_journal_candidate(
                db,
                generation=generation,
                accepted_at=accepted_at,
            )

        if capabilities.get("production_control_journal"):
            production_snapshot = promote_production_journal_candidate(
                db,
                generation=generation,
                accepted_at=accepted_at,
            )
            if production_snapshot is None:
                raise GenerationValidationError(
                    f"generation {generation.id} claims the production_control_journal "
                    "capability but has no journal candidate to publish"
                )
        else:
            promote_production_journal_candidate(
                db,
                generation=generation,
                accepted_at=accepted_at,
            )
    except (PurchaseJournalPromotionError, ProductionControlJournalPromotionError) as exc:
        raise GenerationValidationError(
            f"generation {generation.id} cannot publish read snapshots: {exc}"
        ) from exc

    publish_generation(
        db,
        generation,
        expected_parent_id=expected_parent_id,
    )

    for run_id in fixed_run_ids:
        run = db.get(models.PlanningRun, int(run_id))
        if run is None:
            continue
        if (
            int(run.ledger_generation_id or 0) != int(generation.id)
            or run.ledger_cutoff != generation.cutoff
        ):
            continue
        try:
            build_mrp_result_snapshot(db, int(run_id))
        except ValueError as exc:
            raise GenerationValidationError(
                f"MRP result snapshot for run {run_id} could not be published: {exc}"
            ) from exc


def accept_generation_build(
    db: Session,
    generation_id: int,
    *,
    replay_from: datetime,
    odata_client: Any | None = None,
    explicit_empty_physical: bool = False,
    expected_parent_id: int | None = None,
) -> dict[str, Any]:
    """Build all local projections and publish only after every gate succeeds.

    ``expected_parent_id`` defaults to the generation's own sealed
    ``parent_generation_id``: a refresh build must still descend from the
    pointer at the moment of publication, not merely at the moment of the fork.
    """
    with db.begin_nested():
        generation = _building_generation(db, generation_id)
        physical = materialize_generation_stock_bins(db, int(generation.id))
        future_supply = _carry_forward_parent_future_supply(db, generation)
        if future_supply is None:
            future_supply = _zero_future_supply_capture(db, generation)
        obligations = materialize_historical_obligations(db, int(generation.id))
        replay = run_historical_replay(
            db, int(generation.id), replay_from=replay_from
        )
        try:
            consumption_batch = models.LedgerBuildBatch(
                ledger_generation_id=int(generation.id),
                stage="execution_allocation",
                batch_key=f"g{generation.id}:execution_allocation",
                status="building",
                algorithm_version=RESERVATION_CONSUMPTION_ALGORITHM_VERSION,
                metrics={},
            )
            db.add(consumption_batch)
            db.flush()
            reservation_consumption = materialize_reservation_consumption_allocations(
                db, int(generation.id), int(consumption_batch.id)
            )
            consumption_batch.status = "completed"
            consumption_batch.metrics = reservation_consumption
            consumption_batch.completed_at = datetime.now(timezone.utc)
        except ValueError as exc:
            raise GenerationValidationError(
                f"reservation consumption allocation build failed: {exc}"
            ) from exc
        supplier_candidates = _supplier_candidates(db, int(generation.id))
        if supplier_candidates and odata_client is None:
            raise GenerationValidationError(
                "supplier document evidence requires an OData client"
            )
        extraction = (
            extract_supplier_document_evidence(
                db, odata_client, supplier_candidates
            )
            if supplier_candidates else None
        )
        if extraction is not None and extraction.diagnostics:
            first = extraction.diagnostics[0]
            raise GenerationValidationError(
                "supplier document evidence is incomplete: "
                f"{first.code} at {first.recorder_type}/"
                f"{first.recorder_ref}/{first.line_no}"
            )
        supplier = rebuild_supplier_receipt_coverage(
            db,
            ledger_generation_id=int(generation.id),
            evidence=extraction.evidence if extraction is not None else (),
            cycle_id=f"historical-supplier:g{generation.id}:accept",
        )
        _persist_non_supplier_receipt_rows(
            db,
            generation_id=int(generation.id),
            supplier_candidates=supplier_candidates,
            ignored_stock_ledger_entries=(
                tuple(
                    (
                        entry.stock_ledger_entry_id,
                        entry.operation_key,
                        entry.operation_name,
                    )
                    for entry in (
                        extraction.ignored_stock_ledger_entries
                        if extraction is not None else ()
                    )
                )
            ),
        )
        assembly_outputs = materialize_assembly_output_allocations(
            db, int(generation.id)
        )
        try:
            drum_schedule = materialize_drum_schedule(db, int(generation.id))
            shelf_projection = materialize_shelf_projections(
                db, int(generation.id)
            )
        except ValueError as exc:
            raise GenerationValidationError(
                f"canonical drum build failed: {exc}"
            ) from exc
        validation = validate_generation_build(
            db,
            int(generation.id),
            explicit_empty_physical=explicit_empty_physical,
        )
        try:
            from ..period_plan_service import (
                build_period_plan_execution_snapshots_for_generation,
            )

            planning_snapshots = (
                build_period_plan_execution_snapshots_for_generation(
                    db, int(generation.id)
                )
            )
            assembly_queue_snapshot = build_assembly_queue_snapshot(
                db, int(generation.id)
            )
        except (TypeError, ValueError) as exc:
            raise GenerationValidationError(
                f"planning read snapshot build failed: {exc}"
            ) from exc
        try:
            replenishment_batch = models.LedgerBuildBatch(
                ledger_generation_id=int(generation.id),
                stage="replenishment_work_item",
                batch_key=f"g{generation.id}:replenishment_work_item",
                status="building",
                algorithm_version=_REPLENISHMENT_WORK_ITEM_ALGORITHM_VERSION,
                metrics={},
            )
            db.add(replenishment_batch)
            db.flush()
            replenishment_work_items = materialize_replenishment_work_items(
                db, int(generation.id), int(replenishment_batch.id)
            )
            replenishment_batch.status = "completed"
            replenishment_batch.metrics = replenishment_work_items
            replenishment_batch.completed_at = datetime.now(timezone.utc)
            purchase_journal_snapshot = build_purchase_journal_candidate(
                db, int(generation.id)
            )
            fixed_run_ids = [
                int(run_id)
                for (run_id,) in db.query(models.PlanningRun.run_id)
                .filter(models.PlanningRun.status == "FIXED_SNAPSHOT")
                .order_by(models.PlanningRun.run_id.asc())
                .all()
            ]
            build_material_custody_projection(
                db, ledger_generation_id=int(generation.id)
            )
            production_journal_snapshot = build_production_journal_candidate(
                db,
                int(generation.id),
                accepted_run_ids=fixed_run_ids,
            )
        except ValueError as exc:
            raise GenerationValidationError(
                f"replenishment work item / purchase journal build failed: {exc}"
            ) from exc
        capabilities = {
            **CAPABILITIES,
            "future_supply": future_supply is not None,
        }
        generation.capabilities = dict(capabilities)
        generation.status = "accepted"
        generation.accepted_at = datetime.now(timezone.utc)
        generation.reason = None
        _promote_accepted_generation_read_snapshots(
            db,
            generation=generation,
            accepted_at=generation.accepted_at,
            fixed_run_ids=tuple(fixed_run_ids),
            expected_parent_id=(
                expected_parent_id
                if expected_parent_id is not None
                else _parent_generation_id(generation)
            ),
        )
    return {
        **validation,
        "status": "accepted",
        "capabilities": dict(capabilities),
        "future_supply": future_supply,
        "physical": physical,
        "obligations": obligations,
        "replay": replay,
        "reservation_consumption": reservation_consumption,
        "assembly_outputs": assembly_outputs,
        "drum_schedule": drum_schedule,
        "shelf_projection": shelf_projection,
        "planning_snapshots": planning_snapshots,
        "assembly_queue_snapshot_id": int(assembly_queue_snapshot.id),
        "replenishment_work_items": replenishment_work_items,
        "purchase_journal_snapshot_id": int(purchase_journal_snapshot.id),
        "production_journal_snapshot_id": int(production_journal_snapshot.id),
        "supplier_receipts": {
            "documents_fetched": (
                extraction.fetched_document_count if extraction is not None else 0
            ),
            "evidence": (
                len(extraction.evidence) if extraction is not None else 0
            ),
            "provenance": supplier.provenance_count,
            "allocations": supplier.allocation_count,
            "surplus_qty": str(supplier.surplus_qty),
            "status_counts": validation["supplier_receipt_status_counts"],
        },
    }
