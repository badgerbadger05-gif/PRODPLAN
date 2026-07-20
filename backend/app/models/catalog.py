from sqlalchemy import Column, Integer, String, DECIMAL, TIMESTAMP, ForeignKey, TEXT, Boolean, DateTime, Date, CheckConstraint, JSON, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text
from ..database import Base
from .types import CrossPlatformJSON


class ProductionStage(Base):
    __tablename__ = "production_stages"

    stage_id = Column(Integer, primary_key=True, index=True)
    stage_name = Column(String(255), unique=True, nullable=False)
    stage_order = Column(Integer)
    stage_ref1c = Column(String(36))
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


class Item(Base):
    __tablename__ = "items"

    item_id = Column(Integer, primary_key=True, index=True)
    item_code = Column(String(50), unique=True, nullable=False, index=True)
    item_name = Column(TEXT, nullable=False)
    item_article = Column(String(100), index=True)
    item_ref1c = Column(String(36), index=True)
    supplier_ref1c = Column(String(36), nullable=True, index=True)
    replenishment_method = Column(String(50))
    replenishment_time = Column(Integer)
    unit = Column(String(50))
    category_id = Column(Integer, ForeignKey('item_categories.category_id'), nullable=True, index=True)
    stock_qty = Column(DECIMAL(10, 3), default=0.0)
    # Опциональная оптимальная партия для лот‑сайзинга (шт)
    optimal_batch = Column(DECIMAL(15, 3), nullable=True)
    status = Column(String(20), default='active')
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())

    # Relationship для доступа к продукции в заказах
    category = relationship("ItemCategory", back_populates="items")
    production_products = relationship("ProductionProduct", back_populates="item")


class ItemCategory(Base):
    __tablename__ = "item_categories"

    category_id = Column(Integer, primary_key=True, index=True)
    category_code = Column(String(50), index=True)
    category_name = Column(String(255), nullable=False)
    category_ref1c = Column(String(36), unique=True, index=True)
    parent_id = Column(Integer, ForeignKey('item_categories.category_id'), nullable=True)
    is_folder = Column(Boolean, default=False)
    predefined = Column(Boolean, default=False)
    predefined_name = Column(String(100))
    data_version = Column(String(50))
    deletion_mark = Column(Boolean, default=False)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())

    # Связи
    parent = relationship("ItemCategory", remote_side=[category_id], back_populates="children")
    children = relationship("ItemCategory", back_populates="parent", overlaps="parent")
    items = relationship("Item", back_populates="category")


class StockWarehouse(Base):
    __tablename__ = "stock_warehouses"

    warehouse_id = Column(Integer, primary_key=True, index=True)
    warehouse_ref1c = Column(String(36), unique=True, nullable=False, index=True)
    warehouse_code = Column(String(50), nullable=True, index=True)
    warehouse_name = Column(String(255), nullable=False)
    is_selected = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


class ItemWarehouseStock(Base):
    """
    Per-(item, warehouse) stock breakdown synchronized from 1C OData. Lets
    coverage analysis exclude warehouses listed in `ignored_warehouses` and
    is a foundation for automatic source-warehouse selection during material
    issue creation. Primary key (item_id, warehouse_ref1c).
    """
    __tablename__ = "item_warehouse_stock"

    item_id = Column(
        Integer,
        ForeignKey("items.item_id", ondelete="CASCADE"),
        primary_key=True,
    )
    warehouse_ref1c = Column(String(36), primary_key=True, index=True)
    qty = Column(DECIMAL(15, 3), nullable=False, default=0, server_default="0")
    updated_at = Column(
        TIMESTAMP,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Unit(Base):
    __tablename__ = "units"

    unit_id = Column(Integer, primary_key=True, index=True)
    unit_ref1c = Column(String(36), unique=True, index=True)  # Ref_Key из 1С
    unit_code = Column(String(50), index=True)                # Code
    unit_name = Column(String(255), nullable=False)           # Description / Наименование (краткое)
    unit_full_name = Column(String(255))                      # Полное наименование, если есть
    short_name = Column(String(50))                           # Сокращение/краткое
    iso_code = Column(String(50))                             # Международное сокращение/код
    base_unit_ref1c = Column(String(36))                      # БазоваяЕдиница_Key
    ratio = Column(DECIMAL(18, 6), default=1.0)               # Коэффициент к базовой ЕИ
    precision = Column(Integer)                               # Точность (знаков)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


class Employee(Base):
    __tablename__ = "employees"

    employee_id = Column(Integer, primary_key=True, index=True)
    employee_ref1c = Column(String(36), unique=True, nullable=False, index=True)
    employee_type = Column(String(20), nullable=False, default="employee", server_default="employee", index=True)
    employee_code = Column(String(50), nullable=True, index=True)
    employee_name = Column(String(255), nullable=False)
    deletion_mark = Column(Boolean, nullable=False, default=False, server_default="false", index=True)
    data_version = Column(String(50), nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False)


class ProductionKind(Base):
    __tablename__ = "production_kinds"

    id = Column(Integer, primary_key=True, index=True)
    ref_1c = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False, index=True)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


class ItemEmbedding(Base):
    __tablename__ = "item_embeddings"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey('items.item_id'), nullable=False, unique=True)
    embedding_vector = Column(TEXT, nullable=False)  # JSON строка с вектором эмбеддинга
    model_name = Column(String(100), nullable=False, default='sentence-transformers/all-MiniLM-L6-v2')
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())

    # Связь с изделием
    item = relationship("Item")
