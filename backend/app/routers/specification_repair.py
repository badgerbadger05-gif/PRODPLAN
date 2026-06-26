"""Ремонтный модуль спецификаций — HTTP API операции A (правка состава).

dry_run=True (по умолчанию): предпросмотр, локальная мутация откатывается, в 1С не пишем.
dry_run=False: сначала пишем исправление в 1С (источник истины) через spec_writeback_1c,
и только при успехе коммитим локальную мутацию-зеркало. Если 1С-запись падает —
локальная БД не меняется, отдаём 502.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import spec_repair, spec_writeback_1c
from ..services.spec_repair import SpecRepairError
from ..services.spec_writeback_1c import SpecWritebackError

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


class RemoveRequest(BaseModel):
    component_id: int
    force: bool = False
    dry_run: bool = True


class KindChangePreviewRequest(BaseModel):
    item_id: int
    new_production_kind_id: int


@router.post("/restage")
def restage(req: RestageRequest, db: Session = Depends(get_db)) -> dict:
    try:
        writeback = None
        if not req.dry_run:
            plan = spec_repair.build_restage_plan(
                db, component_id=req.component_id, new_stage_id=req.new_stage_id
            )
            writeback = spec_writeback_1c.writeback_restage(
                spec_writeback_1c.build_client_from_config(),
                spec_ref=plan["spec_ref"],
                nomenclature_key=plan["nomenclature_key"],
                child_spec_key=plan["child_spec_key"],
                new_stage_key=plan["new_stage_key"],
                dry_run=False,
            )
        result = spec_repair.restage_component(
            db, component_id=req.component_id, new_stage_id=req.new_stage_id, dry_run=req.dry_run
        )
        if writeback is not None:
            result["writeback_1c"] = writeback
        return result
    except SpecRepairError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except SpecWritebackError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/move")
def move(req: MoveRequest, db: Session = Depends(get_db)) -> dict:
    try:
        writeback = None
        if not req.dry_run:
            plan = spec_repair.build_move_plan(
                db,
                component_id=req.component_id,
                target_spec_id=req.target_spec_id,
                new_stage_id=req.new_stage_id,
            )
            writeback = spec_writeback_1c.writeback_move(
                spec_writeback_1c.build_client_from_config(),
                source_spec_ref=plan["source_spec_ref"],
                target_spec_ref=plan["target_spec_ref"],
                nomenclature_key=plan["nomenclature_key"],
                child_spec_key=plan["child_spec_key"],
                new_stage_key=plan["new_stage_key"],
                dry_run=False,
            )
        result = spec_repair.move_component(
            db,
            component_id=req.component_id,
            target_spec_id=req.target_spec_id,
            new_stage_id=req.new_stage_id,
            force=req.force,
            dry_run=req.dry_run,
        )
        if writeback is not None:
            result["writeback_1c"] = writeback
        return result
    except SpecRepairError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except SpecWritebackError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/add")
def add(req: AddRequest, db: Session = Depends(get_db)) -> dict:
    try:
        writeback = None
        if not req.dry_run:
            plan = spec_repair.build_add_plan(
                db,
                spec_id=req.spec_id,
                item_id=req.item_id,
                quantity=req.quantity,
                component_type=req.component_type,
                stage_id=req.stage_id,
                component_spec_ref1c=req.component_spec_ref1c,
            )
            writeback = spec_writeback_1c.writeback_add(
                spec_writeback_1c.build_client_from_config(),
                spec_ref=plan["spec_ref"],
                nomenclature_key=plan["nomenclature_key"],
                unit_key=plan["unit_key"],
                quantity=plan["quantity"],
                stage_key=plan["stage_key"],
                component_type=plan["component_type"],
                child_spec_key=plan["child_spec_key"],
                dry_run=False,
            )
        result = spec_repair.add_component(
            db,
            spec_id=req.spec_id,
            item_id=req.item_id,
            quantity=req.quantity,
            component_type=req.component_type,
            stage_id=req.stage_id,
            component_spec_ref1c=req.component_spec_ref1c,
            dry_run=req.dry_run,
        )
        if writeback is not None:
            result["writeback_1c"] = writeback
        return result
    except SpecRepairError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except SpecWritebackError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/remove")
def remove(req: RemoveRequest, db: Session = Depends(get_db)) -> dict:
    try:
        writeback = None
        if not req.dry_run:
            # Сначала валидируем безопасность (осиротение/force) на dry-run — ДО записи в
            # 1С. Иначе при отказе локальной мутации строка осталась бы удалённой в 1С.
            spec_repair.remove_component(
                db, component_id=req.component_id, force=req.force, dry_run=True
            )
            plan = spec_repair.build_remove_plan(db, component_id=req.component_id)
            writeback = spec_writeback_1c.writeback_remove(
                spec_writeback_1c.build_client_from_config(),
                spec_ref=plan["spec_ref"],
                nomenclature_key=plan["nomenclature_key"],
                child_spec_key=plan["child_spec_key"],
                dry_run=False,
            )
        result = spec_repair.remove_component(
            db, component_id=req.component_id, force=req.force, dry_run=req.dry_run
        )
        if writeback is not None:
            result["writeback_1c"] = writeback
        return result
    except SpecRepairError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except SpecWritebackError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/kind-change/preview")
def kind_change_preview(req: KindChangePreviewRequest, db: Session = Depends(get_db)) -> dict:
    """Read-only: чек-лист родителей, которых затронет смена вида производства детали."""
    try:
        return spec_repair.preview_kind_change(
            db, item_id=req.item_id, new_production_kind_id=req.new_production_kind_id
        )
    except SpecRepairError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
