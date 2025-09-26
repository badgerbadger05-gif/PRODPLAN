from typing import List, Any, Dict
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from ..database import get_db
from ..models import ProductionResource, ResourceStage, ProductionStage
from ..schemas import (
    ProductionResource as ProductionResourceSchema,
    ProductionResourceCreate,
    ProductionResourceUpdate,
    ResourceStage as ResourceStageSchema,
    ResourceStageCreate,
    ResourceStageWithName
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
            daily_work_hours=resource.daily_work_hours
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
    
    # Удаляем связанные этапы
    db.query(ResourceStage).filter(ResourceStage.resource_id == resource_id).delete()
    
    db.delete(db_resource)
    db.commit()
    return {"status": "success"}


@router.get("/{resource_id}/stages", response_model=List[ResourceStageWithName])
def get_resource_stages(resource_id: int, db: Session = Depends(get_db)):
    """Получить список этапов, привязанных к участку, с именем этапа"""
    rows = (
        db.query(ResourceStage, ProductionStage.stage_name)
        .outerjoin(ProductionStage, ProductionStage.stage_id == ResourceStage.stage_id)
        .filter(ResourceStage.resource_id == resource_id)
        .all()
    )
    return [
        {
            "id": int(rs.id),
            "resource_id": int(rs.resource_id),
            "stage_id": int(rs.stage_id),
            "stage_name": (stage_name or None),
            "created_at": getattr(rs, "created_at", None),
            "updated_at": getattr(rs, "updated_at", None),
        }
        for (rs, stage_name) in rows
    ]


@router.post("/{resource_id}/stages", response_model=ResourceStageSchema)
def add_stage_to_resource(
    resource_id: int,
    resource_stage: ResourceStageCreate,
    db: Session = Depends(get_db)
):
    """Привязать этап производства к участку"""
    # Проверяем существование участка и этапа
    resource = db.query(ProductionResource).filter(ProductionResource.resource_id == resource_id).first()
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    
    stage = db.query(ProductionStage).filter(ProductionStage.stage_id == resource_stage.stage_id).first()
    if not stage:
        raise HTTPException(status_code=404, detail="Stage not found")
    
    # Проверяем, что связь не существует
    existing = db.query(ResourceStage).filter(
        ResourceStage.resource_id == resource_id,
        ResourceStage.stage_id == resource_stage.stage_id
    ).first()
    
    if existing:
        raise HTTPException(status_code=400, detail="Stage already assigned to this resource")
    
    try:
        # Жёстко используем resource_id из URL, игнорируя поле из payload для предотвращения несоответствий
        db_resource_stage = ResourceStage(
            resource_id=resource_id,
            stage_id=resource_stage.stage_id
        )
        db.add(db_resource_stage)
        db.commit()
        db.refresh(db_resource_stage)
        return db_resource_stage
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Error assigning stage to resource")


@router.delete("/{resource_id}/stages/{stage_id}")
def remove_stage_from_resource(resource_id: int, stage_id: int, db: Session = Depends(get_db)):
    """Удалить привязку этапа производства к участку"""
    resource_stage = db.query(ResourceStage).filter(
        ResourceStage.resource_id == resource_id,
        ResourceStage.stage_id == stage_id
    ).first()
    
    if not resource_stage:
        raise HTTPException(status_code=404, detail="Stage assignment not found")
    
    db.delete(resource_stage)
    db.commit()
    return {"status": "success"}


@router.get("/{resource_id}", response_model=ProductionResourceSchema)
def get_resource(resource_id: int, db: Session = Depends(get_db)):
    """Получить информацию о конкретном участке"""
    resource = db.query(ProductionResource).filter(ProductionResource.resource_id == resource_id).first()
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    return resource