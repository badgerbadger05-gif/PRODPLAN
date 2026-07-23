"""Read-only DBR projection over the current accepted Item Ledger.

The projection deliberately has no compatibility path to mutable production or
purchase-order counters.  An accepted, fully ready generation is either read
exactly, or the caller receives :class:`PlanningTruthUnavailable`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from collections.abc import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.services.planning_truth import (
    CAPABILITY_EXECUTION_ALLOCATIONS,
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_PLANNING_SNAPSHOTS,
    CAPABILITY_RESERVATION_REPLAY,
    require_accepted_truth,
)


CONSUMER = "dbr-ledger-projection"
REQUIRED_CAPABILITIES = (
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_RESERVATION_REPLAY,
    CAPABILITY_EXECUTION_ALLOCATIONS,
    CAPABILITY_PLANNING_SNAPSHOTS,
)
ZERO = Decimal("0")


@dataclass(frozen=True, order=True)
class LedgerProjectionKey:
    item_code: str
    warehouse_ref1c: str


@dataclass(frozen=True)
class FutureSupplyLine:
    supply_kind: str
    source_ref: str
    source_line_ref: str
    planning_stock_pool: str
    eta_date: object | None
    open_qty: Decimal


@dataclass(frozen=True)
class ExcludedFutureSupply:
    supply_kind: str
    source_ref: str
    source_line_ref: str
    evidence_status: str
    destination_warehouse_ref1c: str
    reason: str | None


@dataclass(frozen=True)
class ReservationCoverageLine:
    source_kind: str
    source_ref: str
    source_line_ref: str
    pin_kind: str
    alloc_qty: Decimal
    covered_qty: Decimal
    realized_qty: Decimal
    evaporated_qty: Decimal


@dataclass(frozen=True)
class OpenObligation:
    reservation_id: int
    requirement_id: int
    planning_stock_pool: str
    realization_mode: str
    priority_period_from: object
    priority_period_to: object
    outstanding_qty: Decimal
    uncovered_qty: Decimal
    coverage: tuple[ReservationCoverageLine, ...]


@dataclass(frozen=True)
class LedgerItemProjection:
    key: LedgerProjectionKey
    generation_id: int
    on_hand: Decimal
    inbound: Decimal
    future_supply: tuple[FutureSupplyLine, ...]
    outstanding_obligation_qty: Decimal
    uncovered_qty: Decimal
    obligations: tuple[OpenObligation, ...]
    excluded_future_supply: tuple[ExcludedFutureSupply, ...]


@dataclass(frozen=True)
class LedgerProjection:
    generation_id: int
    rows: tuple[LedgerItemProjection, ...]


def _decimal(value: object | None) -> Decimal:
    if value is None:
        return ZERO
    return Decimal(str(value))


def _nonnegative(value: object | None) -> Decimal:
    return max(_decimal(value), ZERO)


def _normalized_keys(
    pairs: Iterable[LedgerProjectionKey | tuple[str, str]],
) -> tuple[LedgerProjectionKey, ...]:
    keys: set[LedgerProjectionKey] = set()
    for pair in pairs:
        key = pair if isinstance(pair, LedgerProjectionKey) else LedgerProjectionKey(*pair)
        item_code = str(key.item_code).strip()
        warehouse = str(key.warehouse_ref1c).strip()
        if not item_code:
            raise ValueError("item_code must not be empty")
        if not warehouse:
            raise ValueError("warehouse_ref1c must not be empty")
        keys.add(LedgerProjectionKey(item_code=item_code, warehouse_ref1c=warehouse))
    return tuple(sorted(keys))


def _planning_pool_mapping(
    keys: tuple[LedgerProjectionKey, ...],
    planning_pool_by_warehouse: Mapping[str, str],
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw_warehouse, raw_pool in planning_pool_by_warehouse.items():
        warehouse = str(raw_warehouse).strip()
        pool = str(raw_pool).strip()
        if not warehouse:
            raise ValueError("warehouse mapping key must not be empty")
        if not pool:
            raise ValueError(
                f"warehouse {warehouse!r} has an empty planning_stock_pool mapping"
            )
        if warehouse in mapping and mapping[warehouse] != pool:
            raise ValueError(
                f"warehouse {warehouse!r} has conflicting planning pool mappings"
            )
        mapping[warehouse] = pool

    for warehouse in sorted({key.warehouse_ref1c for key in keys}):
        if warehouse not in mapping:
            raise ValueError(
                f"warehouse {warehouse!r} has no exact planning_stock_pool mapping"
            )

    axes: dict[tuple[str, str], LedgerProjectionKey] = {}
    for key in keys:
        axis = (key.item_code, mapping[key.warehouse_ref1c])
        previous = axes.get(axis)
        if previous is not None:
            raise ValueError(
                "duplicate DBR projection axis "
                f"(item_code={axis[0]!r}, planning_stock_pool={axis[1]!r}) "
                f"requested through warehouses {previous.warehouse_ref1c!r} "
                f"and {key.warehouse_ref1c!r}"
            )
        axes[axis] = key
    return mapping


def _require_generation(
    db: Session,
    generation_id: int,
    *,
    expected_status: str,
) -> models.LedgerGeneration:
    status = str(expected_status).strip().lower()
    if status not in {"building", "accepted"}:
        raise ValueError("expected_status must be 'building' or 'accepted'")
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None:
        raise ValueError(f"Ledger generation {generation_id} does not exist")
    if str(generation.status) != status:
        raise ValueError(
            f"Ledger generation {generation_id} status mismatch: "
            f"expected={status}, actual={generation.status}"
        )
    if generation.cutoff is None:
        raise ValueError(f"Ledger generation {generation_id} has no cutoff")
    if status == "accepted" and generation.accepted_at is None:
        raise ValueError(
            f"accepted Ledger generation {generation_id} has no accepted_at"
        )
    missing = sorted(
        capability
        for capability in REQUIRED_CAPABILITIES
        if not bool((generation.capabilities or {}).get(capability))
    )
    if missing:
        raise ValueError(
            f"Ledger generation {generation_id} lacks capabilities: "
            + ", ".join(missing)
        )
    return generation


def build_generation_projection(
    db: Session,
    generation_id: int,
    pairs: Iterable[LedgerProjectionKey | tuple[str, str]],
    planning_pool_by_warehouse: Mapping[str, str],
    expected_status: str,
) -> LedgerProjection:
    """Project one explicit Ledger generation on the ``(item, pool)`` axis.

    ``pairs`` retain warehouse names at the boundary so callers can associate
    rows with their configured positions.  Facts are selected and aggregated
    by the exact warehouse-to-pool mapping, however.  Consequently two
    requested warehouses cannot represent the same ``(item, pool)`` position.

    This function intentionally does not consult ``PlanningTruthState``: it is
    also used while constructing a BUILDING generation before atomic publish.
    """
    _require_generation(db, int(generation_id), expected_status=expected_status)
    generation_id = int(generation_id)
    keys = _normalized_keys(pairs)
    if not keys:
        return LedgerProjection(generation_id=generation_id, rows=())
    pool_by_warehouse = _planning_pool_mapping(keys, planning_pool_by_warehouse)
    item_codes = sorted({key.item_code for key in keys})
    items = db.execute(
        select(models.Item.item_id, models.Item.item_code).where(
            models.Item.item_code.in_(item_codes)
        )
    ).all()
    item_id_by_code = {str(code): int(item_id) for item_id, code in items}
    missing_item_codes = sorted(set(item_codes) - set(item_id_by_code))
    if missing_item_codes:
        raise ValueError(
            "Ledger projection requested unknown item_code(s): "
            + ", ".join(repr(code) for code in missing_item_codes)
        )
    item_ids = sorted(item_id_by_code.values())
    warehouses = sorted(pool_by_warehouse)

    bins = db.execute(
        select(models.StockBin).where(
            models.StockBin.ledger_generation_id == generation_id,
            models.StockBin.item_id.in_(item_ids),
            models.StockBin.warehouse_ref1c.in_(warehouses),
        )
    ).scalars().all() if item_ids else []
    on_hand_by_axis: dict[tuple[int, str], Decimal] = {}
    for row in bins:
        warehouse = str(row.warehouse_ref1c)
        pool = pool_by_warehouse[warehouse]
        axis = (int(row.item_id), pool)
        on_hand_by_axis[axis] = on_hand_by_axis.get(axis, ZERO) + _decimal(row.on_hand)

    future_rows = db.execute(
        select(models.LedgerFutureSupply).where(
            models.LedgerFutureSupply.ledger_generation_id == generation_id,
            models.LedgerFutureSupply.item_id.in_(item_ids),
        )
    ).scalars().all() if item_ids else []
    exact_future: dict[tuple[int, str], list[FutureSupplyLine]] = {}
    excluded_future: dict[tuple[int, str], list[ExcludedFutureSupply]] = {}
    for row in future_rows:
        pool = str(row.planning_stock_pool)
        destination = str(row.destination_warehouse_ref1c or "")
        mapped_pool = pool_by_warehouse.get(destination)
        if row.evidence_status == "exact" and _decimal(row.open_qty_at_cutoff) > ZERO:
            if mapped_pool is None:
                continue
            if mapped_pool != pool:
                raise ValueError(
                    "exact future supply destination conflicts with planning pool "
                    f"(warehouse={destination!r}, mapped_pool={mapped_pool!r}, "
                    f"supply_pool={pool!r})"
                )
            axis = (int(row.item_id), pool)
            exact_future.setdefault(axis, []).append(FutureSupplyLine(
                supply_kind=str(row.supply_kind),
                source_ref=str(row.source_ref or ""),
                source_line_ref=str(row.source_line_ref or ""),
                planning_stock_pool=str(row.planning_stock_pool),
                eta_date=row.eta_date,
                open_qty=_decimal(row.open_qty_at_cutoff),
            ))
        elif row.evidence_status != "exact" and pool in pool_by_warehouse.values():
            excluded_future.setdefault((int(row.item_id), pool), []).append(ExcludedFutureSupply(
                supply_kind=str(row.supply_kind),
                source_ref=str(row.source_ref or ""),
                source_line_ref=str(row.source_line_ref or ""),
                evidence_status=str(row.evidence_status),
                destination_warehouse_ref1c=str(row.destination_warehouse_ref1c or ""),
                reason=row.reason,
            ))

    reservations = db.execute(
        select(models.ReservationEntry).where(
            models.ReservationEntry.ledger_generation_id == generation_id,
            models.ReservationEntry.item_id.in_(item_ids),
            models.ReservationEntry.lifecycle_status == "active",
        )
    ).scalars().all() if item_ids else []
    reservation_ids = [int(row.id) for row in reservations]
    coverage_rows = db.execute(
        select(models.ReservationCoverage).where(
            models.ReservationCoverage.reservation_id.in_(reservation_ids)
        )
    ).scalars().all() if reservation_ids else []
    coverage_by_reservation: dict[int, list[ReservationCoverageLine]] = {}
    for row in coverage_rows:
        coverage_by_reservation.setdefault(int(row.reservation_id), []).append(
            ReservationCoverageLine(
                source_kind=str(row.source_kind),
                source_ref=str(row.source_ref),
                source_line_ref=str(row.source_line_ref),
                pin_kind=str(row.pin_kind),
                alloc_qty=_decimal(row.alloc_qty),
                covered_qty=_decimal(row.covered_qty),
                realized_qty=_decimal(row.realized_qty),
                evaporated_qty=_decimal(row.evaporated_qty),
            )
        )

    obligations_by_axis: dict[tuple[int, str], list[OpenObligation]] = {}
    for row in reservations:
        coverage = tuple(sorted(
            coverage_by_reservation.get(int(row.id), []),
            key=lambda value: (
                value.source_kind, value.source_ref, value.source_line_ref, value.pin_kind
            ),
        ))
        axis = (int(row.item_id), str(row.planning_stock_pool))
        obligations_by_axis.setdefault(axis, []).append(OpenObligation(
            reservation_id=int(row.id),
            requirement_id=int(row.requirement_id),
            planning_stock_pool=str(row.planning_stock_pool),
            realization_mode=str(row.realization_mode),
            priority_period_from=row.priority_period_from,
            priority_period_to=row.priority_period_to,
            outstanding_qty=_nonnegative(
                _decimal(row.reserved_qty) - _decimal(row.realized_qty)
            ),
            uncovered_qty=_nonnegative(row.uncovered_qty),
            coverage=coverage,
        ))

    result: list[LedgerItemProjection] = []
    for key in keys:
        item_id = item_id_by_code.get(key.item_code)
        pool = pool_by_warehouse[key.warehouse_ref1c]
        axis = (item_id, pool)
        supply = tuple(sorted(
            exact_future.get(axis, []),
            key=lambda value: (
                value.eta_date is None,
                value.eta_date,
                value.supply_kind,
                value.source_ref,
                value.source_line_ref,
            ),
        ))
        obligations = tuple(sorted(
            obligations_by_axis.get(axis, []),
            key=lambda value: (
                value.priority_period_from,
                value.priority_period_to,
                value.reservation_id,
            ),
        ))
        diagnostics = tuple(sorted(
            excluded_future.get(axis, []),
            key=lambda value: (
                value.evidence_status,
                value.supply_kind,
                value.source_ref,
                value.source_line_ref,
            ),
        ))
        result.append(LedgerItemProjection(
            key=key,
            generation_id=generation_id,
            on_hand=on_hand_by_axis.get(axis, ZERO),
            inbound=sum((line.open_qty for line in supply), ZERO),
            future_supply=supply,
            outstanding_obligation_qty=sum(
                (entry.outstanding_qty for entry in obligations), ZERO
            ),
            uncovered_qty=sum((entry.uncovered_qty for entry in obligations), ZERO),
            obligations=obligations,
            excluded_future_supply=diagnostics,
        ))
    return LedgerProjection(generation_id=generation_id, rows=tuple(result))


def build_ledger_projection(
    db: Session,
    pairs: Iterable[LedgerProjectionKey | tuple[str, str]],
    planning_pool_by_warehouse: Mapping[str, str],
) -> LedgerProjection:
    """Accepted-truth wrapper around :func:`build_generation_projection`."""
    truth = require_accepted_truth(
        db,
        CONSUMER,
        required_capabilities=REQUIRED_CAPABILITIES,
    )
    return build_generation_projection(
        db,
        int(truth.generation_id),
        pairs,
        planning_pool_by_warehouse,
        "accepted",
    )
