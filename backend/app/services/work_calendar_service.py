from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import ProductionDayClose, WorkCalendarDay


def is_workday(db: Session, d: date) -> bool:
    """Глобальная проверка рабочего дня.

    Правила:
    - суббота и воскресенье всегда выходные;
    - для Пн–Пт запись в work_calendar_day имеет приоритет;
    - иначе Пн–Пт рабочие.
    """
    if d is None:
        return False
    if d.weekday() >= 5:
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


def get_planning_anchor_date(
    db: Session,
    today_override: Optional[date] = None,
) -> Dict[str, Any]:
    """Определить якорную дату для отображения планового окна.

    Требование (UI): показывать план начиная с *первого не закрытого* рабочего дня.

    Формула:
    - last_closed = max(production_day_close.close_date where status='CLOSED')
    - anchor_date = next_workday(last_closed)
    - если last_closed отсутствует: anchor_date = previous_workday(today)
    """

    today = today_override or date.today()
    last_closed: Optional[date] = (
        db.query(func.max(ProductionDayClose.close_date))
        .filter(ProductionDayClose.status == "CLOSED")
        .scalar()
    )

    if last_closed is not None:
        anchor = next_workday(db, last_closed)
    else:
        # Если процесс закрытий ещё не начинали, якорим на дне, который должен закрываться сейчас
        # (предыдущий рабочий день относительно today).
        anchor = previous_workday(db, today)

    return {
        "today": today.isoformat(),
        "last_closed_date": last_closed.isoformat() if last_closed is not None else None,
        "anchor_date": anchor.isoformat(),
    }

