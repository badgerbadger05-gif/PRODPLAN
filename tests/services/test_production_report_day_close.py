from __future__ import annotations

from datetime import date, timedelta

import pytest

from sqlalchemy import and_

from backend.app.models import Item, RootProduct, ProductionPlanEntry, ProductionDayClose
from backend.app.services.production_report_service import close_previous_workday, bulk_upsert_fact


def _mk_root_item(db, code: str = "I1") -> int:
    it = Item(item_code=code, item_name=code, item_article=None, stock_qty=0.0, status="active")
    db.add(it)
    db.flush()
    db.add(RootProduct(item_id=int(it.item_id)))
    db.flush()
    return int(it.item_id)


def _get_plan(db, item_id: int, d: date) -> float:
    # ProductionPlanEntry.date is DateTime; compare by day bounds.
    from datetime import datetime

    start = datetime.combine(d, datetime.min.time())
    end = start + timedelta(days=1)
    row = (
        db.query(ProductionPlanEntry)
        .filter(
            and_(
                ProductionPlanEntry.item_id == item_id,
                ProductionPlanEntry.date >= start,
                ProductionPlanEntry.date < end,
            )
        )
        .first()
    )
    return float(getattr(row, "planned_qty", 0.0) or 0.0) if row else 0.0


def test_close_day_carry_to_target_skips_weekend(db_session):
    db = db_session
    iid = _mk_root_item(db, "A")

    # Choose today=Friday => D_close=Thursday, D_target=Tuesday (skip Sat/Sun)
    today = date(2026, 2, 6)  # Friday
    d_close = today - timedelta(days=1)  # Thu
    d_target = date(2026, 2, 10)  # Tue

    # Plan 10, fact 3 => carry 7
    db.add(ProductionPlanEntry(item_id=iid, stage_id=None, date=d_close, planned_qty=10.0, completed_qty=3.0))
    db.commit()

    res = close_previous_workday(db=db, closed_by="t", today_override=today)
    db.commit()

    assert res["close_date"] == d_close.isoformat()
    assert res["target_date"] == d_target.isoformat()
    assert abs(_get_plan(db, iid, d_target) - 7.0) < 1e-6


def test_close_day_no_negative_carry_on_overproduction(db_session):
    db = db_session
    iid = _mk_root_item(db, "B")
    today = date(2026, 2, 4)  # Wed
    d_close = date(2026, 2, 3)  # Tue
    d_target = date(2026, 2, 6)  # Fri (Thu is +1, Fri is +2)

    db.add(ProductionPlanEntry(item_id=iid, stage_id=None, date=d_close, planned_qty=5.0, completed_qty=8.0))
    db.commit()

    close_previous_workday(db=db, today_override=today)
    db.commit()

    # carry=0 => no plan added
    assert abs(_get_plan(db, iid, d_target) - 0.0) < 1e-6


def test_close_day_rerun_rolls_back_previous_carry(db_session):
    db = db_session
    iid = _mk_root_item(db, "C")
    today = date(2026, 2, 4)  # Wed
    d_close = date(2026, 2, 3)  # Tue
    d_target = date(2026, 2, 6)  # Fri

    # Initial: plan 10, fact 2 => carry 8
    db.add(ProductionPlanEntry(item_id=iid, stage_id=None, date=d_close, planned_qty=10.0, completed_qty=2.0))
    db.commit()

    close_previous_workday(db=db, today_override=today)
    db.commit()
    assert abs(_get_plan(db, iid, d_target) - 8.0) < 1e-6

    # Change fact => carry becomes 3
    from datetime import datetime

    c0 = datetime.combine(d_close, datetime.min.time())
    c1 = c0 + timedelta(days=1)
    (
        db.query(ProductionPlanEntry)
        .filter(
            and_(
                ProductionPlanEntry.item_id == iid,
                ProductionPlanEntry.date >= c0,
                ProductionPlanEntry.date < c1,
            )
        )
        .update({"completed_qty": 7.0})
    )
    db.commit()

    close_previous_workday(db=db, today_override=today)
    db.commit()

    # Should rollback 8 and apply 3 => final 3
    assert abs(_get_plan(db, iid, d_target) - 3.0) < 1e-6


def test_close_day_skip_guard_blocks_when_previous_not_closed(db_session):
    db = db_session
    _ = _mk_root_item(db, "D")

    # last_closed = Monday, today=Thursday => D_close=Wednesday.
    # Expected next_workday(last_closed)=Tuesday, but we try to close Wednesday => error.
    db.add(ProductionDayClose(close_date=date(2026, 2, 2), status="CLOSED"))  # Monday
    db.commit()

    with pytest.raises(ValueError):
        close_previous_workday(db=db, today_override=date(2026, 2, 5))  # Thursday


def test_fact_bulk_upsert_readonly_for_closed_day(db_session):
    db = db_session
    iid = _mk_root_item(db, "E")
    closed_day = date(2026, 2, 3)

    db.add(ProductionDayClose(close_date=closed_day, status="CLOSED"))
    db.commit()

    with pytest.raises(ValueError):
        bulk_upsert_fact(
            db=db,
            entries=[{"item_id": iid, "date": closed_day.isoformat(), "fact_qty": 1.0}],
        )

