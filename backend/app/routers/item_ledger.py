"""Item-ledger per-item read API — the nomenclature card.

Read-only, purely additive inspection endpoints over the item-ledger substrate
and render its state for diagnostics: the pool projection, physical movement
tape, and soft reservations with their provenance journal.

These endpoints always read the accepted Item Ledger generation. Nothing here
touches freeze, cycle, reconcile or netting, writes to 1С, or mutates a row:
every handler is a pure SELECT.

Data-access is reused from the item-ledger services:
  * reservation_ledger.item_ledger_position — the pool projection.
  * reconcile.ledger_on_hand_by_item — the per-item on_hand fold + the planning
    contour (selected − finished-goods − ignored) used for the warehouse split.
  * mrp_freeze.pool_key_for — the canonical pool key.
Direct ORM reads cover physical entries, immutable make/buy reservations and
their append-only event tape.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict

from .. import models
from ..database import get_db
from ..routers.truth_meta import TruthMeta, build_truth_meta
from ..services.item_ledger.physical_visibility import visible_sle_query
from ..services.item_ledger.reservation import replenishment_remaining
from ..services.item_ledger.reservation_ledger import item_ledger_position
from ..services.mrp_freeze import pool_key_for
from ..services.planning_truth import (
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_RESERVATION_REPLAY,
    PlanningTruthReadiness,
    PlanningTruthUnavailable,
    require_accepted_truth,
)

router = APIRouter(prefix="/v1/item-ledger", tags=["item-ledger"])

EPS = 1e-9


def _accepted_generation(
    db: Session,
    *,
    consumer: str,
    capabilities: tuple[str, ...],
) -> PlanningTruthReadiness:
    try:
        truth = require_accepted_truth(
            db,
            consumer,
            required_capabilities=capabilities,
        )
    except PlanningTruthUnavailable as exc:
        raise HTTPException(
            status_code=409,
            detail=jsonable_encoder(exc.as_dict()),
        ) from exc
    return truth


def _truth_meta(readiness: PlanningTruthReadiness) -> dict:
    return build_truth_meta(readiness).model_dump()


class ItemLedgerPositionWarehouse(BaseModel):
    warehouse_ref1c: str
    warehouse_name: str
    qty: float
    qty_negative: bool


class ItemLedgerPositionFlags(BaseModel):
    on_hand_negative: bool
    has_uncovered: bool
    reconcile_pending: bool


class ItemLedgerPositionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: int
    item_code: str
    item_name: str
    pool_key: str
    on_hand: float
    on_hand_by_warehouse: List[ItemLedgerPositionWarehouse]
    incoming_supplier: float
    incoming_wip: float
    incoming: float
    reserved_soft: float
    available: float
    projected: float
    uncovered: float
    flags: ItemLedgerPositionFlags
    truth_meta: TruthMeta


class ItemLedgerMovement(BaseModel):
    id: int
    posting_at: Optional[str]
    warehouse_ref1c: str
    warehouse_name: str
    qty: float
    qty_after: float
    movement_kind: str
    record_type: str
    recorder_type: str
    recorder_ref: str
    line_no: str
    ingest_source: str
    characteristic_ref: str
    organization_ref: str


class ItemLedgerMovementsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    limit: int
    offset: int
    rows: List[ItemLedgerMovement]
    truth_meta: TruthMeta


class ItemLedgerReservationPriority(BaseModel):
    period_from: Optional[str]
    period_to: Optional[str]


class ItemLedgerReservationRow(BaseModel):
    reservation_id: int
    run_id: Optional[int]
    plan_id: Optional[int]
    plan_name: Optional[str]
    requirement_id: int
    realization_mode: str
    priority: ItemLedgerReservationPriority
    reserved_qty: float
    covered_from_stock_at_freeze_qty: float
    replenishment_required_qty: float
    replenishment_received_qty: float
    replenishment_remaining_qty: float
    lifecycle_status: str


class ItemLedgerReservationsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: List[ItemLedgerReservationRow]
    truth_meta: TruthMeta


class ItemLedgerReservationEventRow(BaseModel):
    id: int
    event_at: Optional[str]
    event_kind: str
    reserved_delta: float
    realized_delta: float
    sle_id: Optional[int]
    fact_ref: str
    fact_line_ref: str
    match_rule: str
    cycle_id: str


class ItemLedgerReservationEventsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reservation_id: int
    rows: List[ItemLedgerReservationEventRow]
    truth_meta: TruthMeta


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _get_item_or_404(db: Session, item_id: int) -> models.Item:
    item = db.query(models.Item).filter(models.Item.item_id == int(item_id)).one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail=f"item {item_id} not found")
    return item


def _f(value: Any) -> float:
    if value is None:
        return 0.0
    return float(value)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def _contour(db: Session):
    """Return (name_by_ref, selected_refs, finished_goods_refs, ignored_refs,
    has_settings) — the SAME planning contour ``ledger_on_hand_by_item`` sums, so
    the per-warehouse split adds up to ``on_hand`` (selected, not finished-goods,
    not ignored)."""
    ignored_refs = {
        str(r[0])
        for r in db.query(models.IgnoredWarehouse.warehouse_ref1c).all()
        if r and r[0]
    }
    rows = db.query(
        models.StockWarehouse.warehouse_ref1c,
        models.StockWarehouse.is_selected,
        models.StockWarehouse.is_finished_goods,
        models.StockWarehouse.warehouse_name,
    ).all()
    name_by_ref = {str(ref): str(name or "") for ref, _sel, _fg, name in rows if ref}
    selected_refs = {
        str(ref) for ref, sel, fg, _n in rows if ref and bool(sel) and not bool(fg)
    }
    finished_goods_refs = {str(ref) for ref, _sel, fg, _n in rows if ref and bool(fg)}
    return name_by_ref, selected_refs, finished_goods_refs, ignored_refs, bool(rows)


def _in_contour(ref: str, selected, finished_goods, ignored, has_settings) -> bool:
    if ref in finished_goods or ref in ignored:
        return False
    if has_settings and ref not in selected:
        return False
    return True


# ---------------------------------------------------------------------------
#  — position (card header / summary)
# ---------------------------------------------------------------------------
@router.get("/{item_id}/position", response_model=ItemLedgerPositionResponse)
def get_position(item_id: int, db: Session = Depends(get_db)) -> ItemLedgerPositionResponse:
    """The  pool projection for one item — the ledger's own view (read the
    ledger tables directly. on_hand / available /
    projected / uncovered follow the  formulas; ``available`` and
    ``uncovered`` are surfaced as-is (a negative available is a deficit signal,
    not clamped)."""
    item = _get_item_or_404(db, item_id)
    truth = _accepted_generation(
        db,
        consumer="item_ledger.position",
        capabilities=(CAPABILITY_PHYSICAL_LEDGER, CAPABILITY_RESERVATION_REPLAY),
    )
    generation_id = int(truth.generation_id)
    pos = item_ledger_position(
        db,
        [int(item_id)],
        ledger_generation_id=generation_id,
    ).get(int(item_id), {})

    on_hand = _f(pos.get("on_hand"))
    reserved_soft = _f(pos.get("reserved_soft"))
    uncovered = _f(pos.get("uncovered"))

    # per-warehouse split of on_hand over the same planning contour.
    name_by_ref, sel, fg, ign, has_settings = _contour(db)
    bin_rows = (
        db.query(models.StockBin.warehouse_ref1c, func.sum(models.StockBin.on_hand))
        .filter(
            models.StockBin.item_id == int(item_id),
            models.StockBin.ledger_generation_id == generation_id,
        )
        .group_by(models.StockBin.warehouse_ref1c)
        .all()
    )
    on_hand_by_warehouse: List[Dict[str, Any]] = []
    for ref, qty in bin_rows:
        ref = str(ref or "")
        if not _in_contour(ref, sel, fg, ign, has_settings):
            continue
        q = _f(qty)
        on_hand_by_warehouse.append({
            "warehouse_ref1c": ref,
            "warehouse_name": name_by_ref.get(ref, ""),
            "qty": q,
            "qty_negative": q < 0.0,
        })
    on_hand_by_warehouse.sort(key=lambda r: r["warehouse_ref1c"])

    reconcile_pending = (
        db.query(models.StockBin.id)
        .filter(
            models.StockBin.item_id == int(item_id),
            models.StockBin.ledger_generation_id == generation_id,
            func.abs(models.StockBin.reconcile_pending_qty) > EPS,
        )
        .first()
        is not None
    )

    pk = pool_key_for(int(item_id))
    return {
        "truth_meta": _truth_meta(truth),
        "item_id": int(item_id),
        "item_code": item.item_code,
        "item_name": item.item_name,
        "pool_key": f"{int(item_id)}:{pk.characteristic_ref}:{pk.planning_stock_pool}",
        "on_hand": on_hand,
        "on_hand_by_warehouse": on_hand_by_warehouse,
        "incoming_supplier": _f(pos.get("incoming_supplier")),
        "incoming_wip": _f(pos.get("incoming_wip")),
        "incoming": _f(pos.get("incoming")),
        "reserved_soft": reserved_soft,
        "available": _f(pos.get("available")),
        "projected": _f(pos.get("projected")),
        "uncovered": uncovered,
        "flags": {
            "on_hand_negative": on_hand < 0.0,
            "has_uncovered": uncovered > EPS,
            "reconcile_pending": reconcile_pending,
        },
    }


# ---------------------------------------------------------------------------
#  — movements (physical ledger tape)
# ---------------------------------------------------------------------------
@router.get("/{item_id}/movements", response_model=ItemLedgerMovementsResponse)
def get_movements(
    item_id: int,
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    warehouse_ref1c: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> ItemLedgerMovementsResponse:
    """ — the signed physical movement tape (active StockLedgerEntry rows),
    sorted ``(posting_at, id)``, paginated (total + rows). ``qty_after`` is the
    running balance the ledger carried — "how it computed"."""
    _get_item_or_404(db, item_id)
    truth = _accepted_generation(
        db,
        consumer="item_ledger.movements",
        capabilities=(CAPABILITY_PHYSICAL_LEDGER,),
    )
    generation_id = int(truth.generation_id)

    name_by_ref, *_ = _contour(db)

    generation = db.get(models.LedgerGeneration, generation_id)
    q = visible_sle_query(
        db,
        physical_import_batch_id=int(generation.physical_import_batch_id),
        cutoff=generation.cutoff,
    ).filter(
        models.StockLedgerEntry.item_id == int(item_id),
    )
    if date_from is not None:
        q = q.filter(models.StockLedgerEntry.posting_at >= date_from)
    if date_to is not None:
        # inclusive of the whole day: posting_at is a timestamp, so compare
        # against the start of the next day rather than date_to's midnight.
        q = q.filter(models.StockLedgerEntry.posting_at < date_to + timedelta(days=1))
    if warehouse_ref1c is not None:
        q = q.filter(models.StockLedgerEntry.warehouse_ref1c == warehouse_ref1c)

    total = q.count()
    rows = (
        q.order_by(
            models.StockLedgerEntry.posting_at.asc(),
            models.StockLedgerEntry.id.asc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    out = [
        {
            "id": int(r.id),
            "posting_at": _iso(r.posting_at),
            "warehouse_ref1c": r.warehouse_ref1c or "",
            "warehouse_name": name_by_ref.get(str(r.warehouse_ref1c or ""), ""),
            "qty": _f(r.qty),
            "qty_after": _f(r.qty_after),
            "movement_kind": r.movement_kind or "",
            "record_type": r.record_type or "",
            "recorder_type": r.recorder_type or "",
            "recorder_ref": r.recorder_ref or "",
            "line_no": r.line_no or "",
            "ingest_source": r.ingest_source or "",
            "characteristic_ref": r.characteristic_ref or "",
            "organization_ref": r.organization_ref or "",
        }
        for r in rows
    ]
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "rows": out,
        "truth_meta": _truth_meta(truth),
    }


# ---------------------------------------------------------------------------
#  — reservations (soft reservation tape)
# ---------------------------------------------------------------------------
@router.get("/{item_id}/reservations", response_model=ItemLedgerReservationsResponse)
def get_reservations(
    item_id: int,
    status: Optional[str] = Query(None, description="lifecycle_status filter"),
    run_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> ItemLedgerReservationsResponse:
    """ — the per-item soft reservation tape: on which runs/plans the item hangs
    in reservations, what covers each and how much is uncovered. The make/buy
    value routes replenishment; it does not create or suppress demand."""
    _get_item_or_404(db, item_id)
    truth = _accepted_generation(
        db,
        consumer="item_ledger.reservations",
        capabilities=(CAPABILITY_RESERVATION_REPLAY,),
    )
    generation_id = int(truth.generation_id)

    q = db.query(models.ReservationEntry).filter(
        models.ReservationEntry.item_id == int(item_id),
        models.ReservationEntry.ledger_generation_id == generation_id,
    )
    if status is not None:
        q = q.filter(models.ReservationEntry.lifecycle_status == status)
    if run_id is not None:
        q = q.filter(models.ReservationEntry.run_id == int(run_id))
    entries = q.order_by(models.ReservationEntry.id.asc()).all()

    # resolve run → plan names in one pass.
    run_ids = {int(e.run_id) for e in entries if e.run_id is not None}
    runs = (
        {int(r.run_id): r for r in db.query(models.PlanningRun).filter(models.PlanningRun.run_id.in_(run_ids)).all()}
        if run_ids
        else {}
    )
    plan_ids = {int(r.source_plan_id) for r in runs.values() if r.source_plan_id is not None}
    plan_names = (
        {int(p.id): p.name for p in db.query(models.ProductionPlanHeader).filter(models.ProductionPlanHeader.id.in_(plan_ids)).all()}
        if plan_ids
        else {}
    )

    rows: List[Dict[str, Any]] = []
    for e in entries:
        run = runs.get(int(e.run_id)) if e.run_id is not None else None
        plan_id = int(run.source_plan_id) if run is not None and run.source_plan_id is not None else None
        reserved = _f(e.reserved_qty)
        replenishment_required = _f(e.replenishment_required_qty)
        replenishment_received = _f(e.replenishment_received_qty)
        rows.append({
            "reservation_id": int(e.id),
            "run_id": int(e.run_id) if e.run_id is not None else None,
            "plan_id": plan_id,
            "plan_name": plan_names.get(plan_id) if plan_id is not None else None,
            "requirement_id": int(e.requirement_id),
            "realization_mode": e.realization_mode,
            "priority": {
                "period_from": _iso(e.priority_period_from),
                "period_to": _iso(e.priority_period_to),
            },
            "reserved_qty": reserved,
            "covered_from_stock_at_freeze_qty": _f(
                e.covered_from_stock_at_freeze_qty
            ),
            "replenishment_required_qty": replenishment_required,
            "replenishment_received_qty": replenishment_received,
            "replenishment_remaining_qty": float(
                replenishment_remaining(
                    replenishment_required,
                    replenishment_received,
                )
            ),
            "lifecycle_status": e.lifecycle_status,
        })
    return {"rows": rows, "truth_meta": _truth_meta(truth)}


# ---------------------------------------------------------------------------
#  — reservation events (provenance journal)
# ---------------------------------------------------------------------------
@router.get(
    "/{item_id}/reservations/{reservation_id}/events",
    response_model=ItemLedgerReservationEventsResponse,
)
def get_reservation_events(
    item_id: int,
    reservation_id: int,
    db: Session = Depends(get_db),
) -> ItemLedgerReservationEventsResponse:
    """ — the append-only journal of one reservation (open/amend/realize/…).
    ``sle_id`` links an event to the physical movement that closed it — the debug
    thread. 404 unless the reservation belongs to the item."""
    _get_item_or_404(db, item_id)
    truth = _accepted_generation(
        db,
        consumer="item_ledger.reservation_events",
        capabilities=(CAPABILITY_RESERVATION_REPLAY,),
    )
    generation_id = int(truth.generation_id)
    entry = (
        db.query(models.ReservationEntry)
        .filter(
            models.ReservationEntry.id == int(reservation_id),
            models.ReservationEntry.ledger_generation_id == generation_id,
        )
        .one_or_none()
    )
    if entry is None or int(entry.item_id) != int(item_id):
        raise HTTPException(
            status_code=404,
            detail=f"reservation {reservation_id} not found for item {item_id}",
        )
    events = (
        db.query(models.ReservationEvent)
        .filter(
            models.ReservationEvent.reservation_id == int(reservation_id),
            models.ReservationEvent.ledger_generation_id == generation_id,
        )
        .order_by(
            models.ReservationEvent.event_at.asc(),
            models.ReservationEvent.id.asc(),
        )
        .all()
    )
    rows = [
        {
            "id": int(ev.id),
            "event_at": _iso(ev.event_at),
            "event_kind": ev.event_kind or "",
            "reserved_delta": _f(ev.reserved_delta),
            "realized_delta": _f(ev.realized_delta),
            "sle_id": int(ev.sle_id) if ev.sle_id is not None else None,
            "fact_ref": ev.fact_ref or "",
            "fact_line_ref": ev.fact_line_ref or "",
            "match_rule": ev.match_rule or "",
            "cycle_id": ev.cycle_id or "",
        }
        for ev in events
    ]
    return {
        "reservation_id": int(reservation_id),
        "rows": rows,
        "truth_meta": _truth_meta(truth),
    }
