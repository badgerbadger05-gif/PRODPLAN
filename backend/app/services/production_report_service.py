from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from ..models import (
    Item,
    RootProduct,
    ProductionPlanEntry,
    ProductionDayClose,
    ProductionDayCloseItem,
)
from .work_calendar_service import is_workday, next_workday, previous_workday


def _week_start_monday(d: date) -> date:
    # Monday = 0
    return d if d.weekday() == 0 else (d.fromordinal(d.toordinal() - d.weekday()))


def _week_dates(week_start: date) -> List[date]:
    return [date.fromordinal(week_start.toordinal() + i) for i in range(7)]


def _to_float(v: Any) -> float:
    try:
        return float(v or 0.0)
    except Exception:
        return 0.0


def _day_bounds(d: date) -> Tuple[datetime, datetime]:
    """[start, end) границы суток для DateTime-поля."""
    start = datetime.combine(d, datetime.min.time())
    end = start + timedelta(days=1)
    return start, end


@dataclass
class WeekReportDay:
    date: str
    is_workday: bool
    close_status: Optional[str] = None  # OPEN|CLOSED|None


def get_week_report(
    db: Session,
    week_start: Optional[date] = None,
    any_date_in_week: Optional[date] = None,
) -> Dict[str, Any]:
    """Недельный отчёт (Пн–Вс) по корневым изделиям.

    Возвращает:
    - days: [{date,is_workday,close_status}]
    - rows: изделия + plan/fact по дням + итоги
    - close_hint: {today, close_date, target_date} для UI
    """
    anchor = any_date_in_week or week_start or date.today()
    ws = week_start or _week_start_monday(anchor)
    dates = _week_dates(ws)
    d0, d6 = dates[0], dates[-1]

    # Close statuses for days in the week
    close_rows = (
        db.query(ProductionDayClose.close_date, ProductionDayClose.status)
        .filter(and_(ProductionDayClose.close_date >= d0, ProductionDayClose.close_date <= d6))
        .all()
    )
    close_map: Dict[date, str] = {}
    for cd, st in close_rows:
        try:
            close_map[cd] = str(st)
        except Exception:
            continue

    days_out: List[Dict[str, Any]] = []
    for d in dates:
        days_out.append(
            {
                "date": d.isoformat(),
                "is_workday": bool(is_workday(db, d)),
                "close_status": close_map.get(d),
            }
        )

    # Data by day for all root products (to keep consistent with existing plan matrix)
    root_items = (
        db.query(Item.item_id, Item.item_code, Item.item_name, Item.item_article)
        .join(RootProduct, Item.item_id == RootProduct.item_id)
        .order_by(Item.item_name)
        .all()
    )
    item_ids = [int(r.item_id) for r in root_items]

    plan_map: Dict[int, Dict[str, float]] = {iid: {} for iid in item_ids}
    fact_map: Dict[int, Dict[str, float]] = {iid: {} for iid in item_ids}

    if item_ids:
        start_dt = datetime.combine(d0, datetime.min.time())
        end_dt = datetime.combine(d6 + timedelta(days=1), datetime.min.time())
        rows = (
            db.query(
                ProductionPlanEntry.item_id.label("item_id"),
                func.date(ProductionPlanEntry.date).label("d"),
                func.coalesce(func.sum(ProductionPlanEntry.planned_qty), 0.0).label("plan"),
                func.coalesce(func.sum(ProductionPlanEntry.completed_qty), 0.0).label("fact"),
            )
            .filter(ProductionPlanEntry.item_id.in_(item_ids))
            .filter(ProductionPlanEntry.date >= start_dt)
            .filter(ProductionPlanEntry.date < end_dt)
            .group_by(ProductionPlanEntry.item_id, func.date(ProductionPlanEntry.date))
            .all()
        )
        for r in rows:
            try:
                iid = int(r.item_id)
                ds = r.d.isoformat() if hasattr(r.d, "isoformat") else str(r.d)
                plan_map[iid][ds] = _to_float(r.plan)
                fact_map[iid][ds] = _to_float(r.fact)
            except Exception:
                continue

    rows_out: List[Dict[str, Any]] = []
    for it in root_items:
        iid = int(it.item_id)
        plan_by_day: Dict[str, float] = {d.isoformat(): _to_float(plan_map.get(iid, {}).get(d.isoformat(), 0.0)) for d in dates}
        fact_by_day: Dict[str, float] = {d.isoformat(): _to_float(fact_map.get(iid, {}).get(d.isoformat(), 0.0)) for d in dates}

        plan_week = sum(plan_by_day.values())
        fact_week = sum(fact_by_day.values())
        remaining_week = plan_week - fact_week

        rows_out.append(
            {
                "item_id": iid,
                "item_code": str(it.item_code),
                "item_name": str(it.item_name),
                "item_article": str(it.item_article) if it.item_article else None,
                "plan_by_day": plan_by_day,
                "fact_by_day": fact_by_day,
                "plan_week": plan_week,
                "fact_week": fact_week,
                "remaining_week": remaining_week,
            }
        )

    today = date.today()
    close_date = previous_workday(db, today)
    target_date = next_workday(db, next_workday(db, today))

    return {
        "week_start": ws.isoformat(),
        "days": days_out,
        "rows": rows_out,
        "close_hint": {
            "today": today.isoformat(),
            "close_date": close_date.isoformat(),
            "target_date": target_date.isoformat(),
        },
    }


