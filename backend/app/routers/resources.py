from typing import List, Any, Dict
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from ..database import get_db
from ..schemas import (
    ProductionResource as ProductionResourceSchema,
    ProductionResourceCreate,
    ProductionResourceUpdate,
    ResourceStage as ResourceStageSchema,
    ResourceStageCreate,
    ResourceStageWithName,
    ResourceProductionKind as ResourceProductionKindSchema,
    ResourceProductionKindCreate,
    ProductionKind as ProductionKindSchema,
)
from ..services.resource_calculator import calculate_resource_distribution
from ..services import resources_service

router = APIRouter(prefix="/v1/resources", tags=["resources"])


@router.post("/calculate_distribution", response_model=Dict[str, Any])
def get_resource_distribution(db: Session = Depends(get_db)):
    """
    Рассчитать распределение компонентов по производственным участкам.
    """
    try:
        return calculate_resource_distribution(db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Resource distribution calculation error: {e}")


@router.get("/", response_model=List[ProductionResourceSchema])
def get_resources(db: Session = Depends(get_db)):
    """Получить список всех производственных участков"""
    return resources_service.list_resources(db)


@router.post("/", response_model=ProductionResourceSchema)
def create_resource(resource: ProductionResourceCreate, db: Session = Depends(get_db)):
    """Создать новый производственный участок"""
    try:
        return resources_service.create_resource(db, resource)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Resource with this name already exists")


@router.put("/{resource_id}", response_model=ProductionResourceSchema)
def update_resource(
    resource_id: int,
    resource: ProductionResourceUpdate,
    db: Session = Depends(get_db)
):
    """Обновить информацию о производственном участке"""
    db_resource = resources_service.get_resource_by_id(db, resource_id)
    if not db_resource:
        raise HTTPException(status_code=404, detail="Resource not found")

    try:
        return resources_service.update_resource(db, db_resource, resource)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Resource with this name already exists")


@router.delete("/{resource_id}")
def delete_resource(resource_id: int, db: Session = Depends(get_db)):
    """Удалить производственный участок"""
    db_resource = resources_service.get_resource_by_id(db, resource_id)
    if not db_resource:
        raise HTTPException(status_code=404, detail="Resource not found")

    resources_service.delete_resource(db, db_resource)
    return {"status": "success"}


@router.get("/{resource_id}/stages", response_model=List[ResourceStageWithName])
def get_resource_stages(resource_id: int, db: Session = Depends(get_db)):
    """Получить список этапов, привязанных к участку, с именем этапа"""
    return resources_service.get_resource_stages_with_names(db, resource_id)


@router.get("/production-kinds", response_model=List[ProductionKindSchema])
def list_production_kinds(db: Session = Depends(get_db)):
    """Получить список всех видов производства"""
    return resources_service.list_production_kinds(db)


@router.post("/{resource_id}/stages", response_model=ResourceStageSchema)
def add_stage_to_resource(
    resource_id: int,
    resource_stage: ResourceStageCreate,
    db: Session = Depends(get_db)
):
    """Привязать этап производства к участку"""
    # Проверяем существование участка и этапа
    resource = resources_service.get_resource_by_id(db, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")

    stage = resources_service.get_stage_by_id(db, resource_stage.stage_id)
    if not stage:
        raise HTTPException(status_code=404, detail="Stage not found")

    # Проверяем, что связь не существует
    existing = resources_service.find_resource_stage(db, resource_id, resource_stage.stage_id)

    if existing:
        raise HTTPException(status_code=400, detail="Stage already assigned to this resource")

    try:
        return resources_service.create_resource_stage(db, resource_id, resource_stage.stage_id)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Error assigning stage to resource")


@router.delete("/{resource_id}/stages/{stage_id}")
def remove_stage_from_resource(resource_id: int, stage_id: int, db: Session = Depends(get_db)):
    """Удалить привязку этапа производства к участку"""
    resource_stage = resources_service.find_resource_stage(db, resource_id, stage_id)

    if not resource_stage:
        raise HTTPException(status_code=404, detail="Stage assignment not found")

    resources_service.delete_resource_stage(db, resource_stage)
    return {"status": "success"}


# --- Production kinds mapping (resource_production_kinds) ---

@router.get("/{resource_id}/production-kinds", response_model=List[ResourceProductionKindSchema])
def get_resource_production_kinds(resource_id: int, db: Session = Depends(get_db)):
    """Список видов производства, привязанных к участку"""
    return resources_service.get_resource_production_kinds(db, resource_id)


@router.post("/{resource_id}/production-kinds", response_model=ResourceProductionKindSchema)
def add_production_kind_to_resource(
    resource_id: int,
    payload: ResourceProductionKindCreate,
    db: Session = Depends(get_db),
):
    """Привязать вид производства к участку"""
    # Проверяем существование участка
    res = resources_service.get_resource_by_id(db, resource_id)
    if not res:
        raise HTTPException(status_code=404, detail="Resource not found")

    # Проверяем существование вида производства
    pk = resources_service.get_production_kind_by_id(db, payload.production_kind_id)
    if not pk:
        raise HTTPException(status_code=404, detail="Production kind not found")

    # Глобальная защита от дублей:
    # Если вид производства уже назначен на ЛЮБОЙ другой участок — запрещаем повторное назначение
    existing_global = resources_service.find_production_kind_assignment_global(
        db, payload.production_kind_id
    )
    if existing_global and int(existing_global.resource_id) != int(resource_id):
        raise HTTPException(status_code=400, detail="Production kind already assigned to another resource")

    # Проверяем уникальность связи для текущего участка (idempotent)
    existing = resources_service.find_resource_production_kind(
        db, resource_id, payload.production_kind_id
    )
    if existing:
        raise HTTPException(status_code=400, detail="Production kind already assigned to this resource")

    try:
        return resources_service.create_resource_production_kind(
            db, resource_id, payload.production_kind_id
        )
    except IntegrityError:
        db.rollback()
        # Может сработать на уровне БД при уникальном индексе production_kind_id
        raise HTTPException(status_code=400, detail="Error assigning production kind to resource")


@router.delete("/{resource_id}/production-kinds/{production_kind_id}")
def remove_production_kind_from_resource(resource_id: int, production_kind_id: int, db: Session = Depends(get_db)):
    """Удалить привязку вида производства к участку"""
    rec = resources_service.find_resource_production_kind(db, resource_id, production_kind_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Production kind assignment not found")

    resources_service.delete_resource_production_kind(db, rec)
    return {"status": "success"}


@router.get("/{resource_id}", response_model=ProductionResourceSchema)
def get_resource(resource_id: int, db: Session = Depends(get_db)):
    """Получить информацию о конкретном участке"""
    resource = resources_service.get_resource_by_id(db, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    return resource
