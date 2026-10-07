"""Canonical current physical and senior-hold folds for R6."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable


class CurrentPhysicalStateError(ValueError):
    """Input cannot be interpreted as canonical current physical state."""


@dataclass(frozen=True)
class CurrentPhysicalCell:
    on_hand: Decimal
    last_entry_id: int | None = None


PhysicalKey = tuple[int, str, str, str]
_ROLES = {"material_consumption", "replenishment_receipt"}


def _value(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _key(row: Any) -> PhysicalKey:
    try:
        item_id = int(_value(row, "item_id"))
    except (TypeError, ValueError) as exc:
        raise CurrentPhysicalStateError("physical row has no item identity") from exc
    return (
        item_id,
        str(_value(row, "characteristic_ref", "") or ""),
        str(_value(row, "organization_ref", "") or ""),
        str(_value(row, "warehouse_ref1c", "") or ""),
    )


def fold_current_stock(rows: Iterable[Any]) -> dict[PhysicalKey, CurrentPhysicalCell]:
    """Fold signed Ledger rows without clamping negative physical stock."""
    totals: dict[PhysicalKey, Decimal] = {}
    last_ids: dict[PhysicalKey, int | None] = {}
    for row in rows:
        key = _key(row)
        raw_qty = _value(row, "qty", _value(row, "on_hand", None))
        try:
            qty = raw_qty if isinstance(raw_qty, Decimal) else Decimal(str(raw_qty))
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise CurrentPhysicalStateError("physical row has malformed Decimal quantity") from exc
        if not qty.is_finite():
            raise CurrentPhysicalStateError("physical row quantity must be finite")
        totals[key] = totals.get(key, Decimal("0")) + qty
        raw_id = _value(row, "id", _value(row, "last_entry_id", None))
        if raw_id is not None:
            try:
                last_ids[key] = max(last_ids.get(key, int(raw_id)), int(raw_id))
            except (TypeError, ValueError) as exc:
                raise CurrentPhysicalStateError("physical row has malformed identity") from exc
    return {
        key: CurrentPhysicalCell(on_hand=value, last_entry_id=last_ids.get(key))
        for key, value in totals.items()
    }


def senior_hold_qty(reserved_qty: Any, allocations: Iterable[Any]) -> Decimal:
    """Remaining senior hold after assigned physical consumption only."""
    try:
        reserved = reserved_qty if isinstance(reserved_qty, Decimal) else Decimal(str(reserved_qty))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise CurrentPhysicalStateError("reserved quantity is not Decimal") from exc
    if not reserved.is_finite() or reserved < 0:
        raise CurrentPhysicalStateError("reserved quantity must be non-negative")
    consumed = Decimal("0")
    for allocation in allocations:
        role = str(_value(allocation, "allocation_role", "") or "")
        if role not in _ROLES:
            raise CurrentPhysicalStateError(f"unknown allocation role: {role!r}")
        if role != "material_consumption":
            continue
        try:
            quantity = _value(allocation, "allocated_qty", 0)
            quantity = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise CurrentPhysicalStateError("allocation quantity is not Decimal") from exc
        if not quantity.is_finite() or quantity < 0:
            raise CurrentPhysicalStateError("allocation quantity must be non-negative")
        consumed += quantity
    return max(reserved - consumed, Decimal("0"))


def unconsumed_receipt_qty(rows: Iterable[Any]) -> dict[int, Decimal]:
    """Surviving units of visible receipt lines in one planning stock pool.

    Signed physical movements deplete prior receipts FIFO, including opening
    stock and adjustments. Internal transport of a document has zero pool
    delta and does not spend the same units again. This is inventory evidence
    for pre-freeze facts, never a second calculation of planning demand.
    The caller supplies one exact batch/cutoff and selected warehouse pool.
    """
    from collections import defaultdict, deque
    from datetime import timezone
    from .document_net_output import net_document_output_qty

    rows = tuple(rows)
    output = net_document_output_qty(rows)
    groups = defaultdict(list)
    for row in rows:
        at = _value(row, "posting_at")
        if at is None:
            raise CurrentPhysicalStateError("physical movement has no posting time")
        at = at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)
        key = _key(row)[:3]
        document = (str(_value(row, "recorder_type", "") or ""),
                    str(_value(row, "recorder_ref", "") or f"__sle__:{row.id}"))
        groups[(key, at, document)].append(row)
    lots = defaultdict(deque)
    debt = defaultdict(lambda: Decimal("0"))
    for (key, _at, _document), movements in sorted(
        groups.items(), key=lambda entry: (entry[0][1], min(int(row.id) for row in entry[1]))
    ):
        delta = sum((Decimal(str(row.qty)) for row in movements), Decimal("0"))
        if delta < 0:
            left = -delta
            while left and lots[key]:
                lot = lots[key][0]
                take = min(left, lot[1])
                lot[1] -= take
                left -= take
                if not lot[1]:
                    lots[key].popleft()
            debt[key] += left
        elif delta > 0:
            # Canonical document output owns which assembly-in line represents
            # the net receipt; production-warehouse pass-through is not a lot.
            positive = sorted((row for row in movements if row.qty > 0),
                              key=lambda row: (not bool(output.get(int(row.id))), int(row.id)))
            left = delta
            for row in positive:
                capacity = output.get(int(row.id), Decimal("0")) if row.movement_kind == "assembly_in" else Decimal(str(row.qty))
                portion = min(left, capacity)
                if not portion:
                    continue
                offset = min(debt[key], portion)
                debt[key] -= offset
                if portion > offset:
                    lots[key].append([int(row.id), portion - offset])
                left -= portion
                if not left:
                    break
            if left:
                raise CurrentPhysicalStateError("positive physical delta has no qualified receipt line")
    result = {}
    for queue in lots.values():
        for sle_id, quantity in queue:
            result[sle_id] = result.get(sle_id, Decimal("0")) + quantity
    return result