def bulk_upsert_fact(
    db: Session,
    entries: List[Dict[str, Any]],
) -> int:
    """Bulk upsert факта (completed_qty) по датам.

    Запрещает запись факта для закрытых дней.
    """
    if not entries:
        return 0

    normalized: List[Tuple[int, date, float]] = []
    for e in entries:
        try:
            iid = int(e.get("item_id"))
            d = date.fromisoformat(str(e.get("date")))
            q = _to_float(e.get("fact_qty"))
        except Exception:
            continue
        normalized.append((iid, d, q))

    if not normalized:
        return 0

    # Pre-check: closed days
    # NOTE on re-run: decisions require allowing re-run of the current close day.
    # To enable correcting fact before pressing "Close day" again, we allow fact edits
    # for D_close = previous_workday(today) even if it is already CLOSED.
    close_dates = sorted({d for _, d, _ in normalized})
    if close_dates:
        rerun_allowed_date: Optional[date] = None
        try:
            rerun_allowed_date = previous_workday(db, date.today())
        except Exception:
            rerun_allowed_date = None

        closed = (
            db.query(ProductionDayClose.close_date)
            .filter(ProductionDayClose.close_date.in_(close_dates))
            .filter(ProductionDayClose.status == "CLOSED")
            .all()
        )
        closed_set = {cd for (cd,) in closed}
        if closed_set:
            if rerun_allowed_date is not None:
                closed_set = {d for d in closed_set if d != rerun_allowed_date}

        if closed_set:
            bad = sorted({d.isoformat() for d in closed_set})
            raise ValueError(f"fact is read-only for closed day(s): {', '.join(bad)}")

    saved = 0
    for iid, d, q in normalized:
        start_dt, end_dt = _day_bounds(d)
        # Update first
        updated = (
            db.query(ProductionPlanEntry)
            .filter(and_(ProductionPlanEntry.item_id == iid, ProductionPlanEntry.date >= start_dt, ProductionPlanEntry.date < end_dt))
            .update({"completed_qty": q})
        )
        if updated == 0:
            db.add(
                ProductionPlanEntry(
                    item_id=iid,
                    stage_id=None,
                    date=datetime.combine(d, datetime.min.time()),
                    planned_qty=0.0,
                    completed_qty=q,
                    status="GREEN",
                    notes=None,
                )
            )
        saved += 1

    return saved


