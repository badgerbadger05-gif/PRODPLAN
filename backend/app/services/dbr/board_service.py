"""DBR drum board — days × resources grid for the UI.

Read-only projection of the active schedule: slot tiles with gate colours,
capacity gaps and a KPI header (green/yellow/red/unknown counts, plan vs fact).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from sqlalchemy.orm import Session

from ...models import DbrDrumSchedule, Item
from . import adapters


def _active(db: Session) -> Optional[DbrDrumSchedule]:
    return db.query(DbrDrumSchedule).filter(DbrDrumSchedule.status == "active").first()


def _in_range(day: date, date_from: Optional[date], date_to: Optional[date]) -> bool:
    if date_from is not None and day < date_from:
        return False
    if date_to is not None and day > date_to:
        return False
    return True


def get_board(
    db: Session,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> dict[str, Any]:
    """Board payload for the active schedule (empty shell if none active)."""
    schedule = _active(db)
    if schedule is None:
        return {
            "schedule": None,
            "days": [],
            "resources": [],
            "slots": [],
            "gaps": [],
            "kpi": {
                "green": 0,
                "yellow": 0,
                "red": 0,
                "unknown": 0,
                "slots": 0,
                "plan_qty": 0.0,
                "fact_qty": 0.0,
            },
        }

    resource_names = adapters.resource_name_map(db)
    item_rows = {int(i.item_id): (i.item_code, i.item_name) for i in db.query(Item).all()}

    slots_out: list[dict[str, Any]] = []
    days: set[date] = set()
    resources: dict[int, str] = {}
    kpi = {"green": 0, "yellow": 0, "red": 0, "unknown": 0, "slots": 0, "plan_qty": 0.0, "fact_qty": 0.0}

    for slot in schedule.slots:
        if not _in_range(slot.slot_date, date_from, date_to):
            continue
        code, name = item_rows.get(int(slot.item_id), (None, None))
        rname = resource_names.get(int(slot.resource_id))
        days.add(slot.slot_date)
        resources[int(slot.resource_id)] = rname
        status = slot.kit_status or "unknown"
        kpi[status] = kpi.get(status, 0) + 1
        kpi["slots"] += 1
        kpi["plan_qty"] += float(slot.qty or 0)
        kpi["fact_qty"] += float(slot.produced_qty or 0)
        slots_out.append(
            {
                "id": slot.id,
                "date": slot.slot_date,
                "planned_date": slot.planned_date,
                "resource_id": slot.resource_id,
                "resource_name": rname,
                "item_id": slot.item_id,
                "item_code": code,
                "item_name": name,
                "qty": float(slot.qty or 0),
                "produced_qty": float(slot.produced_qty or 0),
                "kit_status": status,
                "release_status": slot.release_status,
                "shortage": slot.shortage_json,
                "position": slot.position,
            }
        )

    gaps_out: list[dict[str, Any]] = []
    for gap in schedule.capacity_gaps:
        if gap.gap_date is not None and not _in_range(gap.gap_date, date_from, date_to):
            continue
        code, name = item_rows.get(int(gap.item_id), (None, None)) if gap.item_id is not None else (None, None)
        gaps_out.append(
            {
                "id": gap.id,
                "date": gap.gap_date,
                "resource_id": gap.resource_id,
                "resource_name": resource_names.get(int(gap.resource_id)) if gap.resource_id is not None else None,
                "item_id": gap.item_id,
                "item_code": code,
                "item_name": name,
                "required_qty": float(gap.required_qty or 0),
                "takt_qty": float(gap.takt_qty or 0),
                "gap_qty": float(gap.gap_qty or 0),
                "resolution": gap.resolution,
            }
        )

    slots_out.sort(key=lambda s: (s["date"], s["resource_name"] or "", s["position"]))
    return {
        "schedule": {
            "id": schedule.id,
            "period_from": schedule.period_from,
            "period_to": schedule.period_to,
            "status": schedule.status,
            "source_program_id": schedule.source_program_id,
        },
        "days": sorted(days),
        "resources": [{"id": rid, "name": resources[rid]} for rid in sorted(resources, key=lambda r: resources[r] or "")],
        "slots": slots_out,
        "gaps": gaps_out,
        "kpi": kpi,
    }
