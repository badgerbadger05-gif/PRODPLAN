"""Build, validate and atomically publish one explicit Ledger generation.

No external reads happen here.  Every input is an already persisted physical
prefix or frozen planning obligation.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func
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
    bucket_capacity_for_mode,
    run_historical_replay,
)
from .physical import canonical_content_hash
from .physical_visibility import visible_sles_for_generation
from .supplier_receipt_allocation import rebuild_supplier_receipt_coverage
from .supplier_receipt_odata import extract_supplier_document_evidence


CAPABILITIES = {
    "physical_ledger": True,
    "reservation_replay": True,
    "execution_allocations": True,
    "supplier_receipt_coverage": True,
    "planning_snapshots": True,
}
_SUPPLIER_DOCUMENT_TYPES = frozenset({
    "Document_ПриходнаяНакладная",
    "Document_КорректировкаПоступления",
    "Document_РасходнаяНакладная",
})
_SAFE_FACT_MODE = {
    "linked_production": "make",
    "unlinked_production": "make",
    "component_consumption": "consume",
}


class GenerationValidationError(ValueError):
    pass


def _d(value: Any) -> Decimal:
    return Decimal(str(value or 0))


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


def _supplier_candidates(
    db: Session, generation_id: int
) -> tuple[models.StockLedgerEntry, ...]:
    return tuple(
        row for row in visible_sles_for_generation(db, generation_id)
        if str(row.recorder_type or "") in _SUPPLIER_DOCUMENT_TYPES
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


def validate_generation_build(
    db: Session,
    generation_id: int,
    *,
    explicit_empty_physical: bool = False,
) -> dict[str, Any]:
    """Dry, read-only validation.  Raises before truth publication on any gap."""
    generation = _building_generation(db, generation_id)
    physical_batch = db.get(
        models.PhysicalImportBatch, int(generation.physical_import_batch_id)
    )
    if physical_batch is None or str(physical_batch.status) != "completed":
        raise GenerationValidationError("physical import boundary is not completed")
    if physical_batch.cutoff and physical_batch.cutoff > generation.cutoff:
        raise GenerationValidationError("physical import boundary exceeds generation cutoff")
    _validate_historical_bootstrap_watermarks(generation)
    partial_physical = db.query(models.LedgerBuildBatch.id).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(generation.id),
        models.LedgerBuildBatch.stage == "physical_import",
        models.LedgerBuildBatch.status != "completed",
    ).first()
    if partial_physical:
        raise GenerationValidationError("physical import has incomplete checkpoints")

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
    obligation_metrics = dict(obligation_batch.metrics or {})
    replay_metrics = dict(replay_batch.metrics or {})
    if obligation_batch.algorithm_version != OBLIGATION_ALGORITHM_VERSION:
        raise GenerationValidationError("unexpected historical obligation algorithm")
    if replay_batch.algorithm_version != REPLAY_ALGORITHM_VERSION:
        raise GenerationValidationError("unexpected historical replay algorithm")
    selected_requirement_ids = _parse_int_id_list(
        obligation_metrics.get("selected_requirement_ids"), "selected_requirement_ids"
    )
    entries = db.query(models.ReservationEntry).filter(
        models.ReservationEntry.ledger_generation_id == int(generation.id)
    ).all()
    represented = {int(row.requirement_id) for row in entries}
    missing = sorted(selected_requirement_ids - represented)
    if missing:
        raise GenerationValidationError(
            f"selected requirements lack reservations: {missing[:10]}"
        )

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
        if not str(event.cycle_id or "").startswith(
            ("historical-obligations:g", "historical-replay:g")
        ):
            raise GenerationValidationError("legacy reservation event entered generation build")
        reserved, realized = event_sums[int(event.reservation_id)]
        event_sums[int(event.reservation_id)] = (
            reserved + _d(event.reserved_delta),
            realized + _d(event.realized_delta),
        )
    realized_by_req_mode: dict[tuple[int, str], Decimal] = defaultdict(Decimal)
    for entry in entries:
        reserved, realized = event_sums[int(entry.id)]
        if reserved != _d(entry.reserved_qty) or realized != _d(entry.realized_qty):
            raise GenerationValidationError(
                f"reservation {entry.id} cache differs from event fold"
            )
        realized_by_req_mode[
            (int(entry.requirement_id), str(entry.realization_mode))
        ] += realized

    bucket_by_id = {
        int(row.id): row
        for row in db.query(models.MrpRequirementBucket).filter(
            models.MrpRequirementBucket.requirement_id.in_(
                sorted(selected_requirement_ids)
            )
        ).all()
    } if selected_requirement_ids else {}
    bucketed_requirement_ids = {int(row.requirement_id) for row in bucket_by_id.values()}
    make_requirement_ids = {
        int(entry.requirement_id)
        for entry in entries
        if str(entry.realization_mode).lower() == "make"
    }
    legacy_unphased_requirement_ids = _parse_int_id_list(
        obligation_metrics.get("legacy_net_phasing_requirement_ids"),
        "legacy_net_phasing_requirement_ids",
    )
    legacy_unphased_requirement_ids &= make_requirement_ids
    allocation_by_req_mode: dict[tuple[int, str], Decimal] = defaultdict(Decimal)
    bucket_mode_qty: dict[tuple[int, str], Decimal] = defaultdict(Decimal)
    supplier_allocated_qty = Decimal("0")
    allocations = db.query(models.MrpExecutionAllocation).filter(
        models.MrpExecutionAllocation.ledger_generation_id == int(generation.id)
    ).all()
    for allocation in allocations:
        if (
            str(allocation.fact_type or "") == "supplier_receipt"
            and str(allocation.allocation_kind or "") == "coverage_realization"
        ):
            if not str(allocation.cycle_id or "").startswith(
                f"historical-supplier:g{generation.id}:"
            ):
                raise GenerationValidationError(
                    "supplier allocation lacks generation build lineage"
                )
            if int(allocation.requirement_id) not in selected_requirement_ids:
                raise GenerationValidationError(
                    "supplier allocation escapes selected obligations"
                )
            supplier_allocated_qty += _d(allocation.allocated_qty)
            continue
        if not str(allocation.cycle_id or "").startswith("historical-replay:g"):
            raise GenerationValidationError("legacy execution allocation entered generation build")
        mode = _SAFE_FACT_MODE.get(str(allocation.fact_type or ""))
        if mode is None or str(allocation.allocation_kind or "") != "execution":
            raise GenerationValidationError("unsafe or legacy execution allocation")
        requirement_id = int(allocation.requirement_id)
        if requirement_id not in selected_requirement_ids:
            raise GenerationValidationError("execution allocation escapes selected obligations")
        qty = _d(allocation.allocated_qty)
        allocation_by_req_mode[(requirement_id, mode)] += qty
        if allocation.bucket_id is not None:
            bucket = bucket_by_id.get(int(allocation.bucket_id))
            if bucket is None or int(bucket.requirement_id) != requirement_id:
                raise GenerationValidationError("allocation bucket escapes requirement")
            bucket_mode_qty[(int(bucket.id), mode)] += qty
        else:
            if requirement_id in bucketed_requirement_ids and (
                mode != "make"
                or requirement_id not in legacy_unphased_requirement_ids
            ):
                raise GenerationValidationError(
                    "unphased execution allocation requires legacy net-phasing flag"
                )
    if dict(allocation_by_req_mode) != {
        key: qty for key, qty in realized_by_req_mode.items() if qty != 0
    }:
        raise GenerationValidationError("bucket allocation sums differ from realized events")
    for (bucket_id, _mode), qty in bucket_mode_qty.items():
        bucket = bucket_by_id[bucket_id]
        capacity = bucket_capacity_for_mode(bucket, _mode)
        if qty > capacity:
            raise GenerationValidationError("bucket allocation exceeds frozen capacity")

    fact_qty = _d(replay_metrics.get("fact_qty"))
    allocated_qty = _d(replay_metrics.get("allocated_qty"))
    unplanned_qty = _d(replay_metrics.get("unplanned_qty"))
    if fact_qty != allocated_qty + unplanned_qty:
        raise GenerationValidationError("historical replay violates fact conservation")
    if allocated_qty != sum(allocation_by_req_mode.values(), Decimal("0")):
        raise GenerationValidationError("replay metrics disagree with persisted allocations")
    if _d(replay_metrics.get("ambiguous_pool_facts")) != Decimal("0"):
        raise GenerationValidationError("historical replay has unresolved planning-stock pools")
    if _d(replay_metrics.get("ambiguous_identity_facts")) != Decimal("0"):
        raise GenerationValidationError("historical replay has unresolved provenance identities")

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
    if provenance_ids != supplier_physical_ids:
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
    supplier_unplanned_qty = supplier_physical_qty - supplier_allocated_qty

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
    bin_count = len(bins)
    return {
        "ledger_generation_id": int(generation.id),
        "physical_facts": len(visible),
        "stock_bins": bin_count,
        "selected_requirements": len(selected_requirement_ids),
        "reservation_entries": len(entries),
        "reservation_events": len(events),
        "execution_allocations": len(allocations),
        "supplier_receipt_evidence": len(provenance),
        "supplier_receipt_ignored_count": len(excluded_ids),
        "supplier_receipt_ignored_qty": str(supplier_ignored_qty),
        "supplier_receipt_status_counts": supplier_status_counts,
        "supplier_receipt_unplanned_qty": str(supplier_unplanned_qty),
        "fact_qty": str(fact_qty),
        "allocated_qty": str(allocated_qty),
        "unplanned_qty": str(unplanned_qty),
        "valid": True,
    }


def accept_generation_build(
    db: Session,
    generation_id: int,
    *,
    replay_from: datetime,
    odata_client: Any | None = None,
    explicit_empty_physical: bool = False,
) -> dict[str, Any]:
    """Build all local projections and publish only after every gate succeeds."""
    with db.begin_nested():
        generation = _building_generation(db, generation_id)
        physical = materialize_generation_stock_bins(db, int(generation.id))
        obligations = materialize_historical_obligations(db, int(generation.id))
        replay = run_historical_replay(
            db, int(generation.id), replay_from=replay_from
        )
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
        validation = validate_generation_build(
            db,
            int(generation.id),
            explicit_empty_physical=explicit_empty_physical,
        )
        generation.capabilities = dict(CAPABILITIES)
        generation.status = "accepted"
        generation.accepted_at = datetime.now(timezone.utc)
        generation.reason = None
        publish_generation(db, generation)
    return {
        **validation,
        "status": "accepted",
        "capabilities": dict(CAPABILITIES),
        "physical": physical,
        "obligations": obligations,
        "replay": replay,
        "supplier_receipts": {
            "documents_fetched": (
                extraction.fetched_document_count if extraction is not None else 0
            ),
            "evidence": (
                len(extraction.evidence) if extraction is not None else 0
            ),
            "provenance": supplier.provenance_count,
            "allocations": supplier.allocation_count,
            "unplanned_qty": str(supplier.unplanned_qty),
            "status_counts": validation["supplier_receipt_status_counts"],
        },
    }
