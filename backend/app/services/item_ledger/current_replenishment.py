"""Transactional current replenishment application.

R0 owns the pure addressed/FIFO change planner.  This module is the single
database writer for the first current-execution slice: it applies that plan to
the already accepted generation's reservation allocation rows, folds the
result into the existing reservation execution fields, and records a small
source/revision marker in the same transaction.  It deliberately never creates
or copies a generation or snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Iterable, Literal, Mapping

from sqlalchemy import or_, and_
from sqlalchemy.orm import Session

from app import models

from .historical_replay_core import (
    Allocation,
    AllocationChangePlan,
    AllocationUpdate,
    Fact,
    Reserve,
    ReplayResult,
    ReserveRealization,
    plan_allocation_changes,
)
from .reservation import reservation_business_identity


WRITER_KEY = "current_replenishment"
DistributionScope = tuple[int, str, str, str, str]


#: Canonical audit reason for a clear-out the caller explicitly acknowledged.
CONFIRMED_EMPTY_REASON = "confirmed_empty_scope"

#: The source stream of a distribution scope is a property of the scope, not
#: of whoever is writing it (canon R4/R5: one canonical writer per scope).
#: Every entry path - the full accept, the one-off bootstrap and both bounded
#: replays - names the same stream, so entering a scope through a different
#: door is not a stream change.
SUPPLIER_RECEIPT_SOURCE_KEY = "supplier-receipts"
ASSEMBLY_OUTPUT_SOURCE_KEY = "assembly-outputs"

#: Keys earlier entry paths wrote for these same two streams.  They are
#: accepted once and rewritten in place on the next write, which is why no
#: data migration is needed; they are not a fallback for a foreign stream.
SOURCE_KEY_ALIASES = {
    "accepted-physical-receipts": SUPPLIER_RECEIPT_SOURCE_KEY,
    "physical-refresh:buy": SUPPLIER_RECEIPT_SOURCE_KEY,
    "physical-refresh:make": ASSEMBLY_OUTPUT_SOURCE_KEY,
}


def canonical_source_key(value: object) -> str:
    """Resolve a source key to the stream it names."""
    text = str(value or "").strip()
    return SOURCE_KEY_ALIASES.get(text, text)


class CurrentReplenishmentError(ValueError):
    """A current application is unavailable and must fail closed."""


@dataclass(frozen=True)
class CurrentReplenishmentResult:
    generation_id: int
    source_key: str
    source_revision: int
    inserted: int
    updated: int
    deleted: int
    changed_pairs: int
    audit_events: int
    idempotent: bool = False
    #: The operator acknowledgement that authorised clearing a populated
    #: scope, echoed for the caller's own log.  Empty for ordinary replays.
    confirmed_empty_reason: str = ""


@dataclass(frozen=True)
class BoundedMakeReplenishmentResult:
    """Evidence returned by the bounded current-owner make adapter.

    The adapter deliberately reports scope-bounded work separately from the
    historical generation publisher.  ``scope_history_rows`` is the number of
    visible ``assembly_in`` facts read across the requested distribution
    scopes; it is never a count or scan of the whole physical ledger.
    """

    target_generation_id: int
    parent_generation_id: int
    source_revision: int
    affected_scopes: tuple[DistributionScope, ...]
    scope_history_rows: int
    results: tuple[CurrentReplenishmentResult, ...]

    @property
    def fact_rows(self) -> int:
        return int(self.scope_history_rows)

    @property
    def audit_events(self) -> int:
        return sum(int(result.audit_events) for result in self.results)


@dataclass(frozen=True)
class BoundedBuyReplenishmentResult:
    """Evidence returned by the bounded current BUY receipt adapter."""

    target_generation_id: int
    parent_generation_id: int
    source_revision: int
    affected_scopes: tuple[DistributionScope, ...]
    delta_fact_rows: int
    scope_replay_rows: int
    results: tuple[CurrentReplenishmentResult, ...]

    @property
    def fact_rows(self) -> int:
        return int(self.delta_fact_rows)

    @property
    def replayed_rows(self) -> int:
        return int(self.scope_replay_rows)

    @property
    def audit_events(self) -> int:
        return sum(int(result.audit_events) for result in self.results)


@dataclass(frozen=True)
class BoundedBuyReceiptDeltaManifest:
    """Explicit typed BUY evidence for one bounded physical refresh."""

    new_sle_ids: tuple[int, ...] = ()
    receipt_facts: tuple[object, ...] = ()
    # Corrections/returns may need the complete signed stream for the
    # affected scope.  It is never discovered from a generation-wide query.
    scope_receipt_facts: tuple[object, ...] = ()
    supersession_edge_ids: tuple[int, ...] = ()
    backdate_from: datetime | None = None


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _text(value: object) -> str:
    return str(value or "").strip()


def _comparable_datetime(value: datetime | None) -> datetime | None:
    """Compare DB/SQLite datetimes without changing their instant semantics.

    SQLite returns ``DateTime(timezone=True)`` values as naive while PostgreSQL
    returns aware UTC values.  Bounded validation must behave identically on
    both backends; all boundaries are UTC source timestamps, so stripping only
    the adapter-level timezone marker is safe here.
    """

    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _normalise_bounded_buy_manifest(
    value: BoundedBuyReceiptDeltaManifest | Mapping[str, object],
) -> BoundedBuyReceiptDeltaManifest:
    if isinstance(value, BoundedBuyReceiptDeltaManifest):
        result = value
    elif isinstance(value, Mapping):
        raw_backdate = value.get("backdate_from")
        if raw_backdate not in (None, "") and not isinstance(raw_backdate, datetime):
            try:
                raw_backdate = datetime.fromisoformat(str(raw_backdate))
            except ValueError as exc:
                raise CurrentReplenishmentError(
                    "bounded BUY backdate boundary is malformed"
                ) from exc
        try:
            result = BoundedBuyReceiptDeltaManifest(
                new_sle_ids=tuple(int(item) for item in value.get("new_sle_ids", ())),
                receipt_facts=tuple(
                    value.get("receipt_facts", value.get("delta_facts", ()))
                ),
                scope_receipt_facts=tuple(
                    value.get("scope_receipt_facts", value.get("scope_facts", ()))
                ),
                supersession_edge_ids=tuple(
                    int(item) for item in value.get("supersession_edge_ids", ())
                ),
                backdate_from=raw_backdate,
            )
        except (TypeError, ValueError) as exc:
            raise CurrentReplenishmentError("bounded BUY manifest is malformed") from exc
    else:
        raise CurrentReplenishmentError("bounded BUY manifest is required")
    if any(item <= 0 for item in result.new_sle_ids + result.supersession_edge_ids):
        raise CurrentReplenishmentError("bounded BUY manifest IDs must be positive")
    if len(set(result.new_sle_ids)) != len(result.new_sle_ids):
        raise CurrentReplenishmentError("bounded BUY manifest has duplicate SLE IDs")
    if len(set(result.supersession_edge_ids)) != len(result.supersession_edge_ids):
        raise CurrentReplenishmentError(
            "bounded BUY manifest has duplicate supersession IDs"
        )
    return result


def distribution_scope_for_fact(
    item_id: int,
    characteristic_ref: str,
    organization_ref: str,
) -> tuple[int, str, str, str]:
    """The pool part of a fact's distribution scope, canonically collapsed."""
    from app.services.mrp_freeze import distribution_scope_for

    return distribution_scope_for(
        int(item_id), characteristic_ref, organization_ref, mode="",
    )[:4]


def _distribution_scope(value: Fact | Reserve) -> tuple[int, str, str, str, str]:
    return (
        int(value.item_id),
        _text(value.characteristic_ref),
        _text(value.organization_ref),
        _text(value.planning_stock_pool),
        _text(value.mode),
    )


def scope_modes(scope_mode: str) -> tuple[str, ...]:
    """The realization modes one distribution scope owns.

    A MAKE scope owns ``rework`` as well.  Canon §18: an accepted
    ``assembly_in`` closes a rework reserve as much as a make one, and it is
    the same physical fact, so both kinds of owner take part in the same
    replay - which means the replay must also *see and own* the allocations
    of both.  Selecting existing allocations by mode equality hid a rework
    owner's allocation from every later refresh, so each refresh re-inserted
    it: a duplicate current basis, double coverage, and an IntegrityError on
    ``uq_res_consumption_current_sle_reservation`` where that index exists.
    """
    return ("make", "rework") if scope_mode == "make" else (scope_mode,)


def scope_mode_for_owner(realization_mode: str) -> str:
    """Which scope mode an owner of this realization mode belongs to."""
    mode = _text(realization_mode)
    return "make" if mode in {"make", "rework"} else mode


def _allocation_scope(
    row: models.ReservationConsumptionAllocation,
    entry: models.ReservationEntry,
) -> tuple[int, str, str, str, str]:
    """The scope an existing allocation belongs to.

    The allocation keeps its owner's real mode through the reservation it
    points at; this answers the different question of which *scope* replays
    it, so a rework owner's allocation is claimed by the MAKE scope.
    """
    return (
        int(row.item_id),
        _text(row.characteristic_ref),
        _text(row.organization_ref),
        _text(row.planning_stock_pool),
        scope_mode_for_owner(entry.realization_mode),
    )


def _scope_key(scope: DistributionScope) -> str:
    return json.dumps(list(scope), ensure_ascii=False, separators=(",", ":"))


def _reserve_business_identity(row: Reserve) -> str:
    """Canonical identity used before ORM rows are available.

    ReservationEntry.current_identity is populated from this exact contract;
    using the same requirement/mode key in the source marker prevents a new
    BUILDING physical id from changing an otherwise identical replay input.
    """
    return f"reservation:req:{int(row.requirement_id)}:mode:{_text(row.mode)}"


