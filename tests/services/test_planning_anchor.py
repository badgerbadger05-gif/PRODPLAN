from __future__ import annotations

from datetime import date

from backend.app.models import ProductionDayClose
from backend.app.services.work_calendar_service import get_planning_anchor_date


def test_anchor_without_any_closed_days_falls_back_to_previous_workday(db_session):
    db = db_session
    today = date(2026, 2, 5)  # Thu

    res = get_planning_anchor_date(db=db, today_override=today)
    # previous_workday(Thu) -> Wed
    assert res["anchor_date"] == date(2026, 2, 4).isoformat()
    assert res["last_closed_date"] is None


def test_anchor_is_next_workday_after_last_closed_day(db_session):
    db = db_session

    # last_closed = Tue -> anchor = Wed
    db.add(ProductionDayClose(close_date=date(2026, 2, 3), status="CLOSED"))
    db.commit()

    res = get_planning_anchor_date(db=db, today_override=date(2026, 2, 5))
    assert res["last_closed_date"] == date(2026, 2, 3).isoformat()
    assert res["anchor_date"] == date(2026, 2, 4).isoformat()


def test_anchor_skips_weekend_when_last_closed_is_friday(db_session):
    db = db_session

    # Fri -> next_workday should skip Sat/Sun and return Mon
    db.add(ProductionDayClose(close_date=date(2026, 2, 6), status="CLOSED"))
    db.commit()

    res = get_planning_anchor_date(db=db, today_override=date(2026, 2, 10))
    assert res["anchor_date"] == date(2026, 2, 9).isoformat()

