"""Ledger-2 (soft reservations) fold + distribution — design §3, §3.1, §5, §9.

Two layers:
  * PURE in-memory model (Reserve / IncomingLine / Pin / Pool) + `redistribute`
    — the exact 3-pass A→B→C algorithm of §5, byte-identical on repeat inputs
    (INV-idem-dist). The §7 worked examples run against these dataclasses.
  * Pure fold of reservation events + a thin Session writer that materializes
    the fold into a reservation_entry cache. No writer is wired into the
    planning pipeline in Inc1.

make-mode reservations are EXCLUDED from distribution and contribute exactly 0
to reserved_soft / available / projected (INV-RES-make-zero, §3.1). Their
make_uncovered is a separate formula (§3.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence, Tuple, Union

from sqlalchemy.orm import Session

from app import models

EPS = Decimal("1e-9")

Number = Union[int, float, Decimal, str]

CONSUME = "consume"
MAKE = "make"
BUY = "buy"

_SUPPLIER = "supplier_order"
_WIP = "wip_order"
_ON_HAND = "on_hand"


def _dec(value: Number) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# Event fold (INV-RES-fold, design §2.3 / §9)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReservationFold:
    reserved_qty: Decimal
    realized_qty: Decimal
    outstanding: Decimal


def fold_reservation_events(events: Iterable) -> ReservationFold:
    """PURE fold: reservation events → (reserved, realized, outstanding).

    reserved = Σ reserved_delta; realized = Σ realized_delta;
    outstanding = max(reserved − realized, 0)  (design §2.3, §9 INV-RES-fold).
    Accepts ORM ReservationEvent rows or any object with reserved_delta /
    realized_delta attributes, or (reserved_delta, realized_delta) tuples.
    """
    reserved = Decimal("0")
    realized = Decimal("0")
    for ev in events:
        if isinstance(ev, (tuple, list)):
            r_delta, z_delta = ev
        else:
            r_delta = getattr(ev, "reserved_delta", 0)
            z_delta = getattr(ev, "realized_delta", 0)
        reserved += _dec(r_delta)
        realized += _dec(z_delta)
    outstanding = reserved - realized
    if outstanding < 0:
        outstanding = Decimal("0")
    return ReservationFold(reserved, realized, outstanding)


def fold_reservation_entry(session: Session, reservation_id: int) -> ReservationFold:
    """Materialize the event fold into the reservation_entry cache (design §10).

    Reads reservation_event rows, writes reserved_qty/realized_qty onto the
    reservation_entry. Touches only ledger-2 tables. Returns the fold.
    """
    events = (
        session.query(models.ReservationEvent)
        .filter(models.ReservationEvent.reservation_id == reservation_id)
        .order_by(models.ReservationEvent.id.asc())
        .all()
    )
    fold = fold_reservation_events(events)
    entry = session.get(models.ReservationEntry, reservation_id)
    if entry is not None:
        entry.reserved_qty = fold.reserved_qty
        entry.realized_qty = fold.realized_qty
        session.flush()
    return fold


def append_realization_event(
    session: Session,
    entry: models.ReservationEntry,
    *,
    realized_delta: Number,
    sle_id: int | None,
    fact_ref: str,
    fact_line_ref: str,
    match_rule: str,
    cycle_id: str,
    idempotency_key: str,
    event_at: datetime | None = None,
    reserved_delta: Number | None = None,
    event_kind: str | None = None,
) -> bool:
    """Append one idempotent realization fact to the canonical event stream.

    Matching belongs to the caller.  This function is the single persistence
    boundary shared by MAKE, CONSUME and BUY allocators; the materialized
    ``ReservationEntry`` cache is rebuilt only by ``fold_reservation_entry``.
    """
    generation_id = int(entry.ledger_generation_id)
    existing = (
        session.query(models.ReservationEvent.id)
        .filter(
            models.ReservationEvent.ledger_generation_id == generation_id,
            models.ReservationEvent.idempotency_key == str(idempotency_key),
        )
        .first()
    )
    if existing is not None:
        return False
    if reserved_delta is None:
        reservation_seeded = (
            session.query(models.ReservationEvent.id)
            .filter(
                models.ReservationEvent.ledger_generation_id == generation_id,
                models.ReservationEvent.reservation_id == int(entry.id),
            )
            .first()
            is not None
        )
        reserved_delta = 0 if reservation_seeded else entry.reserved_qty
    delta = _dec(realized_delta)
    session.add(models.ReservationEvent(
        ledger_generation_id=generation_id,
        reservation_id=int(entry.id),
        item_id=int(entry.item_id),
        characteristic_ref=str(entry.characteristic_ref or ""),
        organization_ref=str(entry.organization_ref or ""),
        planning_stock_pool=str(entry.planning_stock_pool or "default"),
        event_kind=event_kind or ("realize" if delta >= 0 else "unrealize"),
        reserved_delta=_dec(reserved_delta),
        realized_delta=delta,
        sle_id=int(sle_id) if sle_id is not None else None,
        fact_ref=str(fact_ref or ""),
        fact_line_ref=str(fact_line_ref or ""),
        match_rule=str(match_rule or ""),
        cycle_id=str(cycle_id or ""),
        idempotency_key=str(idempotency_key),
        event_at=event_at or datetime.now(timezone.utc),
    ))
    session.flush()
    return True


# ---------------------------------------------------------------------------
# In-memory distribution model (design §5)
# ---------------------------------------------------------------------------


@dataclass
class IncomingLine:
    """One open incoming supply line (supplier or wip), design §5."""

    line_id: str
    source_kind: str  # supplier_order | wip_order
    remaining: Decimal
    due_date: object = None  # sort key; anything orderable / None
    order_ref: str = ""
    line_ref: str = ""

    def __post_init__(self) -> None:
        self.remaining = _dec(self.remaining)


@dataclass
class Pin:
    """A frozen coverage pin binding a reserve to one incoming line (design §5
    Pass A). pin_live = max(alloc − evaporated − realized, 0)."""

    line_id: str
    source_kind: str  # supplier_order | wip_order
    alloc_qty: Decimal
    evaporated_qty: Decimal = Decimal("0")
    realized_qty: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        self.alloc_qty = _dec(self.alloc_qty)
        self.evaporated_qty = _dec(self.evaporated_qty)
        self.realized_qty = _dec(self.realized_qty)

    @property
    def pin_live(self) -> Decimal:
        live = self.alloc_qty - self.evaporated_qty - self.realized_qty
        return live if live > 0 else Decimal("0")


@dataclass
class Reserve:
    """An active reservation participating in distribution (design §5)."""

    key: Tuple  # K(r) = (priority_period_from, priority_period_to, run_id, requirement_id)
    reserved_qty: Decimal
    realized_qty: Decimal = Decimal("0")
    realization_mode: str = CONSUME
    requirement_id: Optional[int] = None
    pins: List[Pin] = field(default_factory=list)
    # make-side supply state (§3.1)
    produced_qty: Decimal = Decimal("0")
    live_supplier_pin_qty: Decimal = Decimal("0")
    own_open_supply_qty: Decimal = Decimal("0")
    # distribution outputs (rewritten by redistribute)
    covered_on_hand: Decimal = Decimal("0")
    covered_incoming_supplier: Decimal = Decimal("0")
    covered_incoming_wip: Decimal = Decimal("0")
    uncovered: Decimal = Decimal("0")
    coverage_state: str = "uncovered"

    def __post_init__(self) -> None:
        self.reserved_qty = _dec(self.reserved_qty)
        self.realized_qty = _dec(self.realized_qty)
        self.produced_qty = _dec(self.produced_qty)
        self.live_supplier_pin_qty = _dec(self.live_supplier_pin_qty)
        self.own_open_supply_qty = _dec(self.own_open_supply_qty)

    @property
    def outstanding(self) -> Decimal:
        out = self.reserved_qty - self.realized_qty
        return out if out > 0 else Decimal("0")

    @property
    def covered(self) -> Decimal:
        return (
            self.covered_on_hand
            + self.covered_incoming_supplier
            + self.covered_incoming_wip
        )


@dataclass
class Coverage:
    """A floating coverage assignment produced by redistribute (design §2.4)."""

    reserve_key: Tuple
    source_kind: str  # on_hand | supplier_order | wip_order
    source_ref: str
    covered_qty: Decimal


@dataclass
class Pool:
    """A distribution pool (design §3 / §5): raw on_hand + reserves + lines."""

    on_hand: Decimal
    reserves: List[Reserve] = field(default_factory=list)
    lines: List[IncomingLine] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.on_hand = _dec(self.on_hand)


@dataclass
class RedistributeResult:
    coverages: List[Coverage]
    reserves: List[Reserve]  # same objects, covered_* / uncovered filled


# ---------------------------------------------------------------------------
# Pool math (design §3)
# ---------------------------------------------------------------------------


def _consume_active(pool: Pool) -> List[Reserve]:
    return [r for r in pool.reserves if str(r.realization_mode) == CONSUME]


def on_hand_pos(pool: Pool) -> Decimal:
    """on_hand⁺ — only for coverage (design §4a): negative naличие can't cover."""
    return pool.on_hand if pool.on_hand > 0 else Decimal("0")


