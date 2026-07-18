"""API справочника пар «окрашенная ↔ сварная» (окраска ↔ сварка), этап 1.

См. .docs/paint_weld_chain_logic.md. Только чтение справочника, ручные правки,
пересборка auto-пар и гард открытия окрасочного заказа. Без записи в 1С.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import paint_weld_pairs as service

router = APIRouter(prefix="/v1/paint-weld", tags=["paint-weld"])


class ManualPairPayload(BaseModel):
    painted_item_id: int
    welded_item_id: int


@router.get("/pairs", response_model=dict)
async def get_pairs(active_only: bool = True, db: Session = Depends(get_db)):
    return {"pairs": service.list_pairs(db, active_only=active_only)}


@router.post("/pairs/rebuild", response_model=dict)
async def rebuild_pairs(db: Session = Depends(get_db)):
    return service.rebuild_auto_pairs(db)


@router.get("/orphans", response_model=dict)
async def get_orphans(db: Session = Depends(get_db)):
    return service.list_orphans(db)


@router.put("/pairs", response_model=dict)
async def put_manual_pair(payload: ManualPairPayload, db: Session = Depends(get_db)):
    try:
        return service.upsert_manual_pair(
            db,
            painted_item_id=payload.painted_item_id,
            welded_item_id=payload.welded_item_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/pairs/{pair_id}", response_model=dict)
async def delete_pair(pair_id: int, db: Session = Depends(get_db)):
    try:
        return service.deactivate_pair(db, int(pair_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/guard", response_model=dict)
async def guard(
    painted_item_id: int,
    qty: float,
    db: Session = Depends(get_db),
):
    return service.guard_paint_order(db, int(painted_item_id), float(qty))
