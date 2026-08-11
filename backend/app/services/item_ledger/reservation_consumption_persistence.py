"""Persist canonical §16 consumption assignments for one building generation.

Physical ``assembly_out`` expenses are assigned exactly once to the frozen
reservation pool which owned the stock.  The pure allocator owns the addressed
then FIFO rule; this adapter owns only immutable generation evidence, pool
qualification and replay/drift checks.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

from sqlalchemy.orm import Session

from app import models
from app.services.planning_pool_resolver import effective_planning_pool_by_warehouse

from .physical_visibility import visible_sles_for_generation
from .reservation_consumption_core import Fact, Reserve, allocate_consumption_facts


ALGORITHM_VERSION = "reservation-consumption-allocation/1"


def _dec(value: Any) -> Decimal:
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as error:
        raise ValueError(f"malformed decimal {value!r}") from error


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_recorder_type(value: Any) -> str:
    raw = _text(value)
    if raw.lower().startswith("standardodata."):
        raw = raw.split(".", 1)[1]
    return raw


def _qty_text(value: Any) -> str:
    number = _dec(value)
    return "0" if number == 0 else format(number.normalize(), "f")


def _checksum(rows: list[dict[str, Any]]) -> str:
    raw = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(raw.encode("utf-8")).hexdigest()


def _identity_by_recorder(
    db: Session,
    recorder_refs: set[str],
) -> dict[tuple[str, int], tuple[int | None, int | None]]:
    """Resolve an exact document identity via SyncLink; ambiguous mappings fall back."""
    if not recorder_refs:
        return {}
    candidates: dict[tuple[str, int], set[tuple[int, int]]] = {}
    for ref, item_id, requirement_id, run_id in (
        db.query(
            models.SyncLink.target_ref_key,
            models.ProductionProduct.item_id,
            models.MrpRequirement.id,
            models.MrpRequirement.run_id,
        )
        .join(models.ProductionManufacture, models.ProductionManufacture.manufacture_id == models.SyncLink.source_id)
        .join(models.ProductionProduct, models.ProductionProduct.product_id == models.ProductionManufacture.product_id)
        .join(models.MrpRequirement, models.MrpRequirement.id == models.ProductionProduct.source_mrp_requirement_id)
        .filter(
            models.SyncLink.target_ref_key.in_(sorted(recorder_refs)),
            models.SyncLink.source_system == "PRODPLAN",
            models.SyncLink.target_system == "1C",
            models.SyncLink.source_doctype == "manufacture",
            models.SyncLink.target_entity == "Document_СборкаЗапасов",
            models.SyncLink.status == "success",
            models.ProductionProduct.source_mrp_requirement_id.isnot(None),
        )
        .all()
    ):
        candidates.setdefault((_text(ref), int(item_id)), set()).add(
            (int(requirement_id), int(run_id))
        )
    return {
        key: next(iter(values)) if len(values) == 1 else (None, None)
        for key, values in candidates.items()
    }


def _input(
    db: Session,
    generation: models.LedgerGeneration,
) -> tuple[tuple[Fact, ...], tuple[Reserve, ...], dict[str, models.StockLedgerEntry], dict[str, models.ReservationEntry]]:
    if generation.cutoff is None or generation.physical_import_batch_id is None:
        raise ValueError("reservation consumption allocation requires generation cutoff and physical batch")
    mapping = effective_planning_pool_by_warehouse(db)
    entries = (
        db.query(models.ReservationEntry)
        .filter(
            models.ReservationEntry.ledger_generation_id == int(generation.id),
            models.ReservationEntry.lifecycle_status == "active",
        )
        .order_by(models.ReservationEntry.priority_period_from.asc(), models.ReservationEntry.id.asc())
        .all()
    )
    baselines = (
        db.query(models.MrpFreezeBaseline)
        .filter(models.MrpFreezeBaseline.run_id.in_([int(e.run_id) for e in entries if e.run_id is not None]))
        .all()
        if entries else []
    )
    baseline_by_key: dict[tuple[int, int, int, str, str, str], models.MrpFreezeBaseline] = {}
    for baseline in baselines:
        key = (int(baseline.run_id), int(baseline.freeze_version), int(baseline.item_id), _text(baseline.characteristic_ref), _text(baseline.organization_ref), _text(baseline.planning_stock_pool))
        if key in baseline_by_key:
            raise ValueError(f"ambiguous freeze baseline for run {baseline.run_id} item {baseline.item_id}")
        baseline_by_key[key] = baseline
    reserves: list[Reserve] = []
    entry_by_id: dict[str, models.ReservationEntry] = {}
    for entry in entries:
        if entry.run_id is None:
            raise ValueError(f"reservation {entry.id} lacks run lineage")
        key = (int(entry.run_id), int(entry.freeze_version), int(entry.item_id), _text(entry.characteristic_ref), _text(entry.organization_ref), _text(entry.planning_stock_pool))
        baseline = baseline_by_key.get(key)
        if baseline is None or baseline.baseline_at is None:
            raise ValueError(f"reservation {entry.id} lacks exact frozen pool baseline")
        qty = _dec(entry.reserved_qty)
        if qty <= 0:
            continue
        reserve_id = str(int(entry.id))
        reserves.append(Reserve(reserve_id=reserve_id, requirement_id=int(entry.requirement_id), run_id=int(entry.run_id), reserved_qty=qty, baseline_at=baseline.baseline_at, plan_period_from=entry.priority_period_from, plan_period_to=entry.priority_period_to, item_id=int(entry.item_id), pool=_text(entry.planning_stock_pool), characteristic_ref=_text(entry.characteristic_ref), organization_ref=_text(entry.organization_ref)))
        entry_by_id[reserve_id] = entry

    visible_rows = visible_sles_for_generation(db, int(generation.id))
    rows = [
        row
        for row in visible_rows
        if _text(row.record_type) == "Expense"
        and _text(row.movement_kind) == "assembly_out"
        and _dec(row.qty) < 0
    ]
    # ``assembly_out`` is derived from one recorder type, now strict-matched.
    assembly_refs = {
        _text(row.recorder_ref)
        for row in rows
        if _text(row.recorder_ref) and _normalize_recorder_type(row.recorder_type) == "Document_СборкаЗапасов"
    }
    identities = _identity_by_recorder(db, assembly_refs)
    facts: list[Fact] = []
    sle_by_id: dict[str, models.StockLedgerEntry] = {}
    for row in rows:
        warehouse = _text(row.warehouse_ref1c)
        pool = _text(mapping.get(warehouse))
        if not warehouse:
            raise ValueError(f"consumption SLE {row.id} has no warehouse identity")
        if not pool:
            # §16 releases stock held in a planning pool only from a physical
            # expense in that same pool. Workshop/out-of-contour assembly rows
            # may be the paired movement of an assembly output and must neither
            # consume the planning reserve a second time nor block the build.
            continue
        requirement_id, run_id = identities.get((_text(row.recorder_ref), int(row.item_id)), (None, None))
        fact_id = str(int(row.id))
        facts.append(Fact(fact_id=fact_id, item_id=int(row.item_id), qty=abs(_dec(row.qty)), posting_at=row.posting_at, pool=pool, characteristic_ref=_text(row.characteristic_ref), organization_ref=_text(row.organization_ref), requirement_id=requirement_id, run_id=run_id))
        sle_by_id[fact_id] = row
    return tuple(facts), tuple(reserves), sle_by_id, entry_by_id


def _signature(rows: list[models.ReservationConsumptionAllocation]) -> list[dict[str, Any]]:
    return sorted([
        {"sle_id": int(row.sle_id), "reservation_id": int(row.reservation_id), "requirement_id": int(row.requirement_id), "qty": _qty_text(row.allocated_qty), "match_rule": _text(row.match_rule), "idempotency_key": _text(row.idempotency_key)}
        for row in rows
    ], key=lambda row: (row["sle_id"], row["reservation_id"]))


def materialize_reservation_consumption_allocations(db: Session, generation_id: int, batch_id: int | None = None) -> dict[str, Any]:
    """Build an immutable, idempotent allocation projection for BUILDING only."""
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None:
        raise ValueError(f"ledger generation {generation_id} does not exist")
    if str(generation.status) != "building":
        raise ValueError("reservation consumption allocation requires BUILDING generation")
    if batch_id is not None:
        batch = db.get(models.LedgerBuildBatch, int(batch_id))
        if batch is None or int(batch.ledger_generation_id) != int(generation.id):
            raise ValueError("reservation consumption allocation batch is outside generation")

    facts, reserves, sle_by_id, entry_by_id = _input(db, generation)
    result = allocate_consumption_facts(facts, reserves)
    expected: list[dict[str, Any]] = []
    for allocation in result.allocations:
        entry = entry_by_id[allocation.reserve_id]
        sle = sle_by_id[allocation.fact_id]
        expected.append({"sle_id": int(sle.id), "reservation_id": int(entry.id), "requirement_id": int(entry.requirement_id), "qty": _qty_text(allocation.qty), "match_rule": allocation.match_rule, "idempotency_key": f"g{generation.id}:sle{sle.id}:r{entry.id}"})
    expected.sort(key=lambda row: (row["sle_id"], row["reservation_id"]))
    existing = db.query(models.ReservationConsumptionAllocation).filter(models.ReservationConsumptionAllocation.ledger_generation_id == int(generation.id)).all()
    if existing:
        if _signature(existing) != expected:
            raise ValueError("reservation consumption allocation drift")
    else:
        for row in expected:
            sle = sle_by_id[str(row["sle_id"])]
            entry = entry_by_id[str(row["reservation_id"])]
            db.add(models.ReservationConsumptionAllocation(ledger_generation_id=int(generation.id), reservation_id=int(entry.id), sle_id=int(sle.id), requirement_id=int(entry.requirement_id), allocated_qty=_dec(row["qty"]), match_rule=_text(row["match_rule"]), fact_ref=_text(sle.recorder_ref), fact_line_ref=_text(sle.line_no), item_id=int(entry.item_id), characteristic_ref=_text(entry.characteristic_ref), organization_ref=_text(entry.organization_ref), planning_stock_pool=_text(entry.planning_stock_pool), idempotency_key=_text(row["idempotency_key"]), event_at=sle.posting_at))
        db.flush()
    return {"ledger_generation_id": int(generation.id), "batch_id": int(batch_id) if batch_id is not None else None, "algorithm_version": ALGORITHM_VERSION, "facts": len(facts), "allocations": len(expected), "fact_qty": _qty_text(result.fact_qty), "allocated_qty": _qty_text(result.allocated_qty), "surplus_qty": _qty_text(result.surplus_qty), "allocation_checksum": _checksum(expected)}
