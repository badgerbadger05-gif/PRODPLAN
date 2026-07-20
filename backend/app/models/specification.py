from sqlalchemy import Column, Integer, String, DECIMAL, TIMESTAMP, ForeignKey, TEXT, Boolean, DateTime, Date, CheckConstraint, JSON, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text
from ..database import Base
from .types import CrossPlatformJSON


class Specification(Base):
    __tablename__ = "specifications"

    spec_id = Column(Integer, primary_key=True, index=True)
    spec_code = Column(String(50), index=True)
    spec_name = Column(TEXT, nullable=False)
    spec_ref1c = Column(String(36), unique=True, index=True)
    # Новое поле для связи с видом производства
    production_kind_id = Column(Integer, ForeignKey('production_kinds.id'), nullable=True)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())

    # Связь с видом производства
    production_kind = relationship("ProductionKind")


class SpecComponent(Base):
    __tablename__ = "spec_components"

    component_id = Column(Integer, primary_key=True, index=True)
    spec_id = Column(Integer, ForeignKey('specifications.spec_id'), nullable=False)
    item_id = Column(Integer, ForeignKey('items.item_id'), nullable=False)
    quantity = Column(DECIMAL(10, 3), nullable=False)
    stage_id = Column(Integer, ForeignKey('production_stages.stage_id'), nullable=True)
    component_type = Column(String(50), default='Материал')  # Материал, Сборка
    # Закреплённая спецификация компонента (1С: Спецификации_Состав.Спецификация_Key).
    # Значима только для строк типа Сборка/Узел; именно её 1С подставляет в документы.
    # NULL/пусто = компонент идёт по основной спецификации номенклатуры.
    # Входит в естественный ключ строки состава: один и тот же компонент может
    # стоять в одной спецификации несколько раз с разными закреплёнными спеками.
    component_spec_ref1c = Column(String(36), nullable=True, index=True)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


class Operation(Base):
    __tablename__ = "operations"

    operation_id = Column(Integer, primary_key=True, index=True)
    operation_ref1c = Column(String(36), unique=True, index=True)
    operation_name = Column(String(255))
    time_norm = Column(DECIMAL(10, 4), default=0.0)
    operation_price = Column(DECIMAL(10, 2), default=0.0)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


class SpecOperation(Base):
    __tablename__ = "spec_operations"

    spec_operation_id = Column(Integer, primary_key=True, index=True)
    spec_id = Column(Integer, ForeignKey('specifications.spec_id'), nullable=False)
    operation_id = Column(Integer, ForeignKey('operations.operation_id'), nullable=False)
    stage_id = Column(Integer, ForeignKey('production_stages.stage_id'), nullable=True)
    time_norm = Column(DECIMAL(10, 4), default=0.0)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


class DefaultSpecification(Base):
    __tablename__ = "default_specifications"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey('items.item_id'), nullable=False)
    characteristic_id = Column(String(36), nullable=True)
    spec_id = Column(Integer, ForeignKey('specifications.spec_id'), nullable=False)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


class RootProduct(Base):
    __tablename__ = "root_products"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey('items.item_id'), nullable=False, unique=True)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())

    # Связь с изделием
    item = relationship("Item")
