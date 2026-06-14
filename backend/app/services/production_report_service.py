from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
import re

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

    Примечание:
    - Функция использует глобальный календарь рабочих дней через [`next_workday()`](backend/app/services/work_calendar_service.py:45)
      и [`previous_workday()`](backend/app/services/work_calendar_service.py:32).
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


def _strip_carry_notes(notes: Optional[str]) -> Optional[str]:
    """Remove previous carry fragments from notes, preserving other text."""
    if not notes:
        return None
    try:
        cleaned = re.sub(r"(^|;\s*)Carry[^;]*", "", str(notes), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*;\s*", "; ", cleaned).strip(" ;")
        return cleaned or None
    except Exception:
        return notes


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
    - days: [{date,is_workday,close_status, closed_planned, closed_fact}]
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

    # Get closed planned and fact quantities for each day
    closed_data_rows = (
        db.query(
            ProductionDayCloseItem,
            ProductionDayClose.close_date
        )
        .join(ProductionDayClose, ProductionDayCloseItem.day_close_id == ProductionDayClose.id)
        .filter(ProductionDayClose.close_date >= d0, ProductionDayClose.close_date <= d6)
        .all()
    )

    # Effective carry that is currently still present in plan on applied_to_date.
    # We keep history rows intact, but UI must show only "alive" carry in current plan state.
    carry_keys: List[Tuple[int, date]] = []
    for item_rec, _close_date in closed_data_rows:
        try:
            ii = int(item_rec.item_id)
            ad = getattr(item_rec, "applied_to_date", None)
            if ad is not None:
                carry_keys.append((ii, ad))
        except Exception:
            continue

    current_plan_on_target: Dict[Tuple[int, str], float] = {}
    if carry_keys:
        try:
            item_ids_for_carry = sorted({int(i) for i, _ in carry_keys})
            target_dates = [d for _, d in carry_keys]
            min_dt = min(target_dates)
            max_dt = max(target_dates) + timedelta(days=1)
            rows_plan = (
                db.query(
                    ProductionPlanEntry.item_id.label("item_id"),
                    func.date(ProductionPlanEntry.date).label("d"),
                    func.coalesce(func.sum(ProductionPlanEntry.planned_qty), 0.0).label("plan"),
                )
                .filter(ProductionPlanEntry.item_id.in_(item_ids_for_carry))
                .filter(ProductionPlanEntry.date >= datetime.combine(min_dt, datetime.min.time()))
                .filter(ProductionPlanEntry.date < datetime.combine(max_dt, datetime.min.time()))
                .group_by(ProductionPlanEntry.item_id, func.date(ProductionPlanEntry.date))
                .all()
            )
            for iid, dval, p in rows_plan:
                try:
                    ds = dval.isoformat() if hasattr(dval, "isoformat") else str(dval)
                    current_plan_on_target[(int(iid), ds)] = _to_float(p)
                except Exception:
                    continue
        except Exception:
            current_plan_on_target = {}

    def _effective_carry_now(item_rec: ProductionDayCloseItem) -> float:
        carry = _to_float(getattr(item_rec, "carry_qty", 0.0))
        if abs(carry) <= 1e-9:
            return 0.0
        applied = getattr(item_rec, "applied_to_date", None)
        before_raw = getattr(item_rec, "original_planned_qty_before_carry", None)
        # Legacy rows: keep historical carry as fallback
        if applied is None or before_raw is None:
            return carry
        try:
            key = (int(item_rec.item_id), applied.isoformat())
            current_plan = _to_float(current_plan_on_target.get(key, 0.0))
            before = _to_float(before_raw)
            delta = current_plan - before
            if carry > 0:
                return min(max(delta, 0.0), carry)
            return max(min(delta, 0.0), carry)
        except Exception:
            return carry
    
    # Map closed data by date
    closed_data_map: Dict[date, Dict[str, Any]] = {}
    for item_rec, close_date in closed_data_rows:
        eff_carry = _effective_carry_now(item_rec)
        if close_date not in closed_data_map:
            closed_data_map[close_date] = {
                'closed_planned': _to_float(item_rec.planned_qty_snapshot),
                'closed_fact': _to_float(item_rec.fact_qty_snapshot),
                'carry_qty': eff_carry,
            }
        else:
            # Sum up values for multiple items closed on the same day
            closed_data_map[close_date]['closed_planned'] += _to_float(item_rec.planned_qty_snapshot)
            closed_data_map[close_date]['closed_fact'] += _to_float(item_rec.fact_qty_snapshot)
            closed_data_map[close_date]['carry_qty'] += eff_carry

    days_out: List[Dict[str, Any]] = []
    for d in dates:
        day_info = {
            "date": d.isoformat(),
            "is_workday": bool(is_workday(db, d)),
            "close_status": close_map.get(d),
        }
        
        # Add closed data if available for this date
        if d in closed_data_map:
            day_info.update({
                "closed_planned": closed_data_map[d]["closed_planned"],
                "closed_fact": closed_data_map[d]["closed_fact"],
                "carry_qty": closed_data_map[d]["carry_qty"],
            })
        
        days_out.append(day_info)

    # Data by day for all root products (to keep consistent with existing plan matrix)
    root_items = (
        db.query(Item.item_id, Item.item_code, Item.item_name, Item.item_article)
        .join(RootProduct, Item.item_id == RootProduct.item_id)
        .order_by(Item.item_name)
        .all()
    )
    item_ids = [int(r.item_id) for r in root_items]

    # Closed snapshots/carries per item per close_date (for diagnostics UI)
    carry_by_item: Dict[int, Dict[str, float]] = {iid: {} for iid in item_ids}
    closed_plan_by_item: Dict[int, Dict[str, float]] = {iid: {} for iid in item_ids}
    closed_fact_by_item: Dict[int, Dict[str, float]] = {iid: {} for iid in item_ids}
    if item_ids:
        try:
            close_item_rows = (
                db.query(
                    ProductionDayCloseItem.item_id,
                    ProductionDayClose.close_date,
                    ProductionDayCloseItem.planned_qty_snapshot,
                    ProductionDayCloseItem.fact_qty_snapshot,
                    ProductionDayCloseItem.carry_qty,
                    ProductionDayCloseItem.applied_to_date,
                    ProductionDayCloseItem.original_planned_qty_before_carry,
                )
                .join(ProductionDayClose, ProductionDayCloseItem.day_close_id == ProductionDayClose.id)
                .filter(ProductionDayClose.close_date >= d0)
                .filter(ProductionDayClose.close_date <= d6)
                .filter(ProductionDayCloseItem.item_id.in_(item_ids))
                .all()
            )
            for iid, cd, p, f, c, applied_to, before_raw in close_item_rows:
                try:
                    ii = int(iid)
                    ds = cd.isoformat() if hasattr(cd, "isoformat") else str(cd)
                    eff_carry = _to_float(c)
                    if abs(eff_carry) > 1e-9 and applied_to is not None and before_raw is not None:
                        key = (ii, applied_to.isoformat())
                        current_plan = _to_float(current_plan_on_target.get(key, 0.0))
                        before = _to_float(before_raw)
                        delta = current_plan - before
                        if eff_carry > 0:
                            eff_carry = min(max(delta, 0.0), eff_carry)
                        else:
                            eff_carry = max(min(delta, 0.0), eff_carry)
                    carry_by_item.setdefault(ii, {})[ds] = eff_carry
                    closed_plan_by_item.setdefault(ii, {})[ds] = _to_float(p)
                    closed_fact_by_item.setdefault(ii, {})[ds] = _to_float(f)
                except Exception:
                    continue
        except Exception:
            # best-effort only
            pass

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
                # Diagnostics for day close
                "carry_by_day": carry_by_item.get(iid, {}),
                "closed_plan_by_day": closed_plan_by_item.get(iid, {}),
                "closed_fact_by_day": closed_fact_by_item.get(iid, {}),
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
    rerun_editable_date: Optional[date] = None,
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
    # NOTE on re-run: allow fact edits for one explicitly selected close day
    # even if it is already CLOSED.
    close_dates = sorted({d for _, d, _ in normalized})
    if close_dates:
        rerun_allowed_date: Optional[date] = rerun_editable_date

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
    close_date_override: Optional[date] = None,
) -> Dict[str, Any]:
    """Закрыть день по правилам решений (previous_workday(today)).

    Поддерживает re-run: откатывает предыдущий перенос и применяет новый.
    """
    from datetime import datetime

    today = today_override or date.today()
    default_close_date = previous_workday(db, today)
    d_close = close_date_override or default_close_date

    if not is_workday(db, d_close):
        raise ValueError(f"cannot close non-workday: {d_close.isoformat()}")

    # Safety: only historical/current close dates are allowed.
    # "Current" means the default close date for today (previous workday).
    if d_close > default_close_date:
        raise ValueError(
            f"cannot close {d_close.isoformat()} because max allowed close date is {default_close_date.isoformat()}"
        )

    # Keep existing business semantics for carry target:
    # target is calculated relative to "today" (through one workday),
    # regardless of whether close date was selected explicitly.
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
                if abs(carry) <= 1e-9 or applied is None:
                    continue
                # Delta rollback: remove exactly this carry contribution from current plan.
                # This avoids overwriting carries from other close days that target the same date.
                a0, a1 = _day_bounds(applied)
                pe = (
                    db.query(ProductionPlanEntry)
                    .filter(and_(ProductionPlanEntry.item_id == int(it.item_id), ProductionPlanEntry.date >= a0, ProductionPlanEntry.date < a1))
                    .first()
                )
                if pe is None:
                    continue
                pe.planned_qty = max(_to_float(pe.planned_qty) - carry, 0.0)
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

    # --- Compute carry with weekly netting semantics ---
    # Problem: if production happens on other days of the week (plan=0) it should reduce the remaining
    # quantity of the planned day. Otherwise carry becomes overstated (e.g. plan 30 on Fri, fact 10 Thu,
    # fact 5 Fri => naive carry 25 but real week remainder may be 15, or even less if weekend fact exists).
    #
    # We net facts within the week against plans up to D_close (FIFO by date inside the week).
    # Carry for D_close is the incremental backlog attributable to D_close,
    # with support of negative credit on over-fulfillment:
    #   carry = (P_through_close - F_week) - max(P_before_close - F_week, 0)
    # where:
    #   P_before_close  = sum(plan_qty in [week_start .. D_close-1])
    #   P_through_close = sum(plan_qty in [week_start .. D_close])
    #   F_week          = sum(fact_qty in [week_start .. min(today, week_end)])
    # This allows weekend facts (when today is Monday and D_close is previous Friday)
    # to reduce carry and potentially produce negative credit.
    week_start = _week_start_monday(d_close)
    week_end = week_start + timedelta(days=6)
    fact_end = week_end if today > week_end else today

    item_ids = [int(r.item_id) for r in rows]

    plan_before_map: Dict[int, float] = {}
    plan_through_map: Dict[int, float] = {}
    fact_week_map: Dict[int, float] = {}

    if item_ids:
        ws_dt = datetime.combine(week_start, datetime.min.time())
        # Plans before close day (exclude D_close)
        plan_before_rows = (
            db.query(
                ProductionPlanEntry.item_id.label("item_id"),
                func.coalesce(func.sum(ProductionPlanEntry.planned_qty), 0.0).label("plan"),
            )
            .filter(ProductionPlanEntry.item_id.in_(item_ids))
            .filter(ProductionPlanEntry.date >= ws_dt)
            .filter(ProductionPlanEntry.date < c0)
            .group_by(ProductionPlanEntry.item_id)
            .all()
        )
        plan_before_map = {int(iid): _to_float(p) for iid, p in plan_before_rows}

        # Plans through close day (include D_close)
        plan_through_rows = (
            db.query(
                ProductionPlanEntry.item_id.label("item_id"),
                func.coalesce(func.sum(ProductionPlanEntry.planned_qty), 0.0).label("plan"),
            )
            .filter(ProductionPlanEntry.item_id.in_(item_ids))
            .filter(ProductionPlanEntry.date >= ws_dt)
            .filter(ProductionPlanEntry.date < c1)
            .group_by(ProductionPlanEntry.item_id)
            .all()
        )
        plan_through_map = {int(iid): _to_float(p) for iid, p in plan_through_rows}

        # Facts within week up to fact_end (inclusive)
        fact_end_dt = datetime.combine(fact_end + timedelta(days=1), datetime.min.time())
        fact_rows = (
            db.query(
                ProductionPlanEntry.item_id.label("item_id"),
                func.coalesce(func.sum(ProductionPlanEntry.completed_qty), 0.0).label("fact"),
            )
            .filter(ProductionPlanEntry.item_id.in_(item_ids))
            .filter(ProductionPlanEntry.date >= ws_dt)
            .filter(ProductionPlanEntry.date < fact_end_dt)
            .group_by(ProductionPlanEntry.item_id)
            .all()
        )
        fact_week_map = {int(iid): _to_float(f) for iid, f in fact_rows}

    applied_count = 0

    for r in rows:
        iid = int(r.item_id)
        plan = _to_float(r.plan)
        fact = _to_float(r.fact)

        # Weekly netted carry for the close day
        p_before = _to_float(plan_before_map.get(iid, 0.0))
        p_through = _to_float(plan_through_map.get(iid, 0.0))
        f_week = _to_float(fact_week_map.get(iid, 0.0))

        # Signed carry:
        #  > 0 => backlog to move forward
        #  < 0 => over-fulfillment credit to reduce future plan
        backlog_through = (p_through - f_week)
        backlog_before = max(p_before - f_week, 0.0)
        carry = backlog_through - backlog_before

        applied_to: Optional[date] = None
        original_planned_before_carry: Optional[float] = None
        planned_after_carry: Optional[float] = None
        carry_applied = False
        if abs(carry) > 1e-9:
            t0, t1 = _day_bounds(d_target)
            pe_t = (
                db.query(ProductionPlanEntry)
                .filter(and_(ProductionPlanEntry.item_id == iid, ProductionPlanEntry.date >= t0, ProductionPlanEntry.date < t1))
                .first()
            )
            if pe_t is None:
                if carry > 0:
                    applied_to = d_target
                    carry_applied = True
                    original_planned_before_carry = 0.0
                    planned_after_carry = float(carry)
                    db.add(
                        ProductionPlanEntry(
                            item_id=iid,
                            stage_id=None,
                            date=datetime.combine(d_target, datetime.min.time()),
                            planned_qty=carry,
                            completed_qty=0.0,
                            status="GREEN",
                            notes=f"Carry {carry:+g} from {d_close.isoformat()}",
                        )
                    )
                else:
                    # Do not create a negative target plan row when there is nothing to reduce.
                    original_planned_before_carry = 0.0
                    planned_after_carry = 0.0
            else:
                # Store original planned quantity before applying signed carry for accurate rollback
                original_planned_before_carry = _to_float(pe_t.planned_qty)
                planned_after_carry = max(float(original_planned_before_carry + carry), 0.0)
                pe_t.planned_qty = planned_after_carry
                # Mark as applied only when it changed target plan.
                carry_applied = abs(planned_after_carry - original_planned_before_carry) > 1e-9
                if carry_applied:
                    applied_to = d_target
                # Update notes to indicate signed carry application
                if carry_applied:
                    base_notes = _strip_carry_notes(pe_t.notes)
                    carry_note = f"Carry {carry:+g} from {d_close.isoformat()}"
                    pe_t.notes = f"{base_notes}; {carry_note}" if base_notes else carry_note
            if carry_applied:
                applied_count += 1

        db.add(
            ProductionDayCloseItem(
                day_close_id=day_close.id,
                item_id=iid,
                planned_qty_snapshot=plan,
                fact_qty_snapshot=fact,
                carry_qty=carry,
                applied_to_date=applied_to,
                original_planned_qty_before_carry=original_planned_before_carry,
                planned_qty_after_carry=planned_after_carry,
                carry_status="APPLIED" if carry_applied else "NONE",
            )
        )

    day_close.status = "CLOSED"
    day_close.target_date = d_target
    day_close.closed_at = datetime.now(timezone.utc)
    day_close.closed_by = closed_by

    return {
        "status": "ok",
        "close_date": d_close.isoformat(),
        "target_date": d_target.isoformat(),
        "items": int(len(rows)),
        "carried": int(applied_count),
    }

