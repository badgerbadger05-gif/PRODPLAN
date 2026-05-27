from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.production_control_settings import (
    delete_ignored_warehouse,
    delete_workshop_binding,
    list_settings,
    replace_settings,
    upsert_ignored_warehouse,
    upsert_workshop_binding,
)


router = APIRouter()


class WorkshopBindingPayload(BaseModel):
    warehouse_ref1c: str


class IgnoredWarehousePayload(BaseModel):
    warehouse_ref1c: str
    warehouse_name: Optional[str] = None
    reason: Optional[str] = None


class SettingsPayload(BaseModel):
    workshop_warehouses: List[dict] = []
    ignored_warehouses: List[dict] = []


@router.get("/settings", response_model=dict)
def get_settings(db: Session = Depends(get_db)):
    """Return current workshop->warehouse bindings and ignored warehouses."""
    return list_settings(db)


@router.post("/settings", response_model=dict)
def post_settings(payload: SettingsPayload, db: Session = Depends(get_db)):
    try:
        return replace_settings(
            db,
            workshop_warehouses=payload.workshop_warehouses,
            ignored_warehouses=payload.ignored_warehouses,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/settings/workshop-bindings/{workshop_id}", response_model=dict)
def put_workshop_binding(
    workshop_id: int,
    payload: WorkshopBindingPayload,
    db: Session = Depends(get_db),
):
    try:
        return upsert_workshop_binding(db, int(workshop_id), payload.warehouse_ref1c)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/settings/workshop-bindings/{workshop_id}", response_model=dict)
def delete_binding(workshop_id: int, db: Session = Depends(get_db)):
    try:
        return delete_workshop_binding(db, int(workshop_id))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/settings/ignored-warehouses", response_model=dict)
def post_ignored_warehouse(payload: IgnoredWarehousePayload, db: Session = Depends(get_db)):
    try:
        return upsert_ignored_warehouse(
            db,
            payload.warehouse_ref1c,
            warehouse_name=payload.warehouse_name,
            reason=payload.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/settings/ignored-warehouses/{warehouse_ref1c}", response_model=dict)
def delete_ignored(warehouse_ref1c: str, db: Session = Depends(get_db)):
    try:
        return delete_ignored_warehouse(db, str(warehouse_ref1c))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
