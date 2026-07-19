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
from ..services.paint_weld_chain import open_paint_chain

router = APIRouter(prefix="/v1/paint-weld", tags=["paint-weld"])


class ManualPairPayload(BaseModel):
    painted_item_id: int
    welded_item_id: int


class ChainPreviewPayload(BaseModel):
    painted_item_id: Optional[int] = None
    painted_product_id: Optional[int] = None
    qty: Optional[float] = None
    planned_start: Optional[str] = None
    planned_finish: Optional[str] = None


class ChainOpenPayload(ChainPreviewPayload):
    # Preview/confirm pattern as in DBR materialization: dry_run defaults to True.
    dry_run: bool = True
    allow_production: bool = False
    initiated_by: Optional[str] = None


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


@router.post("/chain/preview", response_model=dict)
async def chain_preview(payload: ChainPreviewPayload, db: Session = Depends(get_db)):
    """Предпросмотр цепочки «окраска → сварка» (dry-run, ничего не пишет)."""
    try:
        return open_paint_chain(
            db,
            painted_product_id=payload.painted_product_id,
            painted_item_id=payload.painted_item_id,
            qty=payload.qty,
            planned_start=payload.planned_start,
            planned_finish=payload.planned_finish,
            dry_run=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/chain/open", response_model=dict)
async def chain_open(payload: ChainOpenPayload, db: Session = Depends(get_db)):
    """Открыть цепочку. dry_run=true (по умолчанию) — предпросмотр; dry_run=false —
    реальное создание в 1С (окраска, затем сварка на основании)."""
    try:
        return open_paint_chain(
            db,
            painted_product_id=payload.painted_product_id,
            painted_item_id=payload.painted_item_id,
            qty=payload.qty,
            planned_start=payload.planned_start,
            planned_finish=payload.planned_finish,
            dry_run=bool(payload.dry_run),
            allow_production=bool(payload.allow_production) or not bool(payload.dry_run),
            initiated_by=payload.initiated_by,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