def close_previous_workday(
    db: Session,
    closed_by: Optional[str] = None,
    today_override: Optional[date] = None,
) -> Dict[str, Any]:
    """Закрыть день по правилам решений (previous_workday(today)).

    Поддерживает re-run: откатывает предыдущий перенос и применяет новый.
    """
    from datetime import datetime

    today = today_override or date.today()
    d_close = previous_workday(db, today)
    d_target = next_workday(db, next_workday(db, today))

    # Guard: no skipping workdays once the process has started.
    # If there is a last closed day before d_close, then the next workday after it must be exactly d_close.
    last_closed: Optional[date] = (
        db.query(func.max(ProductionDayClose.close_date))
        .filter(ProductionDayClose.status == "CLOSED")
        .filter(ProductionDayClose.close_date < d_close)
        .scalar()
    )
    if last_closed is not None:
        expected = next_workday(db, last_closed)
        if expected != d_close:
            raise ValueError(
                f"cannot close {d_close.isoformat()} because previous workday {expected.isoformat()} is not closed"
            )

    # Find/create day close record
    day_close = db.query(ProductionDayClose).filter(ProductionDayClose.close_date == d_close).first()
    if day_close is None:
        day_close = ProductionDayClose(close_date=d_close, status="OPEN")
        db.add(day_close)
        db.flush()

    # Re-run rollback
    existing_items = db.query(ProductionDayCloseItem).filter(ProductionDayCloseItem.day_close_id == day_close.id).all()
    if existing_items:
        # Rollback previous carry on their applied_to_date
        for it in existing_items:
            try:
                carry = _to_float(it.carry_qty)
                applied = getattr(it, "applied_to_date", None)
                if carry <= 1e-9 or applied is None:
                    continue
                # subtract from plan on that date
                a0, a1 = _day_bounds(applied)
                pe = (
                    db.query(ProductionPlanEntry)
                    .filter(and_(ProductionPlanEntry.item_id == int(it.item_id), ProductionPlanEntry.date >= a0, ProductionPlanEntry.date < a1))
                    .first()
                )
                if pe is None:
                    continue
                new_val = _to_float(pe.planned_qty) - carry
                if new_val < 0:
                    new_val = 0.0
                pe.planned_qty = new_val
            except Exception:
                continue

        # Delete snapshot rows
        db.query(ProductionDayCloseItem).filter(ProductionDayCloseItem.day_close_id == day_close.id).delete()

    # Build item set for d_close (plan > 0 or fact > 0)
    c0, c1 = _day_bounds(d_close)
    rows = (
        db.query(
            ProductionPlanEntry.item_id.label("item_id"),
            func.coalesce(func.sum(ProductionPlanEntry.planned_qty), 0.0).label("plan"),
            func.coalesce(func.sum(ProductionPlanEntry.completed_qty), 0.0).label("fact"),
        )
        .filter(ProductionPlanEntry.date >= c0)
        .filter(ProductionPlanEntry.date < c1)
        .group_by(ProductionPlanEntry.item_id)
        .having(
            (func.coalesce(func.sum(ProductionPlanEntry.planned_qty), 0.0) > 0)
            | (func.coalesce(func.sum(ProductionPlanEntry.completed_qty), 0.0) > 0)
        )
        .all()
    )

    applied_count = 0
    for r in rows:
        iid = int(r.item_id)
        plan = _to_float(r.plan)
        fact = _to_float(r.fact)
        carry = max(plan - fact, 0.0)

        applied_to: Optional[date] = None
        if carry > 1e-9:
            applied_to = d_target
            t0, t1 = _day_bounds(d_target)
            pe_t = (
                db.query(ProductionPlanEntry)
                .filter(and_(ProductionPlanEntry.item_id == iid, ProductionPlanEntry.date >= t0, ProductionPlanEntry.date < t1))
                .first()
            )
            if pe_t is None:
                db.add(
                    ProductionPlanEntry(
                        item_id=iid,
                        stage_id=None,
                        date=datetime.combine(d_target, datetime.min.time()),
                        planned_qty=carry,
                        completed_qty=0.0,
                        status="GREEN",
                        notes=None,
                    )
                )
            else:
                pe_t.planned_qty = _to_float(pe_t.planned_qty) + carry
            applied_count += 1

        db.add(
            ProductionDayCloseItem(
                day_close_id=day_close.id,
                item_id=iid,
                planned_qty_snapshot=plan,
                fact_qty_snapshot=fact,
                carry_qty=carry,
                applied_to_date=applied_to,
            )
        )

    day_close.status = "CLOSED"
    day_close.target_date = d_target
    day_close.closed_at = datetime.utcnow()
    day_close.closed_by = closed_by

    return {
        "status": "ok",
        "close_date": d_close.isoformat(),
        "target_date": d_target.isoformat(),
        "items": int(len(rows)),
        "carried": int(applied_count),
    }

