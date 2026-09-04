"""Dry-run audit for assembly outputs missed by the old rebase boundary.

This module does not own output allocation.  It feeds the persisted current
queue and a corrected immutable plan-fixation boundary into the canonical
``assembly_output_persistence`` adapter, then reports the delta.  It never
writes a generation, an execution fact, a run root, or a reservation.

The mutation itself is delegated to a durable two-publication workflow:
accept the output first, then replace every affected MRP from its corrected
persisted remainder.  This module remains the read-only source of the approved
evidence and checksum.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

from sqlalchemy.orm import Session

from app import models
from app.services.item_ledger.assembly_output_persistence import (
    _canonical,
    _expected_signatures,
    _fact_provenance,
    _load_live_candidates,
    _load_visible_facts,
    _signature_decisions,
)


ZERO = Decimal("0")
ALGORITHM_VERSION = "rebase-output-repair-audit/2-durable-apply"


class RebaseOutputRepairAuditError(RuntimeError):
    """The current accepted truth cannot prove a safe repair preview."""


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def _qty(value: Any) -> str:
    number = _dec(value)
    if number == ZERO:
        return "0"
    return format(number.normalize(), "f")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _normalized_decision_signature(rows: Any) -> Any:
    """Normalize SQLite/PostgreSQL timezone spelling without hiding drift."""
    normalized = _canonical(rows)
    for row in normalized:
        evidence = row.get("evidence_payload")
        if not isinstance(evidence, dict):
            continue
        posting_at = evidence.get("posting_at")
        if isinstance(posting_at, str) and posting_at:
            parsed = datetime.fromisoformat(posting_at.replace("Z", "+00:00"))
            evidence["posting_at"] = _utc(parsed).isoformat()
    return normalized


def _accepted_generation(db: Session) -> models.LedgerGeneration:
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None or pointer.current_generation_id is None:
        raise RebaseOutputRepairAuditError("current accepted planning truth is unavailable")
    generation = db.get(models.LedgerGeneration, int(pointer.current_generation_id))
    if (
        generation is None
        or str(generation.status) != "accepted"
        or generation.cutoff is None
        or generation.physical_import_batch_id is None
        or generation.physical_import_batch is None
        or str(generation.physical_import_batch.status) != "completed"
    ):
        raise RebaseOutputRepairAuditError("current accepted Ledger generation is incomplete")
    if not bool(dict(generation.capabilities or {}).get("assembly_output_allocation")):
        raise RebaseOutputRepairAuditError(
            "current accepted Ledger generation lacks assembly-output capability"
        )
    return generation


def _unaccepted_visible_facts(db: Session, generation: models.LedgerGeneration):
    facts = _load_visible_facts(db, generation)
    accepted_ids = {
        int(value)
        for (value,) in db.query(models.ProductionPlanExecutionFact.stock_ledger_entry_id)
        .distinct()
        .all()
    }
    return tuple(
        fact for fact in facts if int(fact.stock_ledger_entry_id) not in accepted_ids
    )


def _corrected_candidates(db: Session, candidates):
    run_ids = sorted({int(row.run_id) for row in candidates})
    plan_ids = sorted({int(row.plan_id) for row in candidates})
    runs = {
        int(row.run_id): row
        for row in db.query(models.PlanningRun)
        .filter(models.PlanningRun.run_id.in_(run_ids or [0]))
        .all()
    }
    plans = {
        int(row.id): row
        for row in db.query(models.ProductionPlanHeader)
        .filter(models.ProductionPlanHeader.id.in_(plan_ids or [0]))
        .all()
    }
    if set(runs) != set(run_ids) or set(plans) != set(plan_ids):
        raise RebaseOutputRepairAuditError("assembly queue lineage is incomplete")

    corrected = []
    changed_lines: dict[int, dict[str, Any]] = {}
    for candidate in candidates:
        run = runs[int(candidate.run_id)]
        plan = plans[int(candidate.plan_id)]
        if candidate.eligible_from is None:
            raise RebaseOutputRepairAuditError(
                f"plan_line_id={int(candidate.plan_line_id)} lacks eligible_from"
            )
        if run.prior_run_id is None:
            corrected.append(candidate)
            continue
        if plan.fixed_at is None:
            raise RebaseOutputRepairAuditError(
                f"replacement plan_id={int(plan.id)} lacks immutable fixed_at"
            )
        current_boundary = _utc(candidate.eligible_from)
        original_boundary = _utc(plan.fixed_at)
        if current_boundary < original_boundary:
            raise RebaseOutputRepairAuditError(
                f"replacement plan_line_id={int(candidate.plan_line_id)} predates plan fixation"
            )
        corrected.append(replace(candidate, eligible_from=plan.fixed_at))
        if current_boundary > original_boundary:
            changed_lines[int(candidate.plan_line_id)] = {
                "plan_id": int(candidate.plan_id),
                "plan_line_id": int(candidate.plan_line_id),
                "run_id": int(candidate.run_id),
                "prior_run_id": int(run.prior_run_id),
                "item_id": int(candidate.item_id),
                "persisted_eligible_from": current_boundary.isoformat(),
                "original_plan_fixed_at": original_boundary.isoformat(),
            }
    return tuple(corrected), changed_lines


def _decision_rows_for_sle(db: Session, generation_id: int, sle_ids: set[int]):
    if not sle_ids:
        return []
    return (
        db.query(models.AssemblyOutputFactDecision)
        .filter(
            models.AssemblyOutputFactDecision.ledger_generation_id
            == int(generation_id),
            models.AssemblyOutputFactDecision.stock_ledger_entry_id.in_(
                sorted(sle_ids)
            ),
        )
        .order_by(models.AssemblyOutputFactDecision.stock_ledger_entry_id.asc())
        .all()
    )


def audit_rebase_output_repair(db: Session) -> dict[str, Any]:
    """Return an idempotent, read-only preview of missed output allocations."""

    generation = _accepted_generation(db)
    facts = _unaccepted_visible_facts(db, generation)
    candidates = _load_live_candidates(db, int(generation.id))
    corrected_candidates, changed_lines = _corrected_candidates(db, candidates)

    if not facts or not changed_lines:
        payload = {
            "algorithm_version": ALGORITHM_VERSION,
            "ledger_generation_id": int(generation.id),
            "cutoff": _utc(generation.cutoff).isoformat(),
            "status": "no_missed_rebase_outputs",
            "apply_supported": False,
            "facts": [],
            "allocations": [],
            "by_plan_item": [],
            "conservation": {
                "recoverable_fact_qty": "0",
                "allocated_qty": "0",
                "surplus_qty": "0",
                "balanced": True,
            },
        }
        return {**payload, "audit_checksum": _hash(payload)}

    provenance = _fact_provenance(db, facts, candidates)
    current_fact_rows, current_allocations, _current_metrics, _ = _expected_signatures(
        facts, candidates, provenance
    )
    persisted_decisions = _decision_rows_for_sle(
        db,
        int(generation.id),
        {int(fact.stock_ledger_entry_id) for fact in facts},
    )
    persisted_signature = _normalized_decision_signature(
        _signature_decisions(persisted_decisions)
    )
    current_signature = _normalized_decision_signature(current_fact_rows)
    if persisted_signature != current_signature:
        raise RebaseOutputRepairAuditError(
            "persisted assembly-output decisions drift from the current accepted queue: "
            f"persisted={_hash(persisted_signature)} current={_hash(current_signature)}"
        )
    if current_allocations:
        raise RebaseOutputRepairAuditError(
            "unaccepted output facts unexpectedly allocate on the persisted boundary"
        )

    repaired_provenance = _fact_provenance(db, facts, corrected_candidates)
    repaired_fact_rows, repaired_allocations, _metrics, fact_by_sle = (
        _expected_signatures(facts, corrected_candidates, repaired_provenance)
    )
    repaired_by_sle: dict[int, list[dict[str, Any]]] = {}
    for row in repaired_allocations:
        repaired_by_sle.setdefault(int(row["stock_ledger_entry_id"]), []).append(row)

    recoverable_sle_ids: set[int] = set()
    for fact in facts:
        sle_id = int(fact.stock_ledger_entry_id)
        for allocation in repaired_by_sle.get(sle_id, []):
            line = changed_lines.get(int(allocation["plan_line_id"]))
            if line is None:
                continue
            if _utc(fact.posting_at) < datetime.fromisoformat(
                line["persisted_eligible_from"]
            ) and _utc(fact.posting_at) >= datetime.fromisoformat(
                line["original_plan_fixed_at"]
            ):
                recoverable_sle_ids.add(sle_id)

    fact_rows = []
    allocation_rows = []
    for sle_id in sorted(recoverable_sle_ids):
        fact = fact_by_sle[sle_id]
        decision = next(
            row for row in repaired_fact_rows if int(row["stock_ledger_entry_id"]) == sle_id
        )
        fact_rows.append(
            {
                "stock_ledger_entry_id": sle_id,
                "item_id": int(fact.item_id),
                "posting_at": _utc(fact.posting_at).isoformat(),
                "qty": _qty(fact.qty),
                "recorder_type": str(fact.recorder_type),
                "recorder_ref": str(fact.recorder_ref),
                "source_content_hash": str(fact.source_content_hash),
                "decision_status": str(decision["decision_status"]),
                "surplus_qty": _qty(decision["surplus_qty"]),
            }
        )
        for allocation in repaired_by_sle.get(sle_id, []):
            requires_replacement = int(allocation["plan_line_id"]) in changed_lines
            allocation_rows.append(
                {
                    "stock_ledger_entry_id": sle_id,
                    "run_id": int(allocation["run_id"]),
                    "plan_id": int(allocation["plan_id"]),
                    "plan_line_id": int(allocation["plan_line_id"]),
                    "item_id": int(fact.item_id),
                    "allocated_qty": _qty(allocation["allocated_qty"]),
                    "match_rule": str(allocation["match_rule"]),
                    "requires_mrp_replacement": requires_replacement,
                }
            )

    by_plan_item: dict[tuple[int, int, int], Decimal] = {}
    for row in allocation_rows:
        key = (int(row["plan_id"]), int(row["run_id"]), int(row["item_id"]))
        by_plan_item[key] = by_plan_item.get(key, ZERO) + _dec(row["allocated_qty"])
    grouped = [
        {
            "plan_id": plan_id,
            "run_id": run_id,
            "item_id": item_id,
            "allocated_qty": _qty(qty),
        }
        for (plan_id, run_id, item_id), qty in sorted(by_plan_item.items())
    ]

    recoverable_fact_qty = sum((_dec(row["qty"]) for row in fact_rows), ZERO)
    allocated_qty = sum((_dec(row["allocated_qty"]) for row in allocation_rows), ZERO)
    surplus_by_sle = {
        int(row["stock_ledger_entry_id"]): _dec(row["surplus_qty"])
        for row in fact_rows
    }
    surplus_qty = sum(surplus_by_sle.values(), ZERO)
    balanced = recoverable_fact_qty == allocated_qty + surplus_qty
    if not balanced:
        raise RebaseOutputRepairAuditError("repair preview violates fact conservation")

    affected_plan_ids = sorted(
        {
            int(row["plan_id"])
            for row in allocation_rows
            if row["requires_mrp_replacement"]
        }
    )
    affected_run_ids = sorted(
        {
            int(row["run_id"])
            for row in allocation_rows
            if row["requires_mrp_replacement"]
        }
    )
    payload = {
        "algorithm_version": ALGORITHM_VERSION,
        "ledger_generation_id": int(generation.id),
        "cutoff": _utc(generation.cutoff).isoformat(),
        "status": "repair_preview" if allocation_rows else "no_missed_rebase_outputs",
        "apply_supported": bool(allocation_rows),
        "facts": fact_rows,
        "allocations": allocation_rows,
        "by_plan_item": grouped,
        "affected_plan_ids": affected_plan_ids,
        "affected_run_ids": affected_run_ids,
        "conservation": {
            "recoverable_fact_qty": _qty(recoverable_fact_qty),
            "allocated_qty": _qty(allocated_qty),
            "surplus_qty": _qty(surplus_qty),
            "balanced": True,
        },
        "next_step": {
            "phase_1": "canonical retain-only obligation refresh after eligible_from fix",
            "phase_2": "canonical specification rebase for each affected current live run",
            "required_audit_checksum": "filled_below",
        },
    }
    checksum = _hash(payload)
    payload["next_step"]["required_audit_checksum"] = checksum
    return {**payload, "audit_checksum": checksum}


def apply_rebase_output_repair(
    db: Session,
    *,
    audit_checksum: str,
) -> dict[str, Any]:
    """Compatibility entry point for the durable canonical repair workflow."""

    from .rebase_output_repair import (
        apply_rebase_output_repair as apply_durable_repair,
    )

    return apply_durable_repair(db, audit_checksum=audit_checksum)
