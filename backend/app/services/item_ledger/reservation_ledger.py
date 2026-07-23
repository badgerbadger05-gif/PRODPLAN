"""Ledger-2 (soft reservations) MATERIALIZATION — Increment 4, PURE SHADOW.

This module is the Session-side (ORM) writer for the reservation ledger. It
materializes ``reservation_entry`` / ``reservation_event`` / ``reservation_coverage``
from the canonical MRP scope and the live SLE substrate (inc2–3), runs the pure
``redistribute`` (reservation.py) as a thin ORM adapter, and folds the event
journal back into the entry caches.

CRITICAL — Inc4 is PURE SHADOW and ADDITIVE:
  * NO reader consults these tables. effective_net / freeze / closure are NOT
    affected (that is Inc6).
  * Every entry point is designed to be wrapped by the caller in try/except so a
    failure logs and never breaks freeze or the ledger cycle. The callers
    (mrp_freeze.refreeze_active_snapshots, mrp_execution_ledger.run_ledger_cycle)
    keep their existing behavior byte-identical whether this block runs or not.
  * No OData write, no INSERT into stock_ledger_entry (INV-1way): this module
    only reads ledger-1 and writes the reservation_* tables.

Design references: §2.2 (mode assignment), §2.6 (map to inc1–5), §3.1 (make),
§5 (redistribute), §6.1/§6.3 (SLE→reservation matching), §11 Инк4.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy.orm import Session

from app import models
from ..mrp_freeze import PoolKey, pool_key_for
from ..replenishment import REPLENISHMENT_FLOW_PURCHASE, classify_replenishment_flow
from .reconcile import ledger_on_hand_by_item
from .reservation import (
    CONSUME,
    MAKE,
    Coverage,
    IncomingLine,
    Pin,
    Pool,
    Reserve,
    coverage_state_for,
    fold_reservation_events,
    redistribute,
)

logger = logging.getLogger(__name__)

EPS = Decimal("1e-9")

# ledger-2 coverage source kinds
_ON_HAND = "on_hand"
_SUPPLIER = "supplier_order"
_WIP = "wip_order"
_STOCK = "stock"  # MrpFreezeAllocation.source_type for on_hand netting

# MrpFreezeAllocation.source_type → reservation_coverage.source_kind
_SOURCE_KIND_BY_ALLOC_TYPE = {
    _STOCK: _ON_HAND,
    _SUPPLIER: _SUPPLIER,
    _WIP: _WIP,
}

# SLE movement kinds that CONSUME a reservation (physical issue, qty < 0).
_ISSUE_KINDS = {"assembly_out", "expense", "writeoff", "transfer_out"}
# SLE movement kinds that REALIZE a make reservation (production receipt, qty>0).
_MAKE_RECEIPT_KINDS = {"assembly_in"}
# A transfer receipt REALIZES a make reservation only when it lands on a
# finished-goods (ГП) warehouse — приход ГП на склад ГП по перемещению с
# основанием-заказом. Ordinary transfer_in (materials moved to a workshop)
# stays out of make matching.
_MAKE_TRANSFER_KIND = "transfer_in"

# sync_link doctypes/entities written by OUR document exports (chain source 1):
# one_c_manufacture_export (СборкаЗапасов) and one_c_stock_transfer_export
# (ПеремещениеЗапасов). target_ref_key is exactly the SLE recorder GUID.
_LINK_DOCTYPES = ("manufacture", "material_issue")


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# §2.2 — mode assignment (produced-vs-purchased classification REUSED from
# app.services.replenishment.classify_replenishment_flow — the SAME classifier
# _explode_bom_net_first / mrp_reconciliation use to decide make vs buy).
# ---------------------------------------------------------------------------
def _is_produced(item: Optional[models.Item]) -> bool:
    method = getattr(item, "replenishment_method", None) if item is not None else None
    return classify_replenishment_flow(method) != REPLENISHMENT_FLOW_PURCHASE


def mode_targets(req: models.MrpRequirement, item: Optional[models.Item]) -> List[Tuple[str, Decimal]]:
    """(realization_mode, reserved_qty) records for a requirement (design §2.2).

    * PRODUCED requirement → a ``make`` record, reserved = net_required_qty.
    * CONSUMED item (bom_level ≥ 1) → a ``consume`` record, reserved =
      total_required_qty (gross).

    Combined this yields exactly the §2.2 split:
      bom_level 0 produced (finished good)  → make only
      produced intermediate (level ≥ 1)     → BOTH (consume gross + make net)
      purchased (level ≥ 1)                 → consume only
    """
    bom_level = int(req.bom_level or 0)
    targets: List[Tuple[str, Decimal]] = []
    if _is_produced(item):
        targets.append((MAKE, _dec(req.net_required_qty)))
    if bom_level >= 1:
        targets.append((CONSUME, _dec(req.total_required_qty)))
    return targets


def _pin_mode_for_alloc(source_type: str, has_consume: bool, has_make: bool) -> Optional[str]:
    """Which reservation mode a frozen allocation attaches to (design §2.2/§3.1).

    supplier pins reduce make_uncovered (§3.1) → make (when a make record
    exists); stock/wip pins net the consume gross → consume (when it exists);
    a finished good (make only) takes everything on make.
    """
    if source_type == _SUPPLIER and has_make:
        return MAKE
    if has_consume:
        return CONSUME
    if has_make:
        return MAKE
    return None


# ---------------------------------------------------------------------------
# helpers: entry get-or-create + event append (idempotent) + fold
# ---------------------------------------------------------------------------
def _priority_key(req: models.MrpRequirement, run: Optional[models.PlanningRun]) -> Tuple[date, date]:
    pf = req.period_from or (run.period_from if run else None) or date.min
    pt = req.period_to or (run.period_to if run else None) or date.max
    return pf, pt


def _get_or_create_entry(
    db: Session,
    req: models.MrpRequirement,
    mode: str,
    run: Optional[models.PlanningRun],
) -> models.ReservationEntry:
    entry = (
        db.query(models.ReservationEntry)
        .filter(
            models.ReservationEntry.requirement_id == int(req.id),
            models.ReservationEntry.realization_mode == mode,
        )
        .one_or_none()
    )
    if entry is not None:
        return entry
    pk = pool_key_for(int(req.item_id))
    pf, pt = _priority_key(req, run)
    freeze_version = int(req.freeze_version if req.freeze_version is not None else (run.active_freeze_version if run and run.active_freeze_version is not None else 0))
    entry = models.ReservationEntry(
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
        realized_qty=Decimal("0"),
        lifecycle_status="active",
        coverage_state="uncovered",
        opened_at=_now(),
    )
    db.add(entry)
    db.flush()
    return entry


def _event_exists(db: Session, idempotency_key: str) -> bool:
    return (
        db.query(models.ReservationEvent.id)
        .filter(models.ReservationEvent.idempotency_key == idempotency_key)
        .first()
        is not None
    )


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
    """Append one reservation_event, idempotent by idempotency_key. Returns True
    if a new row was written (design §2.3, append-only, never UPDATE/DELETE)."""
    if _event_exists(db, idempotency_key):
        return False
    db.add(
        models.ReservationEvent(
            reservation_id=int(entry.id),
            item_id=int(entry.item_id),
            characteristic_ref=entry.characteristic_ref,
            organization_ref=entry.organization_ref,
            planning_stock_pool=entry.planning_stock_pool,
            event_kind=event_kind,
            reserved_delta=reserved_delta,
            realized_delta=realized_delta,
            sle_id=sle_id,
            fact_ref=fact_ref,
            fact_line_ref=fact_line_ref,
            match_rule=match_rule,
            cycle_id=cycle_id,
            idempotency_key=idempotency_key,
            event_at=_now(),
        )
    )
    db.flush()
    return True


def _fold_entry(db: Session, entry: models.ReservationEntry) -> Tuple[Decimal, Decimal, Decimal]:
    """Fold the entry's events into its reserved_qty/realized_qty caches
    (INV-RES-fold) and set lifecycle_status closure (realized ≥ reserved)."""
    events = (
        db.query(models.ReservationEvent)
        .filter(models.ReservationEvent.reservation_id == int(entry.id))
        .order_by(models.ReservationEvent.id.asc())
        .all()
    )
    fold = fold_reservation_events(events)
    entry.reserved_qty = fold.reserved_qty
    entry.realized_qty = fold.realized_qty
    # Closure: realized ≥ reserved (both modes, §6.2). Never re-open here (Inc4
    # has no auto-reopen); only active → closed.
    if entry.lifecycle_status == "active" and fold.reserved_qty > EPS and fold.realized_qty + EPS >= fold.reserved_qty:
        entry.lifecycle_status = "closed"
        entry.closed_at = _now()
    db.flush()
    return fold.reserved_qty, fold.realized_qty, fold.outstanding


def _entry_events(db: Session, entry: models.ReservationEntry) -> List[models.ReservationEvent]:
    return (
        db.query(models.ReservationEvent)
        .filter(models.ReservationEvent.reservation_id == int(entry.id))
        .order_by(models.ReservationEvent.id.asc())
        .all()
    )


# ---------------------------------------------------------------------------
# §6.1 (последняя строка матрицы) / §8 — unrealize-компенсация при
# замене-по-регистратору (Д2)
# ---------------------------------------------------------------------------
def unrealize_replaced_sle(
    db: Session,
    sle_ids: Sequence[int],
    recorder_ref: str = "",
) -> int:
    """Compensate realize events whose SLE rows are about to be REPLACED.

    Replace-by-recorder (ingest §3а step 4) deletes the recorder's SLE rows and
    inserts fresh ones with NEW ids; the FK ``reservation_event.sle_id`` is ON
    DELETE SET NULL, so without compensation the applied-mark is lost and the
    fresh rows would realize AGAIN — doubling ``realized_qty`` and closing the
    reserve prematurely. Per design §6.1 (last matrix row) / §8 this appends a
    compensating ``unrealize`` (realized_delta = −realize.realized_delta) for
    every realize event referencing the replaced SLE ids; the fresh SLE rows are
    then re-matched by the regular :func:`realize_from_sle` pass.

    MUST be called BEFORE the SLE delete (while ``sle_id`` still resolves).
    Idempotent: keyed by the compensated realize event's id (each generation of
    the recorder's rows produces new realize events, so one compensation per
    realize event is exactly the per-generation semantics). A reserve that was
    closed by the compensated realize and whose fold no longer satisfies
    realized ≥ reserved is re-opened (``reopen`` event, status → active).
    Returns the number of unrealize events written.
    """
    ids = [int(i) for i in sle_ids or []]
    if not ids:
        return 0
    realize_events = (
        db.query(models.ReservationEvent)
        .filter(
            models.ReservationEvent.sle_id.in_(ids),
            models.ReservationEvent.event_kind == "realize",
        )
        .order_by(models.ReservationEvent.id.asc())
        .all()
    )
    if not realize_events:
        return 0
    written = 0
    touched: Set[int] = set()
    for ev in realize_events:
        entry = db.get(models.ReservationEntry, int(ev.reservation_id))
        if entry is None:
            continue
        wrote = _append_event(
            db, entry,
            event_kind="unrealize",
            idempotency_key=f"unrealize:{int(ev.reservation_id)}:{int(ev.id)}",
            realized_delta=-_dec(ev.realized_delta),
            fact_ref=str(recorder_ref or ev.fact_ref or ""),
            fact_line_ref=str(ev.fact_line_ref or ""),
            match_rule=str(ev.match_rule or ""),
            cycle_id=f"replace:{recorder_ref}"[:64],
        )
        if wrote:
            written += 1
        touched.add(int(entry.id))
        # closed by this realize and no longer satisfied → reopen (design §6.2).
        fold = fold_reservation_events(_entry_events(db, entry))
        if (
            str(entry.lifecycle_status) == "closed"
            and fold.realized_qty + EPS < fold.reserved_qty
        ):
            _append_event(
                db, entry,
                event_kind="reopen",
                idempotency_key=f"reopen:{int(entry.id)}:unrealize:{int(ev.id)}",
                fact_ref=str(recorder_ref or ""),
                cycle_id=f"replace:{recorder_ref}"[:64],
            )
            entry.lifecycle_status = "active"
            entry.closed_at = None
    for eid in touched:
        entry = db.get(models.ReservationEntry, int(eid))
        if entry is not None:
            _fold_entry(db, entry)
    return written


# ---------------------------------------------------------------------------
# §2.2/§11 — backfill: materialize entries + open/amend events
# ---------------------------------------------------------------------------
def materialize_reservations(
    db: Session,
    reqs: Sequence[models.MrpRequirement],
    runs_by_id: Dict[int, models.PlanningRun],
    cycle_id: str,
) -> List[int]:
    """Materialize reservation_entry + open/amend events for active requirements.

    open (first materialization): idempotency_key ``open:{req}:{mode}:{ver}``,
    reserved_delta = target. A later refreeze (new freeze_version) that changes
    the target emits an ``amend`` (delta = target − current fold) instead of a
    second open — the entry survives as identity (design §8). Idempotent: a
    re-run at the same version, target unchanged → no event.

    Returns the list of touched reservation_entry ids.
    """
    if not reqs:
        return []
    items = _load_items(db, {int(r.item_id) for r in reqs})
    touched: List[int] = []
    for req in reqs:
        run = runs_by_id.get(int(req.run_id))
        item = items.get(int(req.item_id))
        version = int(req.freeze_version if req.freeze_version is not None else (run.active_freeze_version if run and run.active_freeze_version is not None else 0))
        for mode, target in mode_targets(req, item):
            entry = _get_or_create_entry(db, req, mode, run)
            touched.append(int(entry.id))
            events = (
                db.query(models.ReservationEvent)
                .filter(models.ReservationEvent.reservation_id == int(entry.id))
                .order_by(models.ReservationEvent.id.asc())
                .all()
            )
            fold = fold_reservation_events(events)
            has_open = any(str(e.event_kind) == "open" for e in events)
            if not has_open and not events:
                _append_event(
                    db, entry,
                    event_kind="open",
                    idempotency_key=f"open:{int(req.id)}:{mode}:{version}",
                    reserved_delta=target,
                    cycle_id=cycle_id,
                )
            elif abs(fold.reserved_qty - target) > EPS:
                _append_event(
                    db, entry,
                    event_kind="amend",
                    idempotency_key=f"amend:{int(req.id)}:{mode}:{version}",
                    reserved_delta=target - fold.reserved_qty,
                    cycle_id=cycle_id,
                )
            _fold_entry(db, entry)
    return touched


def _load_items(db: Session, item_ids: Set[int]) -> Dict[int, models.Item]:
    if not item_ids:
        return {}
    return {
        int(i.item_id): i
        for i in db.query(models.Item).filter(models.Item.item_id.in_(list(item_ids))).all()
    }


# ---------------------------------------------------------------------------
# §2.6 decision #10 — frozen-pin dual-write (mirror MrpFreezeAllocation)
# ---------------------------------------------------------------------------
def mirror_frozen_pins(
    db: Session,
    reqs: Sequence[models.MrpRequirement],
    freeze_allocs: Sequence[models.MrpFreezeAllocation],
    items: Optional[Dict[int, models.Item]] = None,
) -> int:
    """Mirror the frozen MrpFreezeAllocation rows into reservation_coverage
    frozen pins (pin_kind='frozen'; copy alloc_qty/fact_at_freeze/source). The
    old table stays the read source (dual-write only, design §2.6 / decision #10).

    Idempotent per requirement: delete the requirement's existing frozen pins,
    then re-insert from the current allocations (a refreeze re-derives them).
    Returns the number of frozen pins written.
    """
    if not freeze_allocs:
        return 0
    reqs_by_id = {int(r.id): r for r in reqs}
    if items is None:
        items = _load_items(db, {int(r.item_id) for r in reqs})

    allocs_by_req: Dict[int, List[models.MrpFreezeAllocation]] = {}
    for a in freeze_allocs:
        allocs_by_req.setdefault(int(a.requirement_id), []).append(a)

    # resolve which entry each requirement's pins attach to (by mode)
    entries_by_req_mode: Dict[Tuple[int, str], models.ReservationEntry] = {}
    for rid in allocs_by_req:
        for e in (
            db.query(models.ReservationEntry)
            .filter(models.ReservationEntry.requirement_id == int(rid))
            .all()
        ):
            entries_by_req_mode[(int(rid), str(e.realization_mode))] = e

    written = 0
    touched_entries: Set[int] = set()
    for rid, allocs in allocs_by_req.items():
        req = reqs_by_id.get(int(rid))
        if req is None:
            continue
        item = items.get(int(req.item_id))
        has_consume = int(req.bom_level or 0) >= 1
        has_make = _is_produced(item)
        for a in allocs:
            source_type = str(a.source_type or "")
            source_kind = _SOURCE_KIND_BY_ALLOC_TYPE.get(source_type)
            if source_kind is None:
                continue
            mode = _pin_mode_for_alloc(source_type, has_consume, has_make)
            if mode is None:
                continue
            entry = entries_by_req_mode.get((int(rid), mode))
            if entry is None:
                continue
            touched_entries.add(int(entry.id))
    # rewrite: clear existing frozen pins for the touched entries, then insert.
    if touched_entries:
        db.query(models.ReservationCoverage).filter(
            models.ReservationCoverage.reservation_id.in_(list(touched_entries)),
            models.ReservationCoverage.pin_kind == "frozen",
        ).delete(synchronize_session="fetch")
        db.flush()

    seen: Set[Tuple[int, str, str, str]] = set()
    for rid, allocs in allocs_by_req.items():
        req = reqs_by_id.get(int(rid))
        if req is None:
            continue
        item = items.get(int(req.item_id))
        has_consume = int(req.bom_level or 0) >= 1
        has_make = _is_produced(item)
        for a in allocs:
            source_type = str(a.source_type or "")
            source_kind = _SOURCE_KIND_BY_ALLOC_TYPE.get(source_type)
            if source_kind is None:
                continue
            mode = _pin_mode_for_alloc(source_type, has_consume, has_make)
            if mode is None:
                continue
            entry = entries_by_req_mode.get((int(rid), mode))
            if entry is None:
                continue
            src_ref = str(a.source_ref or "")
            src_line = str(a.source_line_ref or "")
            key = (int(entry.id), source_kind, src_ref, src_line)
            if key in seen:
                continue
            seen.add(key)
            db.add(
                models.ReservationCoverage(
                    reservation_id=int(entry.id),
                    source_kind=source_kind,
                    source_ref=src_ref,
                    source_line_ref=src_line,
                    pin_kind="frozen",
                    alloc_qty=_dec(a.alloc_qty),
                    fact_at_freeze=_dec(a.fact_at_freeze),
                    covered_qty=Decimal("0"),
                    realized_qty=_dec(a.realized_qty),
                    evaporated_qty=_dec(a.evaporated_qty),
                )
            )
            written += 1
    db.flush()
    return written


def mirror_verify_realized(
    db: Session,
    freeze_allocs: Sequence[models.MrpFreezeAllocation],
) -> int:
    """Copy MrpFreezeAllocation.realized_qty/evaporated_qty onto the mirror
    frozen pins (design §2.6): where verify_frozen_supply updated the old table,
    also update the pin. Dual-write only — the old table stays the read source.

    Matched by (reservation.requirement_id, mapped source_kind, source_ref,
    source_line_ref, pin_kind='frozen'). Returns the number of pins updated.
    """
    if not freeze_allocs:
        return 0
    req_ids = {int(a.requirement_id) for a in freeze_allocs}
    # entry id → requirement_id, and (requirement_id) → its entry ids
    entry_rows = (
        db.query(models.ReservationEntry.id, models.ReservationEntry.requirement_id)
        .filter(models.ReservationEntry.requirement_id.in_(list(req_ids)))
        .all()
    )
    entry_ids_by_req: Dict[int, List[int]] = {}
    for eid, rid in entry_rows:
        entry_ids_by_req.setdefault(int(rid), []).append(int(eid))
    all_entry_ids = [eid for eids in entry_ids_by_req.values() for eid in eids]
    if not all_entry_ids:
        return 0
    pins = (
        db.query(models.ReservationCoverage)
        .filter(
            models.ReservationCoverage.reservation_id.in_(all_entry_ids),
            models.ReservationCoverage.pin_kind == "frozen",
        )
        .all()
    )
    pin_by_key: Dict[Tuple[int, str, str, str], models.ReservationCoverage] = {
        (int(p.reservation_id), str(p.source_kind), str(p.source_ref), str(p.source_line_ref)): p
        for p in pins
    }
    updated = 0
    for a in freeze_allocs:
        source_kind = _SOURCE_KIND_BY_ALLOC_TYPE.get(str(a.source_type or ""))
        if source_kind is None:
            continue
        for eid in entry_ids_by_req.get(int(a.requirement_id), []):
            pin = pin_by_key.get((int(eid), source_kind, str(a.source_ref or ""), str(a.source_line_ref or "")))
            if pin is None:
                continue
            pin.realized_qty = _dec(a.realized_qty)
            pin.evaporated_qty = _dec(a.evaporated_qty)
            updated += 1
    db.flush()
    return updated


# ---------------------------------------------------------------------------
# §6.3 — SLE → reservation matching (realize / unrealize)
# ---------------------------------------------------------------------------
class _MatchIndex:
    """Precomputed indexes for SLE→reservation pegging within a scope.

    The recorder GUID (СборкаЗапасов / ПеремещениеЗапасов document) is NEVER
    the order GUID, so the producing ProductionOrder is resolved through a
    chain (tech-debt п.1, decision #11 owner steps preserved):

      1. ``sync_link`` — the document was created by OUR export
         (source_doctype ∈ {manufacture, material_issue}, target_ref_key =
         recorder GUID) → ProductionManufacture / ProductionMaterialIssue →
         local order + production line (exact pegging).
      2. ``stock_recorder_pull.order_ref`` — the document was created in 1С
         directly; the pull captured the producing-order GUID from the
         document header (ЗаказНаПроизводство_Key / ДокументОснование) →
         ``ProductionOrder.order_ref1c``.
      3. Degenerate direct hit — the recorder IS an order GUID (an order
         document acting as its own recorder); exact, kept last.

    No chain hit → run-scoped FIFO is impossible (no run anchor) → the issue
    is honest ``unplanned_consumption`` — NEVER a silent global FIFO.
    """

    def __init__(self, db: Session, scope_run_ids: List[int], open_req_ids: Set[int]):
        self.res_by_req_mode: Dict[Tuple[int, str], models.ReservationEntry] = {}
        self.res_by_run_item_mode: Dict[Tuple[int, int, str], List[models.ReservationEntry]] = {}
        entries = (
            db.query(models.ReservationEntry)
            .filter(models.ReservationEntry.requirement_id.in_(list(open_req_ids)))
            .filter(models.ReservationEntry.lifecycle_status == "active")
            .all()
            if open_req_ids
            else []
        )
        for e in entries:
            self.res_by_req_mode[(int(e.requirement_id), str(e.realization_mode))] = e
            self.res_by_run_item_mode.setdefault(
                (int(e.run_id), int(e.item_id), str(e.realization_mode)), []
            ).append(e)

        # order_ref1c → ProductionOrder (peg anchor); (order_id,item_id)→source_req
        self.order_by_ref: Dict[str, models.ProductionOrder] = {}
        self.order_by_id: Dict[int, models.ProductionOrder] = {}
        self.order_run: Dict[int, Optional[int]] = {}
        self.prod_source_req: Dict[Tuple[int, int], int] = {}
        # product_id → (item_id, source_mrp_requirement_id) for line-exact pegging
        self.product_line: Dict[int, Tuple[int, Optional[int]]] = {}
        orders = db.query(models.ProductionOrder).all()
        order_ids = [int(o.order_id) for o in orders]
        for o in orders:
            ref = str(o.order_ref1c or "").strip()
            if ref:
                self.order_by_ref[ref] = o
            self.order_by_id[int(o.order_id)] = o
            self.order_run[int(o.order_id)] = (
                int(o.source_run_id) if o.source_run_id is not None else None
            )
        if order_ids:
            for pp in (
                db.query(models.ProductionProduct)
                .filter(models.ProductionProduct.order_id.in_(order_ids))
                .all()
            ):
                src_req = (
                    int(pp.source_mrp_requirement_id)
                    if pp.source_mrp_requirement_id is not None
                    else None
                )
                self.product_line[int(pp.product_id)] = (int(pp.item_id), src_req)
                if src_req is not None:
                    self.prod_source_req[(int(pp.order_id), int(pp.item_id))] = src_req
        # (run_id, item_id) via any product's source req → run (for run-scoped)
        req_run = {int(r.id): int(r.run_id) for r in (
            db.query(models.MrpRequirement.id, models.MrpRequirement.run_id)
            .filter(models.MrpRequirement.id.in_(list(open_req_ids))).all()
        )} if open_req_ids else {}
        for (oid, _iid), rid in self.prod_source_req.items():
            if self.order_run.get(int(oid)) is None and rid in req_run:
                self.order_run[int(oid)] = req_run[rid]

        # ---- chain source 1: sync_link (documents created by OUR export) ----
        # recorder GUID → (order_id, product_id) via the local source document.
        self.link_doc: Dict[str, Tuple[int, Optional[int]]] = {}
        links = (
            db.query(
                models.SyncLink.source_doctype,
                models.SyncLink.source_id,
                models.SyncLink.target_ref_key,
            )
            .filter(
                models.SyncLink.source_doctype.in_(_LINK_DOCTYPES),
                models.SyncLink.target_ref_key.isnot(None),
            )
            .all()
        )
        man_ids = {int(sid) for dt, sid, ref in links if dt == "manufacture" and str(ref or "").strip()}
        issue_ids = {int(sid) for dt, sid, ref in links if dt == "material_issue" and str(ref or "").strip()}
        man_src: Dict[int, Tuple[int, Optional[int]]] = {}
        if man_ids:
            man_src = {
                int(mid): (int(oid), int(pid) if pid is not None else None)
                for mid, oid, pid in db.query(
                    models.ProductionManufacture.manufacture_id,
                    models.ProductionManufacture.order_id,
                    models.ProductionManufacture.product_id,
                )
                .filter(models.ProductionManufacture.manufacture_id.in_(list(man_ids)))
                .all()
            }
        issue_src: Dict[int, Tuple[int, Optional[int]]] = {}
        if issue_ids:
            issue_src = {
                int(iid): (int(oid), int(pid) if pid is not None else None)
                for iid, oid, pid in db.query(
                    models.ProductionMaterialIssue.issue_id,
                    models.ProductionMaterialIssue.order_id,
                    models.ProductionMaterialIssue.product_id,
                )
                .filter(models.ProductionMaterialIssue.issue_id.in_(list(issue_ids)))
                .all()
            }
        for dt, sid, ref in links:
            recorder = str(ref or "").strip()
            if not recorder:
                continue
            src = man_src.get(int(sid)) if dt == "manufacture" else issue_src.get(int(sid))
            if src is not None:
                self.link_doc[recorder] = src

        # ---- chain source 2: stock_recorder_pull.order_ref (1С-created docs) ----
        # recorder GUID → producing-order GUID captured from the document header.
        self.pull_order_ref: Dict[str, str] = {
            str(rref).strip(): str(oref).strip()
            for rref, oref in db.query(
                models.StockRecorderPull.recorder_ref,
                models.StockRecorderPull.order_ref,
            )
            .filter(models.StockRecorderPull.order_ref.isnot(None))
            .all()
            if str(rref or "").strip() and str(oref or "").strip()
        }

    def _resolve_order(
        self, recorder: str
    ) -> Tuple[Optional[models.ProductionOrder], Optional[int], str]:
        """Resolve the producing order for a recorder GUID via the chain.

        Returns ``(order, product_id, source)`` where ``product_id`` is the
        exact local production line (sync_link path only) and ``source`` ∈
        {``sync_link``, ``order_ref``, ``direct``, ``""``}.
        """
        if not recorder:
            return None, None, ""
        linked = self.link_doc.get(recorder)
        if linked is not None:
            order = self.order_by_id.get(int(linked[0]))
            if order is not None:
                return order, linked[1], "sync_link"
        order_ref = self.pull_order_ref.get(recorder)
        if order_ref:
            order = self.order_by_ref.get(order_ref)
            if order is not None:
                return order, None, "order_ref"
        order = self.order_by_ref.get(recorder)
        if order is not None:
            return order, None, "direct"
        return None, None, ""

    def _key_of(self, entry: models.ReservationEntry) -> Tuple:
        return (
            entry.priority_period_from,
            entry.priority_period_to,
            int(entry.run_id) if entry.run_id is not None else 0,
            int(entry.requirement_id),
        )

    def _oldest(self, entries: List[models.ReservationEntry]) -> Optional[models.ReservationEntry]:
        active = [e for e in entries if str(e.lifecycle_status) == "active"]
        if not active:
            return None
        return sorted(active, key=self._key_of)[0]

    def match_issue(self, sle: models.StockLedgerEntry) -> Tuple[Optional[models.ReservationEntry], str]:
        """Match a physical issue (qty<0) to a CONSUME reservation (design §6.3).

        (1) pegged: recorder → resolution chain (sync_link →
            stock_recorder_pull.order_ref → direct) → ProductionOrder → run →
            consume reservation of (run, issued item); (2) run-scoped FIFO:
            oldest active consume reserve of that run/item; (3) STOP →
            unplanned_consumption.
        """
        recorder = str(sle.recorder_ref or "").strip()
        order, _product_id, _source = self._resolve_order(recorder)
        if order is None:
            return None, "unplanned"
        run_id = self.order_run.get(int(order.order_id))
        if run_id is None:
            return None, "unplanned"
        # direct (pegged) consume reservation of the issued item in that run
        direct = self.res_by_req_mode_for_run_item(int(run_id), int(sle.item_id), CONSUME)
        if direct is not None:
            return direct, "pegged"
        # run-scoped FIFO (oldest active of run/item)
        fifo = self._oldest(
            self.res_by_run_item_mode.get((int(run_id), int(sle.item_id), CONSUME), [])
        )
        if fifo is not None:
            return fifo, "fifo"
        return None, "unplanned"

    def res_by_req_mode_for_run_item(self, run_id: int, item_id: int, mode: str) -> Optional[models.ReservationEntry]:
        entries = self.res_by_run_item_mode.get((int(run_id), int(item_id), mode), [])
        active = [e for e in entries if str(e.lifecycle_status) == "active"]
        if not active:
            return None
        return sorted(active, key=self._key_of)[0]

    def match_receipt(self, sle: models.StockLedgerEntry) -> Tuple[Optional[models.ReservationEntry], str]:
        """Match a make receipt (qty>0: assembly_in, or transfer_in onto a
        ГП-склад) to a MAKE reservation (design §3.1/§6.3): recorder →
        resolution chain → ProductionOrder → produced line
        source_mrp_requirement_id → make reservation; fallback run-scoped."""
        recorder = str(sle.recorder_ref or "").strip()
        order, product_id, _source = self._resolve_order(recorder)
        if order is None:
            return None, "unplanned"
        req_id: Optional[int] = None
        # line-exact pegging (sync_link path): the local production line the
        # document was created for, when it is the received item's line.
        if product_id is not None:
            line = self.product_line.get(int(product_id))
            if line is not None and line[0] == int(sle.item_id):
                req_id = line[1]
        if req_id is None:
            req_id = self.prod_source_req.get((int(order.order_id), int(sle.item_id)))
        if req_id is not None:
            entry = self.res_by_req_mode.get((int(req_id), MAKE))
            if entry is not None and str(entry.lifecycle_status) == "active":
                return entry, "pegged"
        run_id = self.order_run.get(int(order.order_id))
        if run_id is not None:
            fifo = self._oldest(
                self.res_by_run_item_mode.get((int(run_id), int(sle.item_id), MAKE), [])
            )
            if fifo is not None:
                return fifo, "fifo"
        return None, "unplanned"


def realize_from_sle(db: Session, scope, cycle_id: str) -> Dict[str, Any]:
    """Append realize events from physical SLE not yet applied (design §6.1/§6.3).

    Issue (qty<0) → consume-reservation realize, CAPPED at outstanding (Finding
    B: residual → unplanned_consumption, no realize). Make receipt (qty>0
    assembly_in, or transfer_in landing on a finished-goods (ГП) warehouse —
    приход ГП по перемещению с основанием-заказом) pegged → make-reservation
    realize. An unmatched issue is ``unplanned_consumption`` (decision #11):
    NEVER a silent global FIFO. idempotency_key
    ``realize:{reservation_id}:{sle_id}``.
    """
    summary = {
        "realized_consume": 0,
        "realized_make": 0,
        "unplanned_consumption": 0,
        "unplanned_qty": 0.0,
    }
    if not scope.pool_items:
        return summary
    index = _MatchIndex(db, scope.run_ids, scope.open_req_ids)

    # finished-goods (ГП) warehouses: a transfer_in landing on one of these is
    # a make receipt (перемещение ГП на склад ГП), not a material movement.
    fg_warehouse_refs = {
        str(ref)
        for (ref,) in db.query(models.StockWarehouse.warehouse_ref1c)
        .filter(models.StockWarehouse.is_finished_goods.is_(True))
        .all()
        if ref
    }

    # SLE already applied (any reservation_event carrying that sle_id).
    applied_sle_ids = {
        int(sid)
        for (sid,) in db.query(models.ReservationEvent.sle_id)
        .filter(models.ReservationEvent.sle_id.isnot(None))
        .all()
    }

    sles = (
        db.query(models.StockLedgerEntry)
        .filter(models.StockLedgerEntry.item_id.in_(list(scope.pool_items)))
        .filter(models.StockLedgerEntry.active.is_(True))
        .order_by(models.StockLedgerEntry.posting_at.asc(), models.StockLedgerEntry.id.asc())
        .all()
    )
    for sle in sles:
        if int(sle.id) in applied_sle_ids:
            continue
        qty = _dec(sle.qty)
        mk = str(sle.movement_kind or "")
        if qty < 0 and mk in _ISSUE_KINDS:
            entry, rule = index.match_issue(sle)
            if entry is None:
                summary["unplanned_consumption"] += 1
                summary["unplanned_qty"] += float(-qty)
                continue
            _fold_entry(db, entry)  # refresh outstanding before cap
            outstanding = max(_dec(entry.reserved_qty) - _dec(entry.realized_qty), Decimal("0"))
            realize_q = min(-qty, outstanding)
            if realize_q > EPS:
                wrote = _append_event(
                    db, entry,
                    event_kind="realize",
                    idempotency_key=f"realize:{int(entry.id)}:{int(sle.id)}",
                    realized_delta=realize_q,
                    sle_id=int(sle.id),
                    fact_ref=str(sle.recorder_ref or ""),
                    fact_line_ref=str(sle.line_no or ""),
                    match_rule=rule,
                    cycle_id=cycle_id,
                )
                if wrote:
                    summary["realized_consume"] += 1
                _fold_entry(db, entry)
            residual = (-qty) - realize_q
            if residual > EPS:
                summary["unplanned_consumption"] += 1
                summary["unplanned_qty"] += float(residual)
        elif qty > 0 and (
            mk in _MAKE_RECEIPT_KINDS
            or (
                mk == _MAKE_TRANSFER_KIND
                and str(sle.warehouse_ref1c or "") in fg_warehouse_refs
            )
        ):
            entry, rule = index.match_receipt(sle)
            if entry is None:
                continue  # supplier/receipt not pegged to make → coverage, not realize
            _fold_entry(db, entry)
            outstanding = max(_dec(entry.reserved_qty) - _dec(entry.realized_qty), Decimal("0"))
            realize_q = min(qty, outstanding)
            if realize_q > EPS:
                wrote = _append_event(
                    db, entry,
                    event_kind="realize",
                    idempotency_key=f"realize:{int(entry.id)}:{int(sle.id)}",
                    realized_delta=realize_q,
                    sle_id=int(sle.id),
                    fact_ref=str(sle.recorder_ref or ""),
                    fact_line_ref=str(sle.line_no or ""),
                    match_rule=rule,
                    cycle_id=cycle_id,
                )
                if wrote:
                    summary["realized_make"] += 1
                _fold_entry(db, entry)
    return summary


# ---------------------------------------------------------------------------
# §5 — redistribute ORM adapter (persist floating coverage + caches)
# ---------------------------------------------------------------------------
def redistribute_pool(
    db: Session,
    item_id: int,
    on_hand_by_item: Dict[int, float],
    cycle_id: str,
) -> Optional[Pool]:
    """Read pool state from DB, run the pure ``redistribute`` (design §5), then
    PERSIST its floating reservation_coverage rows + covered_*/uncovered_qty/
    coverage_state caches. NEVER touches frozen pins or on_hand.

    Pool key is today's single pool ('', '', 'default') for the item. Frozen
    supplier/wip pins become per-reservation IncomingLines (remaining = pin_live)
    so Pass A gives each reserve its own promise and Pass C frees the surplus.
    """
    pk = pool_key_for(int(item_id))
    entries = (
        db.query(models.ReservationEntry)
        .filter(
            models.ReservationEntry.item_id == int(item_id),
            models.ReservationEntry.characteristic_ref == pk.characteristic_ref,
            models.ReservationEntry.organization_ref == pk.organization_ref,
            models.ReservationEntry.planning_stock_pool == pk.planning_stock_pool,
            models.ReservationEntry.realization_mode == CONSUME,
            models.ReservationEntry.lifecycle_status == "active",
        )
        .all()
    )
    if not entries:
        return None

    entry_by_key: Dict[Tuple, models.ReservationEntry] = {}
    reserves: List[Reserve] = []
    lines: List[IncomingLine] = []
    for e in entries:
        key = (
            e.priority_period_from,
            e.priority_period_to,
            int(e.run_id) if e.run_id is not None else 0,
            int(e.requirement_id),
        )
        entry_by_key[key] = e
        r = Reserve(
            key=key,
            reserved_qty=_dec(e.reserved_qty),
            realized_qty=_dec(e.realized_qty),
            realization_mode=CONSUME,
            requirement_id=int(e.requirement_id),
        )
        # frozen supplier/wip pins → Pin + a private IncomingLine (remaining=pin_live)
        pins = (
            db.query(models.ReservationCoverage)
            .filter(
                models.ReservationCoverage.reservation_id == int(e.id),
                models.ReservationCoverage.pin_kind == "frozen",
            )
            .all()
        )
        for p in pins:
            sk = str(p.source_kind)
            if sk not in (_SUPPLIER, _WIP):
                continue
            line_id = f"{sk}:{p.source_ref}:{p.source_line_ref}:{int(p.reservation_id)}"
            pin = Pin(
                line_id=line_id,
                source_kind=sk,
                alloc_qty=_dec(p.alloc_qty),
                evaporated_qty=_dec(p.evaporated_qty),
                realized_qty=_dec(p.realized_qty),
            )
            r.pins.append(pin)
            if pin.pin_live > 0:
                lines.append(
                    IncomingLine(
                        line_id=line_id,
                        source_kind=sk,
                        remaining=pin.pin_live,
                        order_ref=str(p.source_ref),
                        line_ref=str(p.source_line_ref),
                    )
                )
        reserves.append(r)

    on_hand = _dec(on_hand_by_item.get(int(item_id), 0.0))
    pool = Pool(on_hand=on_hand, reserves=reserves, lines=lines)
    result = redistribute(pool)

    # persist: clear existing floating rows for these entries, rewrite from result
    entry_ids = [int(e.id) for e in entries]
    db.query(models.ReservationCoverage).filter(
        models.ReservationCoverage.reservation_id.in_(entry_ids),
        models.ReservationCoverage.pin_kind == "floating",
    ).delete(synchronize_session="fetch")
    db.flush()

    now = _now()
    for r in result.reserves:
        entry = entry_by_key.get(r.key)
        if entry is None:
            continue
        entry.covered_on_hand_qty = r.covered_on_hand
        entry.covered_incoming_supplier_qty = r.covered_incoming_supplier
        entry.covered_incoming_wip_qty = r.covered_incoming_wip
        entry.uncovered_qty = r.uncovered
        entry.coverage_state = r.coverage_state
    # aggregate floating coverage per (reservation, source_kind)
    for cov in result.coverages:
        entry = entry_by_key.get(cov.reserve_key)
        if entry is None:
            continue
        source_ref = "pool" if cov.source_kind == _ON_HAND else ""
        db.add(
            models.ReservationCoverage(
                reservation_id=int(entry.id),
                source_kind=cov.source_kind,
                source_ref=source_ref,
                source_line_ref="",
                pin_kind="floating",
                alloc_qty=Decimal("0"),
                fact_at_freeze=Decimal("0"),
                covered_qty=cov.covered_qty,
                realized_qty=Decimal("0"),
                evaporated_qty=Decimal("0"),
                cycle_id=cycle_id,
                computed_at=now,
            )
        )
    db.flush()
    return pool


# ---------------------------------------------------------------------------
# orchestrators (wrapped by the caller in try/except)
# ---------------------------------------------------------------------------
def run_reservation_shadow(db: Session, scope, cycle_id: str) -> Dict[str, Any]:
    """The Inc4 reservation block, called from run_ledger_cycle AFTER verify /
    executed / drift / closure. PURE SHADOW: writes only reservation_* tables,
    returns a diagnostic summary that the caller does NOT fold into its own
    (byte-identical) return dict.
    """
    reqs = list(scope.open_reqs)
    items = _load_items(db, set(scope.pool_items))
    materialize_reservations(db, reqs, scope.runs_by_id, cycle_id)
    pins_written = mirror_frozen_pins(db, reqs, scope.freeze_allocs, items)
    verify_mirrored = mirror_verify_realized(db, scope.freeze_allocs)
    realize_summary = realize_from_sle(db, scope, cycle_id)

    on_hand = ledger_on_hand_by_item(db)
    pools_redistributed = 0
    for item_id in sorted(scope.pool_items):
        if redistribute_pool(db, int(item_id), on_hand, cycle_id) is not None:
            pools_redistributed += 1

    return {
        "reservations_materialized": len(reqs),
        "frozen_pins": pins_written,
        "verify_pins_mirrored": verify_mirrored,
        "pools_redistributed": pools_redistributed,
        **realize_summary,
    }


def effective_net_bin(db: Session, req: "models.MrpRequirement") -> Optional[float]:
    """Inc6 (б) — effective_net derived from the reservation ledger (design §3/§3.1).

    ``effective_net(r) = uncovered(consume) + Σ pin_live``
    where ``pin_live = max(alloc_qty − evaporated_qty − realized_qty, 0)`` (design
    §11 "живых supplier-пинов").

    The supplier term reconstructs today's ``net_required`` from ``uncovered``:
    ``net_required`` historically INCLUDES the quantity an existing supplier order
    already covers, whereas ``uncovered`` EXCLUDES it (design §3 counter-example,
    review Finding A). Adding back the still-live frozen supplier commitment
    (``pin_live``) makes the closure threshold identical to legacy ``net_required``
    at freeze — WITH or WITHOUT a supplier pin (evap=0 ⇒ pin_live=alloc−realized).

    Single-channel evaporation (corrected Finding D): a supplier pin is
    ``own_open_coverage`` in the sizer (via ``own_exported_outstanding``) and is
    NOT netted into ``net_required``, so its evaporation MUST resurface through
    exactly ONE channel — ``own_open_coverage`` dropping — and must NOT also
    inflate ``effective_net``. A dead pin raises ``evaporated_qty`` → ``uncovered``
    rises by that amount (redistribute drops its coverage); ``pin_live``
    simultaneously drops by the SAME amount, so ``effective_net`` stays at the true
    demand (the two moves cancel) while the sizer's ``own_open_coverage`` falls to
    0 and sizes the proposal correctly. Using ``alloc − realized`` here (no
    evaporation term) would count the evaporated qty TWICE — once as an
    ``effective_net`` rise and once as the coverage loss — over-ordering by the
    dead pin's alloc. Its atomic partner is the exclusion of ``supplier_order``
    evaporation from ``compute_stock_drift`` (WIP-pin evaporation, which WAS netted
    into ``net_required`` and is NOT own_open_coverage, still resurfaces via drift).

    Returns ``None`` when the requirement has NO consume reservation (make-only,
    e.g. a finished good) so the caller falls back to the legacy net+drift target.
    """
    entry = (
        db.query(models.ReservationEntry)
        .filter(
            models.ReservationEntry.requirement_id == int(req.id),
            models.ReservationEntry.realization_mode == CONSUME,
        )
        .one_or_none()
    )
    if entry is None:
        return None
    uncovered = float(entry.uncovered_qty or 0.0)
    supplier_term = 0.0
    for p in (
        db.query(models.ReservationCoverage)
        .filter(
            models.ReservationCoverage.reservation_id == int(entry.id),
            models.ReservationCoverage.pin_kind == "frozen",
            models.ReservationCoverage.source_kind == _SUPPLIER,
        )
        .all()
    ):
        # pin_live: a live supplier pin still commits alloc − evaporated −
        # realized. Excluding evaporated here (vs alloc − realized) keeps the
        # evaporation single-channel — it resurfaces via own_open_coverage in the
        # sizer, NOT via effective_net. At freeze (evap=0) pin_live=alloc−realized
        # so Finding A is preserved; on cancel pin_live drops to cancel the
        # uncovered rise, holding effective_net at the true demand.
        supplier_term += max(
            float(p.alloc_qty or 0.0)
            - float(p.evaporated_qty or 0.0)
            - float(p.realized_qty or 0.0),
            0.0,
        )
    return max(uncovered + supplier_term, 0.0)


def materialize_reservations_for_freeze(db: Session, active_run_ids: Sequence[int]) -> Dict[str, Any]:
    """Freeze-time hook (design §2.6 / §11): after refreeze wrote every
    MrpFreezeAllocation, materialize reservations + mirror the frozen pins so
    the reservation ledger tracks the fresh freeze. PURE SHADOW, wrapped by the
    caller so a failure never breaks the freeze.
    """
    run_ids = [int(r) for r in active_run_ids]
    if not run_ids:
        return {"reservations": 0, "frozen_pins": 0}
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
        int(r.run_id): r
        for r in db.query(models.PlanningRun).filter(models.PlanningRun.run_id.in_(run_ids)).all()
    }
    open_req_ids = [int(r.id) for r in reqs]
    version_by_run = {rid: int(run.active_freeze_version) for rid, run in runs_by_id.items() if run.active_freeze_version is not None}
    freeze_allocs = [
        a
        for a in db.query(models.MrpFreezeAllocation)
        .filter(models.MrpFreezeAllocation.run_id.in_(run_ids))
        .filter(models.MrpFreezeAllocation.requirement_id.in_(open_req_ids))
        .all()
        if version_by_run.get(int(a.run_id)) == int(a.freeze_version)
    ]
    cycle_id = f"freeze-{_now().isoformat()}"
    materialize_reservations(db, reqs, runs_by_id, cycle_id)
    pins = mirror_frozen_pins(db, reqs, freeze_allocs)
    return {"reservations": len(reqs), "frozen_pins": pins}


# ---------------------------------------------------------------------------
# §11 — shadow reconcile report (read-only) : reservation world vs inc1–5
# ---------------------------------------------------------------------------
def reservation_shadow_report(db: Session) -> Dict[str, Any]:
    """Read-only comparison of the two worlds (design §11 Инк4):

    * per requirement: reservation uncovered_qty vs inc1–5 remaining_qty /
      covered_qty (consume) and produced/executed (make);
    * per pool: reserved_soft (Σ active consume outstanding) vs Σ desired
      remaining (Σ req.remaining_qty).

    So we can watch the two ledgers agree in shadow before Inc5/6. No writes.
    """
    entries = (
        db.query(models.ReservationEntry)
        .filter(models.ReservationEntry.lifecycle_status == "active")
        .all()
    )
    req_ids = {int(e.requirement_id) for e in entries}
    reqs = {
        int(r.id): r
        for r in db.query(models.MrpRequirement).filter(models.MrpRequirement.id.in_(list(req_ids))).all()
    } if req_ids else {}

    items_out: List[Dict[str, Any]] = []
    pool_soft: Dict[Tuple[int, str, str, str], float] = {}
    pool_remaining: Dict[Tuple[int, str, str, str], float] = {}
    divergent = 0
    for e in entries:
        req = reqs.get(int(e.requirement_id))
        reserved = float(e.reserved_qty or 0)
        realized = float(e.realized_qty or 0)
        outstanding = max(reserved - realized, 0.0)
        uncovered = float(e.uncovered_qty or 0)
        legacy_remaining = float(req.remaining_qty or 0) if req is not None else 0.0
        legacy_covered = float(req.covered_qty or 0) if req is not None else 0.0
        legacy_executed = float(req.executed_qty or 0) if req is not None else 0.0
        pool = (int(e.item_id), e.characteristic_ref or "", e.organization_ref or "", e.planning_stock_pool or "default")
        if str(e.realization_mode) == CONSUME:
            pool_soft[pool] = pool_soft.get(pool, 0.0) + outstanding
            pool_remaining[pool] = pool_remaining.get(pool, 0.0) + legacy_remaining
            div = uncovered - legacy_remaining
        else:
            div = realized - legacy_executed
        if abs(div) > 1e-6:
            divergent += 1
        items_out.append({
            "requirement_id": int(e.requirement_id),
            "item_id": int(e.item_id),
            "run_id": int(e.run_id) if e.run_id is not None else None,
            "realization_mode": str(e.realization_mode),
            "reserved_qty": reserved,
            "realized_qty": realized,
            "outstanding": outstanding,
            "reservation_uncovered": uncovered,
            "coverage_state": str(e.coverage_state),
            "lifecycle_status": str(e.lifecycle_status),
            "legacy_remaining_qty": legacy_remaining,
            "legacy_covered_qty": legacy_covered,
            "legacy_executed_qty": legacy_executed,
            "divergence": div,
        })
    items_out.sort(key=lambda r: abs(r["divergence"]), reverse=True)

    pools_out: List[Dict[str, Any]] = []
    pool_div = 0
    for pool, soft in pool_soft.items():
        remaining = pool_remaining.get(pool, 0.0)
        d = soft - remaining
        if abs(d) > 1e-6:
            pool_div += 1
        pools_out.append({
            "item_id": pool[0],
            "characteristic_ref": pool[1],
            "organization_ref": pool[2],
            "planning_stock_pool": pool[3],
            "reserved_soft": soft,
            "legacy_sum_remaining": remaining,
            "divergence": d,
        })
    pools_out.sort(key=lambda r: abs(r["divergence"]), reverse=True)

    return {
        "generated_at": _now().isoformat(),
        "counts": {
            "reservations_active": len(entries),
            "divergent_requirements": divergent,
            "pools": len(pools_out),
            "divergent_pools": pool_div,
        },
        "requirements": items_out,
        "pools": pools_out,
    }


# ---------------------------------------------------------------------------
# §2.5 pool projection — item_ledger_position (Inc5 reader surface)
# ---------------------------------------------------------------------------
def item_ledger_position(
    db: Session,
    item_ids: Optional[Sequence[int]] = None,
) -> Dict[int, Dict[str, float]]:
    """The design §2.5 pool projection rendered ``{item_id: position}``.

    Per item (default pool): ``on_hand`` (Σ stock_bin over the planning contour,
    ГП excluded), ``incoming_supplier`` / ``incoming_wip`` (existing OData
    mirrors), ``reserved_soft`` (Σ outstanding over ACTIVE consume reservations —
    make contributes exactly 0, §3.1/INV-RES-make-zero), and the derived
    ``available`` / ``projected`` / ``uncovered`` by the §3 formulas::

        available  = on_hand − reserved_soft            # MAY be < 0 (surfaced)
        projected  = on_hand + incoming − reserved_soft
        uncovered  = max(reserved_soft − on_hand⁺ − incoming, 0)

    Read-only, additive. This is the projection the material-availability readers
    consult behind the ``STOCK_SOURCE=bin`` flag (Inc5, design §11); the legacy
    path is untouched. ``on_hand`` is the negatives-preserved raw pool sum (§4a):
    ``available``/``projected`` are computed from it honestly, while
    ``uncovered`` uses ``on_hand⁺`` so a transient mirror negative never inflates
    a purchase proposal.

    If ``item_ids`` is given, the result is restricted to those items (items with
    no ledger footprint resolve to an all-zero position).
    """
    want: Optional[Set[int]] = (
        {int(i) for i in item_ids if i is not None} if item_ids is not None else None
    )

    on_hand_all = ledger_on_hand_by_item(db)

    # reserved_soft per item — Σ outstanding over active consume reservations.
    reserved_soft: Dict[int, float] = {}
    res_rows = (
        db.query(
            models.ReservationEntry.item_id,
            models.ReservationEntry.reserved_qty,
            models.ReservationEntry.realized_qty,
        )
        .filter(
            models.ReservationEntry.realization_mode == CONSUME,
            models.ReservationEntry.lifecycle_status == "active",
        )
        .all()
    )
    for iid, reserved, realized in res_rows:
        outstanding = max(float(reserved or 0.0) - float(realized or 0.0), 0.0)
        if outstanding <= 0.0:
            continue
        reserved_soft[int(iid)] = reserved_soft.get(int(iid), 0.0) + outstanding

    # incoming — existing loaders (wip identity loader + supplier remaining).
    from ..mrp_stock_helpers import active_wip_supply_by_item

    incoming_wip: Dict[int, float] = {
        int(iid): float(sum(float(l.remaining) for l in lines))
        for iid, lines in active_wip_supply_by_item(db).items()
    }

    incoming_supplier: Dict[int, float] = {}
    supplier_item_ids = [
        int(iid)
        for (iid,) in db.query(models.SupplierOrderItem.item_id_ref).distinct().all()
        if iid is not None
    ]
    if want is not None:
        supplier_item_ids = [i for i in supplier_item_ids if i in want]
    if supplier_item_ids:
        from ..period_plan_service import _load_purchase_supplier_remaining

        supplier = _load_purchase_supplier_remaining(
            db, supplier_item_ids, date.max - timedelta(days=1)
        )
        for iid, lines in supplier.items():
            incoming_supplier[int(iid)] = float(
                sum(float(l.get("remaining_qty") or 0.0) for l in lines)
            )

    keys: Set[int] = set(on_hand_all) | set(reserved_soft) | set(incoming_wip) | set(incoming_supplier)
    if want is not None:
        keys = set(want)

    result: Dict[int, Dict[str, float]] = {}
    for iid in keys:
        oh = float(on_hand_all.get(iid, 0.0))
        oh_pos = oh if oh > 0.0 else 0.0
        inc_s = float(incoming_supplier.get(iid, 0.0))
        inc_w = float(incoming_wip.get(iid, 0.0))
        inc = inc_s + inc_w
        soft = float(reserved_soft.get(iid, 0.0))
        result[iid] = {
            "on_hand": oh,
            "incoming_supplier": inc_s,
            "incoming_wip": inc_w,
            "incoming": inc,
            "reserved_soft": soft,
            "available": oh - soft,
            "projected": oh + inc - soft,
            "uncovered": max(soft - oh_pos - inc, 0.0),
        }
    return result
