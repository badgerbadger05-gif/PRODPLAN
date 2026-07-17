"""
DBR (drum-buffer-rope) parallel planning module — settings API.

Endpoints:
  GET  /api/v1/dbr/settings          — read singleton settings (created lazily)
  PUT  /api/v1/dbr/settings          — patch settings
  GET  /api/v1/dbr/assembly-rates    — list takts (with resource/item names)
  PUT  /api/v1/dbr/assembly-rates    — upsert one takt by resource_id+item_id
  DELETE /api/v1/dbr/assembly-rates/{rate_id}
  GET  /api/v1/dbr/category-risks     — list category supply-risk rows
  PUT  /api/v1/dbr/category-risks     — upsert category supply-risk rows
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Generator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.dbr import (
    board_service,
    drum_service,
    feeder_position_service,
    feeder_signal_service,
    gate_service,
    program_service,
    settings_service,
    slot_service,
)

router = APIRouter(prefix="/v1/dbr", tags=["dbr"])


def get_dbr_write_db(
    db: Session = Depends(get_db),
) -> Generator[Session, None, None]:
    """Commit successful DBR mutations and roll back every failed request."""
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------


class SettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    frozen_days: int
    gate_horizon_workdays: int
    shelf_threshold_qty: Decimal
    rt_machining_days: int
    rt_welding_days: int
    rt_painting_days: int
    batch_days_turning: int
    batch_days_bending: int
    batch_days_welding: int
    batch_days_paint_black: int
    batch_days_paint_color: int
    feeder_chain_enabled: bool
    feeder_load_horizon_weeks: int
    w2_warehouse_ref1c: Optional[str] = None
    w3_warehouse_ref1c: Optional[str] = None
    w4_warehouse_ref1c: Optional[str] = None
    fastener_categories: list[str] = []


class SettingsUpdate(BaseModel):
    frozen_days: Optional[int] = None
    gate_horizon_workdays: Optional[int] = None
    shelf_threshold_qty: Optional[Decimal] = None
    rt_machining_days: Optional[int] = None
    rt_welding_days: Optional[int] = None
    rt_painting_days: Optional[int] = None
    batch_days_turning: Optional[int] = None
    batch_days_bending: Optional[int] = None
    batch_days_welding: Optional[int] = None
    batch_days_paint_black: Optional[int] = None
    batch_days_paint_color: Optional[int] = None
    feeder_chain_enabled: Optional[bool] = None
    feeder_load_horizon_weeks: Optional[int] = None
    # warehouse roles are explicitly nullable — use `model_fields_set` to tell
    # "not provided" apart from "set to null".
    w2_warehouse_ref1c: Optional[str] = None
    w3_warehouse_ref1c: Optional[str] = None
    w4_warehouse_ref1c: Optional[str] = None
    fastener_categories: Optional[list[str]] = None


class AssemblyRateOut(BaseModel):
    id: int
    resource_id: int
    resource_name: str
    item_id: int
    item_code: str
    item_name: str
    qty_per_capacity: Decimal


class AssemblyRateUpsert(BaseModel):
    resource_id: int
    item_id: int
    qty_per_capacity: Decimal = Field(gt=0)


class CategoryRiskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    item_group: str
    receipt_warehouse_ref1c: Optional[str] = None
    supply_risk_pct: Optional[Decimal] = None


class CategoryRiskIn(BaseModel):
    item_group: str
    receipt_warehouse_ref1c: Optional[str] = None
    supply_risk_pct: Optional[Decimal] = None


class CategoryRisksReplace(BaseModel):
    rows: list[CategoryRiskIn]


class PositionPreviewRequest(BaseModel):
    schedule_id: Optional[int] = None


class PositionRebuildRequest(BaseModel):
    schedule_id: Optional[int] = None
    expected_schedule_id: Optional[int] = None


class SignalRefreshRequest(BaseModel):
    expected_schedule_id: Optional[int] = None


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@router.get("/settings", response_model=SettingsOut)
def get_settings(db: Session = Depends(get_db)):
    return settings_service.get_or_create_settings(db)


@router.put("/settings", response_model=SettingsOut)
def put_settings(
    payload: SettingsUpdate,
    db: Session = Depends(get_dbr_write_db, scope="function"),
):
    # Only apply fields the caller actually sent (so omitted warehouse roles
    # keep their value rather than being cleared to null).
    data = payload.model_dump(exclude_unset=True)
    return settings_service.update_settings(db, data)


# --------------------------------------------------------------------------
# Assembly rates
# --------------------------------------------------------------------------


@router.get("/assembly-rates", response_model=list[AssemblyRateOut])
def get_assembly_rates(db: Session = Depends(get_db)):
    return settings_service.list_assembly_rates(db)


@router.put("/assembly-rates", response_model=AssemblyRateOut)
def put_assembly_rate(
    payload: AssemblyRateUpsert,
    db: Session = Depends(get_dbr_write_db, scope="function"),
):
    rate = settings_service.upsert_assembly_rate(
        db,
        resource_id=payload.resource_id,
        item_id=payload.item_id,
        qty_per_capacity=payload.qty_per_capacity,
    )
    # Re-read joined view so the response carries resource/item display names.
    for row in settings_service.list_assembly_rates(db):
        if row["id"] == rate.id:
            return row
    raise HTTPException(status_code=500, detail="rate not found after upsert")


@router.delete("/assembly-rates/{rate_id}")
def delete_assembly_rate(
    rate_id: int,
    db: Session = Depends(get_dbr_write_db, scope="function"),
):
    if not settings_service.delete_assembly_rate(db, rate_id):
        raise HTTPException(status_code=404, detail="assembly rate not found")
    return {"deleted": rate_id}


# --------------------------------------------------------------------------
# Category supply-risk
# --------------------------------------------------------------------------


@router.get("/category-risks", response_model=list[CategoryRiskOut])
def get_category_risks(db: Session = Depends(get_db)):
    return settings_service.list_category_risks(db)


@router.put("/category-risks", response_model=list[CategoryRiskOut])
def put_category_risks(
    payload: CategoryRisksReplace,
    db: Session = Depends(get_dbr_write_db, scope="function"),
):
    settings_service.replace_category_risks(
        db, [row.model_dump() for row in payload.rows]
    )
    return settings_service.list_category_risks(db)


# --------------------------------------------------------------------------
# Phase-2 static supermarket positions
# --------------------------------------------------------------------------


@router.post("/feeder/positions/preview")
def preview_feeder_positions(
    payload: PositionPreviewRequest, db: Session = Depends(get_db)
):
    try:
        return feeder_position_service.preview_positions(db, payload.schedule_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/feeder/positions/rebuild")
def rebuild_feeder_positions(
    payload: PositionRebuildRequest,
    db: Session = Depends(get_dbr_write_db, scope="function"),
):
    try:
        return feeder_position_service.rebuild_positions(
            db,
            schedule_id=payload.schedule_id,
            expected_schedule_id=payload.expected_schedule_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/feeder/positions")
def get_feeder_positions(
    include_live_nfp: bool = False,
    active: Optional[bool] = None,
    active_only: bool = False,
    mode: Optional[str] = None,
    supply: Optional[str] = None,
    warehouse: Optional[str] = None,
    zone: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(default=1000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return feeder_position_service.query_position_views(
        db,
        include_live_nfp=include_live_nfp,
        active=active,
        active_only=active_only,
        mode=mode,
        supply=supply,
        warehouse=warehouse,
        zone=zone,
        search=search,
        limit=limit,
        offset=offset,
    )


@router.get("/feeder/positions/{position_id}")
def get_feeder_position(
    position_id: int,
    include_live_nfp: bool = True,
    db: Session = Depends(get_db),
):
    result = feeder_position_service.get_position_view(
        db, position_id, include_live_nfp=include_live_nfp
    )
    if result is None:
        raise HTTPException(status_code=404, detail="supermarket position not found")
    return result


# --------------------------------------------------------------------------
# Phase-2 advisory replenishment signals (no launches/orders/1C writes)
# --------------------------------------------------------------------------


@router.post("/feeder/signals/preview")
def preview_feeder_signals(db: Session = Depends(get_db)):
    return feeder_signal_service.preview_signals(db)


@router.post("/feeder/signals/refresh")
def refresh_feeder_signals(
    payload: SignalRefreshRequest,
    db: Session = Depends(get_dbr_write_db, scope="function"),
):
    try:
        return feeder_signal_service.refresh_signals(db, payload.expected_schedule_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/feeder/signals")
def get_feeder_signals(
    status: Optional[str] = None,
    zone: Optional[str] = None,
    signal_type: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(default=1000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return feeder_signal_service.list_signals(
        db, status=status, zone=zone, signal_type=signal_type,
        search=search, limit=limit, offset=offset
    )


@router.get("/feeder/signals/{signal_id}")
def get_feeder_signal(signal_id: int, db: Session = Depends(get_db)):
    result = feeder_signal_service.get_signal(db, signal_id)
    if result is None:
        raise HTTPException(status_code=404, detail="feeder signal not found")
    return result


# --------------------------------------------------------------------------
# Production program
# --------------------------------------------------------------------------


class ProgramItemIn(BaseModel):
    item_id: int
    program_date: date
    qty: Decimal
    comment: Optional[str] = None


class ProgramCreate(BaseModel):
    from_date: date
    to_date: date
    company: Optional[str] = None
    title: Optional[str] = None
    created_by: Optional[str] = None
    items: list[ProgramItemIn] = []


class ProgramUpdate(BaseModel):
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    company: Optional[str] = None
    title: Optional[str] = None
    items: Optional[list[ProgramItemIn]] = None


def _program_out(program) -> dict[str, Any]:
    return {
        "id": program.id,
        "company": program.company,
        "title": program.title,
        "from_date": program.from_date,
        "to_date": program.to_date,
        "status": program.status,
        "created_by": program.created_by,
        "items": [
            {
                "id": it.id,
                "item_id": it.item_id,
                "item_code": it.item.item_code if it.item else None,
                "item_name": it.item.item_name if it.item else None,
                "program_date": it.program_date,
                "qty": float(it.qty or 0),
                "comment": it.comment,
            }
            for it in sorted(program.items, key=lambda i: (i.program_date, i.id))
        ],
    }


@router.post("/programs")
def create_program(
    payload: ProgramCreate,
    db: Session = Depends(get_dbr_write_db, scope="function"),
):
    try:
        program = program_service.create_program(
            db,
            from_date=payload.from_date,
            to_date=payload.to_date,
            company=payload.company,
            title=payload.title,
            created_by=payload.created_by,
            items=[it.model_dump() for it in payload.items],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _program_out(program)


@router.get("/programs")
def list_programs(status: Optional[str] = None, db: Session = Depends(get_db)):
    return [_program_out(p) for p in program_service.list_programs(db, status=status)]


@router.get("/programs/{program_id}")
def get_program(program_id: int, db: Session = Depends(get_db)):
    program = program_service.get_program(db, program_id)
    if program is None:
        raise HTTPException(status_code=404, detail="program not found")
    return _program_out(program)


@router.put("/programs/{program_id}")
def update_program(
    program_id: int,
    payload: ProgramUpdate,
    db: Session = Depends(get_dbr_write_db, scope="function"),
):
    data = payload.model_dump(exclude_unset=True)
    if "items" in data and data["items"] is not None:
        data["items"] = [dict(it) for it in data["items"]]
    try:
        program = program_service.update_program(db, program_id, data)
    except LookupError:
        raise HTTPException(status_code=404, detail="program not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _program_out(program)


@router.post("/programs/{program_id}/approve")
def approve_program(
    program_id: int,
    db: Session = Depends(get_dbr_write_db, scope="function"),
):
    try:
        program = program_service.approve_program(db, program_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="program not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _program_out(program)


# --------------------------------------------------------------------------
# Drum schedule
# --------------------------------------------------------------------------


class DrumBuild(BaseModel):
    program_id: int


class DrumExtend(BaseModel):
    program_id: int


class SlotMove(BaseModel):
    new_date: date
    new_resource_id: Optional[int] = None


def _schedule_out(schedule) -> dict[str, Any]:
    return {
        "id": schedule.id,
        "period_from": schedule.period_from,
        "period_to": schedule.period_to,
        "source_program_id": schedule.source_program_id,
        "status": schedule.status,
        "config_snapshot": schedule.config_snapshot,
    }


@router.post("/drum/build")
def drum_build(
    payload: DrumBuild,
    db: Session = Depends(get_dbr_write_db, scope="function"),
):
    try:
        schedule, meta = drum_service.build_schedule(db, payload.program_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="program not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"schedule": _schedule_out(schedule), **meta}


@router.post("/drum/{schedule_id}/activate")
def drum_activate(
    schedule_id: int,
    db: Session = Depends(get_dbr_write_db, scope="function"),
):
    try:
        schedule = drum_service.activate(db, schedule_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="schedule not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except IntegrityError:
        # Race with a concurrent activation of another schedule — the
        # partial-unique ux_dbr_drum_schedule_one_active fired. Surface a clear
        # conflict instead of a 500 so the client can retry.
        raise HTTPException(
            status_code=409,
            detail="другой график активируется параллельно — повторите попытку",
        )
    return _schedule_out(schedule)


@router.post("/drum/{schedule_id}/extend")
def drum_extend(
    schedule_id: int,
    payload: DrumExtend,
    db: Session = Depends(get_dbr_write_db, scope="function"),
):
    try:
        schedule, meta = drum_service.extend(db, schedule_id, payload.program_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="schedule or program not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"schedule": _schedule_out(schedule), **meta}


@router.get("/drum/active/board")
def drum_board(
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
    db: Session = Depends(get_db),
):
    return board_service.get_board(db, date_from=date_from, date_to=date_to)


@router.post("/drum/{schedule_id}/refresh-gate")
def drum_refresh_gate(
    schedule_id: int,
    db: Session = Depends(get_dbr_write_db, scope="function"),
):
    try:
        return gate_service.refresh_gate(db, schedule_id=schedule_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/drum/{schedule_id}/roll-forward")
def drum_roll_forward(
    schedule_id: int,
    db: Session = Depends(get_dbr_write_db, scope="function"),
):
    return slot_service.roll_forward(db, schedule_id=schedule_id)


@router.post("/drum/slots/{slot_id}/move")
def drum_move_slot(
    slot_id: int,
    payload: SlotMove,
    db: Session = Depends(get_dbr_write_db, scope="function"),
):
    try:
        return slot_service.move_slot(
            db, slot_id, payload.new_date, new_resource_id=payload.new_resource_id
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="slot not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/drum/slots/{slot_id}/release")
def drum_release_slot(
    slot_id: int,
    db: Session = Depends(get_dbr_write_db, scope="function"),
):
    try:
        return slot_service.release_slot(db, slot_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="slot not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
