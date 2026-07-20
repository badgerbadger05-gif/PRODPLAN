"""Data-access and business logic for the production resources router.

Extracted from ``app.routers.resources`` to keep HTTP handlers thin.
Functions accept a SQLAlchemy ``Session`` plus plain parameters and return
plain data / ORM objects. HTTP concerns (status codes, ``HTTPException``,
response shaping) remain in the router.

Behaviour is preserved exactly: same queries, ordering, filters and edge
cases as the original inline handlers.
"""
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models import (
    ProductionResource,
    ResourceStage,
    ProductionStage,
    ResourceProductionKind,
    ProductionKind,
)
from ..schemas import (
    ProductionResourceCreate,
    ProductionResourceUpdate,
)


# --- Production resources ---

def list_resources(db: Session) -> List[ProductionResource]:
    """Список всех производственных участков (первые 100)."""
    return db.query(ProductionResource).offset(0).limit(100).all()


def get_resource_by_id(db: Session, resource_id: int) -> Optional[ProductionResource]:
    """Найти участок по идентификатору (или None)."""
    return (
        db.query(ProductionResource)
        .filter(ProductionResource.resource_id == resource_id)
        .first()
    )


def create_resource(db: Session, resource: ProductionResourceCreate) -> ProductionResource:
    """Создать новый производственный участок.

    Может выбросить ``IntegrityError`` (обрабатывается в роутере).
    """
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


def update_resource(
    db: Session,
    db_resource: ProductionResource,
    resource: ProductionResourceUpdate,
) -> ProductionResource:
    """Обновить поля участка и сохранить.

    Может выбросить ``IntegrityError`` (обрабатывается в роутере).
    """
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


def delete_resource(db: Session, db_resource: ProductionResource) -> None:
    """Удалить участок вместе со связанными этапами."""
    resource_id = db_resource.resource_id
    # Удаляем связанные этапы
    db.query(ResourceStage).filter(ResourceStage.resource_id == resource_id).delete()

    db.delete(db_resource)
    db.commit()


# --- Resource stages ---

def get_resource_stages_with_names(db: Session, resource_id: int) -> List[Dict[str, Any]]:
    """Этапы, привязанные к участку, с именем этапа."""
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


def get_stage_by_id(db: Session, stage_id: int) -> Optional[ProductionStage]:
    """Найти этап производства по идентификатору (или None)."""
    return (
        db.query(ProductionStage)
        .filter(ProductionStage.stage_id == stage_id)
        .first()
    )


def find_resource_stage(db: Session, resource_id: int, stage_id: int) -> Optional[ResourceStage]:
    """Найти связь участок-этап (или None)."""
    return (
        db.query(ResourceStage)
        .filter(
            ResourceStage.resource_id == resource_id,
            ResourceStage.stage_id == stage_id,
        )
        .first()
    )


def create_resource_stage(db: Session, resource_id: int, stage_id: int) -> ResourceStage:
    """Привязать этап к участку.

    ``resource_id`` берётся из URL, ``stage_id`` из payload.
    Может выбросить ``IntegrityError`` (обрабатывается в роутере).
    """
    # Жёстко используем resource_id из URL, игнорируя поле из payload для предотвращения несоответствий
    db_resource_stage = ResourceStage(
        resource_id=resource_id,
        stage_id=stage_id,
    )
    db.add(db_resource_stage)
    db.commit()
    db.refresh(db_resource_stage)
    return db_resource_stage


def delete_resource_stage(db: Session, resource_stage: ResourceStage) -> None:
    """Удалить связь участок-этап."""
    db.delete(resource_stage)
    db.commit()


# --- Production kinds ---

def list_production_kinds(db: Session) -> List[ProductionKind]:
    """Все виды производства, отсортированные по имени."""
    return db.query(ProductionKind).order_by(ProductionKind.name.asc()).all()


def get_resource_production_kinds(db: Session, resource_id: int) -> List[ResourceProductionKind]:
    """Виды производства, привязанные к участку."""
    return (
        db.query(ResourceProductionKind)
        .filter(ResourceProductionKind.resource_id == resource_id)
        .all()
    )


def get_production_kind_by_id(db: Session, production_kind_id: int) -> Optional[ProductionKind]:
    """Найти вид производства по идентификатору (или None)."""
    return (
        db.query(ProductionKind)
        .filter(ProductionKind.id == production_kind_id)
        .first()
    )


def find_production_kind_assignment_global(
    db: Session, production_kind_id: int
) -> Optional[ResourceProductionKind]:
    """Найти любую привязку данного вида производства (глобально)."""
    return (
        db.query(ResourceProductionKind)
        .filter(ResourceProductionKind.production_kind_id == production_kind_id)
        .first()
    )


def find_resource_production_kind(
    db: Session, resource_id: int, production_kind_id: int
) -> Optional[ResourceProductionKind]:
    """Найти привязку вида производства к конкретному участку (или None)."""
    return (
        db.query(ResourceProductionKind)
        .filter(
            ResourceProductionKind.resource_id == resource_id,
            ResourceProductionKind.production_kind_id == production_kind_id,
        )
        .first()
    )


def create_resource_production_kind(
    db: Session, resource_id: int, production_kind_id: int
) -> ResourceProductionKind:
    """Привязать вид производства к участку.

    Может выбросить ``IntegrityError`` (обрабатывается в роутере).
    """
    rec = ResourceProductionKind(
        resource_id=resource_id,
        production_kind_id=production_kind_id,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def delete_resource_production_kind(db: Session, rec: ResourceProductionKind) -> None:
    """Удалить привязку вида производства к участку."""
    db.delete(rec)
    db.commit()
