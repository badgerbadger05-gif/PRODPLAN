"""Canonical make/buy reservation ledger writer utilities.

This module hosts the SESSION-side reservation projection entrypoints used by the
current canonical flow:

* reserve target assignment (`mode_targets`)
* reservation materialization (`materialize_reservations`, `materialize_reservations_for_freeze`)
* pool projection (`item_ledger_position`)

Only the minimal private helpers consumed by `historical_obligations` are kept
(`_append_event`, `_fold_entry`, `_get_or_create_entry`, `_load_items`) plus the
generation/stock readers required by the same contract.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from ..mrp_freeze import PoolKey, pool_key_for
from ..replenishment import (
    REPLENISHMENT_FLOW_PURCHASE,
    classify_replenishment_flow,
)
from .reservation import (
    BUY,
    MAKE,
    append_realization_event,
    fold_reservation_events,
    replenishment_remaining,
)

EPS = Decimal("1e-9")


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_generation_id(
    db: Session,
    ledger_generation_id: Optional[int] = None,
    *,
    for_write: bool = True,
) -> int:
    generation = (
        db.get(models.LedgerGeneration, int(ledger_generation_id))
        if ledger_generation_id is not None
        else db.get(models.PlanningTruthState, 1).current_generation
        if db.get(models.PlanningTruthState, 1) is not None
        else None
    )
    if generation is None:
        raise ValueError(
            "reservation Ledger requires explicit ledger_generation_id "
            "or PlanningTruthState context"
        )
    if for_write and str(generation.status) != "building":
        raise ValueError(
            f"reservation Ledger generation {generation.id} is {generation.status}; "
            "writes require building"
        )
    if (
        not for_write
        and str(generation.status) == "accepted"
        and (generation.cutoff is None or generation.accepted_at is None)
    ):
        raise ValueError(
            f"reservation Ledger generation {generation.id} is malformed: "
            "accepted reads require cutoff and accepted_at"
        )
    return int(generation.id)


def _ledger_on_hand_by_generation(
    db: Session,
    ledger_generation_id: int,
) -> Dict[int, float]:
    ignored_refs = {
        str(ref)
        for (ref,) in db.query(models.IgnoredWarehouse.warehouse_ref1c).all()
        if ref
    }
    warehouse_rows = db.query(
        models.StockWarehouse.warehouse_ref1c,
        models.StockWarehouse.is_selected,
        models.StockWarehouse.is_finished_goods,
    ).all()
    selected_refs = {
        str(ref)
        for ref, selected, finished in warehouse_rows
        if ref and bool(selected) and not bool(finished)
    }
    finished_refs = {
        str(ref)
        for ref, _selected, finished in warehouse_rows
        if ref and bool(finished)
    }
    query = db.query(
        models.StockBin.item_id,
        func.sum(models.StockBin.on_hand),
    ).filter(models.StockBin.ledger_generation_id == ledger_generation_id)
    if selected_refs:
        query = query.filter(models.StockBin.warehouse_ref1c.in_(selected_refs))
    if ignored_refs:
        query = query.filter(~models.StockBin.warehouse_ref1c.in_(ignored_refs))
    if finished_refs:
        query = query.filter(~models.StockBin.warehouse_ref1c.in_(finished_refs))
    return {
        int(item_id): float(quantity or 0)
        for item_id, quantity in query.group_by(models.StockBin.item_id).all()
    }


def _is_produced(item: Optional[models.Item]) -> bool:
    method = getattr(item, "replenishment_method", None) if item is not None else None
    return classify_replenishment_flow(method) != REPLENISHMENT_FLOW_PURCHASE


def mode_targets(req: models.MrpRequirement, item: Optional[models.Item]) -> List[Tuple[str, Decimal]]:
    """Assign each requirement to one canonical mode: make or buy."""
    flow = MAKE if _is_produced(item) else BUY
    return [(flow, _dec(req.total_required_qty))]


def _priority_key(req: models.MrpRequirement, run: Optional[models.PlanningRun]) -> Tuple[date, date]:
    pf = req.period_from or (run.period_from if run else None) or date.min
    pt = req.period_to or (run.period_to if run else None) or date.max
    return pf, pt


def _get_or_create_entry(
    db: Session,
    req: models.MrpRequirement,
    mode: str,
    run: Optional[models.PlanningRun],
    ledger_generation_id: int,
) -> models.ReservationEntry:
    entry = (
        db.query(models.ReservationEntry)
        .filter(
            models.ReservationEntry.requirement_id == int(req.id),
            models.ReservationEntry.realization_mode == mode,
            models.ReservationEntry.ledger_generation_id == ledger_generation_id,
        )
        .one_or_none()
    )
    if entry is not None:
        return entry
    pk = pool_key_for(int(req.item_id))
    pf, pt = _priority_key(req, run)
    freeze_version = int(
        req.freeze_version
        if req.freeze_version is not None
        else (run.active_freeze_version if run and run.active_freeze_version is not None else 0)
    )
    entry = models.ReservationEntry(
        ledger_generation_id=ledger_generation_id,
        item_id=int(req.item_id),
        characteristic_ref=pk.characteristic_ref,
        organization_ref=pk.organization_ref,
        planning_stock_pool=pk.planning_stock_pool,
        run_id=int(req.run_id),
        freeze_version=freeze_version,
        requirement_id=int(req.id),
        priority_period_from=pf,
        priority_period_to=pt,
        realization_mode=mode,
        reserved_qty=Decimal("0"),
        covered_from_stock_at_freeze_qty=max(
            _dec(req.total_required_qty) - _dec(req.net_required_qty),
            Decimal("0"),
        ),
        replenishment_required_qty=max(_dec(req.net_required_qty), Decimal("0")),
        replenishment_received_qty=Decimal("0"),
        realized_qty=Decimal("0"),
        lifecycle_status="active",
        opened_at=_now(),
    )
    db.add(entry)
    db.flush()
    return entry


def _append_event(
    db: Session,
    entry: models.ReservationEntry,
    *,
    event_kind: str,
    idempotency_key: str,
    reserved_delta: Decimal = Decimal("0"),
    realized_delta: Decimal = Decimal("0"),
    sle_id: Optional[int] = None,
    fact_ref: str = "",
    fact_line_ref: str = "",
    match_rule: str = "",
    cycle_id: str = "",
) -> bool:
    """Append one reservation_event, idempotent by idempotency_key."""
    return append_realization_event(
        db,
        entry,
        realized_delta=realized_delta,
        sle_id=sle_id,
        fact_ref=fact_ref,
        fact_line_ref=fact_line_ref,
        match_rule=match_rule,
        cycle_id=cycle_id,
        idempotency_key=idempotency_key,
        event_at=_now(),
        reserved_delta=reserved_delta,
        event_kind=event_kind,
    )


def _fold_entry(
    db: Session,
    entry: models.ReservationEntry,
) -> Tuple[Decimal, Decimal, Decimal]:
    """Fold events into one reserve cache row."""
    events = (
        db.query(models.ReservationEvent)
        .filter(models.ReservationEvent.reservation_id == int(entry.id))
        .filter(models.ReservationEvent.ledger_generation_id == int(entry.ledger_generation_id))
        .order_by(models.ReservationEvent.id.asc())
        .all()
    )
    fold = fold_reservation_events(events)
    entry.reserved_qty = fold.reserved_qty
    entry.realized_qty = fold.realized_qty
    entry.replenishment_received_qty = min(
        fold.realized_qty,
        _dec(entry.replenishment_required_qty),
    )
    db.flush()
    remaining = replenishment_remaining(
        entry.replenishment_required_qty,
        entry.replenishment_received_qty,
    )
    return fold.reserved_qty, fold.realized_qty, remaining


def _load_items(db: Session, item_ids: Set[int]) -> Dict[int, models.Item]:
    if not item_ids:
        return {}
    return {
        int(item.item_id): item
        for item in db.query(models.Item)
        .filter(models.Item.item_id.in_(list(item_ids)))
        .all()
    }


def materialize_reservations(
    db: Session,
    reqs: Sequence[models.MrpRequirement],
    runs_by_id: Dict[int, models.PlanningRun],
    cycle_id: str,
    *,
    ledger_generation_id: Optional[int] = None,
) -> List[int]:
    """Materialize make/buy reservation_entry and open/amend events for requirements."""
    if not reqs:
        return []
    generation_id = _resolve_generation_id(db, ledger_generation_id, for_write=True)
    items = _load_items(db, {int(r.item_id) for r in reqs})
    touched: List[int] = []
    for req in reqs:
        run = runs_by_id.get(int(req.run_id))
        version = int(
            req.freeze_version
            if req.freeze_version is not None
            else (run.active_freeze_version if run and run.active_freeze_version is not None else 0)
        )
        for mode, target in mode_targets(req, items.get(int(req.item_id))):
            entry = _get_or_create_entry(db, req, mode, run, generation_id)
            touched.append(int(entry.id))
            events = (
                db.query(models.ReservationEvent)
                .filter(models.ReservationEvent.reservation_id == int(entry.id))
                .filter(models.ReservationEvent.ledger_generation_id == generation_id)
                .order_by(models.ReservationEvent.id.asc())
                .all()
            )
            fold = fold_reservation_events(events)
            if not events:
                if target > EPS:
                    _append_event(
                        db,
                        entry,
                        event_kind="open",
                        idempotency_key=f"open:{int(req.id)}:{mode}:{version}",
                        reserved_delta=target,
                        cycle_id=cycle_id,
                    )
            elif abs(fold.reserved_qty - target) > EPS:
                _append_event(
                    db,
                    entry,
                    event_kind="amend",
                    idempotency_key=f"amend:{int(req.id)}:{mode}:{version}",
                    reserved_delta=target - fold.reserved_qty,
                    cycle_id=cycle_id,
                )
            if abs(fold.reserved_qty - target) > EPS or not events:
                _fold_entry(db, entry)
    return touched


def materialize_reservations_for_freeze(
    db: Session,
    active_run_ids: Sequence[int],
    *,
    ledger_generation_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Materialize make/buy reservation obligations for freeze runs."""
    run_ids = [int(r) for r in active_run_ids]
    if not run_ids:
        return {"reservations": 0, "frozen_pins": 0}
    generation_id = _resolve_generation_id(db, ledger_generation_id, for_write=True)
    reqs = (
        db.query(models.MrpRequirement)
        .filter(models.MrpRequirement.run_id.in_(run_ids))
        .filter(models.MrpRequirement.status == "open")
        .order_by(models.MrpRequirement.id.asc())
        .all()
    )
    if not reqs:
        return {"reservations": 0, "frozen_pins": 0}
    runs_by_id = {
        int(row.run_id): row
        for row in db.query(models.PlanningRun).filter(models.PlanningRun.run_id.in_(run_ids)).all()
    }
    cycle_id = f"freeze-{_now().isoformat()}"
    touched = materialize_reservations(
        db,
        reqs,
        runs_by_id,
        cycle_id,
        ledger_generation_id=generation_id,
    )
    return {"reservations": len(touched), "frozen_pins": 0}


