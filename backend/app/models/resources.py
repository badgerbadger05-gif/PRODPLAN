from sqlalchemy import Column, Integer, String, DECIMAL, TIMESTAMP, ForeignKey, TEXT, Boolean, DateTime, Date, CheckConstraint, JSON, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text
from ..database import Base
from .types import CrossPlatformJSON


class ProductionResource(Base):
    __tablename__ = "production_resources"

    resource_id = Column(Integer, primary_key=True, index=True)
    resource_name = Column(String(255), nullable=False)
    # Соответствие существующей БД: planning_offset -> shift_offset
    shift_offset = Column("planning_offset", Integer, default=0)  # Сдвиг планирования
    # Соответствие существующей БД: planning_horizon -> planning_range
    planning_range = Column("planning_horizon", Integer, default=30)  # Диапазон планирования в днях
    # В БД numeric(10,2)
    capacity = Column(DECIMAL(10, 2), default=0.0)  # Мощность
    # В БД varchar(100)
    work_schedule = Column(String(100), default='5/2')  # График работы
    # Соответствие существующей БД: work_hours_per_day -> daily_work_hours
    daily_work_hours = Column("work_hours_per_day", DECIMAL(4, 2), default=8.0)  # Рабочее время в часах в сутки
    # Буфер (дней) для расчёта базового количества запуска на участке
    buffer_days = Column(Integer, default=0, nullable=False)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


class ResourceStage(Base):
    __tablename__ = "resource_stages"

    id = Column(Integer, primary_key=True, index=True)
    resource_id = Column(Integer, ForeignKey('production_resources.resource_id'), nullable=False)
    stage_id = Column(Integer, ForeignKey('production_stages.stage_id'), nullable=False)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())

    # Связи
    resource = relationship("ProductionResource")
    stage = relationship("ProductionStage")


class ResourceProductionKind(Base):
    __tablename__ = "resource_production_kinds"

    id = Column(Integer, primary_key=True, index=True)
    resource_id = Column(Integer, ForeignKey('production_resources.resource_id'), nullable=False)
    production_kind_id = Column(Integer, ForeignKey('production_kinds.id'), nullable=False)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())

    # Связи
    resource = relationship("ProductionResource")
    production_kind = relationship("ProductionKind")


class WorkCalendarDay(Base):
    __tablename__ = "work_calendar_day"

    date = Column(Date, primary_key=True)
    is_workday = Column(Boolean, nullable=False, default=True)
    comment = Column(TEXT, nullable=True)
