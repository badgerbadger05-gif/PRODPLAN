"""Item-ledger per-item read API — the "nomenclature card" (Increment 7).

Read-only, purely additive inspection endpoints over the item-ledger substrate
(inc1–6). They render an item's ledger state for the diagnostic/explainability
screen: the pool projection (§1), the physical movement tape (§2), the soft
reservations (§3), a reservation's provenance journal (§4) and the drift/
reconcile events (§5).

These endpoints ALWAYS read the ledger tables directly — they are NOT gated by
STOCK_SOURCE: the card is a ledger inspection tool that shows ledger state
regardless of which stock source the compute core currently consults. Nothing
here touches the compute core (freeze / cycle / reconcile / netting), writes to
1С, or mutates any row — every handler is a pure SELECT.

Data-access is reused from the item-ledger services:
  * reservation_ledger.item_ledger_position — the §1 pool projection.
  * reconcile.ledger_on_hand_by_item — the per-item on_hand fold + the planning
    contour (selected − finished-goods − ignored) used for the warehouse split.
  * mrp_freeze.pool_key_for — the canonical pool key.
Direct ORM reads: StockLedgerEntry (§2), ReservationEntry / ReservationEvent /
ReservationCoverage (§3/§4), MrpDriftEvent (§5).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..services.item_ledger.reconcile import ledger_on_hand_by_item
from ..services.item_ledger.reservation_ledger import item_ledger_position
from ..services.mrp_freeze import pool_key_for

router = APIRouter(prefix="/v1/item-ledger", tags=["item-ledger"])

EPS = 1e-9


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
# §1 — position (card header / summary)
# ---------------------------------------------------------------------------
@router.get("/{item_id}/position")
def get_position(item_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """The §1 pool projection for one item — the ledger's own view (read the
    ledger tables directly, never gated by STOCK_SOURCE). on_hand / available /
    projected / uncovered follow the §2.5 formulas; ``available`` and
    ``uncovered`` are surfaced as-is (a negative available is a deficit signal,
    not clamped)."""
    item = _get_item_or_404(db, item_id)
    pos = item_ledger_position(db, [int(item_id)]).get(int(item_id), {})

    on_hand = _f(pos.get("on_hand"))
    reserved_soft = _f(pos.get("reserved_soft"))
    uncovered = _f(pos.get("uncovered"))

    # per-warehouse split of on_hand over the same planning contour.
    name_by_ref, sel, fg, ign, has_settings = _contour(db)
    bin_rows = (
        db.query(models.StockBin.warehouse_ref1c, func.sum(models.StockBin.on_hand))
        .filter(models.StockBin.item_id == int(item_id))
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
            func.abs(models.StockBin.reconcile_pending_qty) > EPS,
        )
        .first()
        is not None
    )

    pk = pool_key_for(int(item_id))
    return {
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
# §2 — movements (physical ledger tape)
# ---------------------------------------------------------------------------
@router.get("/{item_id}/movements")
def get_movements(
    item_id: int,
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    warehouse_ref1c: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """§2 — the signed physical movement tape (active StockLedgerEntry rows),
    sorted ``(posting_at, id)``, paginated (total + rows). ``qty_after`` is the
    running balance the ledger carried — "how it computed"."""
    _get_item_or_404(db, item_id)

    name_by_ref, *_ = _contour(db)

    q = db.query(models.StockLedgerEntry).filter(
        models.StockLedgerEntry.item_id == int(item_id),
        models.StockLedgerEntry.active.is_(True),
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
    return {"total": total, "limit": limit, "offset": offset, "rows": out}


# ---------------------------------------------------------------------------
# §3 — reservations (soft reservation tape)
# ---------------------------------------------------------------------------
@router.get("/{item_id}/reservations")
def get_reservations(
    item_id: int,
    status: Optional[str] = Query(None, description="lifecycle_status filter"),
    run_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """§3 — the per-item soft reservation tape: on which runs/plans the item hangs
    in reservations, what covers each and how much is uncovered. ``make`` rows
    contribute 0 to reserved_soft (surfaced separately as production)."""
    _get_item_or_404(db, item_id)

    q = db.query(models.ReservationEntry).filter(
        models.ReservationEntry.item_id == int(item_id)
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
        realized = _f(e.realized_qty)
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
            "realized_qty": realized,
            "outstanding": max(reserved - realized, 0.0),
            "covered": {
                "on_hand": _f(e.covered_on_hand_qty),
                "incoming_supplier": _f(e.covered_incoming_supplier_qty),
                "incoming_wip": _f(e.covered_incoming_wip_qty),
            },
            "uncovered_qty": _f(e.uncovered_qty),
            "lifecycle_status": e.lifecycle_status,
            "coverage_state": e.coverage_state,
        })
    return {"rows": rows}


# ---------------------------------------------------------------------------
# §4 — reservation events (provenance journal)
# ---------------------------------------------------------------------------
@router.get("/{item_id}/reservations/{reservation_id}/events")
def get_reservation_events(
    item_id: int,
    reservation_id: int,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """§4 — the append-only journal of one reservation (open/amend/realize/…).
    ``sle_id`` links an event to the physical movement that closed it — the debug
    thread. 404 unless the reservation belongs to the item."""
    _get_item_or_404(db, item_id)
    entry = (
        db.query(models.ReservationEntry)
        .filter(models.ReservationEntry.id == int(reservation_id))
        .one_or_none()
    )
    if entry is None or int(entry.item_id) != int(item_id):
        raise HTTPException(
            status_code=404,
            detail=f"reservation {reservation_id} not found for item {item_id}",
        )
    events = (
        db.query(models.ReservationEvent)
        .filter(models.ReservationEvent.reservation_id == int(reservation_id))
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
    return {"reservation_id": int(reservation_id), "rows": rows}


# ---------------------------------------------------------------------------
# §5 — drift (reconciliation issues)
# ---------------------------------------------------------------------------
# kind → coarse cause label (MrpDriftEvent stores no explicit cause/adjustment
# link; the label is derived so the card can group issues). See handler note.
_CAUSE_BY_KIND = {
    "evaporation": "supply_evaporation",
    "shortfall": "unplanned_consumption",
    "surplus": "balance_reconcile",
}


@router.get("/{item_id}/drift")
def get_drift(
    item_id: int,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """§5 — MrpDriftEvent rows for the item (where reality diverged from plan).
    ``cause`` is derived from ``kind`` and ``adjustment_sle_id`` is not tracked on
    the drift row (returned null); ``details`` carries the raw provenance."""
    _get_item_or_404(db, item_id)
    q = db.query(models.MrpDriftEvent).filter(models.MrpDriftEvent.item_id == int(item_id))
    total = q.count()
    rows = (
        q.order_by(
            models.MrpDriftEvent.created_at.asc(),
            models.MrpDriftEvent.id.asc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    out = [
        {
            "id": int(r.id),
            "cycle_id": r.cycle_id or "",
            "kind": r.kind or "",
            "drift_qty": _f(r.drift_qty),
            "expected_stock": None if r.expected_stock is None else _f(r.expected_stock),
            "actual_stock": None if r.actual_stock is None else _f(r.actual_stock),
            "at": _iso(r.created_at),
            "cause": _CAUSE_BY_KIND.get(str(r.kind or "")),
            "adjustment_sle_id": None,
            "matured": bool(r.matured),
            "first_seen_cycle_id": r.first_seen_cycle_id,
            "requirement_id": int(r.requirement_id) if r.requirement_id is not None else None,
            "details": r.details,
        }
        for r in rows
    ]
    return {"total": total, "limit": limit, "offset": offset, "rows": out}
