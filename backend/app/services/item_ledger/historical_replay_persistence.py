"""Generation-scoped persistence adapter for historical Item Ledger replay."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    LedgerBuildBatch,
    LedgerGeneration,
    MrpExecutionAllocation,
    MrpRequirementBucket,
    ReservationEntry,
    ReservationEvent,
    StockLedgerEntry,
)

from .historical_replay_core import Fact, Reserve, allocate_historical_facts
from .physical_visibility import visible_sles_for_generation


_ALGORITHM_VERSION = "historical-replay-persistence/1"
_SAFE_REALIZATION_KINDS = frozenset({"assembly_in", "assembly_out", "writeoff"})
_IGNORED_FACT_KINDS = frozenset({"receipt", "expense"})


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def _checksum(rows: list[dict[str, Any]]) -> str:
    raw = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fact_mode(row: StockLedgerEntry) -> str:
    return "make" if str(row.movement_kind or "") == "assembly_in" else "consume"


def run_historical_replay(db: Session, ledger_generation_id: int) -> dict[str, Any]:
    """Replay one explicit BUILDING generation and persist only scoped output."""
    generation = db.get(LedgerGeneration, int(ledger_generation_id))
    if generation is None:
        raise ValueError(f"LedgerGeneration {ledger_generation_id} not found")
    if generation.status != "building":
        raise ValueError("historical replay requires an explicit BUILDING LedgerGeneration")
    if generation.cutoff is None:
        raise ValueError("historical replay requires generation cutoff")
    if generation.physical_import_batch_id is None:
        raise ValueError("historical replay requires physical_import_batch_id")

    entries = (
        db.query(ReservationEntry)
        .filter(
            ReservationEntry.ledger_generation_id == generation.id,
            ReservationEntry.lifecycle_status == "active",
        )
        .order_by(ReservationEntry.id.asc())
        .all()
    )
    requirement_ids = [int(row.requirement_id) for row in entries]
    bucket_by_requirement: dict[int, MrpRequirementBucket] = {}
    if requirement_ids:
        for bucket in (
            db.query(MrpRequirementBucket)
            .filter(MrpRequirementBucket.requirement_id.in_(requirement_ids))
            .order_by(
                MrpRequirementBucket.requirement_id.asc(),
                MrpRequirementBucket.bucket_date.asc(),
                MrpRequirementBucket.id.asc(),
            )
            .all()
        ):
            bucket_by_requirement.setdefault(int(bucket.requirement_id), bucket)

    reserves: list[Reserve] = []
    entry_by_core_id: dict[str, ReservationEntry] = {}
    pools_by_key: dict[tuple[int, str, str, str], set[str]] = {}
    for row in entries:
        if row.run_id is None:
            raise ValueError(f"reservation {row.id} has no run lineage")
        core_id = str(int(row.id))
        bucket = bucket_by_requirement.get(int(row.requirement_id))
        reserve = Reserve(
            reserve_id=core_id,
            item_id=int(row.item_id),
            mode=str(row.realization_mode),  # validated by pure core
            reserved_qty=_decimal(row.reserved_qty),
            due_date=row.priority_period_to,
            plan_period_from=row.priority_period_from,
            plan_period_to=row.priority_period_to,
            run_id=int(row.run_id),
            requirement_id=int(row.requirement_id),
            bucket_date=bucket.bucket_date if bucket else None,
            bucket_id=int(bucket.id) if bucket else None,
            characteristic_ref=str(row.characteristic_ref or ""),
            organization_ref=str(row.organization_ref or ""),
            planning_stock_pool=str(row.planning_stock_pool or ""),
        )
        reserves.append(reserve)
        entry_by_core_id[core_id] = row
        pools_by_key.setdefault(
            (
                reserve.item_id,
                reserve.characteristic_ref,
                reserve.organization_ref,
                reserve.mode,
            ),
            set(),
        ).add(reserve.planning_stock_pool)

    candidate_rows = [
        row
        for row in visible_sles_for_generation(db, int(generation.id))
        if str(row.movement_kind or "") in (
            _SAFE_REALIZATION_KINDS | _IGNORED_FACT_KINDS
        )
        and _decimal(row.qty) != 0
    ]
    physical_rows = [
        row for row in candidate_rows
        if str(row.movement_kind or "") in _SAFE_REALIZATION_KINDS
    ]
    ignored_rows = [
        row for row in candidate_rows
        if str(row.movement_kind or "") in _IGNORED_FACT_KINDS
    ]
    facts: list[Fact] = []
    sle_by_core_id: dict[str, StockLedgerEntry] = {}
    ambiguous_pool_facts = 0
    for row in physical_rows:
        mode = _fact_mode(row)
        key = (
            int(row.item_id),
            str(row.characteristic_ref or ""),
            str(row.organization_ref or ""),
            mode,
        )
        pools = pools_by_key.get(key, set())
        if len(pools) == 1:
            pool = next(iter(pools))
        else:
            pool = f"__unresolved_pool__:{row.id}"
            ambiguous_pool_facts += 1
        core_id = str(int(row.id))
        facts.append(Fact(
            fact_id=core_id,
            item_id=int(row.item_id),
            mode=mode,  # type: ignore[arg-type]
            qty=abs(_decimal(row.qty)),
            posting_at=row.posting_at,
            characteristic_ref=str(row.characteristic_ref or ""),
            organization_ref=str(row.organization_ref or ""),
            planning_stock_pool=pool,
        ))
        sle_by_core_id[core_id] = row

    result = allocate_historical_facts(facts, reserves)
    cycle_id = f"historical-replay:g{generation.id}"
    inserted_events = 0
    inserted_allocations = 0
    allocation_rows_for_checksum: list[dict[str, Any]] = []
    for allocation in result.allocations:
        entry = entry_by_core_id[allocation.reserve_id]
        sle = sle_by_core_id[allocation.fact_id]
        idempotency_key = f"hist:g{generation.id}:sle{sle.id}:r{entry.id}"
        event = (
            db.query(ReservationEvent)
            .filter(
                ReservationEvent.ledger_generation_id == generation.id,
                ReservationEvent.idempotency_key == idempotency_key,
            )
            .one_or_none()
        )
        if event is None:
            event = ReservationEvent(
                ledger_generation_id=generation.id,
                reservation_id=entry.id,
                item_id=entry.item_id,
                characteristic_ref=entry.characteristic_ref,
                organization_ref=entry.organization_ref,
                planning_stock_pool=entry.planning_stock_pool,
                event_kind="realize",
                reserved_delta=Decimal("0"),
                realized_delta=allocation.qty,
                sle_id=sle.id,
                fact_ref=str(sle.recorder_ref or f"sle:{sle.id}"),
                fact_line_ref=str(sle.line_no or ""),
                match_rule="fifo" if allocation.match_rule == "fifo" else "pegged",
                cycle_id=cycle_id,
                idempotency_key=idempotency_key,
                event_at=sle.posting_at,
            )
            db.add(event)
            inserted_events += 1

        bucket = bucket_by_requirement.get(int(entry.requirement_id))
        fact_ref = str(sle.recorder_ref or f"sle:{sle.id}")
        fact_line_ref = str(sle.line_no or "")
        execution = (
            db.query(MrpExecutionAllocation)
            .filter(
                MrpExecutionAllocation.ledger_generation_id == generation.id,
                MrpExecutionAllocation.requirement_id == entry.requirement_id,
                MrpExecutionAllocation.bucket_id == (bucket.id if bucket else None),
                MrpExecutionAllocation.fact_type == (
                    "unlinked_production" if _fact_mode(sle) == "make" else "component_consumption"
                ),
                MrpExecutionAllocation.fact_ref == fact_ref,
                MrpExecutionAllocation.fact_line_ref == fact_line_ref,
                MrpExecutionAllocation.allocation_kind == "execution",
            )
            .one_or_none()
        )
        if execution is None:
            db.add(MrpExecutionAllocation(
                ledger_generation_id=generation.id,
                cycle_id=cycle_id,
                requirement_id=entry.requirement_id,
                bucket_id=bucket.id if bucket else None,
                fact_type="unlinked_production" if _fact_mode(sle) == "make" else "component_consumption",
                allocation_kind="execution",
                fact_ref=fact_ref,
                fact_line_ref=fact_line_ref,
                fact_date=sle.posting_at,
                allocated_qty=allocation.qty,
            ))
            inserted_allocations += 1
        allocation_rows_for_checksum.append({
            "sle_id": int(sle.id),
            "reservation_id": int(entry.id),
            "requirement_id": int(entry.requirement_id),
            "qty": str(allocation.qty),
            "rule": allocation.match_rule,
        })

    db.flush()
    for entry in entries:
        realized = (
            db.query(func.coalesce(func.sum(ReservationEvent.realized_delta), 0))
            .filter(
                ReservationEvent.ledger_generation_id == generation.id,
                ReservationEvent.reservation_id == entry.id,
            )
            .scalar()
        )
        entry.realized_qty = _decimal(realized)

    input_rows = [
        {
            "sle_id": int(row.id),
            "qty": str(abs(_decimal(row.qty))),
            "kind": row.movement_kind,
            "eligible": str(row.movement_kind or "") in _SAFE_REALIZATION_KINDS,
        }
        for row in candidate_rows
    ]
    metrics = {
        "facts": len(facts),
        "ignored_facts": len(ignored_rows),
        "ignored_fact_qty": str(sum((abs(_decimal(row.qty)) for row in ignored_rows), Decimal("0"))),
        "reservations": len(reserves),
        "allocations": len(result.allocations),
        "events_inserted": inserted_events,
        "execution_allocations_inserted": inserted_allocations,
        "fact_qty": str(result.fact_qty),
        "allocated_qty": str(result.allocated_qty),
        "unplanned_qty": str(result.unplanned_qty),
        "unplanned_facts": len(result.unplanned),
        "ambiguous_pool_facts": ambiguous_pool_facts,
        "input_checksum": _checksum(input_rows),
        "allocation_checksum": _checksum(allocation_rows_for_checksum),
    }
    batch_key = f"g{generation.id}:{generation.replay_version or _ALGORITHM_VERSION}"
    batch = (
        db.query(LedgerBuildBatch)
        .filter(
            LedgerBuildBatch.ledger_generation_id == generation.id,
            LedgerBuildBatch.stage == "reservation_replay",
            LedgerBuildBatch.batch_key == batch_key,
        )
        .one_or_none()
    )
    if batch is None:
        batch = LedgerBuildBatch(
            ledger_generation_id=generation.id,
            stage="reservation_replay",
            batch_key=batch_key,
            status="completed",
            algorithm_version=_ALGORITHM_VERSION,
            metrics=metrics,
            completed_at=datetime.now(timezone.utc),
        )
        db.add(batch)
    else:
        batch.status = "completed"
        batch.metrics = metrics
        batch.completed_at = datetime.now(timezone.utc)
    db.flush()
    return {"ledger_generation_id": int(generation.id), "batch_id": int(batch.id), **metrics}