def item_ledger_position(
    db: Session,
    item_ids: Optional[Sequence[int]] = None,
    *,
    ledger_generation_id: Optional[int] = None,
) -> Dict[int, Dict[str, float]]:
    """Render pool projection `{item_id: {on_hand, incoming, reserved_soft, ...}}`."""
    generation_id = _resolve_generation_id(db, ledger_generation_id, for_write=False)
    want: Optional[Set[int]] = (
        {int(i) for i in item_ids if i is not None} if item_ids is not None else None
    )
    on_hand_all = _ledger_on_hand_by_generation(db, generation_id)
    reserved_soft: Dict[int, float] = {}
    res_rows = (
        db.query(
            models.ReservationEntry.item_id,
            models.ReservationEntry.reserved_qty,
        )
        .filter(
            models.ReservationEntry.lifecycle_status == "active",
            models.ReservationEntry.ledger_generation_id == generation_id,
        )
        .all()
    )
    for item_id, reserved in res_rows:
        frozen = max(float(reserved or 0.0), 0.0)
        if frozen > 0.0:
            reserved_soft[int(item_id)] = reserved_soft.get(int(item_id), 0.0) + frozen

    keys = set(on_hand_all) | set(reserved_soft)
    if want is not None:
        keys = set(want)

    result: Dict[int, Dict[str, float]] = {}
    for item_id in keys:
        oh = float(on_hand_all.get(item_id, 0.0))
        oh_pos = oh if oh > 0.0 else 0.0
        soft = float(reserved_soft.get(item_id, 0.0))
        incoming_supplier = 0.0
        incoming_wip = 0.0
        incoming = incoming_supplier + incoming_wip
        result[int(item_id)] = {
            "on_hand": oh,
            "incoming_supplier": incoming_supplier,
            "incoming_wip": incoming_wip,
            "incoming": incoming,
            "reserved_soft": soft,
            "available": oh - soft,
            "projected": oh + incoming - soft,
            "uncovered": max(soft - oh_pos - incoming, 0.0),
        }
    return result
