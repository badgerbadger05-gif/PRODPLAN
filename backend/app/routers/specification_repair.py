"""Ремонтный модуль спецификаций — HTTP API операции A (правка состава).

Все эндпоинты по умолчанию dry_run=True: возвращают предпросмотр без записи в БД.
Запись в 1С НЕ выполняется ни при каком dry_run — это отдельный supervised-шаг
(см. services/spec_repair.py, поле pending_1c в ответе).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import spec_repair
from ..services.spec_repair import SpecRepairError

router = APIRouter(prefix="/v1/specification-repair", tags=["specification-repair"])


class RestageRequest(BaseModel):
    component_id: int
    new_stage_id: Optional[int] = None
    dry_run: bool = True


class MoveRequest(BaseModel):
    component_id: int
    target_spec_id: int
    new_stage_id: Optional[int] = None
    force: bool = False
    dry_run: bool = True


class AddRequest(BaseModel):
    spec_id: int
    item_id: int
    quantity: float
    component_type: str = "Сборка"
    stage_id: Optional[int] = None
    component_spec_ref1c: Optional[str] = None
    dry_run: bool = True


class KindChangePreviewRequest(BaseModel):
    item_id: int
    new_production_kind_id: int


@router.post("/restage")
def restage(req: RestageRequest, db: Session = Depends(get_db)) -> dict:
    try:
        return spec_repair.restage_component(
            db, component_id=req.component_id, new_stage_id=req.new_stage_id, dry_run=req.dry_run
        )
    except SpecRepairError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/move")
def move(req: MoveRequest, db: Session = Depends(get_db)) -> dict:
    try:
        return spec_repair.move_component(
            db,
            component_id=req.component_id,
            target_spec_id=req.target_spec_id,
            new_stage_id=req.new_stage_id,
            force=req.force,
            dry_run=req.dry_run,
        )
    except SpecRepairError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/add")
def add(req: AddRequest, db: Session = Depends(get_db)) -> dict:
    try:
        return spec_repair.add_component(
            db,
            spec_id=req.spec_id,
            item_id=req.item_id,
            quantity=req.quantity,
            component_type=req.component_type,
            stage_id=req.stage_id,
            component_spec_ref1c=req.component_spec_ref1c,
            dry_run=req.dry_run,
        )
    except SpecRepairError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/kind-change/preview")
def kind_change_preview(req: KindChangePreviewRequest, db: Session = Depends(get_db)) -> dict:
    """Read-only: чек-лист родителей, которых затронет смена вида производства детали."""
    try:
        return spec_repair.preview_kind_change(
            db, item_id=req.item_id, new_production_kind_id=req.new_production_kind_id
        )
    except SpecRepairError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
