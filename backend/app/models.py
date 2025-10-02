from sqlalchemy import Column, Integer, String, DECIMAL, TIMESTAMP, ForeignKey, TEXT, Boolean, DateTime, Date, CheckConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from sqlalchemy.dialects.postgresql import JSONB
from .database import Base


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
    replenishment_method = Column(String(50))
    replenishment_time = Column(Integer)
    unit = Column(String(50))
    stock_qty = Column(DECIMAL(10, 3), default=0.0)
    status = Column(String(20), default='active')
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


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
    parent = relationship("ItemCategory", remote_side=[category_id])
    children = relationship("ItemCategory")


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
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


class Operation(Base):
    __tablename__ = "operations"

    operation_id = Column(Integer, primary_key=True, index=True)
    operation_ref1c = Column(String(36), unique=True, index=True)
    operation_name = Column(String(255))
    time_norm = Column(DECIMAL(10, 4), default=0.0)
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


class ProductionOrder(Base):
    __tablename__ = "production_orders"

    order_id = Column(Integer, primary_key=True, index=True)
    order_number = Column(String(50), index=True)
    order_date = Column(DateTime, nullable=False)
    order_ref1c = Column(String(36), unique=True, index=True)
    is_posted = Column(Boolean, default=False)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


class ProductionProduct(Base):
    __tablename__ = "production_products"

    product_id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey('production_orders.order_id'), nullable=False)
    item_id = Column(Integer, ForeignKey('items.item_id'), nullable=False)
    quantity = Column(DECIMAL(10, 3), nullable=False)
    spec_id = Column(Integer, ForeignKey('specifications.spec_id'), nullable=True)
    stage_id = Column(Integer, ForeignKey('production_stages.stage_id'), nullable=True)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


class ProductionComponent(Base):
    __tablename__ = "production_components"

    component_id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey('production_orders.order_id'), nullable=False)
    item_id = Column(Integer, ForeignKey('items.item_id'), nullable=False)
    quantity = Column(DECIMAL(10, 3), nullable=False)
    spec_id = Column(Integer, ForeignKey('specifications.spec_id'), nullable=True)
    stage_id = Column(Integer, ForeignKey('production_stages.stage_id'), nullable=True)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


class ProductionOperation(Base):
    __tablename__ = "production_operations"

    operation_id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey('production_orders.order_id'), nullable=False)
    operation_id_ref = Column(Integer, ForeignKey('operations.operation_id'), nullable=False)
    planned_quantity = Column(DECIMAL(10, 3), default=0.0)
    time_norm = Column(DECIMAL(10, 4), default=0.0)
    standard_hours = Column(DECIMAL(10, 4), default=0.0)
    stage_id = Column(Integer, ForeignKey('production_stages.stage_id'), nullable=True)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


class SupplierOrder(Base):
    __tablename__ = "supplier_orders"

    order_id = Column(Integer, primary_key=True, index=True)
    order_number = Column(String(50), index=True)
    order_date = Column(DateTime, nullable=False)
    order_ref1c = Column(String(36), unique=True, index=True)
    supplier_id = Column(Integer, ForeignKey('suppliers.supplier_id'), nullable=True)
    document_amount = Column(DECIMAL(10, 2), default=0.0)
    is_posted = Column(Boolean, default=False)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


class Supplier(Base):
    __tablename__ = "suppliers"

    supplier_id = Column(Integer, primary_key=True, index=True)
    supplier_ref1c = Column(String(36), unique=True, index=True)
    supplier_name = Column(String(255))
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


class SupplierOrderItem(Base):
    __tablename__ = "supplier_order_items"

    item_id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey('supplier_orders.order_id'), nullable=False)
    item_id_ref = Column(Integer, ForeignKey('items.item_id'), nullable=False)
    quantity = Column(DECIMAL(10, 3), nullable=False)
    price = Column(DECIMAL(10, 2), default=0.0)
    amount = Column(DECIMAL(10, 2), default=0.0)
    delivery_date = Column(DateTime, nullable=True)
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


class ProductionPlanEntry(Base):
    __tablename__ = "production_plan_entries"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey('items.item_id'), nullable=False)
    stage_id = Column(Integer, ForeignKey('production_stages.stage_id'), nullable=True)
    date = Column(DateTime, nullable=False)
    planned_qty = Column(DECIMAL(10, 3), default=0.0)
    completed_qty = Column(DECIMAL(10, 3), default=0.0)
    status = Column(String(20), default='GREEN')  # GREEN, YELLOW, RED
    notes = Column(TEXT, nullable=True)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())

    # Связи
    item = relationship("Item")
    stage = relationship("ProductionStage")


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

# --- MRP planning module ORM models (synchronized with Alembic schema) ---

