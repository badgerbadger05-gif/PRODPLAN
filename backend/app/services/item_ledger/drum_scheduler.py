"""Pure deterministic canonical drum scheduler.

Inputs are canonical queue rows + assembly rate metadata + calendar workday map.
The result is a deterministic split of queue demand into dated slots, plus
explicit gaps when the configured horizon cannot absorb all demand.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN
from hashlib import sha256
import json
from typing import Any


_QTY_QUANTUM = Decimal("0.001")


def _dec(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class QueueLine:
    queue_line_id: int
    plan_id: int
    plan_line_id: int
    item_id: int
    sort_key: str
    planned_output_qty: Decimal
    accepted_plan_output_qty: Decimal
    original_priority: tuple[Any, ...]


@dataclass(frozen=True)
class AssemblyRateProfile:
    resource_id: int
    qty_per_capacity: Decimal


@dataclass(frozen=True)
class PlannedSlot:
    queue_line_id: int
    plan_id: int
    plan_line_id: int
    item_id: int
    resource_id: int
    slot_date: date
    slot_qty: Decimal
    planned_output_qty: Decimal
    slot_ordinal: int
    original_priority: tuple[Any, ...]


@dataclass(frozen=True)
class CapacityGap:
    queue_line_id: int
    plan_id: int
    plan_line_id: int
    item_id: int
    resource_id: int
    gap_date: date
    required_qty: Decimal
    available_capacity: Decimal
    gap_qty: Decimal
    original_priority: tuple[Any, ...]


@dataclass(frozen=True)
class DrumSchedulePlan:
    schedule_from: date
    schedule_to: date
    slots: tuple[PlannedSlot, ...]
    gaps: tuple[CapacityGap, ...]
    queue_signature: str
    slot_signature: str
    gap_signature: str
    metrics: dict[str, Any]


def _workday_flag(calendar_by_date: dict[date, bool], candidate: date) -> bool:
    if candidate in calendar_by_date:
        return bool(calendar_by_date[candidate])
    return candidate.weekday() < 5


def _signature(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        rows,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _normalize_queue_signature(lines: list[QueueLine]) -> str:
    rows: list[dict[str, Any]] = []
    for line in lines:
        rows.append(
            {
                "queue_line_id": int(line.queue_line_id),
                "plan_id": int(line.plan_id),
                "plan_line_id": int(line.plan_line_id),
                "item_id": int(line.item_id),
                "sort_key": str(line.sort_key),
                "planned_output_qty": str(_dec(line.planned_output_qty).normalize()),
                "accepted_plan_output_qty": str(_dec(line.accepted_plan_output_qty).normalize()),
                "original_priority": tuple(line.original_priority),
            }
        )
    return sha256(repr(rows).encode("utf-8")).hexdigest()


def _open_qty(line: QueueLine) -> Decimal:
    return max(_dec(line.planned_output_qty) - _dec(line.accepted_plan_output_qty), Decimal("0"))


def build_drum_plan(
    queue_lines: tuple[QueueLine, ...],
    rates_by_item: dict[int, tuple[AssemblyRateProfile, ...]],
    calendar_by_date: dict[date, bool] | None,
    *,
    schedule_from: date,
    schedule_to: date,
    resource_capacity_by_id: dict[int, Decimal],
    resource_horizon_end_by_id: dict[int, date] | None = None,
) -> DrumSchedulePlan:
    if schedule_to < schedule_from:
        raise ValueError("schedule_to must be >= schedule_from")

    calendar = calendar_by_date or {}
    horizon_end = dict(resource_horizon_end_by_id or {})
    ordered = sorted(
        tuple(queue_lines),
        key=lambda row: (str(row.sort_key), int(row.queue_line_id)),
    )

    if not ordered:
        return DrumSchedulePlan(
            schedule_from=schedule_from,
            schedule_to=schedule_to,
            slots=(),
            gaps=(),
            queue_signature="",
            slot_signature="",
            gap_signature="",
            metrics={
                "lines": 0,
                "slots": 0,
                "gaps": 0,
                "total_open_qty": "0",
                "total_slot_qty": "0",
                "total_gap_qty": "0",
            },
        )

    # Consumption is booked in *capacity units*, never in SKU units. Two SKUs
    # sharing one resource have different takts, so a slot of 30 units of a
    # 10-per-capacity SKU must charge the resource 3 capacity units, not 30.
    used_capacity: dict[tuple[int, date], Decimal] = {}
    output_slots: list[PlannedSlot] = []
    output_gaps: list[CapacityGap] = []

    for queue_line in ordered:
        open_qty = _open_qty(queue_line)
        if open_qty <= 0:
            continue

        profiles = rates_by_item.get(int(queue_line.item_id))
        if not profiles:
            raise ValueError(f"missing assembly rate for item {int(queue_line.item_id)}")
        if len(profiles) != 1:
            raise ValueError(f"ambiguous assembly rates for item {int(queue_line.item_id)}")

        profile = profiles[0]
        resource_id = int(profile.resource_id)
        resource_capacity = _dec(resource_capacity_by_id.get(resource_id, Decimal("0")))
        rate = _dec(profile.qty_per_capacity)
        if rate <= 0:
            raise ValueError(f"invalid takt for item {int(queue_line.item_id)}")
        if resource_capacity * rate <= 0:
            raise ValueError(f"non-positive capacity for resource {resource_id}")

        resource_last_day = min(horizon_end.get(resource_id, schedule_to), schedule_to)
        if resource_last_day < schedule_from:
            resource_last_day = schedule_from

        remaining = open_qty
        current = schedule_from
        slot_ordinal = 0

        while remaining > 0 and current <= resource_last_day:
            if _workday_flag(calendar, current):
                used = _dec(used_capacity.get((resource_id, current), Decimal("0")))
                free_capacity = max(resource_capacity - used, Decimal("0"))
                available = free_capacity * rate
                if available > 0:
                    # Persisted drum quantities have scale 3.  Repeated
                    # ``take / rate`` operations can leave a positive Decimal
                    # residue such as 2.25E-27 after the day is physically
                    # exhausted.  It must not become a slot that the database
                    # rounds to 0.000; floor the candidate to the canonical
                    # quantity quantum and leave any unusable residue for a
                    # later day or an explicit capacity gap.
                    take = min(remaining, available).quantize(
                        _QTY_QUANTUM,
                        rounding=ROUND_DOWN,
                    )
                    if take > 0:
                        output_slots.append(
                            PlannedSlot(
                                queue_line_id=int(queue_line.queue_line_id),
                                plan_id=int(queue_line.plan_id),
                                plan_line_id=int(queue_line.plan_line_id),
                                item_id=int(queue_line.item_id),
                                resource_id=resource_id,
                                slot_date=current,
                                slot_qty=take,
                                planned_output_qty=_dec(queue_line.planned_output_qty),
                                slot_ordinal=slot_ordinal,
                                original_priority=tuple(queue_line.original_priority),
                            )
                        )
                        # Exhausting the day is booked exactly, so repeated
                        # non-terminating divisions can never drift the day
                        # above or below its configured capacity.
                        if take >= available:
                            used_capacity[(resource_id, current)] = resource_capacity
                        else:
                            used_capacity[(resource_id, current)] = used + (take / rate)
                        remaining -= take
                        slot_ordinal += 1
            current += timedelta(days=1)

        if remaining > 0:
            available_last = Decimal("0")
            if _workday_flag(calendar, resource_last_day):
                used = _dec(
                    used_capacity.get((resource_id, resource_last_day), Decimal("0"))
                )
                available_last = max(resource_capacity - used, Decimal("0")) * rate

            output_gaps.append(
                CapacityGap(
                    queue_line_id=int(queue_line.queue_line_id),
                    plan_id=int(queue_line.plan_id),
                    plan_line_id=int(queue_line.plan_line_id),
                    item_id=int(queue_line.item_id),
                    resource_id=resource_id,
                    gap_date=resource_last_day,
                    required_qty=remaining,
                    available_capacity=available_last,
                    gap_qty=remaining,
                    original_priority=tuple(queue_line.original_priority),
                )
            )

    total_open = Decimal("0")
    total_slots = Decimal("0")
    total_gaps = Decimal("0")
    for line in ordered:
        row_open = _open_qty(line)
        if row_open <= 0:
            continue
        row_slot = sum((slot.slot_qty for slot in output_slots if slot.queue_line_id == int(line.queue_line_id)), Decimal("0"))
        row_gap = sum((gap.gap_qty for gap in output_gaps if gap.queue_line_id == int(line.queue_line_id)), Decimal("0"))
        if row_slot + row_gap != row_open:
            raise ValueError(
                "drum scheduler conservation failed for queue line "
                f"{int(line.queue_line_id)}"
            )
        total_open += row_open
        total_slots += row_slot
        total_gaps += row_gap

    queue_signature_payload = sorted(
        [
            {
                "line": int(row.queue_line_id),
                "item_id": int(row.item_id),
                "open_qty": str(_open_qty(row).normalize()),
            }
            for row in ordered
            if _open_qty(row) > 0
        ],
        key=lambda row: row["line"],
    )
    slot_signature_payload = sorted(
        [
            {
                "line": int(slot.queue_line_id),
                "date": slot.slot_date.isoformat(),
                "resource": int(slot.resource_id),
                "qty": str(_dec(slot.slot_qty).normalize()),
            }
            for slot in output_slots
        ],
        key=lambda row: (row["line"], row["date"], row["resource"]),
    )
    gap_signature_payload = sorted(
        [
            {
                "line": int(gap.queue_line_id),
                "date": gap.gap_date.isoformat(),
                "resource": int(gap.resource_id),
                "qty": str(_dec(gap.gap_qty).normalize()),
            }
            for gap in output_gaps
        ],
        key=lambda row: (row["line"], row["date"], row["resource"]),
    )

    return DrumSchedulePlan(
        schedule_from=schedule_from,
        schedule_to=schedule_to,
        slots=tuple(output_slots),
        gaps=tuple(output_gaps),
        queue_signature=_signature(queue_signature_payload),
        slot_signature=_signature(slot_signature_payload),
        gap_signature=_signature(gap_signature_payload),
        metrics={
            "total_lines": len(ordered),
            "queue_rows": len([line for line in ordered if _open_qty(line) > 0]),
            "slots": len(output_slots),
            "gaps": len(output_gaps),
            "total_open_qty": str(total_open),
            "total_slot_qty": str(total_slots),
            "total_gap_qty": str(total_gaps),
            "lines_total": len(ordered),
            "schedule_days": (schedule_to - schedule_from).days + 1,
        },
    )
