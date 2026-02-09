from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ..models import WorkCalendarDay


def is_workday(db: Session, d: date) -> bool:
    """Глобальная проверка рабочего дня.

    Правила:
    - если есть запись в таблице work_calendar_day, она приоритетна;
    - иначе базово: Пн–Пт рабочие, Сб/Вс выходные.
    """
    if d is None:
        return False

    try:
        rec: Optional[WorkCalendarDay] = db.query(WorkCalendarDay).filter(WorkCalendarDay.date == d).first()
    except Exception:
        rec = None

    if rec is not None:
        return bool(getattr(rec, "is_workday", True))

    return d.weekday() < 5  # Mon-Fri


def previous_workday(db: Session, d: date) -> date:
    """Предыдущий рабочий день относительно d (не включая d)."""
    if d is None:
        raise ValueError("date is required")
    cur = d - timedelta(days=1)
    # Safety guard to prevent infinite loops in broken calendars
    for _ in range(366 * 5):
        if is_workday(db, cur):
            return cur
        cur -= timedelta(days=1)
    raise RuntimeError("previous_workday: calendar loop guard exceeded")


def next_workday(db: Session, d: date) -> date:
    """Следующий рабочий день относительно d (не включая d)."""
    if d is None:
        raise ValueError("date is required")
    cur = d + timedelta(days=1)
    for _ in range(366 * 5):
        if is_workday(db, cur):
            return cur
        cur += timedelta(days=1)
    raise RuntimeError("next_workday: calendar loop guard exceeded")

