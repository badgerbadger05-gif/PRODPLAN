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

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.dbr import settings_service

router = APIRouter(prefix="/v1/dbr", tags=["dbr"])


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
    qty_per_capacity: Decimal


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


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@router.get("/settings", response_model=SettingsOut)
def get_settings(db: Session = Depends(get_db)):
    return settings_service.get_or_create_settings(db)


@router.put("/settings", response_model=SettingsOut)
def put_settings(payload: SettingsUpdate, db: Session = Depends(get_db)):
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
def put_assembly_rate(payload: AssemblyRateUpsert, db: Session = Depends(get_db)):
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
def delete_assembly_rate(rate_id: int, db: Session = Depends(get_db)):
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
def put_category_risks(payload: CategoryRisksReplace, db: Session = Depends(get_db)):
    settings_service.replace_category_risks(
        db, [row.model_dump() for row in payload.rows]
    )
    return settings_service.list_category_risks(db)
