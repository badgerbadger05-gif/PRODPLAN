"""Validated access to the immutable calendar saved with one drum snapshot."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any


class DrumSavedCalendarError(ValueError):
    """The persisted drum calendar is missing or internally inconsistent."""


def saved_working_days(schedule: Any) -> tuple[date, ...]:
    raw = schedule.working_days
    if not isinstance(raw, list):
        raise DrumSavedCalendarError("drum working_days must be a JSON list")
    try:
        parsed = tuple(date.fromisoformat(str(value)) for value in raw)
    except (TypeError, ValueError) as exc:
        raise DrumSavedCalendarError("drum working_days contains an invalid date") from exc
    if parsed != tuple(sorted(set(parsed))):
        raise DrumSavedCalendarError("drum working_days must be unique and ordered")
    if any(
        value < schedule.schedule_from or value > schedule.schedule_to
        for value in parsed
    ):
        raise DrumSavedCalendarError("drum working_days escapes the saved horizon")
    if any(value.weekday() >= 5 for value in parsed):
        raise DrumSavedCalendarError(
            "drum working_days cannot contain Saturday or Sunday"
        )
    return parsed


def saved_resource_horizon_ends(schedule: Any) -> dict[int, date]:
    raw = schedule.resource_horizon_ends
    if not isinstance(raw, dict):
        raise DrumSavedCalendarError(
            "drum resource_horizon_ends must be a JSON object"
        )
    result: dict[int, date] = {}
    try:
        for raw_resource_id, raw_end in raw.items():
            resource_id = int(raw_resource_id)
            end_date = date.fromisoformat(str(raw_end))
            if resource_id <= 0:
                raise ValueError("resource id must be positive")
            if not (schedule.schedule_from <= end_date <= schedule.schedule_to):
                raise ValueError("resource horizon escapes schedule")
            result[resource_id] = end_date
    except (TypeError, ValueError) as exc:
        raise DrumSavedCalendarError(
            "drum resource_horizon_ends contains an invalid value"
        ) from exc
    return result


def saved_resource_daily_capacities(schedule: Any) -> dict[int, Decimal]:
    raw = schedule.resource_daily_capacities
    if not isinstance(raw, dict):
        raise DrumSavedCalendarError(
            "drum resource_daily_capacities must be a JSON object"
        )
    result: dict[int, Decimal] = {}
    try:
        for raw_resource_id, raw_capacity in raw.items():
            resource_id = int(raw_resource_id)
            capacity = Decimal(str(raw_capacity))
            if resource_id <= 0:
                raise ValueError("resource id must be positive")
            if not capacity.is_finite() or capacity <= 0:
                raise ValueError("resource capacity must be positive and finite")
            result[resource_id] = capacity
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DrumSavedCalendarError(
            "drum resource_daily_capacities contains an invalid value"
        ) from exc
    return result