def reserved_soft(pool: Pool) -> Decimal:
    """Σ outstanding over active consume-mode reserves (design §3). make/buy → exactly 0."""
    return sum((r.outstanding for r in _consume_active(pool)), Decimal("0"))


def incoming(pool: Pool) -> Decimal:
    return sum((line.remaining for line in pool.lines), Decimal("0"))


def available(pool: Pool) -> Decimal:
    """on_hand − reserved_soft. MAY be < 0 — a surfaced deficit, never clamped."""
    return pool.on_hand - reserved_soft(pool)


def projected(pool: Pool) -> Decimal:
    return pool.on_hand + incoming(pool) - reserved_soft(pool)


def uncovered_pool(pool: Pool) -> Decimal:
    """max(reserved_soft − on_hand⁺ − incoming, 0) — the pool-level identity
    that Σ uncovered(r) must equal after full distribution (INV-RES-uncov)."""
    val = reserved_soft(pool) - on_hand_pos(pool) - incoming(pool)
    return val if val > 0 else Decimal("0")


def coverage_state_for(outstanding: Number, covered: Number) -> str:
    outstanding = _dec(outstanding)
    covered = _dec(covered)
    if outstanding <= EPS:
        return "covered"
    if covered + EPS >= outstanding:
        return "covered"
    if covered <= EPS:
        return "uncovered"
    return "partial"