def _input_checksum(
    facts: tuple[Fact, ...], reserves: tuple[Reserve, ...], scope_key: str
) -> str:
    payload = {
        "distribution_scope": scope_key,
        "facts": [
            {
                "id": str(row.fact_id),
                "item": int(row.item_id),
                "mode": row.mode,
                "qty": str(row.qty),
                "posting_at": row.posting_at.isoformat(),
                "characteristic": _text(row.characteristic_ref),
                "organization": _text(row.organization_ref),
                "pool": _text(row.planning_stock_pool),
                "requirement": row.requirement_id,
                "order": row.order_ref,
            }
            for row in sorted(facts, key=lambda item: str(item.fact_id))
        ],
        "reserves": [
            {
                "id": _reserve_business_identity(row),
                "item": int(row.item_id),
                "mode": row.mode,
                "qty": str(row.reserved_qty),
                "due": row.due_date.isoformat(),
                "from": row.plan_period_from.isoformat(),
                "to": row.plan_period_to.isoformat(),
                "run": int(row.run_id),
                "requirement": int(row.requirement_id),
                "characteristic": _text(row.characteristic_ref),
                "organization": _text(row.organization_ref),
                "pool": _text(row.planning_stock_pool),
                **_baseline_part(row),
            }
            for row in sorted(reserves, key=lambda item: str(item.reserve_id))
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _baseline_part(row: Reserve) -> dict[str, str]:
    """The owner's freeze cutoff is allocation input (§49); absent = legacy."""
    baseline = getattr(row, "baseline_at", None)
    return {"baseline": baseline.isoformat()} if baseline is not None else {}


def freeze_baselines_by_reservation(
    db: Session, entries: Iterable[models.ReservationEntry]
) -> dict[int, datetime]:
    """Each owner's freeze cutoff: its run's ``MrpFreezeBaseline.baseline_at``.

    The one source of the §49 boundary for every replenishment path (the
    generation replay, the supplier rebuild and the R4 adapters), keyed
    exactly as the consumption allocator keys it (run, freeze version, item,
    characteristic, organization, pool).

    It fails closed like the consumption side: an owner whose run was frozen
    (the run has baseline rows for the owner's freeze version) but that has no
    baseline of its own, or a null one, is refused.  The one explicit
    compatibility branch is a run with no freeze baseline at all - data from
    before freeze baselines existed - which replays without a boundary.
    """
    rows = list(entries)
    run_ids = sorted({int(row.run_id) for row in rows if row.run_id is not None})
    if not run_ids:
        return {}
    by_key: dict[tuple[int, int, int, str, str, str], datetime | None] = {}
    frozen_versions: set[tuple[int, int]] = set()
    for baseline in db.query(models.MrpFreezeBaseline).filter(
        models.MrpFreezeBaseline.run_id.in_(run_ids)
    ):
        frozen_versions.add((int(baseline.run_id), int(baseline.freeze_version)))
        by_key[(
            int(baseline.run_id), int(baseline.freeze_version), int(baseline.item_id),
            _text(baseline.characteristic_ref), _text(baseline.organization_ref),
            _text(baseline.planning_stock_pool),
        )] = baseline.baseline_at
    result: dict[int, datetime] = {}
    for row in rows:
        if row.run_id is None:
            continue
        version = (int(row.run_id), int(row.freeze_version or 0))
        if version not in frozen_versions:
            # Pre-baseline compatibility: nothing recorded for this freeze.
            continue
        found = by_key.get((
            *version, int(row.item_id),
            _text(row.characteristic_ref), _text(row.organization_ref),
            _text(row.planning_stock_pool),
        ))
        if found is None:
            raise CurrentReplenishmentError(
                f"reservation {int(row.id)} lacks exact frozen pool baseline"
            )
        result[int(row.id)] = found
    return result


def _receipt_input_checksum(
    facts: tuple[object, ...],
    reserves: tuple[Reserve, ...],
    scope_key: str,
    history_mode: str,
) -> str:
    """Checksum the complete signed source stream, including both time axes."""

    payload = {
        "distribution_scope": scope_key,
        "history_mode": history_mode,
        "facts": [
            {
                "id": int(row.sle_id),
                "item": int(row.item_id),
                "qty": str(row.signed_qty),
                "posting_at": row.posting_at.isoformat(),
                "known_at": row.known_at.isoformat() if row.known_at else None,
                "order": _text(row.supplier_order_ref),
                "line": _text(row.supplier_order_line_no),
                "receipt": _text(row.receipt_ref),
                "correction": _text(row.correction_receipt_ref),
                "pool": _text(row.planning_stock_pool),
            }
            for row in sorted(facts, key=lambda item: int(item.sle_id))
        ],
        "reserves": [
            {
                "id": _reserve_business_identity(row),
                "qty": str(row.reserved_qty),
                "due": row.due_date.isoformat(),
                "requirement": int(row.requirement_id),
                **_baseline_part(row),
            }
            for row in sorted(reserves, key=lambda item: str(item.reserve_id))
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _receipt_change_plan(
    replay: object,
    previous_allocations: tuple[Allocation, ...],
    reserves: tuple[Reserve, ...],
    receipt_facts: tuple[object, ...],
    reservation_identity_by_id: dict[str, str],
) -> AllocationChangePlan:
    """Adapt the canonical signed supplier replay to R4 persistence deltas."""

    after = tuple(
        Allocation(
            fact_id=str(row.fact.sle_id),
            reserve_id=reservation_identity_by_id.get(
                str(int(row.reservation.id)), ""
            ),
            qty=_decimal(row.qty),
            match_rule=(
                row.match_rule
                if row.match_rule in ("pegged", "fifo", "mixed")
                else "fifo"
            ),
            is_addressed=row.match_rule == "pegged",
        )
        for row in replay.allocations
    )
    if any(not row.reserve_id for row in after):
        raise CurrentReplenishmentError(
            "receipt replay references a reservation without stable current identity"
        )
    before_by_key = {(row.fact_id, row.reserve_id): row for row in previous_allocations}
    after_by_key = {(row.fact_id, row.reserve_id): row for row in after}
    reserve_qty = {
        str(row.reserve_id): Decimal("0") for row in reserves
    }
    for row in after:
        reserve_qty[row.reserve_id] = reserve_qty.get(row.reserve_id, Decimal("0")) + row.qty
    realizations = tuple(
        ReserveRealization(
            reserve_id=row.reserve_id,
            reserved_qty=row.reserved_qty,
            realized_qty=reserve_qty.get(row.reserve_id, Decimal("0")),
        )
        for row in reserves
    )
    result = ReplayResult(
        allocations=after,
        surplus=(),
        realizations=realizations,
        fact_qty=sum((row.signed_qty for row in receipt_facts), Decimal("0")),
        allocated_qty=sum((row.qty for row in after), Decimal("0")),
        surplus_qty=_decimal(replay.surplus_qty),
    )
    return AllocationChangePlan(
        result=result,
        insertions=tuple(after_by_key[key] for key in sorted(after_by_key.keys() - before_by_key.keys())),
        updates=tuple(
            AllocationUpdate(before_by_key[key], after_by_key[key])
            for key in sorted(after_by_key.keys() & before_by_key.keys())
            if before_by_key[key] != after_by_key[key]
        ),
        deletions=tuple(before_by_key[key] for key in sorted(before_by_key.keys() - after_by_key.keys())),
    )


def _as_allocation(
    row: models.ReservationConsumptionAllocation,
    reservation_identity_by_id: dict[str, str],
) -> Allocation:
    identity = reservation_identity_by_id.get(str(int(row.reservation_id)))
    if not identity:
        raise CurrentReplenishmentError(
            f"allocation references reservation {row.reservation_id} without stable current identity"
        )
    return Allocation(
        fact_id=str(int(row.sle_id)),
        reserve_id=identity,
        qty=_decimal(row.allocated_qty),
        match_rule=_text(row.match_rule),
        is_addressed=_text(row.match_rule) == "pegged",
    )


def _require_complete_scope(
    facts: tuple[Fact, ...],
    reserves: tuple[Reserve, ...],
    complete_scope: bool,
    distribution_scope: DistributionScope | None,
) -> DistributionScope:
    if complete_scope is not True:
        raise CurrentReplenishmentError(
            "current replenishment requires an explicit complete scope"
        )
    fact_ids = [str(f.fact_id) for f in facts]
    reserve_ids = [str(r.reserve_id) for r in reserves]
    if len(fact_ids) != len(set(fact_ids)):
        raise CurrentReplenishmentError("complete scope contains duplicate facts")
    if len(reserve_ids) != len(set(reserve_ids)):
        raise CurrentReplenishmentError("complete scope contains duplicate reservations")
    scopes = {_distribution_scope(row) for row in (*facts, *reserves)}
    if len(scopes) > 1:
        raise CurrentReplenishmentError(
            "complete scope must cover exactly one distribution pool"
        )
    if not scopes:
        if distribution_scope is None:
            raise CurrentReplenishmentError(
                "empty complete scope requires distribution_scope"
            )
        return tuple(distribution_scope)
    derived = next(iter(scopes))
    if distribution_scope is not None and tuple(distribution_scope) != derived:
        raise CurrentReplenishmentError(
            "distribution_scope does not match complete scope inputs"
        )
    return derived


OWNER_RETIRED_REASON = "owner_retired"


def retire_current_allocations_of_closed_owners(
    db: Session,
    *,
    generation_id: int,
    reservation_ids: Iterable[int] | None = None,
) -> dict[str, int]:
    """Retire every current allocation of owners that are closed (§42).

    Canon: "рабочая строка резерва после закрытия больше не существует", a
    fact on a closed reserve goes FIFO (CANON "Судьба резерва при
    поступлении"), and one physical fact is counted once with assignments
    never exceeding it (planning-truth-contract, Инварианты 2-3).  A closed
    owner therefore cannot keep a *current* claim on a fact: its allocation
    becomes history (``is_current=false``) and the fact's quantity returns to
    FIFO for the live owners.  Every R4 replay already excludes a closed
    owner from its before-set, so the replay that runs for the successor
    allocates the same fact to it; keeping the closed claim current was a
    second count - on the stand 7287 rows / 324 975 units, 477 facts over
    their own quantity after 14 obligation refreshes.

    Both roles (§42: all allocations of the closed owner).  A closed owner
    holds no S0, so its ``material_consumption`` claims go too; left current,
    a replace or rebase after period start made the same ``assembly_out``
    current for the closed and for the new owner.  "Closed" is
    ``lifecycle_status='closed'`` or an owner that is neither current nor
    BUILDING staging (a legacy copy); such an owner is no longer live either.

    The single current writer owns this change.  A ``replenishment_receipt``
    pair gets one ``retire`` audit row with reason ``owner_retired`` in its
    scope's own marker, stamped with the publishing generation
    (``GENERATION_REVISION``); ``material_consumption`` is not an R4 basis
    and has no marker, so it is counted, not audited there.  Idempotent: a
    retired allocation is no longer current.  ``reservation_ids`` bounds the
    owners considered; ``None`` means every closed owner, which is what heals
    a database already in the double-counted state.
    """
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None:
        raise CurrentReplenishmentError(f"generation {generation_id} does not exist")
    query = (
        db.query(models.ReservationConsumptionAllocation, models.ReservationEntry)
        .join(
            models.ReservationEntry,
            models.ReservationEntry.id
            == models.ReservationConsumptionAllocation.reservation_id,
        )
        .filter(
            models.ReservationConsumptionAllocation.is_current.is_(True),
            models.ReservationConsumptionAllocation.allocation_role.in_(
                ("replenishment_receipt", "material_consumption")
            ),
            closed_owner_predicate(),
        )
    )
    if reservation_ids is not None:
        ids = sorted({int(value) for value in reservation_ids})
        if not ids:
            return _retire_result(0, 0, Decimal("0"), 0)
        query = query.filter(models.ReservationEntry.id.in_(ids))
    pairs = (
        query.with_for_update(of=models.ReservationConsumptionAllocation)
        .order_by(models.ReservationConsumptionAllocation.id.asc())
        .all()
    )
    states: dict[str, models.CurrentReplenishmentState | None] = {}
    retired = 0
    consumption = 0
    unaudited = 0
    total = Decimal("0")
    for row, entry in pairs:
        if _text(row.allocation_role) == "material_consumption":
            row.is_current = False
            retired += 1
            consumption += 1
            total += _decimal(row.allocated_qty)
            continue
        scope_key = _scope_key(_allocation_scope(row, entry))
        if scope_key not in states:
            states[scope_key] = (
                db.query(models.CurrentReplenishmentState)
                .filter(models.CurrentReplenishmentState.scope_key == scope_key)
                .one_or_none()
            )
        row.is_current = False
        retired += 1
        total += _decimal(row.allocated_qty)
        state = states[scope_key]
        if state is None:
            # Retiring a claim is the safe direction; a pair written before
            # the scope had a marker is retired without an audit row rather
            # than left double-counted.
            unaudited += 1
            continue
        _audit_change(
            db,
            state=state,
            generation=generation,
            entry=entry,
            fact_id=str(int(row.sle_id)),
            scope_key=scope_key,
            source_revision=int(generation.id),
            operation="retire",
            before=Allocation(
                fact_id=str(int(row.sle_id)),
                reserve_id=str(int(entry.id)),
                qty=_decimal(row.allocated_qty),
                match_rule=_text(row.match_rule) or "fifo",
            ),
            after=None,
            reason=OWNER_RETIRED_REASON,
        )
    db.flush()
    return _retire_result(retired, consumption, total, unaudited)


def closed_owner_predicate():
    """An owner that is no longer live: closed, or a non-current legacy copy."""
    return or_(
        models.ReservationEntry.lifecycle_status == "closed",
        and_(
            models.ReservationEntry.is_current.is_(False),
            models.ReservationEntry.owner_kind != "building",
        ),
    )


def _retire_result(
    retired: int, consumption: int, total: Decimal, unaudited: int
) -> dict[str, object]:
    return {
        "retired_allocations": int(retired),
        "retired_receipt_allocations": int(retired) - int(consumption),
        "retired_consumption_allocations": int(consumption),
        # Exact Decimal(15,3) as text: never truncated to whole units.
        "retired_qty": canonical_decimal_text(total),
        "unaudited": int(unaudited),
    }


def canonical_decimal_text(value: Decimal) -> str:
    return format(_decimal(value).quantize(Decimal("0.001")), "f")


def over_allocated_facts(
    db: Session,
    *,
    item_ids: Iterable[int] | None = None,
    sle_ids: Iterable[int] | None = None,
    limit: int = 8,
) -> list[tuple[int, str, Decimal, Decimal]]:
    """Facts whose current allocations exceed the fact (invariants 2-3).

    Returns ``(sle_id, role, allocated, |qty|)`` for the first ``limit``
    offenders; empty means the invariant holds.  Compared per ``(sle,
    role)``.  ``sle_ids`` bounds the check to the facts a bounded publication
    brought in (decision §41); ``item_ids`` bounds it by item.
    """
    from sqlalchemy import func

    allocated = func.sum(models.ReservationConsumptionAllocation.allocated_qty)
    query = (
        db.query(
            models.ReservationConsumptionAllocation.sle_id,
            models.ReservationConsumptionAllocation.allocation_role,
            allocated,
            func.abs(models.StockLedgerEntry.qty),
        )
        .join(
            models.StockLedgerEntry,
            models.StockLedgerEntry.id == models.ReservationConsumptionAllocation.sle_id,
        )
        .filter(models.ReservationConsumptionAllocation.is_current.is_(True))
        .group_by(
            models.ReservationConsumptionAllocation.sle_id,
            models.ReservationConsumptionAllocation.allocation_role,
            models.StockLedgerEntry.qty,
        )
        .having(allocated > func.abs(models.StockLedgerEntry.qty))
        .order_by(models.ReservationConsumptionAllocation.sle_id.asc())
    )
    if item_ids is not None:
        ids = sorted({int(value) for value in item_ids})
        if not ids:
            return []
        query = query.filter(models.ReservationConsumptionAllocation.item_id.in_(ids))
    if sle_ids is not None:
        sle_filter = sorted({int(value) for value in sle_ids})
        if not sle_filter:
            return []
        query = query.filter(
            models.ReservationConsumptionAllocation.sle_id.in_(sle_filter)
        )
    if limit and int(limit) > 0:
        query = query.limit(int(limit))
    return [
        (int(sle_id), str(role), _decimal(total), _decimal(qty))
        for sle_id, role, total, qty in query.all()
    ]


def require_facts_not_over_allocated(
    db: Session,
    *,
    item_ids: Iterable[int] | None = None,
    sle_ids: Iterable[int] | None = None,
) -> None:
    """Fail closed when any fact carries more current allocation than itself."""
    offenders = over_allocated_facts(db, item_ids=item_ids, sle_ids=sle_ids, limit=8)
    if offenders:
        listed = ", ".join(
            f"SLE {sle_id} {role} {total}>{qty}"
            for sle_id, role, total, qty in offenders
        )
        raise CurrentReplenishmentError(
            "current allocations exceed their physical fact "
            f"(one fact is counted once); first offenders: {listed}"
        )


def _audit_change(
    db: Session,
    *,
    state: models.CurrentReplenishmentState,
    generation: models.LedgerGeneration,
    entry: models.ReservationEntry,
    fact_id: str,
    scope_key: str,
    source_revision: int,
    operation: str,
    before: Allocation | None,
    after: Allocation | None,
    reason: str = "current_replay",
    basis_fact_ids: tuple[int, ...] = (),
) -> None:
    """Audit one changed basis pair, never the unchanged assignment matrix."""

    db.add(
        models.CurrentReplenishmentAudit(
            state_id=int(state.id),
            ledger_generation_id=int(generation.id),
            scope_key=str(scope_key),
            source_revision=int(source_revision),
            sle_id=int(fact_id),
            reservation_id=int(entry.id),
            operation=str(operation),
            reason=str(reason)[:128],
            basis_fact_ids=[int(value) for value in basis_fact_ids],
            before_qty=before.qty if before is not None else None,
            after_qty=after.qty if after is not None else None,
            before_match_rule=before.match_rule if before is not None else None,
            after_match_rule=after.match_rule if after is not None else None,
        )
    )


#: R4 revision rule: a current replenishment marker is stamped with the id of
#: the ledger generation whose publication wrote it.  Generation ids are
#: unique and monotonic across every kind that publishes (historical accept,
#: bootstrap on the pointer, bounded physical refresh, obligation refresh),
#: and a candidate can only publish while its parent is still the pointer, so
#: the published sequence is strictly increasing.  The import batch id is not
#: a publication identity: an obligation refresh inherits its parent's batch,
#: so two consecutive obligation refreshes stamped the same revision with
#: different reserve ids and the second failed "same source revision has
#: payload drift".
GENERATION_REVISION = "generation"
RevisionBasis = Literal["explicit", "generation"]


def _previous_revision(
    state: models.CurrentReplenishmentState, revision_basis: str
) -> tuple[int, bool]:
    """The stored revision on the caller's axis, and whether it is legacy.

    Under the generation rule a marker always satisfies
    ``source_revision == ledger_generation_id``.  A marker that does not was
    stamped with an import batch id before the rule (``14440`` against
    generation ids near ``1450``): numerically it looks newer than every
    generation, so comparing it as stored would refuse every write forever.
    Its generation column is the publication that wrote it, so that is its
    revision on the generation axis - deterministic, no migration, and the
    marker is rewritten to the rule by the first write that passes.
    """
    stored = int(state.source_revision)
    if revision_basis != GENERATION_REVISION:
        return stored, False
    publishing_generation = int(state.ledger_generation_id)
    if stored == publishing_generation:
        return stored, False
    return publishing_generation, True


def apply_current_replenishment(
    db: Session,
    *,
    generation_id: int,
    source_key: str,
    source_revision: int,
    facts: Iterable[Fact],
    reserves: Iterable[Reserve],
    complete_scope: bool,
    distribution_scope: DistributionScope | None = None,
    allow_building: bool = False,
    fail_after: Literal["assignments", "execution", "marker"] | None = None,
    writer: str = WRITER_KEY,
    receipt_replay: object | None = None,
    receipt_facts: tuple[object, ...] = (),
    history_mode: str = "as_occurred",
    receipt_unmatched_return_qty: Decimal = Decimal("0"),
    confirmed_empty_reason: str = "",
    revision_basis: RevisionBasis = "explicit",
) -> CurrentReplenishmentResult:
    """Apply one complete accepted-fact scope atomically.

    The caller owns the surrounding transaction (the function intentionally
    does not commit).  PostgreSQL locks the generation row and all existing
    allocation rows before checking the source marker, which serializes two
    imports for the same allocation scope.  A stale revision is rejected and
    the exact same revision is an idempotent no-op.

    ``confirmed_empty_reason`` is the explicit R4 acknowledgement that the
    receipt fact set for this scope is provably empty.  Without it the writer
    refuses to reduce a populated complete scope to zero allocations, and it
    is only authoritative for the reserves it was handed.  The reason is
    stamped on every basis-change audit row the clear-out produces, so the
    emptiness always has a recorded cause in the database.

    ``revision_basis="generation"`` is the production rule (see
    :data:`GENERATION_REVISION`): the revision is the id of the generation
    this write publishes.  ``"explicit"`` keeps a caller-chosen monotonic
    integer and is a test/maintenance seam only.
    """

    fact_rows = tuple(facts)
    reserve_rows = tuple(reserves)
    distribution_scope = _require_complete_scope(
        fact_rows, reserve_rows, complete_scope, distribution_scope
    )
    canonical_scope_key = _scope_key(distribution_scope)
    if receipt_replay is not None and history_mode not in ("as_occurred", "as_known"):
        raise CurrentReplenishmentError(
            "history_mode must be as_occurred or as_known"
        )
    input_checksum = (
        _receipt_input_checksum(
            receipt_facts,
            reserve_rows,
            canonical_scope_key,
            history_mode,
        )
        if receipt_replay is not None
        else _input_checksum(fact_rows, reserve_rows, canonical_scope_key)
    )
    if _text(writer) != WRITER_KEY:
        raise CurrentReplenishmentError(
            "single current writer is current_replenishment; legacy writer is retired"
        )
    if not _text(source_key):
        raise CurrentReplenishmentError("source_key is required")
    canonical_key = canonical_source_key(source_key)
    try:
        revision = int(source_revision)
    except (TypeError, ValueError) as exc:
        raise CurrentReplenishmentError("source_revision must be an integer") from exc
    if revision < 0:
        raise CurrentReplenishmentError("source_revision must be non-negative")
    if revision_basis not in ("explicit", GENERATION_REVISION):
        raise CurrentReplenishmentError(f"unknown revision basis {revision_basis!r}")
    if revision_basis == GENERATION_REVISION and revision != int(generation_id):
        raise CurrentReplenishmentError(
            f"generation revision {revision} does not identify the publishing "
            f"generation {int(generation_id)}"
        )

    generation = (
        db.query(models.LedgerGeneration)
        .filter(models.LedgerGeneration.id == int(generation_id))
        .with_for_update()
        .one_or_none()
    )
    if generation is None:
        raise CurrentReplenishmentError(f"generation {generation_id} does not exist")
    if _text(generation.status) != "accepted" and not (
        allow_building and _text(generation.status) == "building"
    ):
        raise CurrentReplenishmentError("current replenishment requires accepted generation")

    state = (
        db.query(models.CurrentReplenishmentState)
        .filter(models.CurrentReplenishmentState.scope_key == canonical_scope_key)
        .with_for_update()
        .one_or_none()
    )
    if state is not None:
        if _text(state.writer_key) != WRITER_KEY:
            raise CurrentReplenishmentError("single current writer is current_replenishment")
        if canonical_source_key(state.source_key) != canonical_key:
            raise CurrentReplenishmentError(
                "source stream changed for canonical distribution scope"
            )
        # A row written under a documented legacy spelling is the same stream;
        # rewrite it in place so the alias disappears on first write instead
        # of needing a data migration.  Idempotent.
        if _text(state.source_key) != canonical_key:
            state.source_key = canonical_key
        previous_revision, legacy_marker = _previous_revision(state, revision_basis)
        if revision < previous_revision:
            raise CurrentReplenishmentError(
                f"stale source revision {revision}; current is {previous_revision}"
            )
        if revision == previous_revision:
            if _text(state.scope_checksum) != input_checksum:
                raise CurrentReplenishmentError("same source revision has payload drift")
            if _text(state.status) != "completed":
                raise CurrentReplenishmentError("current source marker is still applying")
            generation_changed = int(state.ledger_generation_id) != int(generation.id)
            if generation_changed or legacy_marker:
                # A legacy marker re-published by its own generation is the
                # same publication: only its revision spelling is brought to
                # the generation rule, so the alias disappears on first write.
                state.ledger_generation_id = int(generation.id)
                state.source_revision = revision
                state.updated_at = datetime.now(timezone.utc)
                db.flush()
            return CurrentReplenishmentResult(
                generation_id=int(generation.id),
                source_key=canonical_key,
                source_revision=revision,
                inserted=0,
                updated=0,
                deleted=0,
                changed_pairs=0,
                audit_events=0,
                idempotent=not (generation_changed or legacy_marker),
            )
    else:
        state = models.CurrentReplenishmentState(
            scope_key=canonical_scope_key,
            source_key=canonical_key,
            ledger_generation_id=int(generation.id),
            source_revision=revision,
            scope_checksum=input_checksum,
            writer_key=WRITER_KEY,
            status="applying",
        )
        db.add(state)
        db.flush()

    allocations = (
        db.query(models.ReservationConsumptionAllocation)
        .join(
            models.ReservationEntry,
            models.ReservationEntry.id
            == models.ReservationConsumptionAllocation.reservation_id,
        )
        .filter(
            models.ReservationConsumptionAllocation.is_current.is_(True),
            models.ReservationConsumptionAllocation.allocation_role
            == "replenishment_receipt",
            models.ReservationConsumptionAllocation.item_id == distribution_scope[0],
            models.ReservationConsumptionAllocation.characteristic_ref == distribution_scope[1],
            models.ReservationConsumptionAllocation.organization_ref == distribution_scope[2],
            models.ReservationConsumptionAllocation.planning_stock_pool == distribution_scope[3],
            models.ReservationEntry.realization_mode.in_(
                scope_modes(distribution_scope[4])
            ),
        )
        .with_for_update()
        .order_by(models.ReservationConsumptionAllocation.id.asc())
        .all()
    )
    legacy_allocations = (
        db.query(models.ReservationConsumptionAllocation)
        .join(
            models.ReservationEntry,
            models.ReservationEntry.id
            == models.ReservationConsumptionAllocation.reservation_id,
        )
        .filter(
            models.ReservationConsumptionAllocation.ledger_generation_id
            == int(generation.id),
            models.ReservationConsumptionAllocation.is_current.is_(False),
            models.ReservationConsumptionAllocation.allocation_role
            == "replenishment_receipt",
            models.ReservationConsumptionAllocation.item_id == distribution_scope[0],
            models.ReservationConsumptionAllocation.characteristic_ref == distribution_scope[1],
            models.ReservationConsumptionAllocation.organization_ref == distribution_scope[2],
            models.ReservationConsumptionAllocation.planning_stock_pool == distribution_scope[3],
            models.ReservationEntry.realization_mode.in_(
                scope_modes(distribution_scope[4])
            ),
        )
        .with_for_update()
        .all()
    )
    allocation_entries = (
        db.query(models.ReservationEntry)
        .filter(
            models.ReservationEntry.id.in_(
                sorted({int(row.reservation_id) for row in allocations} | {
                    int(row.reserve_id) for row in reserve_rows
                })
            )
        )
        .with_for_update()
        .all()
        if allocations or reserve_rows
        else []
    )
    allocation_entry_by_id = {str(int(row.id)): row for row in allocation_entries}
    # R4 planning must compare reservations by their stable current owner,
    # never by BUILDING staging ids.  A missing identity is only tolerated for
    # old accepted compatibility fixtures; the live BUILDING path fails
    # closed, because deriving an id there would hide a publication defect.
    strict_identity = _text(generation.status) == "building"
    reservation_identity_by_id: dict[str, str] = {}
    entries_by_identity: dict[str, list[models.ReservationEntry]] = {}
    for entry in allocation_entries:
        physical_id = str(int(entry.id))
        identity = _text(entry.current_identity)
        if not identity:
            if strict_identity:
                raise CurrentReplenishmentError(
                    f"reservation {physical_id} lacks stable current identity"
                )
            identity = f"reservation:req:{int(entry.requirement_id)}:mode:{_text(entry.realization_mode)}"
        canonical_identity = reservation_business_identity(
            int(entry.requirement_id), _text(entry.realization_mode)
        )
        if identity != canonical_identity:
            raise CurrentReplenishmentError(
                f"reservation {physical_id} has mismatched current identity"
            )
        reservation_identity_by_id[physical_id] = identity
        entries_by_identity.setdefault(identity, []).append(entry)

    # A normal refresh has one accepted owner plus its same-identity BUILDING
    # replacement.  Other duplicate shapes are ambiguous and must fail
    # closed.  Prefer the accepted owner for updates/deletes; a pure BUILDING
    # scope uses its staging row.
    entry_by_identity: dict[str, models.ReservationEntry] = {}
    for identity, candidates in entries_by_identity.items():
        current = [row for row in candidates if bool(row.is_current)]
        building = [row for row in candidates if _text(row.owner_kind) == "building"]
        if len(candidates) > 1 and not (len(current) == 1 and len(building) == 1):
            raise CurrentReplenishmentError(
                f"reservation current identity collision: {identity}"
            )
        entry_by_identity[identity] = (current or candidates)[0]

    # Replace physical staging ids in the pure replay input with stable
    # business identities.  DML below maps those identities back to the
    # physical row selected by the current publication boundary.
    stable_reserves: list[Reserve] = []
    for reserve in reserve_rows:
        physical_id = str(reserve.reserve_id)
        identity = reservation_identity_by_id.get(physical_id)
        if not identity:
            raise CurrentReplenishmentError(
                f"reservation {physical_id} is unavailable or lacks stable current identity"
            )
        stable_reserves.append(
            Reserve(
                reserve_id=identity,
                item_id=reserve.item_id,
                mode=reserve.mode,
                reserved_qty=reserve.reserved_qty,
                due_date=reserve.due_date,
                plan_period_from=reserve.plan_period_from,
                plan_period_to=reserve.plan_period_to,
                run_id=reserve.run_id,
                requirement_id=reserve.requirement_id,
                bucket_date=reserve.bucket_date,
                bucket_id=reserve.bucket_id,
                characteristic_ref=reserve.characteristic_ref,
                organization_ref=reserve.organization_ref,
                planning_stock_pool=reserve.planning_stock_pool,
                order_refs=reserve.order_refs,
                baseline_at=reserve.baseline_at,
            )
        )
    reserve_rows_for_plan = tuple(stable_reserves)
    if len({row.reserve_id for row in reserve_rows_for_plan}) != len(reserve_rows_for_plan):
        raise CurrentReplenishmentError("complete scope contains colliding reservation identities")
    # The "before" set is the part of the locked scope this replay is
    # authoritative for: the reserves it was actually handed, by stable
    # identity or by physical row.
    #
    # Canon R4 makes "complete scope" a statement about the distribution
    # scope, so the natural reading is that a reserve which left the scope
    # must have its allocations retired by the same replay.  That reading is
    # kept for the case it describes - a reserve whose owner is gone: the
    # publisher closes such owners in ``publish_current_reservations`` and
    # retires their current allocations in the same transaction
    # (``retire_current_allocations_of_closed_owners``), so the fact returns
    # to FIFO and this replay allocates it to the live owners once.  The
    # closed owner is not reopened (R5: "closed или неизвестная reservation
    # не переоткрывается"); its allocation stays only as non-current history.
    #
    # What is *not* retired is the allocation of an owner that is still
    # active and simply was not handed in.  Deleting those was the defect: a
    # BUILDING staging replay (``allow_building``) saw none of the accepted
    # identities - an obligation refresh renumbers requirements - and wiped
    # the accepted current assignment inside a transaction that had not
    # published anything yet.  Such an owner must be handed in by the caller;
    # the fail-closed guard below catches the case where a complete-scope
    # replay would clear a populated scope while producing nothing.
    confirmed_empty = bool(_text(confirmed_empty_reason))
    replayed_identities = {str(row.reserve_id) for row in reserve_rows_for_plan}
    replayed_reservation_ids = {int(row.reserve_id) for row in reserve_rows}
    # Canon R4: an empty complete scope is a statement about the whole scope,
    # so it keeps the whole scope as its "before" set.  Whether it may then be
    # cleared is decided by the fail-closed guard below, not by quietly
    # narrowing the set to nothing.
    authoritative_for_whole_scope = not reserve_rows

    def _is_replayed(row: models.ReservationConsumptionAllocation) -> bool:
        if authoritative_for_whole_scope:
            return True
        if int(row.reservation_id) in replayed_reservation_ids:
            return True
        identity = reservation_identity_by_id.get(str(int(row.reservation_id)), "")
        return bool(identity) and identity in replayed_identities

    scoped_allocations = tuple(
        row
        for row in allocations
        if distribution_scope is not None
        and str(row.reservation_id) in allocation_entry_by_id
        and _allocation_scope(
            row, allocation_entry_by_id[str(row.reservation_id)]
        ) == distribution_scope
        and _is_replayed(row)
    )
    previous = tuple(
        _as_allocation(row, reservation_identity_by_id) for row in scoped_allocations
    )
    try:
        plan = (
            _receipt_change_plan(
                receipt_replay,
                previous,
                reserve_rows_for_plan,
                receipt_facts,
                reservation_identity_by_id,
            )
            if receipt_replay is not None
            else plan_allocation_changes(
                fact_rows,
                reserve_rows_for_plan,
                previous_allocations=previous,
            )
        )
    except (TypeError, ValueError) as exc:
        raise CurrentReplenishmentError(str(exc)) from exc

    # Fail closed on a silent wipe.  A complete-scope replay that deletes every
    # current allocation it owns while producing none is either a genuinely
    # empty fact set — which the caller must name — or a defect upstream of the
    # writer (missing supplier provenance, an unpublished obligation refresh).
    # An unproven empty input is never allowed to publish as emptiness.
    if complete_scope and previous and not plan.result.allocations and not confirmed_empty:
        raise CurrentReplenishmentError(
            "complete-scope replay would delete all "
            f"{len(previous)} current replenishment allocations of scope "
            f"{canonical_scope_key} while producing none; pass "
            "confirmed_empty_reason to record why the receipt fact set is "
            "provably empty"
        )

    entries = allocation_entries
    entry_by_id = {str(int(row.id)): row for row in entries}
    fact_by_id = {str(row.fact_id): row for row in fact_rows}
    reserve_by_id = {str(row.reserve_id): row for row in reserve_rows_for_plan}
    basis_fact_ids = tuple(sorted({int(row.sle_id) for row in receipt_facts}))
    audit_reason = "r5_signed_replay" if receipt_replay is not None else "current_replay"
    if receipt_replay is not None and receipt_unmatched_return_qty > 0:
        audit_reason = "r5_signed_replay_unmatched_return"
    if confirmed_empty:
        # ``reason`` stays the canonical enumerated cause that readers group
        # by; the operator's free text must not replace it.  There is no
        # free-text column on this audit today, so the acknowledgement is
        # echoed back to the caller in the result instead of being smuggled
        # into ``reason`` or into ``basis_fact_ids`` (which is the fact-id
        # contract, not a notes field).
        audit_reason = CONFIRMED_EMPTY_REASON
    allocation_by_key = {
        (str(int(row.sle_id)), reservation_identity_by_id[str(int(row.reservation_id))]): row for row in allocations
        if row in scoped_allocations
    }
    legacy_by_key = {
        (str(int(row.sle_id)), reservation_identity_by_id.get(str(int(row.reservation_id)), "")): row
        for row in legacy_allocations
    }
    audit_events = 0

    for old in plan.deletions:
        row = allocation_by_key[(old.fact_id, old.reserve_id)]
        entry = entry_by_identity.get(old.reserve_id)
        if entry is None:
            raise CurrentReplenishmentError(
                f"allocation references missing reservation {old.reserve_id}"
            )
        db.delete(row)
        _audit_change(
            db,
            state=state,
            generation=generation,
            entry=entry,
            fact_id=old.fact_id,
            scope_key=canonical_scope_key,
            source_revision=revision,
            operation="delete",
            before=old,
            after=None,
            reason=audit_reason,
            basis_fact_ids=basis_fact_ids,
        )
        audit_events += 1

    for update in plan.updates:
        row = allocation_by_key[(update.before.fact_id, update.before.reserve_id)]
        entry = entry_by_identity.get(update.after.reserve_id)
        if entry is None:
            raise CurrentReplenishmentError(
                f"allocation references missing reservation {update.after.reserve_id}"
            )
        row.allocated_qty = update.after.qty
        row.match_rule = update.after.match_rule
        row.ledger_generation_id = int(generation.id)
        _audit_change(
            db,
            state=state,
            generation=generation,
            entry=entry,
            fact_id=update.after.fact_id,
            scope_key=canonical_scope_key,
            source_revision=revision,
            operation="update",
            before=update.before,
            after=update.after,
            reason=audit_reason,
            basis_fact_ids=basis_fact_ids,
        )
        audit_events += 1

    for insertion in plan.insertions:
        fact = fact_by_id.get(insertion.fact_id)
        reserve = reserve_by_id.get(insertion.reserve_id)
        entry = entry_by_identity.get(insertion.reserve_id)
        if fact is None or reserve is None or entry is None:
            raise CurrentReplenishmentError("allocation scope references unknown fact or reservation")
        key = (insertion.fact_id, insertion.reserve_id)
        row = legacy_by_key.get(key)
        if row is None:
            row = models.ReservationConsumptionAllocation(
                ledger_generation_id=int(generation.id),
                reservation_id=int(entry.id),
                sle_id=int(fact.fact_id),
                requirement_id=int(entry.requirement_id),
                allocated_qty=insertion.qty,
                match_rule=insertion.match_rule,
                fact_ref=_text(fact.fact_id),
                fact_line_ref="",
                item_id=int(entry.item_id),
                characteristic_ref=_text(entry.characteristic_ref),
                organization_ref=_text(entry.organization_ref),
                planning_stock_pool=_text(reserve.planning_stock_pool) or "default",
                idempotency_key=(
                    f"r4:{canonical_key}:{revision}:{insertion.fact_id}:{insertion.reserve_id}"
                ),
                allocation_role="replenishment_receipt",
                is_current=True,
                event_at=fact.posting_at,
            )
            db.add(row)
        else:
            # A pre-R4 generation copy is promoted in place.  Its row ID is
            # retained; no duplicate current allocation is created.
            row.is_current = True
            row.ledger_generation_id = int(generation.id)
            row.allocated_qty = insertion.qty
            row.match_rule = insertion.match_rule
            row.idempotency_key = (
                f"r4:{source_key}:{revision}:{insertion.fact_id}:{insertion.reserve_id}"
            )
            row.planning_stock_pool = _text(reserve.planning_stock_pool) or "default"
        _audit_change(
            db,
            state=state,
            generation=generation,
            entry=entry,
            fact_id=insertion.fact_id,
            scope_key=canonical_scope_key,
            source_revision=revision,
            operation="insert",
            before=None,
            after=insertion,
            reason=audit_reason,
            basis_fact_ids=basis_fact_ids,
        )
        audit_events += 1

    db.flush()
    if fail_after == "assignments":
        raise CurrentReplenishmentError("injected failure after assignments")

    realization_by_reserve = {
        str(row.reserve_id): _decimal(row.realized_qty)
        for row in plan.result.realizations
    }
    for reserve_id, realized in realization_by_reserve.items():
        entry = entry_by_identity.get(reserve_id)
        reserve = reserve_by_id.get(reserve_id)
        if entry is None or reserve is None:
            raise CurrentReplenishmentError(f"realization references unknown reservation {reserve_id}")
        received = min(max(realized, Decimal("0")), _decimal(entry.replenishment_required_qty))
        if _decimal(entry.replenishment_received_qty) != received:
            entry.replenishment_received_qty = received
            entry.realized_qty = received
    db.flush()
    if fail_after == "execution":
        raise CurrentReplenishmentError("injected failure after execution")

    state.status = "completed"
    state.ledger_generation_id = int(generation.id)
    state.source_revision = revision
    state.scope_checksum = input_checksum
    state.changed_pairs = len(plan.insertions) + len(plan.updates) + len(plan.deletions)
    state.updated_at = datetime.now(timezone.utc)
    db.flush()
    if fail_after == "marker":
        raise CurrentReplenishmentError("injected failure after marker")

    return CurrentReplenishmentResult(
        generation_id=int(generation.id),
        source_key=canonical_key,
        source_revision=revision,
        inserted=len(plan.insertions),
        updated=len(plan.updates),
        deleted=len(plan.deletions),
        changed_pairs=len(plan.insertions) + len(plan.updates) + len(plan.deletions),
        audit_events=audit_events,
        confirmed_empty_reason=_text(confirmed_empty_reason),
    )


def apply_current_receipt_replay(
    db: Session,
    *,
    generation_id: int,
    source_key: str,
    source_revision: int,
    receipt_facts: Iterable[object],
    reserves: Iterable[Reserve],
    complete_scope: bool,
    history_mode: str,
    exact_allocation_caps: dict[tuple[int, str, str], dict[int, Decimal]] | None = None,
    distribution_scope: DistributionScope | None = None,
    fail_after: Literal["assignments", "execution", "marker"] | None = None,
    allow_building: bool = False,
    validated_visible_ids: Iterable[int] | None = None,
    confirmed_empty_reason: str = "",
    revision_basis: RevisionBasis = "explicit",
) -> CurrentReplenishmentResult:
    """Publish signed correction/return replay through the R4 current writer."""

    from .supplier_receipt_allocation import replay_supplier_receipt_basis
    from .physical_visibility import visible_sles_for_generation

    rows = tuple(receipt_facts)
    if history_mode not in ("as_occurred", "as_known"):
        raise CurrentReplenishmentError(
            "history_mode must be as_occurred or as_known"
        )
    ids = [int(row.sle_id) for row in rows]
    if len(ids) != len(set(ids)):
        raise CurrentReplenishmentError("receipt correction source has duplicate sle_id")
    visible_ids = (
        {int(value) for value in validated_visible_ids}
        if validated_visible_ids is not None
        else {
            int(row.id)
            for row in visible_sles_for_generation(db, int(generation_id))
        }
    )
    missing = sorted(set(ids) - visible_ids)
    if missing:
        raise CurrentReplenishmentError(
            f"receipt correction source is unavailable in accepted Ledger: {missing}"
        )
    reserve_rows = tuple(reserves)
    active_reserves = tuple(
        row
        for row in reserve_rows
        if _text(getattr(row, "lifecycle_status", "active")) == "active"
    )
    reservation_ids = [int(row.reserve_id) for row in active_reserves]
    reservation_models = {
        int(row.id): row
        for row in db.query(models.ReservationEntry)
        .filter(models.ReservationEntry.id.in_(reservation_ids))
        .all()
    } if reservation_ids else {}
    if set(reservation_ids) != set(reservation_models):
        raise CurrentReplenishmentError(
            "receipt replay references an unavailable reservation"
        )
    # Use the caller's complete-scope values for attribution, while keeping
    # the persisted reservation row as the execution target.  This avoids
    # treating a database-default pool as a wildcard when the frozen DTO has
    # already named the exact pool.
    from types import SimpleNamespace

    replay_reservations = {}
    for pure in active_reserves:
        persisted = reservation_models[int(pure.reserve_id)]
        replay_reservations[int(pure.reserve_id)] = SimpleNamespace(
            id=int(persisted.id),
            item_id=int(persisted.item_id),
            planning_stock_pool=_text(pure.planning_stock_pool),
            characteristic_ref=_text(pure.characteristic_ref),
            organization_ref=_text(pure.organization_ref),
            realization_mode=_text(pure.mode),
            run_id=int(pure.run_id),
            requirement_id=int(pure.requirement_id),
            priority_period_from=pure.plan_period_from,
            priority_period_to=pure.plan_period_to,
            lifecycle_status="active",
            replenishment_required_qty=pure.reserved_qty,
            replenishment_received_qty=Decimal("0"),
            baseline_at=getattr(pure, "baseline_at", None),
        )
    reservations_by_item: dict[int, tuple[object, ...]] = {}
    for row in replay_reservations.values():
        reservations_by_item.setdefault(int(row.item_id), ())
        reservations_by_item[int(row.item_id)] = (
            *reservations_by_item[int(row.item_id)],
            row,
        )
    replay = replay_supplier_receipt_basis(
        rows,
        reservations_by_item,
        exact_allocation_caps=exact_allocation_caps,
        history_mode=history_mode,
    )
    scope = distribution_scope
    if scope is None and reserve_rows:
        first = reserve_rows[0]
        scope = (
            int(first.item_id),
            _text(first.characteristic_ref),
            _text(first.organization_ref),
            _text(first.planning_stock_pool),
            _text(first.mode),
        )
    if scope is None and rows:
        scope = (
            int(rows[0].item_id),
            "",
            "",
            _text(rows[0].planning_stock_pool),
            "buy",
        )
    fact_rows = tuple(
        Fact(
            fact_id=str(row.sle_id),
            item_id=int(row.item_id),
            mode="buy",
            qty=_decimal(row.signed_qty),
            posting_at=row.posting_at,
            planning_stock_pool=_text(row.planning_stock_pool),
        )
        for row in rows
        if _decimal(row.signed_qty) > 0
    )
    return apply_current_replenishment(
        db,
        generation_id=int(generation_id),
        source_key=source_key,
        source_revision=source_revision,
        facts=fact_rows,
        reserves=active_reserves,
        complete_scope=complete_scope,
        distribution_scope=scope,
        allow_building=allow_building,
        fail_after=fail_after,
        receipt_replay=replay,
        receipt_facts=rows,
        history_mode=history_mode,
        receipt_unmatched_return_qty=replay.unmatched_return_qty,
        confirmed_empty_reason=confirmed_empty_reason,
        revision_basis=revision_basis,
    )


def read_current_replenishment(
    db: Session, *, generation_id: int, item_id: int | None = None
) -> list[dict[str, object]]:
    """Read the accepted current assignment basis without lineage traversal."""

    query = (
        db.query(models.ReservationConsumptionAllocation)
        .join(
            models.ReservationEntry,
            models.ReservationEntry.id
            == models.ReservationConsumptionAllocation.reservation_id,
        )
        .filter(
            models.ReservationConsumptionAllocation.is_current.is_(True),
            models.ReservationConsumptionAllocation.allocation_role
            == "replenishment_receipt",
        )
        .order_by(
            models.ReservationConsumptionAllocation.sle_id.asc(),
            models.ReservationConsumptionAllocation.reservation_id.asc(),
        )
    )
    if item_id is not None:
        query = query.filter(models.ReservationConsumptionAllocation.item_id == int(item_id))
    return [
        {
            "id": int(row.id),
            "sle_id": int(row.sle_id),
            "reservation_id": int(row.reservation_id),
            "requirement_id": int(row.requirement_id),
            "allocated_qty": _decimal(row.allocated_qty),
            "match_rule": _text(row.match_rule),
            "item_id": int(row.item_id),
            "planning_stock_pool": _text(row.planning_stock_pool),
        }
        for row in query.all()
    ]


def reject_legacy_supplier_receipt_writer(
    db: Session, reservation: models.ReservationEntry
) -> None:
    """Retire ReservationEvent as a second writer after R4 owns a scope.

    ``ReservationEvent`` remains historical evidence for pre-R4 generation
    builds.  Once the accepted current writer has a completed marker for this
    business scope, a supplier-receipt rebuild must not fold the same receipt
    into the reservation a second time.
    """

    scope = (
        int(reservation.item_id),
        _text(reservation.characteristic_ref),
        _text(reservation.organization_ref),
        _text(reservation.planning_stock_pool),
        _text(reservation.realization_mode),
    )
    state = (
        db.query(models.CurrentReplenishmentState)
        .filter(models.CurrentReplenishmentState.scope_key == _scope_key(scope))
        .first()
    )
    if state is not None and _text(state.status) == "completed":
        raise CurrentReplenishmentError(
            "supplier receipt ReservationEvent writer is retired for an R4 current scope"
        )


def _adapter_revision(
    source_revision: int | None, generation_id: int
) -> tuple[int, RevisionBasis]:
    """Production adapters stamp the publishing generation (the R4 rule).

    An explicit integer is kept only as a test/maintenance seam and is
    compared as given, exactly as before the rule.
    """
    if source_revision is None:
        return int(generation_id), GENERATION_REVISION
    try:
        revision = int(source_revision)
    except (TypeError, ValueError) as exc:
        raise CurrentReplenishmentError("source_revision must be an integer") from exc
    if revision < 0:
        raise CurrentReplenishmentError("source_revision must be non-negative")
    return revision, "explicit"


def apply_current_replenishment_for_accepted_generation(
    db: Session,
    *,
    generation_id: int,
    source_revision: int | None = None,
    allow_building: bool = False,
    retained_run_ids: Iterable[int] = (),
) -> tuple[CurrentReplenishmentResult, ...]:
    """Publish current supplier-receipt replenishment at physical acceptance.

    This is the production orchestration adapter: it reads only the accepted
    generation's positive receipt facts and its frozen buy reservations, then
    delegates all persistence to :func:`apply_current_replenishment` in the
    caller's transaction.  A single item cannot be silently fanned out to
    multiple pools because the physical receipt has no pool identity; such an
    ambiguous input fails closed.

    ``retained_run_ids`` completes a BUILDING scope for an obligation
    refresh.  A retained run is never staged - its obligations stay anchored
    to the generation that froze them - yet its current owners stay live
    after the publication, so they belong to the scope's live owners.  Left
    out, their allocations were neither in the replay's before-set nor
    subtracted from the fact, and the staging owners were handed the same
    fact again: one fact counted by a retained owner and by a successor.
    """

    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or (
        _text(generation.status) != "accepted"
        and not (allow_building and _text(generation.status) == "building")
    ):
        raise CurrentReplenishmentError(
            "current replenishment publication requires an accepted generation"
        )
    reservation_query = db.query(models.ReservationEntry).filter(
        models.ReservationEntry.lifecycle_status == "active",
        models.ReservationEntry.realization_mode == "buy",
        models.ReservationEntry.replenishment_required_qty > 0,
    )
    if _text(generation.status) == "building":
        retained = sorted({int(value) for value in retained_run_ids})
        staged = and_(
            models.ReservationEntry.ledger_generation_id == int(generation_id),
            models.ReservationEntry.owner_kind == "building",
        )
        reservation_query = reservation_query.filter(
            or_(
                staged,
                and_(
                    models.ReservationEntry.is_current.is_(True),
                    models.ReservationEntry.owner_kind == "current",
                    models.ReservationEntry.run_id.in_(retained),
                ),
            )
            if retained else staged
        )
    else:
        pointer = db.get(models.PlanningTruthState, 1)
        if pointer is None or int(pointer.current_generation_id or 0) != int(generation_id):
            # Pre-owner fixtures have no populated current_identity/is_current
            # rows yet.  Once the owner migration has published any current
            # row, an accepted generation must match the exact pointer.
            if db.query(models.ReservationEntry.id).filter(
                models.ReservationEntry.is_current.is_(True),
            ).first() is not None:
                raise CurrentReplenishmentError(
                    "accepted reservation publication is not the exact planning truth pointer"
                )
        # Rows created before the owner migration have an empty identity.  This
        # compatibility branch is removed by the migration backfill; it is not
        # a runtime legacy fallback once current_identity is populated.
        reservation_query = reservation_query.filter(or_(
            models.ReservationEntry.is_current.is_(True),
            and_(
                models.ReservationEntry.current_identity == "",
                models.ReservationEntry.ledger_generation_id == int(generation_id),
            ),
        ))
    reservations = tuple(
        reservation_query
        .order_by(models.ReservationEntry.id.asc())
        .all()
    )
    if not reservations:
        return ()
    item_scopes: dict[int, set[DistributionScope]] = {}
    for row in reservations:
        item_scopes.setdefault(int(row.item_id), set()).add(
            (
                int(row.item_id),
                _text(row.characteristic_ref),
                _text(row.organization_ref),
                _text(row.planning_stock_pool),
                _text(row.realization_mode),
            )
        )
    for item_id, scopes in item_scopes.items():
        if len(scopes) > 1:
            raise CurrentReplenishmentError(
                f"receipt facts for item {item_id} have ambiguous distribution pools"
            )
    # Use the complete visible physical prefix, then restrict it by persisted
    # typed supplier provenance.  A raw positive ``receipt`` movement is not
    # sufficient: internal transfers and other receipt-like rows are not BUY.
    from .physical_visibility import visible_sles_for_generation

    visible = {
        int(row.id): row
        for row in visible_sles_for_generation(db, int(generation_id))
        if bool(row.active)
    }
    provenance = (
        db.query(models.StockLedgerSupplierReceiptProvenance)
        .filter(
            models.StockLedgerSupplierReceiptProvenance.ledger_generation_id
            == int(generation_id),
            models.StockLedgerSupplierReceiptProvenance.operation_kind.in_(
                ("supplier_receipt", "correction", "supplier_return")
            ),
            models.StockLedgerSupplierReceiptProvenance.match_status != "excluded_non_supplier",
        )
        .order_by(models.StockLedgerSupplierReceiptProvenance.stock_ledger_entry_id.asc())
        .all()
    )
    # Two different failures hide behind "no rows here", and each has its own
    # guard.  This one is the degenerate case: the generation owns no typed
    # evidence at all while supplier documents are visible, so the typing step
    # never ran or never committed and every BUY scope would publish empty -
    # the receipt facts vanish, the replay deletes the whole current
    # assignment and coverage silently drops to zero.  The other case, a fact
    # this system typed once and then stopped owning, is caught earlier by
    # ``lost_supplier_receipt_provenance_sle_ids`` at the acceptance gate.
    from .physical_refresh_supplier_evidence import (
        is_supplier_document_type,
        lost_supplier_receipt_provenance_sle_ids,
    )

    from app.services.planning_pool_resolver import (
        PlanningPoolConfigurationError,
        resolve_planning_pool_by_warehouse,
    )

    try:
        contour = resolve_planning_pool_by_warehouse(db)
    except PlanningPoolConfigurationError:
        contour = None
    lost = lost_supplier_receipt_provenance_sle_ids(
        db, ledger_generation_id=int(generation_id), contour=contour,
    )
    if lost:
        raise CurrentReplenishmentError(
            f"generation {int(generation_id)} lost supplier receipt provenance "
            f"for {len(lost)} visible supplier facts it does not own; first "
            f"sle_ids={list(lost[:8])}"
        )
    if not provenance:
        untyped = sorted(
            int(row.id)
            for row in visible.values()
            if is_supplier_document_type(row.recorder_type)
            and _decimal(row.qty) != 0
        )
        if untyped:
            raise CurrentReplenishmentError(
                f"generation {int(generation_id)} has {len(untyped)} visible "
                "supplier-receipt ledger rows but no supplier receipt "
                f"provenance; first sle_ids={untyped[:8]}"
            )
    from .supplier_receipt_allocation import (
        ReceiptFact,
        _exact_allocation_caps_by_order_line,
    )

    exact_caps = _exact_allocation_caps_by_order_line(
        db,
        ledger_generation_id=int(generation_id),
    )
    # A retained owner is anchored to the generation that froze it, so the
    # generation-scoped lookup cannot see its exact supplier-order link and it
    # replayed as FIFO - flip-flopping with the bounded path, which finds it
    # by stable owner id.  Look the retained owners up the same way.
    retained_owner_ids = sorted(
        int(row.id) for row in reservations if bool(row.is_current)
    )
    if _text(generation.status) == "building" and retained_owner_ids:
        owner_caps = _exact_allocation_caps_by_order_line(
            db,
            ledger_generation_id=int(generation_id),
            current_owner_ids=retained_owner_ids,
        )
        for key, by_owner in owner_caps.items():
            exact_caps.setdefault(key, {}).update(by_owner)
    exact_keys = {
        (
            int(row.evidence_payload.get("item_id") or visible[int(row.stock_ledger_entry_id)].item_id),
            _text(row.supplier_order_ref),
            _text(row.supplier_order_line_no),
        )
        for row in provenance
        if _text(row.match_status) == "exact"
        and _text(row.supplier_order_ref)
        and _text(row.supplier_order_line_no)
        and int(row.stock_ledger_entry_id) in visible
    }
    exact_caps = {key: value for key, value in exact_caps.items() if key in exact_keys}
    typed_facts: list[ReceiptFact] = []
    for row in provenance:
        sle = visible.get(int(row.stock_ledger_entry_id))
        if sle is None or _decimal(sle.qty) == 0:
            continue
        typed_facts.append(
            ReceiptFact(
                sle_id=int(sle.id),
                item_id=int(sle.item_id),
                signed_qty=_decimal(sle.qty),
                posting_at=sle.posting_at,
                known_at=getattr(sle, "known_at", None) or getattr(sle, "created_at", None),
                supplier_order_ref=_text(row.supplier_order_ref),
                supplier_order_line_no=_text(row.supplier_order_line_no),
                receipt_ref=_text(row.receipt_doc_ref),
                receipt_line_no=_text(row.receipt_doc_line_no),
                correction_receipt_ref=_text(row.correction_receipt_ref) or None,
            )
        )
    revision, basis = _adapter_revision(source_revision, int(generation.id))
    baselines = freeze_baselines_by_reservation(db, reservations)
    result: list[CurrentReplenishmentResult] = []
    for item_id, scope_set in sorted(item_scopes.items()):
        scope = next(iter(scope_set))
        reserve_rows = tuple(
            Reserve(
                reserve_id=str(row.id),
                item_id=int(row.item_id),
                mode="buy",
                reserved_qty=_decimal(row.replenishment_required_qty),
                due_date=row.priority_period_to,
                plan_period_from=row.priority_period_from,
                plan_period_to=row.priority_period_to,
                run_id=int(row.run_id or 0),
                requirement_id=int(row.requirement_id),
                characteristic_ref=scope[1],
                organization_ref=scope[2],
                planning_stock_pool=scope[3],
                baseline_at=baselines.get(int(row.id)),
            )
            for row in reservations
            if int(row.item_id) == item_id
        )
        facts = tuple(
            ReceiptFact(
                **{
                    **fact.__dict__,
                    "planning_stock_pool": scope[3],
                }
            )
            for fact in typed_facts
            if int(fact.item_id) == item_id
        )
        result.append(
            apply_current_receipt_replay(
                db,
                generation_id=int(generation_id),
                source_key=SUPPLIER_RECEIPT_SOURCE_KEY,
                source_revision=revision,
                receipt_facts=facts,
                reserves=reserve_rows,
                complete_scope=True,
                distribution_scope=scope,
                exact_allocation_caps=exact_caps,
                history_mode="as_occurred",
                allow_building=allow_building,
                revision_basis=basis,
            )
        )
    return tuple(result)


def apply_current_replenishment_for_bounded_make_scopes(
    db: Session,
    *,
    target_generation_id: int,
    parent_generation_id: int,
    target_cutoff: datetime,
    affected_scopes: Iterable[DistributionScope],
    source_revision: int | None = None,
) -> BoundedMakeReplenishmentResult:
    """Apply bounded ``assembly_in`` facts to stable current MAKE owners.

    This is intentionally a current-owner adapter, not a generation
    publisher.  ``target_generation_id`` must still be BUILDING and is used
    only as provenance for the current writer; no reservation, work-item, or
    generation-scoped execution copy is created and the truth pointer is never
    touched.  The allocator needs a complete reconciliation input, so the
    query reads the complete visible physical history only for each requested
    ``DistributionScope``'s item/characteristic/organization key.  It never
    calls ``visible_sles_for_generation`` and never loads unrelated items.

    All persistence is delegated to :func:`apply_current_replenishment`; the
    caller owns the transaction and may roll it back if any affected scope
    fails.  The preflight validates every visible ``assembly_in`` row before
    applying the first scope, so an out-of-scope or ambiguous fact cannot
    leave a partial bounded publication.
    """

    revision, basis = _adapter_revision(source_revision, int(target_generation_id))

    target = db.get(models.LedgerGeneration, int(target_generation_id))
    parent = db.get(models.LedgerGeneration, int(parent_generation_id))
    if target is None or _text(target.status) != "building":
        raise CurrentReplenishmentError(
            "bounded make publication requires a BUILDING target generation"
        )
    if parent is None or _text(parent.status) != "accepted":
        raise CurrentReplenishmentError(
            "bounded make publication requires an accepted parent generation"
        )
    if int(target.id) == int(parent.id):
        raise CurrentReplenishmentError("bounded make target must differ from parent")
    if target.physical_import_batch_id is None or parent.physical_import_batch_id is None:
        raise CurrentReplenishmentError("bounded make generations require import batches")
    if int(target.physical_import_batch_id) < int(parent.physical_import_batch_id):
        raise CurrentReplenishmentError(
            "bounded make target import batch is older than its parent"
        )
    if target_cutoff is None:
        raise CurrentReplenishmentError("bounded make target cutoff is required")
    if target.cutoff is not None:
        target_value = target.cutoff
        requested_value = target_cutoff
        if target_value.tzinfo is not None and requested_value.tzinfo is None:
            requested_value = requested_value.replace(tzinfo=timezone.utc)
        elif target_value.tzinfo is None and requested_value.tzinfo is not None:
            requested_value = requested_value.replace(tzinfo=None)
        if target_value != requested_value:
            raise CurrentReplenishmentError(
                "bounded make target cutoff does not match generation cutoff"
            )

    scopes: list[DistributionScope] = []
    seen_scopes: set[DistributionScope] = set()
    for raw_scope in affected_scopes:
        try:
            values = tuple(raw_scope)
            scope = (
                int(values[0]),
                _text(values[1]),
                _text(values[2]),
                _text(values[3]),
                _text(values[4]),
            )
        except (TypeError, ValueError, IndexError) as exc:
            raise CurrentReplenishmentError(
                "bounded make affected scope is malformed"
            ) from exc
        if len(values) != 5 or scope[0] <= 0:
            raise CurrentReplenishmentError("bounded make affected scope is malformed")
        if scope[4] != "make":
            raise CurrentReplenishmentError(
                "bounded make publication supports only realization_mode=make"
            )
        if scope in seen_scopes:
            raise CurrentReplenishmentError("bounded make affected scopes contain duplicates")
        seen_scopes.add(scope)
        scopes.append(scope)
    if not scopes:
        raise CurrentReplenishmentError("bounded make publication requires affected scopes")
    scopes.sort()

    # A physical SLE has no planning-pool column.  A requested item/key must
    # therefore resolve to exactly one current pool; multiple pools would make
    # attribution ambiguous and must fail closed rather than inventing one.
    #
    # Facts are keyed through the canonical collapse, the same one the
    # reservations were frozen with (``mrp_freeze.pool_key_for``).  Matching
    # raw columns was a second rule: the owner carries the collapsed
    # ``('', '', 'default')`` while the row carries its real characteristic
    # and the 1C organization, so the predicate selected nothing at all.
    scope_by_physical_key: dict[tuple[int, str, str, str], list[DistributionScope]] = {}
    for scope in scopes:
        scope_by_physical_key.setdefault(
            distribution_scope_for_fact(int(scope[0]), scope[1], scope[2]), [],
        ).append(scope)

    from .physical_visibility import visible_sle_query
    from .historical_replay_persistence import _identity_for_sle

    physical_scope_predicate = models.StockLedgerEntry.item_id.in_(
        sorted({int(scope[0]) for scope in scopes})
    )
    rows = (
        visible_sle_query(
            db,
            physical_import_batch_id=int(target.physical_import_batch_id),
            cutoff=target_cutoff,
        )
        .filter(
            physical_scope_predicate,
            models.StockLedgerEntry.movement_kind == "assembly_in",
        )
        .order_by(
            models.StockLedgerEntry.posting_at.asc(),
            models.StockLedgerEntry.id.asc(),
        )
        .all()
    )
    facts_by_scope: dict[DistributionScope, list[Fact]] = {
        scope: [] for scope in scopes
    }
    for row in rows:
        if _decimal(row.qty) <= 0:
            raise CurrentReplenishmentError(
                f"assembly_in fact {int(row.id)} has non-positive quantity"
            )
        physical_key = distribution_scope_for_fact(
            int(row.item_id),
            _text(row.characteristic_ref),
            _text(row.organization_ref),
        )
        candidates = scope_by_physical_key.get(physical_key, [])
        if not candidates:
            raise CurrentReplenishmentError(
                f"assembly_in fact {int(row.id)} is outside affected make scope"
            )
        if len(candidates) != 1:
            raise CurrentReplenishmentError(
                f"assembly_in fact {int(row.id)} has ambiguous planning pool"
            )
        scope = candidates[0]
        requirement_id, order_ref, ambiguous = _identity_for_sle(db, row)
        if ambiguous:
            raise CurrentReplenishmentError(
                f"assembly_in fact {int(row.id)} has ambiguous production identity"
            )
        facts_by_scope[scope].append(
            Fact(
                fact_id=str(int(row.id)),
                item_id=int(row.item_id),
                mode="make",
                qty=_decimal(row.qty),
                posting_at=row.posting_at,
                characteristic_ref=scope[1],
                # Attribution keys come from the scope, exactly as the BUY
                # path takes its pool from the scope: the distribution scope
                # is a property of the owner, and a fact carries neither the
                # planning pool nor the planning organization.
                organization_ref=scope[2],
                planning_stock_pool=scope[3],
                requirement_id=requirement_id,
                order_ref=order_ref,
            )
        )

    # Preflight all owner scopes before the first write.  This keeps an
    # out-of-scope/owner defect from producing a partial result even though
    # transaction rollback remains the caller's responsibility.
    reserves_by_scope: dict[DistributionScope, tuple[Reserve, ...]] = {}
    for scope in scopes:
        # Owners are selected by the canonical pool key, not by raw columns.
        # Canon §18: an accepted ``assembly_in`` closes a ``rework`` reserve
        # as well as a ``make`` one, and it is the same physical fact - so
        # both kinds of owner belong to this one scope and the canonical
        # allocator settles them addressed-then-FIFO.  Selecting only
        # ``make`` here silently left every rework reserve open.
        owners = [
            owner
            for owner in db.query(models.ReservationEntry)
            .filter(
                models.ReservationEntry.is_current.is_(True),
                models.ReservationEntry.lifecycle_status == "active",
                models.ReservationEntry.current_identity != "",
                models.ReservationEntry.item_id == scope[0],
                models.ReservationEntry.realization_mode.in_(
                    scope_modes(scope[4])
                ),
            )
            .order_by(models.ReservationEntry.id.asc())
            .all()
            if distribution_scope_for_fact(
                int(owner.item_id),
                _text(owner.characteristic_ref),
                _text(owner.organization_ref),
            ) == distribution_scope_for_fact(int(scope[0]), scope[1], scope[2])
        ]
        if not owners:
            raise CurrentReplenishmentError(
                f"bounded make scope has no stable current reservation owners: {_scope_key(scope)}"
            )
        if len({str(row.current_identity) for row in owners}) != len(owners):
            raise CurrentReplenishmentError(
                f"bounded make scope has duplicate current reservation identities: {_scope_key(scope)}"
            )
        requirement_ids = sorted({int(row.requirement_id) for row in owners})
        order_refs: dict[int, tuple[str, ...]] = {}
        if requirement_ids:
            linked = (
                db.query(
                    models.ProductionProduct.source_mrp_requirement_id,
                    models.ProductionOrder.order_ref1c,
                )
                .join(
                    models.ProductionOrder,
                    models.ProductionOrder.order_id == models.ProductionProduct.order_id,
                )
                .filter(
                    models.ProductionProduct.source_mrp_requirement_id.in_(requirement_ids),
                    models.ProductionOrder.order_ref1c.isnot(None),
                )
                .all()
            )
            refs: dict[int, set[str]] = {}
            for requirement_id, order_ref in linked:
                if str(order_ref or "").strip():
                    refs.setdefault(int(requirement_id), set()).add(str(order_ref))
            order_refs = {
                int(requirement_id): tuple(sorted(values))
                for requirement_id, values in refs.items()
            }
        owner_baselines = freeze_baselines_by_reservation(db, owners)
        reserves_by_scope[scope] = tuple(
            Reserve(
                reserve_id=str(int(row.id)),
                item_id=int(row.item_id),
                mode="make",
                reserved_qty=_decimal(row.replenishment_required_qty),
                due_date=row.priority_period_to,
                plan_period_from=row.priority_period_from,
                plan_period_to=row.priority_period_to,
                run_id=int(row.run_id or 0),
                requirement_id=int(row.requirement_id),
                bucket_date=None,
                bucket_id=None,
                # Attribution keys come from the scope, the canonical pool key
                # both sides were resolved through; taking them from the raw
                # owner columns would put the reserve and the fact in two
                # different pools for the same obligation.
                characteristic_ref=scope[1],
                organization_ref=scope[2],
                planning_stock_pool=scope[3],
                order_refs=order_refs.get(int(row.requirement_id), ()),
                baseline_at=owner_baselines.get(int(row.id)),
            )
            for row in owners
        )

    results: list[CurrentReplenishmentResult] = []
    for scope in scopes:
        results.append(
            apply_current_replenishment(
                db,
                generation_id=int(target.id),
                source_key=ASSEMBLY_OUTPUT_SOURCE_KEY,
                source_revision=revision,
                facts=tuple(facts_by_scope[scope]),
                reserves=reserves_by_scope[scope],
                complete_scope=True,
                distribution_scope=scope,
                allow_building=True,
                revision_basis=basis,
            )
        )
    return BoundedMakeReplenishmentResult(
        target_generation_id=int(target.id),
        parent_generation_id=int(parent.id),
        source_revision=revision,
        affected_scopes=tuple(scopes),
        scope_history_rows=sum(len(rows) for rows in facts_by_scope.values()),
        results=tuple(results),
    )


def _normalise_bounded_buy_scopes(
    affected_scopes: Iterable[DistributionScope],
) -> tuple[DistributionScope, ...]:
    scopes: list[DistributionScope] = []
    seen: set[DistributionScope] = set()
    for raw in affected_scopes:
        try:
            values = tuple(raw)
            scope = (
                int(values[0]),
                _text(values[1]),
                _text(values[2]),
                _text(values[3]),
                _text(values[4]),
            )
        except (TypeError, ValueError, IndexError) as exc:
            raise CurrentReplenishmentError("bounded BUY scope is malformed") from exc
        if len(values) != 5 or scope[0] <= 0:
            raise CurrentReplenishmentError("bounded BUY scope is malformed")
        if scope[4] != "buy":
            raise CurrentReplenishmentError(
                "bounded BUY publication supports only realization_mode=buy"
            )
        if scope in seen:
            raise CurrentReplenishmentError("bounded BUY scopes contain duplicates")
        seen.add(scope)
        scopes.append(scope)
    if not scopes:
        raise CurrentReplenishmentError("bounded BUY publication requires affected scopes")
    return tuple(sorted(scopes))


def _bounded_buy_fact_scope(
    fact: object,
    scopes_by_item_pool: dict[tuple[int, str], tuple[DistributionScope, ...]],
) -> DistributionScope:
    from .supplier_receipt_allocation import ReceiptFact

    if not isinstance(fact, ReceiptFact):
        raise CurrentReplenishmentError(
            "bounded BUY manifest requires typed ReceiptFact evidence"
        )
    candidates = scopes_by_item_pool.get(
        (int(fact.item_id), _text(fact.planning_stock_pool)), ()
    )
    if len(candidates) != 1:
        raise CurrentReplenishmentError(
            f"supplier receipt {int(fact.sle_id)} has ambiguous BUY distribution scope"
        )
    return candidates[0]


def _bounded_buy_manifest_facts(
    value: BoundedBuyReceiptDeltaManifest,
    *,
    scopes: tuple[DistributionScope, ...],
    db: Session,
    parent: models.LedgerGeneration,
    target: models.LedgerGeneration,
) -> tuple[
    dict[DistributionScope, tuple[object, ...]],
    dict[int, object],
    set[int],
]:
    """Validate typed delta/full-scope facts and explicit supersession IDs."""

    from .supplier_receipt_allocation import ReceiptFact

    lower = int(parent.physical_import_batch_id)
    upper = int(target.physical_import_batch_id)
    scopes_by_item_pool: dict[tuple[int, str], tuple[DistributionScope, ...]] = {}
    for scope in scopes:
        key = (scope[0], scope[3])
        scopes_by_item_pool[key] = (*scopes_by_item_pool.get(key, ()), scope)

    delta_by_id: dict[int, object] = {}
    if set(value.new_sle_ids) != {
        int(getattr(row, "sle_id", -1)) for row in value.receipt_facts
    }:
        raise CurrentReplenishmentError(
            "BUY manifest new_sle_ids must exactly match typed delta facts"
        )
    for fact in value.receipt_facts:
        if not isinstance(fact, ReceiptFact):
            raise CurrentReplenishmentError(
                "bounded BUY manifest requires typed ReceiptFact evidence"
            )
        fact_id = int(fact.sle_id)
        if fact_id in delta_by_id:
            raise CurrentReplenishmentError("bounded BUY manifest has duplicate receipt fact")
        delta_by_id[fact_id] = fact

    declared_ids = set(delta_by_id)
    if value.scope_receipt_facts and not declared_ids and not value.supersession_edge_ids:
        raise CurrentReplenishmentError(
            "BUY complete scope evidence requires an explicit delta or supersession edge"
        )
    persisted = {
        int(row.id): row
        for row in db.query(models.StockLedgerEntry)
        .filter(
            models.StockLedgerEntry.id.in_(sorted(declared_ids))
            if declared_ids else models.StockLedgerEntry.id < 0
        )
        .all()
    }
    if set(persisted) != declared_ids:
        raise CurrentReplenishmentError("BUY manifest references missing SLE")
    for fact_id, fact in delta_by_id.items():
        row = persisted[fact_id]
        scope = _bounded_buy_fact_scope(fact, scopes_by_item_pool)
        # Organization is deliberately absent from this comparison.  The scope
        # carries the planning organization of the BUY owner while the row
        # carries the 1C organization that posted the document; the publisher
        # that built this scope already refused to equate the two, and the
        # manifest builder places rows by item and characteristic for the same
        # reason.  Everything that is genuinely the same fact on both sides -
        # item, characteristic, signed quantity and posting instant - is still
        # compared.
        if (
            int(row.item_id) != int(fact.item_id)
            or _text(row.characteristic_ref) != scope[1]
            or _decimal(row.qty) != _decimal(fact.signed_qty)
            or _comparable_datetime(row.posting_at)
            != _comparable_datetime(fact.posting_at)
        ):
            raise CurrentReplenishmentError(
                f"typed supplier receipt {fact_id} contradicts persisted SLE"
            )
        if not (lower < int(row.ingest_batch_id) <= upper):
            raise CurrentReplenishmentError("BUY delta SLE is outside target import boundary")
        if row.posting_at is None or _comparable_datetime(row.posting_at) > _comparable_datetime(target.cutoff):
            raise CurrentReplenishmentError("BUY delta SLE is outside target cutoff")
        if _comparable_datetime(row.posting_at) <= _comparable_datetime(parent.cutoff) and value.backdate_from is None:
            raise CurrentReplenishmentError(
                "BUY backdated delta requires explicit bounded scope evidence"
            )
        if value.backdate_from is not None and _comparable_datetime(row.posting_at) < _comparable_datetime(value.backdate_from):
            raise CurrentReplenishmentError("BUY delta precedes declared backdate boundary")

    edges = {
        int(row.id): row
        for row in db.query(models.StockLedgerFactSupersession)
        .filter(
            models.StockLedgerFactSupersession.id.in_(sorted(value.supersession_edge_ids))
            if value.supersession_edge_ids else models.StockLedgerFactSupersession.id < 0
        )
        .all()
    }
    if set(edges) != set(value.supersession_edge_ids):
        raise CurrentReplenishmentError("BUY manifest references missing supersession edge")
    old_ids: set[int] = set()
    for edge in edges.values():
        if edge.old_sle_id is None or int(edge.old_sle_id) in old_ids:
            raise CurrentReplenishmentError("BUY manifest has duplicate supersession basis")
        old_ids.add(int(edge.old_sle_id))
        old = db.get(models.StockLedgerEntry, int(edge.old_sle_id))
        if old is None:
            raise CurrentReplenishmentError("BUY supersession basis SLE is missing")
        matching = [
            scope for scope in scopes
            if int(old.item_id) == scope[0]
            and _text(old.characteristic_ref) == scope[1]
            and _text(old.organization_ref) == scope[2]
        ]
        if len(matching) != 1:
            raise CurrentReplenishmentError(
                "BUY supersession basis is outside or ambiguous affected scope"
            )
        if not (lower < int(edge.import_batch_id) <= upper):
            raise CurrentReplenishmentError("BUY supersession edge is outside target boundary")
        if int(old.ingest_batch_id) > lower and int(old.id) not in declared_ids:
            raise CurrentReplenishmentError("BUY supersession old delta SLE is missing")
        if edge.new_sle_id is not None:
            if int(edge.new_sle_id) not in declared_ids:
                raise CurrentReplenishmentError("BUY supersession new SLE is missing typed evidence")
            new = persisted.get(int(edge.new_sle_id)) or db.get(
                models.StockLedgerEntry, int(edge.new_sle_id)
            )
            if new is None or (
                _text(new.characteristic_ref) != _text(old.characteristic_ref)
                or _text(new.organization_ref) != _text(old.organization_ref)
                or int(new.item_id) != int(old.item_id)
            ):
                raise CurrentReplenishmentError("BUY supersession old/new keys differ")
        prior = (
            db.query(models.StockLedgerFactSupersession.id)
            .filter(
                models.StockLedgerFactSupersession.old_sle_id == int(old.id),
                models.StockLedgerFactSupersession.import_batch_id <= lower,
            )
            .first()
        )
        if prior is not None:
            raise CurrentReplenishmentError("BUY supersession basis was absent from parent")

    scope_facts_by_scope: dict[DistributionScope, list[object]] = {
        scope: [] for scope in scopes
    }
    scope_ids: set[int] = set()
    for fact in value.scope_receipt_facts:
        if not isinstance(fact, ReceiptFact):
            raise CurrentReplenishmentError(
                "bounded BUY scope evidence requires typed ReceiptFact rows"
            )
        fact_id = int(fact.sle_id)
        if fact_id in scope_ids:
            raise CurrentReplenishmentError("bounded BUY scope evidence has duplicate SLE")
        scope_ids.add(fact_id)
        scope = _bounded_buy_fact_scope(fact, scopes_by_item_pool)
        row = db.get(models.StockLedgerEntry, fact_id)
        if row is None or (
            _decimal(row.qty) != _decimal(fact.signed_qty)
            or int(row.item_id) != int(fact.item_id)
            or _text(row.characteristic_ref) != scope[1]
            or _text(row.organization_ref) != scope[2]
            or _comparable_datetime(row.posting_at)
            != _comparable_datetime(fact.posting_at)
        ):
            raise CurrentReplenishmentError("BUY scope evidence contradicts persisted SLE")
        if int(row.ingest_batch_id) > upper or _comparable_datetime(row.posting_at) > _comparable_datetime(target.cutoff):
            raise CurrentReplenishmentError("BUY scope evidence is outside target boundary")
        if int(row.ingest_batch_id) > lower and fact_id not in declared_ids:
            raise CurrentReplenishmentError(
                "BUY scope evidence has undeclared target delta SLE"
            )
        scope_facts_by_scope[scope].append(fact)
    if not declared_ids.issubset(scope_ids) and value.scope_receipt_facts:
        raise CurrentReplenishmentError("BUY scope evidence omits a typed delta fact")
    if (value.supersession_edge_ids or any(
        _decimal(fact.signed_qty) < 0
        or _comparable_datetime(fact.posting_at) <= _comparable_datetime(parent.cutoff)
        for fact in value.receipt_facts
    )) and not value.scope_receipt_facts:
        raise CurrentReplenishmentError(
            "BUY correction/return requires complete bounded scope evidence"
        )
    for fact_id in declared_ids:
        fact = delta_by_id[fact_id]
        scope = _bounded_buy_fact_scope(fact, scopes_by_item_pool)
        if not value.scope_receipt_facts:
            scope_facts_by_scope[scope].append(fact)
    return (
        {scope: tuple(rows) for scope, rows in scope_facts_by_scope.items()},
        delta_by_id,
        old_ids,
    )


def _bounded_current_buy_basis_facts(
    db: Session,
    *,
    parent: models.LedgerGeneration,
    scopes: tuple[DistributionScope, ...],
    fallback_by_id: dict[int, object],
) -> dict[DistributionScope, tuple[object, ...]]:
    """Adapt stable current allocation IDs to typed parent receipt facts."""

    from .supplier_receipt_allocation import ReceiptFact

    item_ids = sorted({scope[0] for scope in scopes})
    rows = (
        db.query(models.ReservationConsumptionAllocation, models.ReservationEntry)
        .join(
            models.ReservationEntry,
            models.ReservationEntry.id
            == models.ReservationConsumptionAllocation.reservation_id,
        )
        .filter(
            models.ReservationConsumptionAllocation.is_current.is_(True),
            models.ReservationConsumptionAllocation.allocation_role
            == "replenishment_receipt",
            models.ReservationEntry.is_current.is_(True),
            models.ReservationEntry.lifecycle_status == "active",
            models.ReservationEntry.realization_mode == "buy",
            models.ReservationEntry.item_id.in_(item_ids),
        )
        .with_for_update()
        .all()
        if item_ids else []
    )
    allocation_scope_by_id: dict[int, DistributionScope] = {}
    for allocation, entry in rows:
        scope = (
            int(entry.item_id),
            _text(entry.characteristic_ref),
            _text(entry.organization_ref),
            _text(entry.planning_stock_pool),
            "buy",
        )
        if scope not in scopes:
            raise CurrentReplenishmentError(
                "current BUY allocation is outside affected scopes"
            )
        allocation_scope_by_id[int(allocation.sle_id)] = scope
    sle_ids = sorted(allocation_scope_by_id)
    if not sle_ids:
        return {scope: () for scope in scopes}
    sles = {
        int(row.id): row
        for row in db.query(models.StockLedgerEntry)
        .filter(models.StockLedgerEntry.id.in_(sle_ids))
        .all()
    }
    provenance_rows = (
        db.query(models.StockLedgerSupplierReceiptProvenance)
        .filter(
            models.StockLedgerSupplierReceiptProvenance.stock_ledger_entry_id.in_(sle_ids),
            models.StockLedgerSupplierReceiptProvenance.operation_kind.in_(
                ("supplier_receipt", "correction", "supplier_return")
            ),
            models.StockLedgerSupplierReceiptProvenance.match_status != "excluded_non_supplier",
        )
        .all()
    )
    provenance: dict[int, models.StockLedgerSupplierReceiptProvenance] = {}
    for row in provenance_rows:
        sle_id = int(row.stock_ledger_entry_id)
        previous = provenance.get(sle_id)
        if previous is not None:
            signature = (
                _text(row.receipt_doc_ref), _text(row.receipt_doc_line_no),
                _text(row.supplier_order_ref), _text(row.supplier_order_line_no),
                _text(row.operation_kind), _text(row.correction_receipt_ref),
            )
            previous_signature = (
                _text(previous.receipt_doc_ref), _text(previous.receipt_doc_line_no),
                _text(previous.supplier_order_ref), _text(previous.supplier_order_line_no),
                _text(previous.operation_kind), _text(previous.correction_receipt_ref),
            )
            if signature != previous_signature:
                raise CurrentReplenishmentError(
                    "supplier evidence for one SLE is inconsistent across generations"
                )
            continue
        provenance[sle_id] = row
    by_scope: dict[DistributionScope, list[object]] = {scope: [] for scope in scopes}
    for sle_id, scope in allocation_scope_by_id.items():
        sle = sles.get(sle_id)
        if sle is None:
            raise CurrentReplenishmentError("current BUY allocation references missing SLE")
        evidence = provenance.get(sle_id)
        fallback = fallback_by_id.get(sle_id)
        if evidence is None and isinstance(fallback, ReceiptFact):
            fact = fallback
        elif evidence is None:
            raise CurrentReplenishmentError(
                "current BUY allocation lacks typed parent supplier evidence"
            )
        else:
            if str(evidence.match_status) in {"ambiguous", "excluded_non_supplier"}:
                raise CurrentReplenishmentError(
                    "current BUY allocation has ambiguous or excluded supplier evidence"
                )
            fact = ReceiptFact(
                sle_id=sle_id,
                posting_at=sle.posting_at,
                known_at=getattr(sle, "known_at", None),
                signed_qty=_decimal(sle.qty),
                item_id=int(sle.item_id),
                supplier_order_ref=_text(evidence.supplier_order_ref),
                supplier_order_line_no=_text(evidence.supplier_order_line_no),
                receipt_ref=_text(evidence.receipt_doc_ref),
                receipt_line_no=_text(evidence.receipt_doc_line_no),
                correction_receipt_ref=_text(evidence.correction_receipt_ref) or None,
                planning_stock_pool=scope[3],
            )
        if not isinstance(fact, ReceiptFact) or int(fact.sle_id) != sle_id:
            raise CurrentReplenishmentError("current BUY basis evidence is malformed")
        by_scope[scope].append(fact)
    return {scope: tuple(rows) for scope, rows in by_scope.items()}


def _bounded_current_buy_reserves(
    db: Session,
    *,
    scopes: tuple[DistributionScope, ...],
) -> dict[DistributionScope, tuple[Reserve, ...]]:
    owners = (
        db.query(models.ReservationEntry)
        .filter(
            models.ReservationEntry.is_current.is_(True),
            models.ReservationEntry.lifecycle_status == "active",
            models.ReservationEntry.current_identity != "",
            models.ReservationEntry.realization_mode == "buy",
            models.ReservationEntry.item_id.in_(sorted({scope[0] for scope in scopes})),
        )
        .order_by(models.ReservationEntry.id.asc())
        .all()
    )
    result: dict[DistributionScope, list[Reserve]] = {scope: [] for scope in scopes}
    identities: dict[DistributionScope, set[str]] = {scope: set() for scope in scopes}
    baselines = freeze_baselines_by_reservation(db, owners)
    for row in owners:
        scope = (
            int(row.item_id),
            _text(row.characteristic_ref),
            _text(row.organization_ref),
            _text(row.planning_stock_pool),
            "buy",
        )
        if scope not in result:
            continue
        identity = _text(row.current_identity)
        if identity in identities[scope]:
            raise CurrentReplenishmentError(
                f"bounded BUY scope has duplicate current reservation identity: {_scope_key(scope)}"
            )
        identities[scope].add(identity)
        result[scope].append(
            Reserve(
                reserve_id=str(int(row.id)),
                item_id=int(row.item_id),
                mode="buy",
                reserved_qty=_decimal(row.replenishment_required_qty),
                due_date=row.priority_period_to,
                plan_period_from=row.priority_period_from,
                plan_period_to=row.priority_period_to,
                run_id=int(row.run_id or 0),
                requirement_id=int(row.requirement_id),
                characteristic_ref=_text(row.characteristic_ref),
                organization_ref=_text(row.organization_ref),
                planning_stock_pool=_text(row.planning_stock_pool),
                baseline_at=baselines.get(int(row.id)),
            )
        )
    missing = [scope for scope in scopes if not result[scope]]
    if missing:
        raise CurrentReplenishmentError(
            f"bounded BUY scope has no stable current reservation owners: {_scope_key(missing[0])}"
        )
    return {scope: tuple(rows) for scope, rows in result.items()}


def _ensure_bounded_supplier_evidence(
    db: Session,
    *,
    target: models.LedgerGeneration,
    facts_by_id: dict[int, object],
) -> None:
    """Persist each new typed fact once; never clone existing evidence."""

    from .supplier_receipt_allocation import ReceiptFact

    if not facts_by_id:
        return
    existing_rows = (
        db.query(models.StockLedgerSupplierReceiptProvenance)
        .filter(
            models.StockLedgerSupplierReceiptProvenance.stock_ledger_entry_id.in_(
                sorted(facts_by_id)
            ),
            models.StockLedgerSupplierReceiptProvenance.operation_kind.in_(
                ("supplier_receipt", "correction", "supplier_return")
            ),
        )
        .all()
    )
    existing_by_id: dict[int, models.StockLedgerSupplierReceiptProvenance] = {}
    for row in existing_rows:
        sle_id = int(row.stock_ledger_entry_id)
        previous = existing_by_id.get(sle_id)
        if previous is not None:
            signature = (
                _text(row.receipt_doc_ref), _text(row.receipt_doc_line_no),
                _text(row.supplier_order_ref), _text(row.supplier_order_line_no),
                _text(row.operation_kind), _text(row.correction_receipt_ref),
            )
            previous_signature = (
                _text(previous.receipt_doc_ref), _text(previous.receipt_doc_line_no),
                _text(previous.supplier_order_ref), _text(previous.supplier_order_line_no),
                _text(previous.operation_kind), _text(previous.correction_receipt_ref),
            )
            if signature != previous_signature:
                raise CurrentReplenishmentError(
                    f"typed supplier evidence conflicts across generations for SLE {sle_id}"
                )
            continue
        existing_by_id[sle_id] = row
    for fact_id, raw in facts_by_id.items():
        if not isinstance(raw, ReceiptFact):
            raise CurrentReplenishmentError("typed supplier evidence is malformed")
        kind = (
            "correction"
            if raw.correction_receipt_ref
            else "supplier_return"
            if _decimal(raw.signed_qty) < 0
            else "supplier_receipt"
        )
        expected_signature = (
            _text(raw.receipt_ref), _text(raw.receipt_line_no),
            _text(raw.supplier_order_ref), _text(raw.supplier_order_line_no),
            kind, _text(raw.correction_receipt_ref),
        )
        existing = existing_by_id.get(int(fact_id))
        if existing is not None:
            if _text(existing.match_status) in {"ambiguous", "excluded_non_supplier"}:
                raise CurrentReplenishmentError(
                    f"typed supplier evidence for SLE {fact_id} is ambiguous or excluded"
                )
            actual_signature = (
                _text(existing.receipt_doc_ref), _text(existing.receipt_doc_line_no),
                _text(existing.supplier_order_ref), _text(existing.supplier_order_line_no),
                _text(existing.operation_kind), _text(existing.correction_receipt_ref),
            )
            if actual_signature != expected_signature:
                raise CurrentReplenishmentError(
                    f"typed supplier evidence conflicts for SLE {fact_id}"
                )
            from .supplier_receipt_allocation import provenance_is_pre_contract

            if not provenance_is_pre_contract(existing):
                continue
            # A row written before the one-row contract names its writer
            # instead of a 1C document/operation, or lacks the order type of
            # an exact line.  The fact and its typing are unchanged - only the
            # row's own evidence is brought up to the contract - so this is a
            # repair on write, idempotent.  Rows no later delta touches are
            # resolved by the reader and normalised by the repair phase.
        from .supplier_receipt_allocation import (
            SupplierReceiptEvidenceError,
            build_supplier_receipt_provenance,
            canonical_operation_for_kind,
        )

        exact = bool(_text(raw.supplier_order_ref) and _text(raw.supplier_order_line_no))
        sle = db.get(models.StockLedgerEntry, int(fact_id))
        if sle is None:
            raise CurrentReplenishmentError(
                f"typed supplier evidence references missing SLE {fact_id}"
            )
        operation_key, operation_name = canonical_operation_for_kind(kind)
        try:
            upgraded = build_supplier_receipt_provenance(
                ledger_generation_id=int(target.id),
                stock_ledger_entry_id=int(fact_id),
                # One row contract: the same builder the canonical writer uses, so
                # both stores are filled the same way and an obligation refresh can
                # rebuild from a row the bounded path wrote.
                # The document identity is the SLE's own: the rebuild matches
                # evidence to its physical row by exactly this triple.
                receipt_doc_type=sle.recorder_type,
                receipt_doc_ref=_text(raw.receipt_ref) or sle.recorder_ref,
                receipt_doc_line_no=_text(raw.receipt_line_no) or sle.line_no,
                # The operation is the documented one for this kind, not a marker
                # naming the writer: the rebuild resolves a row back to its
                # operation from these two fields.
                operation_kind=kind,
                operation_key=operation_key,
                operation_name=operation_name,
                item_id=int(raw.item_id),
                signed_qty=raw.signed_qty,
                match_rule="bounded-typed",
                match_status="exact" if exact else "unmatched",
                # The order document the manifest matched; the builder refuses an
                # exact row without it, because the rebuild would replay it as
                # ``unmatched`` and the receipt would stop allocating.
                supplier_order_type=_text(getattr(raw, "supplier_order_type", "")),
                supplier_order_ref=raw.supplier_order_ref,
                supplier_order_line_no=raw.supplier_order_line_no,
                characteristic_ref=sle.characteristic_ref,
                warehouse_ref1c=sle.warehouse_ref1c,
                correction_receipt_ref=raw.correction_receipt_ref,
                ambiguity_count=0,
                reason=None if exact else "typed evidence has no supplier order line",
            )
        except SupplierReceiptEvidenceError as exc:
            raise CurrentReplenishmentError(str(exc)) from exc
        if existing is not None:
            for field in (
                "receipt_doc_type", "receipt_doc_ref", "receipt_doc_line_no",
                "operation_key", "operation_name", "evidence_hash",
                "evidence_payload", "match_rule",
            ):
                setattr(existing, field, getattr(upgraded, field))
        else:
            db.add(upgraded)
    db.flush()


def apply_current_replenishment_for_bounded_buy_scopes(
    db: Session,
    *,
    target_generation_id: int,
    parent_generation_id: int,
    target_cutoff: datetime,
    affected_scopes: Iterable[DistributionScope],
    delta_manifest: BoundedBuyReceiptDeltaManifest | Mapping[str, object],
    source_revision: int | None = None,
) -> BoundedBuyReplenishmentResult:
    """Apply typed supplier receipts to stable current BUY owners only.

    Forward receipts use the current allocation basis plus the explicit typed
    delta.  Returns, corrections, supersessions and backdates must provide a
    complete typed stream for the affected scope.  No generation-wide
    visibility query or generation-scoped provenance copy is performed.
    """

    revision, basis = _adapter_revision(source_revision, int(target_generation_id))
    scopes = _normalise_bounded_buy_scopes(affected_scopes)
    manifest = _normalise_bounded_buy_manifest(delta_manifest)
    target = db.get(models.LedgerGeneration, int(target_generation_id))
    parent = db.get(models.LedgerGeneration, int(parent_generation_id))
    if target is None or _text(target.status) != "building":
        raise CurrentReplenishmentError(
            "bounded BUY publication requires a BUILDING target generation"
        )
    if parent is None or _text(parent.status) != "accepted":
        raise CurrentReplenishmentError(
            "bounded BUY publication requires an accepted parent generation"
        )
    if int(target.id) == int(parent.id):
        raise CurrentReplenishmentError("bounded BUY target must differ from parent")
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None or int(pointer.current_generation_id or -1) != int(parent.id):
        raise CurrentReplenishmentError("bounded BUY parent is not current truth")
    if target_cutoff is None or target.cutoff is None or parent.cutoff is None:
        raise CurrentReplenishmentError("bounded BUY cutoffs are required")
    requested_cutoff = target_cutoff
    if target.cutoff.tzinfo is not None and requested_cutoff.tzinfo is None:
        requested_cutoff = requested_cutoff.replace(tzinfo=timezone.utc)
    elif target.cutoff.tzinfo is None and requested_cutoff.tzinfo is not None:
        requested_cutoff = requested_cutoff.replace(tzinfo=None)
    if _comparable_datetime(target.cutoff) != _comparable_datetime(requested_cutoff):
        raise CurrentReplenishmentError("bounded BUY target cutoff mismatch")
    if _comparable_datetime(target.cutoff) < _comparable_datetime(parent.cutoff):
        raise CurrentReplenishmentError("bounded BUY target cutoff precedes parent")
    if target.physical_import_batch_id is None or parent.physical_import_batch_id is None:
        raise CurrentReplenishmentError("bounded BUY generations require import batches")
    if int(target.physical_import_batch_id) < int(parent.physical_import_batch_id):
        raise CurrentReplenishmentError("bounded BUY target import boundary precedes parent")
    from .physical_visibility import PhysicalVisibilityError, require_import_batch

    try:
        parent_batch = require_import_batch(db, int(parent.physical_import_batch_id))
        target_batch = require_import_batch(db, int(target.physical_import_batch_id))
    except PhysicalVisibilityError as exc:
        raise CurrentReplenishmentError(str(exc)) from exc
    if (
        _comparable_datetime(parent_batch.cutoff) != _comparable_datetime(parent.cutoff)
        or _comparable_datetime(target_batch.cutoff) != _comparable_datetime(target.cutoff)
    ):
        raise CurrentReplenishmentError("BUY generation cutoff does not match import boundary")

    if not (
        manifest.new_sle_ids
        or manifest.receipt_facts
        or manifest.scope_receipt_facts
        or manifest.supersession_edge_ids
    ):
        return BoundedBuyReplenishmentResult(
            target_generation_id=int(target.id),
            parent_generation_id=int(parent.id),
            source_revision=revision,
            affected_scopes=scopes,
            delta_fact_rows=0,
            scope_replay_rows=0,
            results=(),
        )

    scope_facts, delta_by_id, superseded_ids = _bounded_buy_manifest_facts(
        manifest,
        scopes=scopes,
        db=db,
        parent=parent,
        target=target,
    )
    _ensure_bounded_supplier_evidence(
        db,
        target=target,
        facts_by_id=delta_by_id,
    )
    reserves_by_scope = _bounded_current_buy_reserves(db, scopes=scopes)
    basis_by_scope = _bounded_current_buy_basis_facts(
        db,
        parent=parent,
        scopes=scopes,
        fallback_by_id=delta_by_id,
    )
    from .supplier_receipt_allocation import _exact_allocation_caps_by_order_line

    exact_caps = _exact_allocation_caps_by_order_line(
        db,
        ledger_generation_id=int(parent.id),
        item_ids={scope[0] for scope in scopes},
        current_owner_ids={
            int(reserve.reserve_id)
            for reserves in reserves_by_scope.values()
            for reserve in reserves
        },
    )
    scopes_by_item_pool: dict[tuple[int, str], tuple[DistributionScope, ...]] = {}
    for candidate in scopes:
        key = (candidate[0], candidate[3])
        scopes_by_item_pool[key] = (*scopes_by_item_pool.get(key, ()), candidate)
    delta_scopes = {
        _bounded_buy_fact_scope(fact, scopes_by_item_pool)
        for fact in manifest.receipt_facts
    }
    results: list[CurrentReplenishmentResult] = []
    replay_rows = 0
    for scope in scopes:
        explicit = tuple(scope_facts[scope])
        retired_here = {
            int(fact.sle_id) for fact in basis_by_scope[scope]
        } & set(superseded_ids)
        if not explicit and scope not in delta_scopes and not retired_here:
            # A declared scope may legitimately have no semantic input in a
            # multi-scope manifest.  Do not touch its marker or allocations.
            continue
        if manifest.scope_receipt_facts:
            # A superseded fact is deliberately absent from the replacement
            # stream: the correction removes it from the basis, and its current
            # assignment is retired by this very replay.
            baseline_ids = {
                int(fact.sle_id) for fact in basis_by_scope[scope]
            } - set(superseded_ids)
            explicit_ids = {int(fact.sle_id) for fact in explicit}
            if not baseline_ids.issubset(explicit_ids):
                raise CurrentReplenishmentError(
                    "BUY complete scope evidence omits current allocation basis"
                )
            full_facts = explicit
        else:
            full_by_id = {int(fact.sle_id): fact for fact in basis_by_scope[scope]}
            for fact in explicit:
                existing = full_by_id.get(int(fact.sle_id))
                if existing is not None and existing != fact:
                    raise CurrentReplenishmentError(
                        "BUY current basis and delta evidence disagree"
                    )
                full_by_id[int(fact.sle_id)] = fact
            full_facts = tuple(full_by_id.values())
        if not full_facts and not retired_here:
            continue
        # SQLite strips timezone markers from persisted SLE timestamps while
        # typed import evidence commonly arrives as aware UTC.  The allocator
        # sorts the complete bounded stream, so keep its timestamp axis
        # homogeneous without changing the represented source instant.
        full_facts = tuple(
            replace(
                fact,
                posting_at=_comparable_datetime(fact.posting_at),
                known_at=_comparable_datetime(fact.known_at),
            )
            for fact in full_facts
        )
        replay_rows += len(full_facts)
        visible_ids = {int(fact.sle_id) for fact in full_facts}
        results.append(
            apply_current_receipt_replay(
                db,
                generation_id=int(target.id),
                source_key=SUPPLIER_RECEIPT_SOURCE_KEY,
                source_revision=revision,
                receipt_facts=full_facts,
                reserves=reserves_by_scope[scope],
                complete_scope=True,
                distribution_scope=scope,
                exact_allocation_caps=exact_caps,
                history_mode="as_occurred",
                allow_building=True,
                validated_visible_ids=visible_ids,
                revision_basis=basis,
            )
        )
    return BoundedBuyReplenishmentResult(
        target_generation_id=int(target.id),
        parent_generation_id=int(parent.id),
        source_revision=revision,
        affected_scopes=scopes,
        delta_fact_rows=len(delta_by_id),
        scope_replay_rows=replay_rows,
        results=tuple(results),
    )