class PlanningConfigVersion(Base):
    __tablename__ = "planning_config_versions"

    id = Column(Integer, primary_key=True, index=True)
    version = Column(Integer, nullable=False)
    is_active = Column(Boolean, nullable=False, default=False)
    config = Column(JSONB, nullable=False)
    comment = Column(TEXT, nullable=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(TIMESTAMP, default=func.now())


class PlanningRun(Base):
    __tablename__ = "planning_run"

    run_id = Column(Integer, primary_key=True, index=True)
    started_at = Column(TIMESTAMP, default=func.now(), nullable=False)
    finished_at = Column(TIMESTAMP, nullable=True)
    status = Column(String(20), nullable=False, default="PENDING")
    started_by = Column(String(100), nullable=True)
    horizon_days = Column(Integer, nullable=True)
    use_weekly = Column(Boolean, nullable=False, default=True)
    # Флаг закрепления прогона от авто‑очистки
    pinned = Column(Boolean, nullable=False, default=False)
    config_version_id = Column(Integer, ForeignKey("planning_config_versions.id"), nullable=True)
    config_snapshot = Column(JSONB, nullable=False)
    warnings = Column(JSONB, nullable=True)
    kpi = Column(JSONB, nullable=True)


class PlannedOrder(Base):
    __tablename__ = "planned_order"
    __table_args__ = (
        CheckConstraint("bucket_type IN ('daily','weekly')", name="ck_planned_order_bucket_type"),
    )

    order_id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False)
    qty = Column(DECIMAL(15, 3), nullable=False)
    need_date = Column(Date, nullable=False)
    start_date = Column(Date, nullable=True)
    finish_date = Column(Date, nullable=True)
    route_ref = Column(String(255), nullable=True)
    priority_index = Column(DECIMAL(10, 4), nullable=True)
    bucket_type = Column(String(10), nullable=False)
    bucket_date = Column(Date, nullable=False)
    demand_ref = Column(TEXT, nullable=True)
    demand_date = Column(Date, nullable=True)


class PlannedOrderStage(Base):
    __tablename__ = "planned_order_stage"
    __table_args__ = (
        CheckConstraint("bucket_type IN ('daily','weekly')", name="ck_planned_order_stage_bucket_type"),
    )

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=False)
    order_id = Column(Integer, ForeignKey("planned_order.order_id", ondelete="CASCADE"), nullable=False)
    stage_id = Column(Integer, ForeignKey("production_stages.stage_id"), nullable=False)
    area_id = Column(Integer, ForeignKey("production_resources.resource_id"), nullable=True)
    bucket_type = Column(String(10), nullable=False)
    bucket_date = Column(Date, nullable=False)
    hours = Column(DECIMAL(12, 3), nullable=False, default=0.0)


class PlannedPurchase(Base):
    __tablename__ = "planned_purchase"
    __table_args__ = (
        CheckConstraint("bucket_type IN ('daily','weekly')", name="ck_planned_purchase_bucket_type"),
    )

    purchase_id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False)
    qty = Column(DECIMAL(15, 3), nullable=False)
    need_date = Column(Date, nullable=False)
    order_date = Column(Date, nullable=False)
    lead_time_days = Column(Integer, nullable=False)
    priority_index = Column(DECIMAL(10, 4), nullable=True)
    bucket_type = Column(String(10), nullable=False)
    bucket_date = Column(Date, nullable=False)
    supplier_ref1c = Column(String(255), nullable=True)


class CapacityLoad(Base):
    __tablename__ = "capacity_load"
    __table_args__ = (
        CheckConstraint("bucket_type IN ('daily','weekly')", name="ck_capacity_load_bucket_type"),
    )

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=False)
    area_id = Column(Integer, ForeignKey("production_resources.resource_id"), nullable=False)
    bucket_type = Column(String(10), nullable=False)
    bucket_date = Column(Date, nullable=False)
    hours_planned = Column(DECIMAL(12, 3), nullable=False, default=0.0)
    hours_available = Column(DECIMAL(12, 3), nullable=False, default=0.0)
    overload_hours = Column(DECIMAL(12, 3), nullable=False, default=0.0)


class PeggingLink(Base):
    __tablename__ = "pegging_link"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=False)
    child_item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False)
    parent_item_id = Column(Integer, ForeignKey("items.item_id"), nullable=True)
    demand_ref = Column(TEXT, nullable=True)
    qty_contribution = Column(DECIMAL(15, 3), nullable=False)
    need_date = Column(Date, nullable=True)
    parent_need_date = Column(Date, nullable=True)


class ProductionKind(Base):
    __tablename__ = "production_kinds"

    id = Column(Integer, primary_key=True, index=True)
    ref_1c = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False, index=True)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


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

