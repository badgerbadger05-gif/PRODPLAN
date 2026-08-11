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
from ..services import planning_truth
from ..services.paint_weld_chain import close_paint_chain, open_paint_chain

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
    # DEPRECATED: демо-гард записи в 1С удалён после go-live. Поле принимается
    # и игнорируется, чтобы существующие клиенты не получали 422.
    allow_production: bool = False
    initiated_by: Optional[str] = None


class ChainClosePayload(BaseModel):
    """Закрытие цепочки из окна журнала (этап 4): любая сторона цепочки."""

    product_id: int
    weld_qty: Optional[float] = None
    paint_qty: Optional[float] = None
    executor: Optional[str] = None
    weld_operation_executors: Optional[list] = None
    paint_operation_executors: Optional[list] = None
    comment: Optional[str] = None
    dry_run: bool = True
    # DEPRECATED, см. ChainOpenPayload: принимается, не влияет.
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
    try:
        return service.guard_paint_order(db, int(painted_item_id), float(qty))
    except planning_truth.PlanningTruthUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.as_dict())


@router.post("/chain/preview", response_model=dict)
async def chain_preview(payload: ChainPreviewPayload, db: Session = Depends(get_db)):
    """Предпросмотр цепочки «сварка → окраска» (dry-run, ничего не пишет)."""
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
    реальное создание в 1С (сварка, затем окраска на основании сварочного заказа)."""
    try:
        return open_paint_chain(
            db,
            painted_product_id=payload.painted_product_id,
            painted_item_id=payload.painted_item_id,
            qty=payload.qty,
            planned_start=payload.planned_start,
            planned_finish=payload.planned_finish,
            dry_run=bool(payload.dry_run),
            initiated_by=payload.initiated_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/chain/close", response_model=dict)
async def chain_close(payload: ChainClosePayload, db: Session = Depends(get_db)):
    """Закрыть цепочку «окраска-сварка» одним действием: выпуски обеих строк,
    СборкаЗапасов обоих заказов и один комбинированный СдельныйНаряд, закрывающий
    оба заказа. dry_run=true — предпросмотр.

    Закрытие возобновляемо. Если часть шага прошла (одна СборкаЗапасов в 1С, или
    обе сборки есть, а наряда нет), ответ приходит с HTTP 200 и
    ``status='partial'``, ``resume_required=true``, ``chain_state``,
    ``posted_sides``/``pending_sides`` и человекочитаемым ``message``: проведённые
    документы не откатываются, повторный вызов докатывает недостающие без дублей.
    400 остаётся только для случая, когда в 1С не ушло ничего."""
    try:
        return close_paint_chain(
            db,
            product_id=int(payload.product_id),
            weld_qty=payload.weld_qty,
            paint_qty=payload.paint_qty,
            executor=payload.executor,
            weld_operation_executors=payload.weld_operation_executors,
            paint_operation_executors=payload.paint_operation_executors,
            comment=payload.comment,
            dry_run=bool(payload.dry_run),
            initiated_by=payload.initiated_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
