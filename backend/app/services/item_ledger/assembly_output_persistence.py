"""Persist assembly-output allocation decisions for a BUILDING generation.

The stage is deterministic:
- read only visible positive ``assembly_in`` facts
- allocate only against the generation's sealed live-plan scope and fixed headers
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
from app.services.item_ledger.recorder_identity import build_recorder_identity_index
from app.services.item_ledger.assembly_queue_snapshot import materialize_assembly_queue_lines

_STAGE = "assembly_output_allocation"
_ALGORITHM_VERSION = "assembly-output-allocation/2"
_BATCH_INTERNAL_KEYS = {"batch_version", "fact_signature", "allocation_signature"}


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


@dataclass(frozen=True)
class _FactProvenance:
    exact_plan_line_ids: tuple[int, ...] = ()
    link_kind: str = "none"
    status: str = "none"
    reason: str | None = None
    exact_product_ids: tuple[int, ...] = ()


def _load_visible_facts(
    db: Session,
    generation: models.LedgerGeneration,
) -> tuple[_OutputFact, ...]:
    if generation.physical_import_batch_id is None:
        raise ValueError("assembly output allocation requires physical import batch")
    if generation.cutoff is None:
        raise ValueError("assembly output allocation requires generation cutoff")

    current_generation_allocated_sle = db.query(
        models.AssemblyOutputAllocation.stock_ledger_entry_id
    ).filter(
        models.AssemblyOutputAllocation.ledger_generation_id == int(generation.id)
    )
    already_accepted_sle = db.query(
        models.ProductionPlanExecutionFact.stock_ledger_entry_id
    ).filter(
        ~models.ProductionPlanExecutionFact.stock_ledger_entry_id.in_(
            current_generation_allocated_sle
        )
    )
    rows = (
        visible_sle_query(
            db,
            physical_import_batch_id=int(generation.physical_import_batch_id),
            cutoff=generation.cutoff,
        )
        .filter(
            models.StockLedgerEntry.movement_kind == "assembly_in",
            models.StockLedgerEntry.qty > 0,
            ~models.StockLedgerEntry.id.in_(already_accepted_sle),
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
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None:
        raise ValueError(f"ledger generation {generation_id} does not exist")
    rows = (
        db.query(models.AssemblyQueueLine)
        .filter(
            models.AssemblyQueueLine.ledger_generation_id == int(generation.id),
        )
        .order_by(
            models.AssemblyQueueLine.sort_key.asc(),
            models.AssemblyQueueLine.id.asc(),
        )
        .all()
    )

    candidates: list[QueueCandidate] = []
    for row in rows:
        eligible_from = row.eligible_from
        if eligible_from is None:
            raise ValueError(
                f"assembly queue line {int(row.plan_line_id)} lacks frozen eligible_from"
            )

        candidates.append(
            QueueCandidate(
                run_id=int(row.planning_run_id),
                plan_id=int(row.plan_id),
                plan_line_id=int(row.plan_line_id),
                item_id=int(row.item_id),
                open_qty=_dec(row.assembly_remaining_qty),
                eligible_from=eligible_from,
            )
        )

    return tuple(candidates)


def _fact_provenance(
    db: Session,
    facts: tuple[_OutputFact, ...],
    candidates: tuple[QueueCandidate, ...],
) -> dict[int, _FactProvenance]:
    """Resolve auditable command -> product -> top-level plan lineage.

    ``ProductionManufacture`` is exact to ``ProductionProduct``. A product is
    exact to a plan line only when it names a level-0 MRP requirement and that
    run has one eligible live candidate line for the same item. Older/direct
    1C orders do not retain a plan-line id and remain FIFO fallback.
    """
    recorder_index = build_recorder_identity_index(
        db, [_text(fact.recorder_ref) for fact in facts]
    )
    product_ids = sorted(
        {
            int(product_id)
            for values in recorder_index.exact_product_ids.values()
            for product_id in values
        }
    )
    products = {
        int(row.product_id): row
        for row in (
            db.query(models.ProductionProduct)
            .filter(models.ProductionProduct.product_id.in_(product_ids))
            .all()
            if product_ids
            else ()
        )
    }
    requirement_ids = sorted(
        {
            int(row.source_mrp_requirement_id)
            for row in products.values()
            if row.source_mrp_requirement_id is not None
        }
    )
    requirements = {
        int(row.id): row
        for row in (
            db.query(models.MrpRequirement)
            .filter(models.MrpRequirement.id.in_(requirement_ids))
            .all()
            if requirement_ids
            else ()
        )
    }
    run_ids = sorted({int(row.run_id) for row in requirements.values()})
    runs = {
        int(row.run_id): row
        for row in (
            db.query(models.PlanningRun)
            .filter(models.PlanningRun.run_id.in_(run_ids))
            .all()
            if run_ids
            else ()
        )
    }
    candidate_ids_by_plan_item: dict[tuple[int, int], list[int]] = {}
    for candidate in candidates:
        candidate_ids_by_plan_item.setdefault(
            (int(candidate.plan_id), int(candidate.item_id)), []
        ).append(int(candidate.plan_line_id))

    result: dict[int, _FactProvenance] = {}
    for fact in facts:
        exact_product_ids = tuple(
            sorted(
                int(value)
                for value in recorder_index.exact_product_ids.get(
                    _text(fact.recorder_ref), ()
                )
            )
        )
        if not exact_product_ids:
            result[int(fact.stock_ledger_entry_id)] = _FactProvenance()
            continue
        if len(exact_product_ids) != 1:
            result[int(fact.stock_ledger_entry_id)] = _FactProvenance(
                link_kind="exact_plan_line",
                status="ambiguous",
                reason="recorder resolves to multiple production products",
                exact_product_ids=exact_product_ids,
            )
            continue

        product = products.get(exact_product_ids[0])
        if product is None or int(product.item_id) != int(fact.item_id):
            result[int(fact.stock_ledger_entry_id)] = _FactProvenance(
                link_kind="exact_plan_line",
                status="invalid",
                reason="exact production product is missing or has another item",
                exact_product_ids=exact_product_ids,
            )
            continue
        if product.source_mrp_requirement_id is None:
            result[int(fact.stock_ledger_entry_id)] = _FactProvenance(
                link_kind="planned_order",
                exact_product_ids=exact_product_ids,
            )
            continue

        requirement = requirements.get(int(product.source_mrp_requirement_id))
        if requirement is None:
            result[int(fact.stock_ledger_entry_id)] = _FactProvenance(
                link_kind="exact_plan_line",
                status="invalid",
                reason="exact production product points to a missing MRP requirement",
                exact_product_ids=exact_product_ids,
            )
            continue
        if int(requirement.bom_level or 0) != 0:
            # Exact to component replenishment, not to top-level plan output.
            result[int(fact.stock_ledger_entry_id)] = _FactProvenance(
                link_kind="planned_order",
                exact_product_ids=exact_product_ids,
            )
            continue

        run = runs.get(int(requirement.run_id))
        plan_id = (
            int(run.source_plan_id)
            if run is not None and run.source_plan_id is not None
            else None
        )
        line_ids = (
            tuple(
                sorted(
                    candidate_ids_by_plan_item.get(
                        (int(plan_id), int(fact.item_id)), ()
                    )
                )
            )
            if plan_id is not None
            else ()
        )
        if len(line_ids) == 1:
            result[int(fact.stock_ledger_entry_id)] = _FactProvenance(
                exact_plan_line_ids=line_ids,
                link_kind="exact_plan_line",
                status="exact",
                exact_product_ids=exact_product_ids,
            )
        elif len(line_ids) > 1:
            result[int(fact.stock_ledger_entry_id)] = _FactProvenance(
                exact_plan_line_ids=line_ids,
                link_kind="exact_plan_line",
                # All candidates come from the one plan selected by the exact
                # top-level requirement.  Their order is the frozen assembly
                # queue order, so this is deterministic addressed allocation,
                # not ambiguous provenance.
                status="exact",
                exact_product_ids=exact_product_ids,
            )
        else:
            result[int(fact.stock_ledger_entry_id)] = _FactProvenance(
                link_kind="exact_plan_line",
                status="invalid",
                reason="top-level MRP requirement has no eligible live plan line",
                exact_product_ids=exact_product_ids,
            )
    return result


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
                run_id=int(candidate.run_id),
                plan_id=int(candidate.plan_id),
                plan_line_id=int(candidate.plan_line_id),
                item_id=int(candidate.item_id),
                open_qty=remaining,
                eligible_from=candidate.eligible_from,
            )
        )
    return tuple(ordered)


def _facts_by_sle(facts: tuple[_OutputFact, ...]) -> dict[int, _OutputFact]:
    return {int(fact.stock_ledger_entry_id): fact for fact in facts}


def _expected_signatures(
    facts: tuple[_OutputFact, ...],
    ordered_candidates: tuple[QueueCandidate, ...],
    provenance_by_sle: dict[int, _FactProvenance],
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
        provenance = provenance_by_sle[int(fact.stock_ledger_entry_id)]
        core_fact = OutputFact(
            stock_ledger_entry_id=fact.stock_ledger_entry_id,
            item_id=fact.item_id,
            qty=fact.qty,
            exact_plan_line_ids=provenance.exact_plan_line_ids,
            link_kind=provenance.link_kind,
            posting_at=fact.posting_at,
            provenance_status=provenance.status,
            provenance_reason=provenance.reason,
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
                "evidence_payload": _decision_payload_by_fact(
                    fact, provenance
                ),
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
                    "run_id": int(allocation.run_id),
                    "plan_id": int(allocation.plan_id),
                    "plan_line_id": int(allocation.plan_line_id),
                    "allocated_qty": _qty_text(allocation.qty),
                    "match_rule": _text(allocation.match_rule),
                    "allocation_ordinal": int(allocation.allocation_ordinal),
                }
            )

    decision_by_sle = {
        int(row["stock_ledger_entry_id"]): row for row in decision_rows
    }
    allocated_by_sle: dict[int, Decimal] = {}
    allocated_by_line: dict[int, Decimal] = {}
    for row in allocation_rows:
        sle_id = int(row["stock_ledger_entry_id"])
        line_id = int(row["plan_line_id"])
        allocated_by_sle[sle_id] = (
            allocated_by_sle.get(sle_id, Decimal("0"))
            + _dec(row["allocated_qty"])
        )
        allocated_by_line[line_id] = (
            allocated_by_line.get(line_id, Decimal("0"))
            + _dec(row["allocated_qty"])
        )
    for fact in facts:
        sle_id = int(fact.stock_ledger_entry_id)
        if (
            allocated_by_sle.get(sle_id, Decimal("0"))
            + _dec(decision_by_sle[sle_id]["surplus_qty"])
            != _dec(fact.qty)
        ):
            raise ValueError(
                f"assembly output fact conservation failed for SLE {sle_id}"
            )
    initial_by_line = {
        int(row.plan_line_id): _dec(row.open_qty) for row in ordered_candidates
    }
    if any(
        qty > initial_by_line.get(line_id, Decimal("0"))
        for line_id, qty in allocated_by_line.items()
    ):
        raise ValueError("assembly output plan-line conservation failed")

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
        "exact_allocations": sum(
            1 for row in allocation_rows if row["match_rule"] == "exact"
        ),
        "fifo_allocations": sum(
            1 for row in allocation_rows if row["match_rule"] == "fifo"
        ),
        "ambiguous_facts": sum(
            1 for row in decision_rows if row["decision_status"] == "ambiguous"
        ),
        "invalid_facts": sum(
            1 for row in decision_rows if row["decision_status"] == "invalid"
        ),
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
                "run_id": int(row.run_id or 0),
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
            int(row["run_id"]),
            int(row["plan_id"]),
            int(row["plan_line_id"]),
            int(row["allocation_ordinal"]),
        ),
    )


def _verify_existing_batch_replay(
    facts: tuple[_OutputFact, ...],
    existing_decisions: list[models.AssemblyOutputFactDecision],
    existing_allocations: list[models.AssemblyOutputAllocation],
    existing_batch: models.LedgerBuildBatch | None,
) -> bool:
    if existing_batch is None:
        return False

    if existing_batch.metrics is None:
        return False

    batch_metrics = _canonical(dict(existing_batch.metrics))
    if _canonical(_signature_decisions(existing_decisions)) != _canonical(
        batch_metrics.get("fact_signature", [])
    ):
        return False
    if _canonical(_signature_allocations(existing_allocations)) != _canonical(
        batch_metrics.get("allocation_signature", [])
    ):
        return False

    facts_by_sle = _facts_by_sle(facts)
    allocations_by_sle: dict[int, Decimal] = {}
    for row in existing_allocations:
        sle_id = int(row.stock_ledger_entry_id)
        allocations_by_sle[sle_id] = allocations_by_sle.get(sle_id, Decimal("0")) + _dec(
            row.allocated_qty
        )

    decision_rows = _signature_decisions(existing_decisions)
    if len(decision_rows) != len(facts_by_sle):
        return False
    for decision_row in decision_rows:
        sle_id = int(decision_row["stock_ledger_entry_id"])
        fact = facts_by_sle.get(sle_id)
        if fact is None:
            return False
        if _text(fact.source_content_hash) != _text(decision_row["source_content_hash"]):
            return False
        if _dec(fact.qty) != _dec(decision_row["surplus_qty"]) + _dec(
            allocations_by_sle.get(sle_id, Decimal("0"))
        ):
            return False

    return True


def _public_batch_metrics(raw_metrics: Any) -> dict[str, Any]:
    return {
        key: value
        for key, value in (_canonical(dict(raw_metrics or {}))).items()
        if key not in _BATCH_INTERNAL_KEYS
    }


def _decision_payload_by_fact(
    current_fact: _OutputFact,
    provenance: _FactProvenance,
) -> dict[str, Any]:
    return _canonical(
        {
            "stock_ledger_entry_id": int(current_fact.stock_ledger_entry_id),
            "source_content_hash": current_fact.source_content_hash,
            "recorder_type": current_fact.recorder_type,
            "recorder_ref": current_fact.recorder_ref,
            "line_no": current_fact.line_no,
            "posting_at": current_fact.posting_at,
            "exact_product_ids": list(provenance.exact_product_ids),
            "exact_plan_line_ids": list(provenance.exact_plan_line_ids),
            "provenance_status": provenance.status,
            "provenance_reason": provenance.reason,
        }
    )


def _persist_rows(
    db: Session,
    generation: models.LedgerGeneration,
    fact_signature: list[dict[str, Any]],
    allocation_signature: list[dict[str, Any]],
    fact_by_sle: dict[int, _OutputFact],
    provenance_by_sle: dict[int, _FactProvenance],
) -> None:
    for fact_row in fact_signature:
        fact = fact_by_sle[int(fact_row["stock_ledger_entry_id"])]
        db.add(
            models.AssemblyOutputFactDecision(
                ledger_generation_id=int(generation.id),
                stock_ledger_entry_id=int(fact_row["stock_ledger_entry_id"]),
                decision_status=_text(fact_row["decision_status"]),
                link_kind=_text(fact_row["link_kind"]),
                reason=_text(fact_row["reason"]) or None,
                evidence_payload=_decision_payload_by_fact(
                    fact,
                    provenance_by_sle[int(fact.stock_ledger_entry_id)],
                ),
                source_content_hash=_text(fact_row["source_content_hash"]),
                surplus_qty=_dec(fact_row["surplus_qty"]),
            )
        )

    for alloc in allocation_signature:
        db.add(
            models.AssemblyOutputAllocation(
                ledger_generation_id=int(generation.id),
                stock_ledger_entry_id=int(alloc["stock_ledger_entry_id"]),
                run_id=int(alloc["run_id"]),
                plan_id=int(alloc["plan_id"]),
                plan_line_id=int(alloc["plan_line_id"]),
                allocated_qty=_dec(alloc["allocated_qty"]),
                match_rule=_text(alloc["match_rule"]),
                allocation_ordinal=int(alloc["allocation_ordinal"]),
            )
        )

        line = db.get(models.ProductionPlanLine, int(alloc["plan_line_id"]))
        root = (
            db.query(models.MrpRunRoot)
            .filter(
                models.MrpRunRoot.run_id == int(alloc["run_id"]),
                models.MrpRunRoot.plan_line_id == int(alloc["plan_line_id"]),
            )
            .with_for_update()
            .one_or_none()
        )
        if line is None:
            raise ValueError("assembly allocation references missing plan line")
        if root is None:
            planned = max(_dec(line.qty), Decimal("0"))
            accepted = max(_dec(line.accepted_output_qty), Decimal("0"))
            remaining = (
                _dec(line.remaining_output_qty)
                if line.remaining_output_qty is not None
                else max(planned - accepted, Decimal("0"))
            )
            root = models.MrpRunRoot(
                run_id=int(alloc["run_id"]),
                plan_line_id=int(alloc["plan_line_id"]),
                planned_qty=remaining,
                accepted_qty=Decimal("0"),
                remaining_qty=remaining,
            )
            db.add(root)
            line.accepted_output_qty = accepted
            line.remaining_output_qty = remaining
            db.flush()
        qty = _dec(alloc["allocated_qty"])
        if qty > _dec(root.remaining_qty) or qty > _dec(line.remaining_output_qty):
            raise ValueError("assembly allocation exceeds persisted execution remainder")
        db.add(models.ProductionPlanExecutionFact(
            stock_ledger_entry_id=int(alloc["stock_ledger_entry_id"]),
            plan_id=int(alloc["plan_id"]),
            plan_line_id=int(alloc["plan_line_id"]),
            run_id=int(alloc["run_id"]),
            allocated_qty=qty,
            match_rule=_text(alloc["match_rule"]),
            accepted_at=generation.cutoff or datetime.now(timezone.utc),
        ))
        line.accepted_output_qty = _dec(line.accepted_output_qty) + qty
        line.remaining_output_qty = max(_dec(line.remaining_output_qty) - qty, Decimal("0"))
        root.accepted_qty = _dec(root.accepted_qty) + qty
        root.remaining_qty = max(_dec(root.remaining_qty) - qty, Decimal("0"))

    db.flush()


def _apply_allocations_to_assembly_queue(
    db: Session,
    generation: models.LedgerGeneration,
    allocation_signature: list[dict[str, Any]],
) -> None:
    if not allocation_signature:
        return

    allocated_by_line: dict[int, Decimal] = {}
    for row in allocation_signature:
        line_id = int(row["plan_line_id"])
        allocated_by_line[line_id] = allocated_by_line.get(line_id, Decimal("0")) + _dec(
            row["allocated_qty"]
        )

    rows = (
        db.query(models.AssemblyQueueLine)
        .filter(
            models.AssemblyQueueLine.ledger_generation_id == int(generation.id),
            models.AssemblyQueueLine.plan_line_id.in_(allocated_by_line.keys()),
            models.AssemblyQueueLine.line_status == "open",
        )
        .all()
    )
    by_line = {int(row.plan_line_id): row for row in rows}
    for line_id, allocated_qty in sorted(allocated_by_line.items()):
        if line_id not in by_line:
            raise ValueError(
                "allocation references plan line not materialized in assembly queue"
            )

        row = by_line[line_id]
        new_accepted = max(_dec(row.accepted_plan_output_qty), _dec(allocated_qty))
        row.accepted_plan_output_qty = new_accepted
        row.assembly_remaining_qty = max(
            _dec(row.planned_output_qty) - new_accepted,
            Decimal("0"),
        )
        row.line_status = (
            "fulfilled"
            if _dec(row.assembly_remaining_qty) == Decimal("0")
            else "open"
        )

    db.flush()


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
    materialize_assembly_queue_lines(db, int(generation.id))

    facts = _load_visible_facts(db, generation)
    batch_key = _build_batch_key(generation)
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

    if (
        existing_batch is not None
        and not existing_decisions
        and not existing_allocations
    ):
        rows = (
            db.query(models.AssemblyQueueLine)
            .filter(
                models.AssemblyQueueLine.ledger_generation_id == int(generation.id),
                models.AssemblyQueueLine.line_status == "open",
            )
            .all()
        )
        for row in rows:
            row.accepted_plan_output_qty = Decimal("0")
            row.assembly_remaining_qty = _dec(row.planned_output_qty)
            row.line_status = "open"
        if rows:
            db.flush()

    if _verify_existing_batch_replay(
        facts=facts,
        existing_decisions=existing_decisions,
        existing_allocations=existing_allocations,
        existing_batch=existing_batch,
    ):
        if existing_batch is None:
            raise ValueError("assembly output allocation batch drift")
        return {
            "ledger_generation_id": int(generation.id),
            "batch_id": int(existing_batch.id),
            **_public_batch_metrics(existing_batch.metrics),
        }

    ordered_candidates = _load_live_candidates(db, int(generation.id))
    provenance_by_sle = _fact_provenance(db, facts, ordered_candidates)

    fact_signature, allocation_signature, metrics, fact_by_sle = _expected_signatures(
        facts,
        ordered_candidates,
        provenance_by_sle,
    )

    expected_batch_metrics = _expected_batch_metrics(
        metrics,
        fact_signature,
        allocation_signature,
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
            **_public_batch_metrics(expected_batch_metrics),
        }

    if existing_batch is not None:
        # A resumed refresh replays this stage from the top, so the batch this
        # very generation already completed is a success — not drift — provided
        # the rebuilt set still matches the metrics it recorded.  The frequent
        # case is a close with no assembly facts at all: the pass writes neither
        # a decision nor an allocation, and the old unconditional raise made it
        # reject the empty batch it had written itself, killing every resume
        # past this stage.  Anything the interrupted worker never got to write
        # is written now; a real content change still fails on the metrics.
        if _canonical(dict(existing_batch.metrics or {})) != _canonical(
            expected_batch_metrics
        ):
            raise ValueError("assembly output allocation batch drift")
        _persist_rows(
            db,
            generation,
            fact_signature,
            allocation_signature,
            fact_by_sle,
            provenance_by_sle,
        )
        _apply_allocations_to_assembly_queue(db, generation, allocation_signature)
        return {
            "ledger_generation_id": int(generation.id),
            "batch_id": int(existing_batch.id),
            **metrics,
        }

    _persist_rows(
        db,
        generation,
        fact_signature,
        allocation_signature,
        fact_by_sle,
        provenance_by_sle,
    )
    _apply_allocations_to_assembly_queue(db, generation, allocation_signature)

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
