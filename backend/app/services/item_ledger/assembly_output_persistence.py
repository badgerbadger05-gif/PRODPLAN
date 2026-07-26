"""Persist assembly-output allocation decisions for a BUILDING generation.

The stage is deterministic:
- read only visible positive ``assembly_in`` facts
- allocate only against FIXED-SNAPSHOT runs and fixed headers
- sort candidates by fixed planning FIFO order
- share remaining candidate capacities across facts
- persist exact checksums and return them for repeated-run drift control
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

from sqlalchemy.orm import Session

from app import models
from app.services.item_ledger.assembly_output_core import (
    OutputFact,
    QueueCandidate,
    allocate_output_fact,
)
from app.services.item_ledger.physical_visibility import visible_sle_query

_STAGE = "assembly_output_allocation"
_ALGORITHM_VERSION = "assembly-output-allocation/1"


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _qty_text(value: Any) -> str:
    """Use one scale-independent representation for checksums and DB re-reads."""
    number = _dec(value)
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def _checksum(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(payload.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, datetime):
        return value.isoformat()
    return value


@dataclass(frozen=True)
class _OutputFact:
    stock_ledger_entry_id: int
    item_id: int
    posting_at: datetime
    qty: Decimal
    source_content_hash: str
    recorder_type: str
    recorder_ref: str
    line_no: str


def _load_visible_facts(
    db: Session,
    generation: models.LedgerGeneration,
) -> tuple[_OutputFact, ...]:
    if generation.physical_import_batch_id is None:
        raise ValueError("assembly output allocation requires physical import batch")
    if generation.cutoff is None:
        raise ValueError("assembly output allocation requires generation cutoff")

    rows = (
        visible_sle_query(
            db,
            physical_import_batch_id=int(generation.physical_import_batch_id),
            cutoff=generation.cutoff,
        )
        .filter(
            models.StockLedgerEntry.movement_kind == "assembly_in",
            models.StockLedgerEntry.qty > 0,
        )
        .order_by(
            models.StockLedgerEntry.posting_at.asc(),
            models.StockLedgerEntry.id.asc(),
        )
        .all()
    )

    return tuple(
        _OutputFact(
            stock_ledger_entry_id=int(row.id),
            item_id=int(row.item_id),
            posting_at=row.posting_at,
            qty=_dec(row.qty),
            source_content_hash=_text(row.source_content_hash),
            recorder_type=_text(row.recorder_type),
            recorder_ref=_text(row.recorder_ref),
            line_no=_text(row.line_no),
        )
        for row in rows
    )


def _load_live_candidates(db: Session, generation_id: int) -> tuple[QueueCandidate, ...]:
    rows = (
        db.query(models.PlanningRun, models.ProductionPlanHeader, models.ProductionPlanLine)
        .join(
            models.ProductionPlanHeader,
            models.PlanningRun.source_plan_id == models.ProductionPlanHeader.id,
        )
        .join(
            models.ProductionPlanLine,
            models.ProductionPlanLine.plan_id == models.ProductionPlanHeader.id,
        )
        .filter(
            models.PlanningRun.status == "FIXED_SNAPSHOT",
            models.PlanningRun.ledger_generation_id == int(generation_id),
            models.ProductionPlanHeader.status == "fixed",
            models.ProductionPlanLine.qty > 0,
            models.ProductionPlanLine.bucket_date >= models.ProductionPlanHeader.period_from,
        )
        .order_by(
            models.ProductionPlanHeader.period_from.asc(),
            models.ProductionPlanHeader.period_to.asc(),
            models.ProductionPlanHeader.id.asc(),
            models.ProductionPlanLine.id.asc(),
        )
        .all()
    )

    return tuple(
        QueueCandidate(
            plan_id=int(plan.id),
            plan_line_id=int(line.id),
            item_id=int(line.item_id),
            open_qty=_dec(line.qty),
        )
        for _run, plan, line in rows
    )


def _candidate_rows_for_fact(
    ordered_candidates: tuple[QueueCandidate, ...],
    remaining_by_line: dict[int, Decimal],
) -> tuple[QueueCandidate, ...]:
    # Keep all planned line IDs with positive remaining to get deterministic FIFO.
    ordered = []
    for candidate in ordered_candidates:
        remaining = _dec(remaining_by_line.get(candidate.plan_line_id, Decimal("0")))
        if remaining <= 0:
            continue
        ordered.append(
            QueueCandidate(
                plan_id=int(candidate.plan_id),
                plan_line_id=int(candidate.plan_line_id),
                item_id=int(candidate.item_id),
                open_qty=remaining,
            )
        )
    return tuple(ordered)


def _facts_by_sle(facts: tuple[_OutputFact, ...]) -> dict[int, _OutputFact]:
    return {int(fact.stock_ledger_entry_id): fact for fact in facts}


def _expected_signatures(
    facts: tuple[_OutputFact, ...],
    ordered_candidates: tuple[QueueCandidate, ...],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    dict[int, _OutputFact],
]:
    remaining_by_line: dict[int, Decimal] = {
        row.plan_line_id: _dec(row.open_qty) for row in ordered_candidates
    }

    fact_by_sle = _facts_by_sle(facts)

    decision_rows: list[dict[str, Any]] = []
    allocation_rows: list[dict[str, Any]] = []

    for fact in facts:
        current_candidates = _candidate_rows_for_fact(ordered_candidates, remaining_by_line)
        core_fact = OutputFact(
            stock_ledger_entry_id=fact.stock_ledger_entry_id,
            item_id=fact.item_id,
            qty=fact.qty,
            exact_plan_line_ids=(),
            link_kind="none",
        )
        decision = allocate_output_fact(core_fact, current_candidates)

        decision_rows.append(
            {
                "stock_ledger_entry_id": int(fact.stock_ledger_entry_id),
                "decision_status": _text(decision.decision_status),
                "link_kind": _text(decision.link_kind),
                "reason": _text(decision.reason),
                "source_content_hash": fact.source_content_hash,
                "surplus_qty": _qty_text(decision.surplus_qty),
                "evidence_payload": {
                    "stock_ledger_entry_id": int(fact.stock_ledger_entry_id),
                    "source_content_hash": fact.source_content_hash,
                    "recorder_type": fact.recorder_type,
                    "recorder_ref": fact.recorder_ref,
                    "line_no": fact.line_no,
                },
            }
        )

        for allocation in decision.allocations:
            remaining_by_line[allocation.plan_line_id] = max(
                _dec(remaining_by_line.get(allocation.plan_line_id, Decimal("0")))
                - _dec(allocation.qty),
                Decimal("0"),
            )
            allocation_rows.append(
                {
                    "stock_ledger_entry_id": int(allocation.stock_ledger_entry_id),
                    "plan_id": int(allocation.plan_id),
                    "plan_line_id": int(allocation.plan_line_id),
                    "allocated_qty": _qty_text(allocation.qty),
                    "match_rule": _text(allocation.match_rule),
                    "allocation_ordinal": int(allocation.allocation_ordinal),
                }
            )

    fact_qty = sum((_dec(fact.qty) for fact in facts), Decimal("0"))
    allocated_qty = sum((
        _dec(row["allocated_qty"]) for row in allocation_rows
    ), Decimal("0"))
    surplus_qty = sum((
        _dec(item["surplus_qty"]) for item in decision_rows
    ), Decimal("0"))
    if fact_qty != allocated_qty + surplus_qty:
        raise ValueError("assembly output allocation conservation failed")

    metrics: dict[str, Any] = {
        "facts": len(facts),
        "allocations": len(allocation_rows),
        "fact_qty": _qty_text(fact_qty),
        "allocated_qty": _qty_text(allocated_qty),
        "surplus_total": _qty_text(surplus_qty),
        "surplus_facts": len([row for row in decision_rows if _dec(row["surplus_qty"]) > 0]),
        "fact_checksum": _checksum(_canonical(decision_rows)),
        "allocation_checksum": _checksum(_canonical(allocation_rows)),
        "surplus_checksum": _checksum(_canonical([
            {
                "stock_ledger_entry_id": row["stock_ledger_entry_id"],
                "surplus_qty": row["surplus_qty"],
            }
            for row in decision_rows
            if _dec(row["surplus_qty"]) > 0
        ])),
    }

    normalized_fact_rows = sorted(
        _canonical(decision_rows),
        key=lambda row: int(row["stock_ledger_entry_id"]),
    )
    normalized_allocation_rows = sorted(
        _canonical(allocation_rows),
        key=lambda row: (
            int(row["stock_ledger_entry_id"]),
            int(row["plan_id"]),
            int(row["plan_line_id"]),
            int(row["allocation_ordinal"]),
        ),
    )

    return (
        normalized_fact_rows,
        normalized_allocation_rows,
        metrics,
        fact_by_sle,
    )


def _build_batch_key(generation: models.LedgerGeneration) -> str:
    return f"g{int(generation.id)}:assembly-output-allocation:{_ALGORITHM_VERSION}"


def _expected_batch_metrics(
    metrics: dict[str, Any],
    fact_signature: list[dict[str, Any]],
    allocation_signature: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        **metrics,
        "batch_version": _ALGORITHM_VERSION,
        "fact_signature": fact_signature,
        "allocation_signature": allocation_signature,
    }


def _signature_decisions(rows: list[models.AssemblyOutputFactDecision]) -> list[dict[str, Any]]:
    return sorted(
        [
            {
                "stock_ledger_entry_id": int(row.stock_ledger_entry_id),
                "decision_status": _text(row.decision_status),
                "link_kind": _text(row.link_kind),
                "reason": _text(row.reason),
                "source_content_hash": _text(row.source_content_hash),
                "surplus_qty": _qty_text(row.surplus_qty),
                "evidence_payload": _canonical(dict(row.evidence_payload or {})),
            }
            for row in rows
        ],
        key=lambda row: int(row["stock_ledger_entry_id"]),
    )


def _signature_allocations(
    rows: list[models.AssemblyOutputAllocation],
) -> list[dict[str, Any]]:
    return sorted(
        [
            {
                "stock_ledger_entry_id": int(row.stock_ledger_entry_id),
                "plan_id": int(row.plan_id),
                "plan_line_id": int(row.plan_line_id),
                "allocated_qty": _qty_text(row.allocated_qty),
                "match_rule": _text(row.match_rule),
                "allocation_ordinal": int(row.allocation_ordinal),
            }
            for row in rows
        ],
        key=lambda row: (
            int(row["stock_ledger_entry_id"]),
            int(row["plan_id"]),
            int(row["plan_line_id"]),
            int(row["allocation_ordinal"]),
        ),
    )


def _decision_payload_by_fact(current_fact: _OutputFact) -> dict[str, Any]:
    return _canonical(
        {
            "stock_ledger_entry_id": int(current_fact.stock_ledger_entry_id),
            "source_content_hash": current_fact.source_content_hash,
            "recorder_type": current_fact.recorder_type,
            "recorder_ref": current_fact.recorder_ref,
            "line_no": current_fact.line_no,
        }
    )


def materialize_assembly_output_allocations(
    db: Session,
    generation_id: int,
) -> dict[str, Any]:
    """Persist assembly-output allocations for one BUILDING Ledger generation."""

    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None:
        raise ValueError(f"ledger generation {generation_id} does not exist")
    if str(generation.status) != "building":
        raise ValueError("assembly output allocation requires BUILDING generation")

    if generation.physical_import_batch is None:
        raise ValueError("assembly output allocation requires physical import batch")
    if str(generation.physical_import_batch.status) != "completed":
        raise ValueError("assembly output allocation requires completed physical import batch")

    facts = _load_visible_facts(db, generation)
    ordered_candidates = _load_live_candidates(db, int(generation.id))

    fact_signature, allocation_signature, metrics, fact_by_sle = _expected_signatures(
        facts,
        ordered_candidates,
    )

    batch_key = _build_batch_key(generation)
    expected_batch_metrics = _expected_batch_metrics(
        metrics,
        fact_signature,
        allocation_signature,
    )

    existing_decisions = (
        db.query(models.AssemblyOutputFactDecision)
        .filter(models.AssemblyOutputFactDecision.ledger_generation_id == int(generation.id))
        .order_by(
            models.AssemblyOutputFactDecision.stock_ledger_entry_id.asc(),
            models.AssemblyOutputFactDecision.id.asc(),
        )
        .all()
    )
    existing_allocations = (
        db.query(models.AssemblyOutputAllocation)
        .filter(models.AssemblyOutputAllocation.ledger_generation_id == int(generation.id))
        .order_by(
            models.AssemblyOutputAllocation.stock_ledger_entry_id.asc(),
            models.AssemblyOutputAllocation.allocation_ordinal.asc(),
            models.AssemblyOutputAllocation.id.asc(),
        )
        .all()
    )
    existing_batch = (
        db.query(models.LedgerBuildBatch)
        .filter(
            models.LedgerBuildBatch.ledger_generation_id == int(generation.id),
            models.LedgerBuildBatch.stage == _STAGE,
            models.LedgerBuildBatch.batch_key == batch_key,
        )
        .one_or_none()
    )

    expected_fact_signature = _canonical(fact_signature)
    expected_allocation_signature = _canonical(allocation_signature)

    if existing_decisions or existing_allocations:
        if _signature_decisions(existing_decisions) != expected_fact_signature:
            raise ValueError("assembly output allocation decision drift")
        if _signature_allocations(existing_allocations) != expected_allocation_signature:
            raise ValueError("assembly output allocation allocation drift")
        if existing_batch is None:
            raise ValueError("assembly output allocation batch drift")

        persisted_batch_metrics = existing_batch.metrics or {}
        if _canonical(dict(persisted_batch_metrics)) != _canonical(
            expected_batch_metrics
        ):
            raise ValueError("assembly output allocation batch drift")

        return {
            "ledger_generation_id": int(generation.id),
            "batch_id": int(existing_batch.id),
            **metrics,
        }

    if existing_batch is not None:
        raise ValueError("assembly output allocation batch drift")

    for fact_row in fact_signature:
        fact = fact_by_sle[int(fact_row["stock_ledger_entry_id"])]
        db.add(
            models.AssemblyOutputFactDecision(
                ledger_generation_id=int(generation.id),
                stock_ledger_entry_id=int(fact_row["stock_ledger_entry_id"]),
                decision_status=_text(fact_row["decision_status"]),
                link_kind=_text(fact_row["link_kind"]),
                reason=_text(fact_row["reason"]) or None,
                evidence_payload=_decision_payload_by_fact(fact),
                source_content_hash=_text(fact_row["source_content_hash"]),
                surplus_qty=_dec(fact_row["surplus_qty"]),
            )
        )

    for alloc in allocation_signature:
        db.add(
            models.AssemblyOutputAllocation(
                ledger_generation_id=int(generation.id),
                stock_ledger_entry_id=int(alloc["stock_ledger_entry_id"]),
                plan_id=int(alloc["plan_id"]),
                plan_line_id=int(alloc["plan_line_id"]),
                allocated_qty=_dec(alloc["allocated_qty"]),
                match_rule=_text(alloc["match_rule"]),
                allocation_ordinal=int(alloc["allocation_ordinal"]),
            )
        )

    db.flush()

    batch = models.LedgerBuildBatch(
        ledger_generation_id=int(generation.id),
        stage=_STAGE,
        batch_key=batch_key,
        status="completed",
        algorithm_version=_ALGORITHM_VERSION,
        metrics=_canonical(expected_batch_metrics),
        completed_at=datetime.now(timezone.utc),
    )
    db.add(batch)
    db.flush()

    return {
        "ledger_generation_id": int(generation.id),
        "batch_id": int(batch.id),
        **metrics,
    }
