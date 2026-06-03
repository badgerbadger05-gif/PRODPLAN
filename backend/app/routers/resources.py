from typing import List, Any, Dict
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from ..database import get_db
from ..models import ProductionResource, ResourceProductionKind, ProductionKind
from ..schemas import (
    ProductionResource as ProductionResourceSchema,
    ProductionResourceCreate,
    ProductionResourceUpdate,
    ResourceProductionKind as ResourceProductionKindSchema,
    ResourceProductionKindCreate,
    ProductionKind as ProductionKindSchema,
)
from ..services.resource_calculator import calculate_resource_distribution

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
    resources = db.query(ProductionResource).offset(0).limit(100).all()
    return resources


@router.post("/", response_model=ProductionResourceSchema)
def create_resource(resource: ProductionResourceCreate, db: Session = Depends(get_db)):
    """Создать новый производственный участок"""
    try:
        db_resource = ProductionResource(
            resource_name=resource.resource_name,
            shift_offset=resource.shift_offset,
            planning_range=resource.planning_range,
            capacity=resource.capacity,
            work_schedule=resource.work_schedule,
            daily_work_hours=resource.daily_work_hours,
            buffer_days=resource.buffer_days,
        )
        db.add(db_resource)
        db.commit()
        db.refresh(db_resource)
        return db_resource
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
    db_resource = db.query(ProductionResource).filter(ProductionResource.resource_id == resource_id).first()
    if not db_resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    
    try:
        db_resource.resource_name = resource.resource_name
        db_resource.shift_offset = resource.shift_offset
        db_resource.planning_range = resource.planning_range
        db_resource.capacity = resource.capacity
        db_resource.work_schedule = resource.work_schedule
        db_resource.daily_work_hours = resource.daily_work_hours
        db_resource.buffer_days = resource.buffer_days
        
        db.commit()
        db.refresh(db_resource)
        return db_resource
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Resource with this name already exists")


@router.delete("/{resource_id}")
def delete_resource(resource_id: int, db: Session = Depends(get_db)):
    """Удалить производственный участок"""
    db_resource = db.query(ProductionResource).filter(ProductionResource.resource_id == resource_id).first()
    if not db_resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    
    db.delete(db_resource)
    db.commit()
    return {"status": "success"}


@router.get("/production-kinds", response_model=List[ProductionKindSchema])
def list_production_kinds(db: Session = Depends(get_db)):
    """Получить список всех видов производства"""
    kinds = db.query(ProductionKind).order_by(ProductionKind.name.asc()).all()
    return kinds

# --- Production kinds mapping (resource_production_kinds) ---

@router.get("/{resource_id}/production-kinds", response_model=List[ResourceProductionKindSchema])
def get_resource_production_kinds(resource_id: int, db: Session = Depends(get_db)):
    """Список видов производства, привязанных к участку"""
    rows = (
        db.query(ResourceProductionKind)
        .filter(ResourceProductionKind.resource_id == resource_id)
        .all()
    )
    return rows


@router.post("/{resource_id}/production-kinds", response_model=ResourceProductionKindSchema)
def add_production_kind_to_resource(
    resource_id: int,
    payload: ResourceProductionKindCreate,
    db: Session = Depends(get_db),
):
    """Привязать вид производства к участку"""
    # Проверяем существование участка
    res = db.query(ProductionResource).filter(ProductionResource.resource_id == resource_id).first()
    if not res:
        raise HTTPException(status_code=404, detail="Resource not found")

    # Проверяем существование вида производства
    pk = db.query(ProductionKind).filter(ProductionKind.id == payload.production_kind_id).first()
    if not pk:
        raise HTTPException(status_code=404, detail="Production kind not found")

    # Глобальная защита от дублей:
    # Если вид производства уже назначен на ЛЮБОЙ другой участок — запрещаем повторное назначение
    existing_global = (
        db.query(ResourceProductionKind)
        .filter(ResourceProductionKind.production_kind_id == payload.production_kind_id)
        .first()
    )
    if existing_global and int(existing_global.resource_id) != int(resource_id):
        raise HTTPException(status_code=400, detail="Production kind already assigned to another resource")

    # Проверяем уникальность связи для текущего участка (idempotent)
    existing = (
        db.query(ResourceProductionKind)
        .filter(
            ResourceProductionKind.resource_id == resource_id,
            ResourceProductionKind.production_kind_id == payload.production_kind_id,
        )
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail="Production kind already assigned to this resource")

    try:
        rec = ResourceProductionKind(
            resource_id=resource_id,
            production_kind_id=payload.production_kind_id,
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        return rec
    except IntegrityError:
        db.rollback()
        # Может сработать на уровне БД при уникальном индексе production_kind_id
        raise HTTPException(status_code=400, detail="Error assigning production kind to resource")


@router.delete("/{resource_id}/production-kinds/{production_kind_id}")
def remove_production_kind_from_resource(resource_id: int, production_kind_id: int, db: Session = Depends(get_db)):
    """Удалить привязку вида производства к участку"""
    rec = (
        db.query(ResourceProductionKind)
        .filter(
            ResourceProductionKind.resource_id == resource_id,
            ResourceProductionKind.production_kind_id == production_kind_id,
        )
        .first()
    )
    if not rec:
        raise HTTPException(status_code=404, detail="Production kind assignment not found")

    db.delete(rec)
    db.commit()
    return {"status": "success"}


@router.get("/{resource_id}", response_model=ProductionResourceSchema)
def get_resource(resource_id: int, db: Session = Depends(get_db)):
    """Получить информацию о конкретном участке"""
    resource = db.query(ProductionResource).filter(ProductionResource.resource_id == resource_id).first()
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    return resource