# ---------------------------------------------------------------------------
# make-side formulas (design §3.1)
# ---------------------------------------------------------------------------


def make_uncovered(reserve: Reserve) -> Decimal:
    """max(reserved − produced − Σ live supplier pins, 0) (design §3.1).

    Only supplier pins are subtracted — a производственный (wip) заказ under the
    make-reserve is own_open_supply, handled by make_materialization_gap.
    """
    val = reserve.reserved_qty - reserve.produced_qty - reserve.live_supplier_pin_qty
    return val if val > 0 else Decimal("0")


def make_materialization_gap(reserve: Reserve) -> Decimal:
    """make_uncovered minus own already-open production coverage (G2, v2 §8):
    the residual that would spawn a NEW production proposal (design §3.1)."""
    val = make_uncovered(reserve) - reserve.own_open_supply_qty
    return val if val > 0 else Decimal("0")


# ---------------------------------------------------------------------------
# redistribute — the exact 3-pass A→B→C (design §5)
# ---------------------------------------------------------------------------


def _assert_invariants(reserves: Sequence[Reserve], remaining_now, on_hand_p: Decimal) -> None:
    # covered(r) ≤ outstanding(r)
    for r in reserves:
        assert r.covered <= r.outstanding + EPS, (
            f"INV-RES-noverbook: covered {r.covered} > outstanding {r.outstanding} for {r.key}"
        )
    # Σ covered_on_hand ≤ on_hand⁺
    total_oh = sum((r.covered_on_hand for r in reserves), Decimal("0"))
    assert total_oh <= on_hand_p + EPS, (
        f"INV-RES-noverbook: Σ covered_on_hand {total_oh} > on_hand⁺ {on_hand_p}"
    )
    # Σ covered per incoming line ≤ its original remaining (INV-RES-shared-line):
    # remaining_now never went negative — checked at decrement time.
    for line_id, rem in remaining_now.items():
        assert rem >= -EPS, f"INV-RES-shared-line: remaining({line_id}) went negative: {rem}"


