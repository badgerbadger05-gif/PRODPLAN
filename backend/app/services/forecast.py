"""Canonical forecast shift classification shared by planning projections."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal


ForecastStatus = Literal["early", "on_time", "delayed", "critical", "unavailable"]
CRITICAL_FORECAST_SHIFT_DAYS = 5


def _date_iso(value: date | datetime | None) -> str | None:
    if value is None:
        return None
    return (value.date() if isinstance(value, datetime) else value).isoformat()


def forecast_status(shift_days: int | None) -> ForecastStatus:
    if shift_days is None:
        return "unavailable"
    if shift_days > CRITICAL_FORECAST_SHIFT_DAYS:
        return "critical"
    if shift_days > 0:
        return "delayed"
    if shift_days < 0:
        return "early"
    return "on_time"


def forecast_payload(
    forecast_date: date | datetime | None,
    due_date: date | datetime | None,
    *,
    reason: str = "capacity",
) -> dict[str, Any]:
    if forecast_date is None or due_date is None:
        return {
            "forecast_date": _date_iso(forecast_date),
            "forecast_shift_days": None,
            "forecast_reason": None,
            "forecast_status": "unavailable",
        }
    forecast_day = forecast_date.date() if isinstance(forecast_date, datetime) else forecast_date
    due_day = due_date.date() if isinstance(due_date, datetime) else due_date
    shift = (forecast_day - due_day).days
    reason_text = (
        "смещение по мощностям" if shift > 0 and reason == "capacity"
        else reason if shift > 0
        else "раньше плановой даты" if shift < 0
        else "в срок"
    )
    return {
        "forecast_date": forecast_day.isoformat(),
        "forecast_shift_days": shift,
        "forecast_reason": reason_text,
        "forecast_status": forecast_status(shift),
    }
