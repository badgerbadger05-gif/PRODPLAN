"""Pure canonical make/buy reservation amounts and event fold."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Literal, Union

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models

EPS = Decimal("1e-9")
Number = Union[int, float, Decimal, str]
MAKE = "make"
BUY = "buy"
# A known rework demand has no execution journal yet.  It is nevertheless a
# frozen reservation and an assembly receipt may realize it.
REWORK = "rework"


def reservation_business_identity(requirement_id: int, realization_mode: str) -> str:
    """Stable obligation identity; generation is deliberately excluded."""
    value = f"reservation:req:{int(requirement_id)}:mode:{str(realization_mode or '').strip()}"
    if len(value) > 256:
        raise ValueError("reservation business identity exceeds 256 characters")
    return value


def reservation_event_identity(
    entry: models.ReservationEntry,
    *,
    event_kind: str,
    reserved_delta: Number,
    realized_delta: Number,
    sle_id: int | None,
    fact_ref: str,
    fact_line_ref: str,
    match_rule: str,
) -> str:
    """Stable semantic event identity, excluding generation/cycle/idempotency."""
    import hashlib

    def _qty(value: Number) -> str:
        return format(_dec(value).quantize(Decimal("0.001")), "f")

    raw = "|".join((
        reservation_business_identity(int(entry.requirement_id), str(entry.realization_mode)),
        str(event_kind or ""),
        str(int(sle_id)) if sle_id is not None else "",
        str(fact_ref or "").strip(),
        str(fact_line_ref or "").strip(),
        _qty(reserved_delta),
        _qty(realized_delta),
        str(match_rule or "").strip(),
    ))
    if len(raw) <= 320:
        return raw
    return f"{raw[:255]}|sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def reservation_event_origin_kind(
    *, event_kind: str, realized_delta: Number, sle_id: int | None
) -> str:
    if sle_id is None and _dec(realized_delta) == 0:
        return "obligation"
    if str(event_kind or "") == "unrealize" or _dec(realized_delta) < 0:
        return "correction"
    return "factual"


def _dec(value: Number) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class FrozenReservation:
    reserved_qty: Decimal
    covered_from_stock_at_freeze_qty: Decimal
    replenishment_required_qty: Decimal


def freeze_reservation_amounts(
    reserved_qty: Number,
    free_stock_qty: Number,
) -> FrozenReservation:
    reserved = max(_dec(reserved_qty), Decimal("0"))
    free_stock = max(_dec(free_stock_qty), Decimal("0"))
    covered = min(reserved, free_stock)
    return FrozenReservation(
        reserved_qty=reserved,
        covered_from_stock_at_freeze_qty=covered,
        replenishment_required_qty=reserved - covered,
    )


def replenishment_remaining(required_qty: Number, received_qty: Number) -> Decimal:
    return max(_dec(required_qty) - _dec(received_qty), Decimal("0"))


def replenishment_execution_pct(
    required_qty: Number,
    received_qty: Number,
) -> Decimal | None:
    required = max(_dec(required_qty), Decimal("0"))
    if required <= EPS:
        return None
    received = min(max(_dec(received_qty), Decimal("0")), required)
    return received / required * Decimal("100")


ExecutionProgressStatus = Literal[
    "unavailable", "not_started", "in_progress", "complete", "lower_bound"
]


def replenishment_execution_status(
    required_qty: Number,
    received_qty: Number,
    *,
    partial_truth: bool = False,
) -> ExecutionProgressStatus:
    required = max(_dec(required_qty), Decimal("0"))
    if required <= EPS:
        return "unavailable"
    received = min(max(_dec(received_qty), Decimal("0")), required)
    if partial_truth:
        return "lower_bound"
    if received <= EPS:
        return "not_started"
    if received >= required - EPS:
        return "complete"
    return "in_progress"


@dataclass(frozen=True)
class ReservationFold:
    reserved_qty: Decimal
    realized_qty: Decimal
    outstanding: Decimal


def fold_reservation_events(events: Iterable) -> ReservationFold:
    reserved = Decimal("0")
    realized = Decimal("0")
    for event in events:
        if isinstance(event, (tuple, list)):
            reserved_delta, realized_delta = event
        else:
            reserved_delta = getattr(event, "reserved_delta", 0)
            realized_delta = getattr(event, "realized_delta", 0)
        reserved += _dec(reserved_delta)
        realized += _dec(realized_delta)
    return ReservationFold(
        reserved,
        realized,
        max(reserved - realized, Decimal("0")),
    )


def fold_reservation_entry(
    session: Session,
    reservation_id: int,
) -> ReservationFold:
    entry = session.get(models.ReservationEntry, reservation_id)

    event_query = session.query(models.ReservationEvent).filter(
        models.ReservationEvent.reservation_id == reservation_id,
    )
    if entry is not None:
        if bool(getattr(entry, "is_current", False)):
            # A stable owner may retain factual events whose source generation
            # predates the current publication.  The current flag, not that
            # provenance value, defines the accepted fold.
            event_query = event_query.filter(
                models.ReservationEvent.is_current.is_(True),
            )
        else:
            event_query = event_query.filter(
                models.ReservationEvent.ledger_generation_id == entry.ledger_generation_id,
            )

    events = (
        event_query
        .order_by(models.ReservationEvent.id.asc())
        .all()
    )
    fold = fold_reservation_events(events)

    if entry is not None:
        entry.reserved_qty = fold.reserved_qty
        entry.realized_qty = fold.realized_qty
        entry.replenishment_received_qty = min(
            fold.realized_qty,
            _dec(entry.replenishment_required_qty),
        )
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
    generation_id = int(entry.ledger_generation_id)
    resolved_kind = event_kind or ("realize" if _dec(realized_delta) >= 0 else "unrealize")
    delta = _dec(realized_delta)
    if reserved_delta is None:
        seeded_query = session.query(models.ReservationEvent.id).filter(
            models.ReservationEvent.reservation_id == int(entry.id),
            models.ReservationEvent.origin_kind == "obligation",
        )
        if not bool(getattr(entry, "is_current", False)):
            seeded_query = seeded_query.filter(
                models.ReservationEvent.ledger_generation_id == generation_id,
            )
        seeded = seeded_query.first() is not None
        reserved_delta = Decimal("0") if seeded else _dec(entry.reserved_qty)
    event_identity = reservation_event_identity(
        entry,
        event_kind=resolved_kind,
        reserved_delta=reserved_delta,
        realized_delta=delta,
        sle_id=sle_id,
        fact_ref=fact_ref,
        fact_line_ref=fact_line_ref,
        match_rule=match_rule,
    )
    if session.query(models.ReservationEvent.id).filter(
        models.ReservationEvent.event_identity == event_identity,
        models.ReservationEvent.reservation_id == int(entry.id),
    ).first() is not None:
        return False
    exists = (
        session.query(models.ReservationEvent.id)
        .filter(
            models.ReservationEvent.ledger_generation_id == generation_id,
            models.ReservationEvent.idempotency_key == str(idempotency_key),
        )
        .first()
    )
    if exists is not None:
        return False
    if sle_id is not None and delta != 0:
        sle = session.get(models.StockLedgerEntry, int(sle_id))
        if sle is None:
            raise ValueError(f"realization references missing SLE {sle_id}")
        physical_qty = _dec(sle.qty)
        if physical_qty == 0 or (physical_qty > 0) != (delta > 0):
            raise ValueError(
                f"realization sign conflicts with physical SLE {sle_id}"
            )
        same_allocation = (
            session.query(models.ReservationEvent)
            .filter(
                models.ReservationEvent.ledger_generation_id == generation_id,
                models.ReservationEvent.reservation_id == int(entry.id),
                models.ReservationEvent.sle_id == int(sle_id),
                models.ReservationEvent.realized_delta != 0,
            )
            .one_or_none()
        )
        if same_allocation is not None:
            if (
                _dec(same_allocation.realized_delta) == delta
                and str(same_allocation.fact_ref or "") == str(fact_ref or "")
                and str(same_allocation.fact_line_ref or "")
                == str(fact_line_ref or "")
            ):
                return False
            raise ValueError(
                "one physical SLE cannot be allocated to the same reservation twice"
            )
        allocated = _dec(
            session.query(func.coalesce(func.sum(models.ReservationEvent.realized_delta), 0))
            .filter(
                models.ReservationEvent.ledger_generation_id == generation_id,
                models.ReservationEvent.sle_id == int(sle_id),
                models.ReservationEvent.realized_delta != 0,
            )
            .scalar()
        )
        if abs(allocated + delta) > abs(physical_qty):
            raise ValueError(
                f"realization allocations exceed physical SLE {sle_id}"
            )
    if reserved_delta is None:
        seeded = (
            session.query(models.ReservationEvent.id)
            .filter(
                models.ReservationEvent.ledger_generation_id == generation_id,
                models.ReservationEvent.reservation_id == int(entry.id),
            )
            .first()
            is not None
        )
        reserved_delta = 0 if seeded else entry.reserved_qty
    session.add(
        models.ReservationEvent(
            ledger_generation_id=generation_id,
            reservation_id=int(entry.id),
            item_id=int(entry.item_id),
            characteristic_ref=str(entry.characteristic_ref or ""),
            organization_ref=str(entry.organization_ref or ""),
            planning_stock_pool=str(entry.planning_stock_pool or "default"),
            event_kind=resolved_kind,
            reserved_delta=_dec(reserved_delta),
            realized_delta=delta,
            sle_id=int(sle_id) if sle_id is not None else None,
            fact_ref=str(fact_ref or ""),
            fact_line_ref=str(fact_line_ref or ""),
            match_rule=str(match_rule or ""),
            cycle_id=str(cycle_id or ""),
            idempotency_key=str(idempotency_key),
            event_identity=event_identity,
            origin_kind=reservation_event_origin_kind(
                event_kind=resolved_kind, realized_delta=delta, sle_id=sle_id,
            ),
            is_current=bool(getattr(entry, "is_current", False)),
            event_at=event_at or datetime.now(timezone.utc),
        )
    )
    session.flush()
    return True