def redistribute(pool: Pool) -> RedistributeResult:
    """PURE 3-pass distribution (design §5). Rewrites each consume reserve's
    covered_* / uncovered / coverage_state and returns floating Coverage rows.
    Does NOT mutate pool.on_hand or line.remaining (idempotent — INV-idem-dist).
    make reserves never participate.
    """
    consume = sorted(_consume_active(pool), key=lambda r: r.key)  # oldest-first K(r)

    # reset outputs so a re-run from the same inputs is byte-identical
    for r in consume:
        r.covered_on_hand = Decimal("0")
        r.covered_incoming_supplier = Decimal("0")
        r.covered_incoming_wip = Decimal("0")
        r.uncovered = Decimal("0")
        r.coverage_state = "uncovered"

    on_hand_p = on_hand_pos(pool)
    oh = on_hand_p
    # ONE shared remaining counter per line for Pass A and Pass C (INV-RES-shared-line).
    remaining = {line.line_id: line.remaining for line in pool.lines}
    line_by_id = {line.line_id: line for line in pool.lines}

    def _add_incoming(reserve: Reserve, source_kind: str, take: Decimal) -> None:
        if source_kind == _SUPPLIER:
            reserve.covered_incoming_supplier += take
        else:
            reserve.covered_incoming_wip += take

    # --- Pass A: frozen pins (consistent with K by freeze construction) ---
    for r in consume:
        for pin in r.pins:
            if pin.source_kind not in (_SUPPLIER, _WIP):
                continue
            avail_line = remaining.get(pin.line_id, Decimal("0"))
            need = r.outstanding - r.covered
            take = min(pin.pin_live, avail_line, need)
            if take < 0:
                take = Decimal("0")
            if take > 0:
                _add_incoming(r, pin.source_kind, take)
                remaining[pin.line_id] = avail_line - take
    _assert_invariants(consume, remaining, on_hand_p)

    # --- Pass B: on_hand, cumulative oldest-first (floating; no stock pins) ---
    for r in consume:
        need = r.outstanding - r.covered
        take = min(need, oh)
        if take < 0:
            take = Decimal("0")
        if take > 0:
            r.covered_on_hand += take
            oh -= take
    _assert_invariants(consume, remaining, on_hand_p)

    # --- Pass C: free (un-pinned) incoming remainder, by reserve priority;
    #     within a reserve, lines in (due_date, order_ref, line_ref) order. ---
    def _line_sort_key(line: IncomingLine):
        return (
            0 if line.due_date is not None else 1,
            line.due_date if line.due_date is not None else 0,
            line.order_ref,
            line.line_ref,
            line.line_id,
        )

    ordered_lines = sorted(pool.lines, key=_line_sort_key)
    for r in consume:
        need = r.outstanding - r.covered
        if need <= EPS:
            continue
        for line in ordered_lines:
            rem = remaining.get(line.line_id, Decimal("0"))
            if rem <= EPS or need <= EPS:
                continue
            take = min(need, rem)
            _add_incoming(r, line.source_kind, take)
            remaining[line.line_id] = rem - take
            need -= take
    _assert_invariants(consume, remaining, on_hand_p)

    # --- finalize caches + floating coverage rows ---
    coverages: List[Coverage] = []
    for r in consume:
        unc = r.outstanding - r.covered
        r.uncovered = unc if unc > 0 else Decimal("0")
        r.coverage_state = coverage_state_for(r.outstanding, r.covered)
        if r.covered_on_hand > 0:
            coverages.append(Coverage(r.key, _ON_HAND, "pool", r.covered_on_hand))
        if r.covered_incoming_supplier > 0:
            coverages.append(Coverage(r.key, _SUPPLIER, "", r.covered_incoming_supplier))
        if r.covered_incoming_wip > 0:
            coverages.append(Coverage(r.key, _WIP, "", r.covered_incoming_wip))

    # --- INV-RES-uncov: Σ uncovered(r) == pool identity ---
    total_uncovered = sum((r.uncovered for r in consume), Decimal("0"))
    assert abs(total_uncovered - uncovered_pool(pool)) <= EPS, (
        f"INV-RES-uncov: Σ uncovered(r) {total_uncovered} != "
        f"max(reserved_soft − on_hand⁺ − incoming, 0) {uncovered_pool(pool)}"
    )

    return RedistributeResult(coverages=coverages, reserves=consume)
