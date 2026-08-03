from decimal import Decimal

from sqlalchemy import Column, Integer, BigInteger, String, DECIMAL, TIMESTAMP, ForeignKey, TEXT, Boolean, DateTime, Date, CheckConstraint, JSON, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.types import TypeDecorator
from .database import Base

# Use JSON for SQLite, JSONB for others
class CrossPlatformJSON(TypeDecorator):
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == 'postgresql':
            return dialect.type_descriptor(JSONB())
        else:
            return dialect.type_descriptor(JSON())

@compiles(CrossPlatformJSON, 'postgresql')
def compile_jsonb(element, compiler, **kw):
    return "JSONB"


# BIGSERIAL on Postgres, but SQLite only autoincrements INTEGER PRIMARY KEY —
# so ledger primary keys resolve to INTEGER under SQLite (test DB) and bigint
# under Postgres. Used by the item-ledger append-only tables.
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


class PhysicalImportBatch(Base):
    """One immutable import boundary for shared physical Ledger facts."""
    __tablename__ = "physical_import_batch"
    __table_args__ = (
        CheckConstraint(
            "status IN ('building', 'completed', 'rejected')",
            name="ck_physical_import_batch_status",
        ),
        UniqueConstraint("batch_key", name="uq_physical_import_batch_key"),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    batch_key = Column(String(128), nullable=False)
    status = Column(String(16), nullable=False, server_default="building")
    cutoff = Column(DateTime(timezone=True), nullable=True)
    source_watermarks = Column(CrossPlatformJSON, nullable=False, default=dict)
    reason = Column(TEXT, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)


class LedgerGeneration(Base):
    """A versioned build of the Item Ledger fact set.

    A generation is not planning truth until its status is ``accepted`` and
    ``PlanningTruthState`` points at it.
    """
    __tablename__ = "ledger_generation"
    __table_args__ = (
        CheckConstraint(
            "status IN ('uninitialized', 'building', 'accepted', 'stale', 'rejected')",
            name="ck_ledger_generation_status",
        ),
        UniqueConstraint("generation_key", name="uq_ledger_generation_key"),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    generation_key = Column(String(128), nullable=False)
    status = Column(String(16), nullable=False, server_default="building")
    cutoff = Column(DateTime(timezone=True), nullable=True)
    source_watermarks = Column(CrossPlatformJSON, nullable=False, default=dict)
    capabilities = Column(CrossPlatformJSON, nullable=False, default=dict)
    physical_import_batch_id = Column(
        BigInteger,
        ForeignKey("physical_import_batch.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    algorithm_version = Column(String(128), nullable=False)
    replay_version = Column(String(128), nullable=True)
    reason = Column(TEXT, nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    physical_import_batch = relationship("PhysicalImportBatch")


class LedgerBuildBatch(Base):
    """Auditable stage execution while constructing one Ledger generation."""
    __tablename__ = "ledger_build_batch"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('physical_import', 'reservation_replay', "
            "'reservation_materialize', "
            "'execution_allocation', 'assembly_output_allocation', "
            "'replenishment_work_item', 'future_supply_capture', "
            "'snapshot_build', 'drum_schedule', 'shelf_projection')",
            name="ck_ledger_build_batch_stage",
        ),
        CheckConstraint(
            "status IN ('building', 'completed', 'rejected')",
            name="ck_ledger_build_batch_status",
        ),
        UniqueConstraint(
            "ledger_generation_id", "stage", "batch_key",
            name="uq_ledger_build_batch_generation_stage_key",
        ),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    stage = Column(String(32), nullable=False)
    batch_key = Column(String(128), nullable=False)
    status = Column(String(16), nullable=False, server_default="building")
    algorithm_version = Column(String(128), nullable=False)
    metrics = Column(CrossPlatformJSON, nullable=False, default=dict)
    reason = Column(TEXT, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    ledger_generation = relationship("LedgerGeneration")

class LedgerFutureSupply(Base):
    """Immutable, generation-scoped snapshot of supply available after cutoff.

    This is deliberately a captured source fact, not an MRP proposal.  Future
    WIP and supplier-order coverage therefore remains auditable against the
    precise Ledger generation which consumed it.
    """

    __tablename__ = "ledger_future_supply"
    __table_args__ = (
        UniqueConstraint(
            "ledger_generation_id",
            "supply_kind",
            "source_ref",
            "source_line_ref",
            name="uq_ledger_future_supply_generation_source_line",
        ),
        CheckConstraint(
            "supply_kind IN ('wip_order', 'supplier_order')",
            name="ck_ledger_future_supply_kind",
        ),
        CheckConstraint(
            "evidence_status IN ('exact', 'ambiguous', 'unmatched', 'rejected')",
            name="ck_ledger_future_supply_evidence_status",
        ),
        CheckConstraint(
            "ordered_qty_at_cutoff >= 0 AND realized_qty_at_cutoff >= 0 "
            "AND open_qty_at_cutoff >= 0",
            name="ck_ledger_future_supply_quantities_nonnegative",
        ),
        CheckConstraint(
            "capture_cutoff IS NOT NULL",
            name="ck_ledger_future_supply_capture_cutoff",
        ),
        Index(
            "ix_ledger_future_supply_generation_kind_item_eta",
            "ledger_generation_id", "supply_kind", "item_id", "eta_date",
        ),
        Index(
            "ix_ledger_future_supply_generation_item_eta",
            "ledger_generation_id", "item_id", "eta_date",
        ),
        Index(
            "ix_ledger_future_supply_source_requirement_id",
            "source_requirement_id",
        ),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    supply_kind = Column(String(32), nullable=False)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    characteristic_ref = Column(String(36), nullable=False, server_default="")
    organization_ref = Column(String(36), nullable=False, server_default="")
    planning_stock_pool = Column(String(128), nullable=False)
    destination_warehouse_ref1c = Column(String(36), nullable=False, server_default="")
    # Old 1C documents occasionally do not retain every external identity.
    # Later capture services may accept an exact row only when its required
    # identity evidence is actually present.
    source_ref = Column(String(64), nullable=True)
    source_line_ref = Column(String(64), nullable=True)
    source_local_id = Column(String(128), nullable=True)
    source_requirement_id = Column(
        Integer,
        ForeignKey("mrp_requirement.id", ondelete="RESTRICT"),
        nullable=True,
    )
    ordered_qty_at_cutoff = Column(DECIMAL(15, 3), nullable=False)
    realized_qty_at_cutoff = Column(DECIMAL(15, 3), nullable=False)
    open_qty_at_cutoff = Column(DECIMAL(15, 3), nullable=False)
    eta_date = Column(Date, nullable=True)
    source_state_key = Column(String(64), nullable=False)
    source_updated_at = Column(DateTime(timezone=True), nullable=True)
    capture_cutoff = Column(DateTime(timezone=True), nullable=False)
    source_content_hash = Column(String(64), nullable=False)
    capture_batch_id = Column(
        BigInteger,
        ForeignKey("ledger_build_batch.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    evidence_status = Column(String(16), nullable=False)
    reason = Column(TEXT, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    ledger_generation = relationship("LedgerGeneration")
    item = relationship("Item")
    capture_batch = relationship("LedgerBuildBatch")


class PlanningTruthState(Base):
    """Singleton pointer to the generation exposed to dependent calculations."""
    __tablename__ = "planning_truth_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_planning_truth_state_singleton"),
    )

    id = Column(Integer, primary_key=True, default=1)
    current_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    current_generation = relationship("LedgerGeneration")


class PlanningReadSnapshot(Base):
    """Immutable payload published for a UI/report consumer."""
    __tablename__ = "planning_read_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "consumer", "snapshot_key", "ledger_generation_id",
            name="uq_planning_read_snapshot_consumer_key_generation",
        ),
        Index(
            "ix_planning_read_snapshot_latest",
            "consumer", "ledger_generation_id", "published_at",
        ),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    consumer = Column(String(128), nullable=False)
    snapshot_key = Column(String(256), nullable=False)
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cutoff = Column(DateTime(timezone=True), nullable=False)
    truth_status = Column(String(16), nullable=False)
    payload = Column(CrossPlatformJSON, nullable=False)
    reason = Column(TEXT, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    published_at = Column(DateTime(timezone=True), nullable=False)

    ledger_generation = relationship("LedgerGeneration")
    rows = relationship(
        "PlanningReadRow",
        back_populates="snapshot",
        cascade="all, delete-orphan",
    )
    root_members = relationship(
        "PlanningReadRootMember",
        back_populates="snapshot",
        cascade="all, delete-orphan",
    )


class ClosedPlanSnapshot(Base):
    """Immutable execution payload captured at explicit plan closure."""

    __tablename__ = "closed_plan_snapshot"
    __table_args__ = (
        UniqueConstraint("plan_id", "run_id", name="uq_closed_plan_snapshot_plan_run"),
        Index("ix_closed_plan_snapshot_plan", "plan_id"),
        Index("ix_closed_plan_snapshot_run", "run_id"),
        Index("ix_closed_plan_snapshot_generation", "ledger_generation_id"),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    plan_id = Column(
        Integer,
        ForeignKey("production_plan_header.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    run_id = Column(
        BigInteger,
        ForeignKey("planning_run.run_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    cutoff = Column(DateTime(timezone=True), nullable=False)
    payload = Column(CrossPlatformJSON, nullable=False, default=dict)
    closed_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    plan = relationship("ProductionPlanHeader")
    run = relationship("PlanningRun")
    ledger_generation = relationship("LedgerGeneration")


class PlanningReadRow(Base):
    """Generic immutable row belonging to one planning read snapshot."""

    __tablename__ = "planning_read_row"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "row_key",
            name="uq_planning_read_row_snapshot_key",
        ),
        Index(
            "ix_planning_read_row_snapshot_kind",
            "snapshot_id", "row_kind",
        ),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    snapshot_id = Column(
        BigInteger,
        ForeignKey("planning_read_snapshot.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    row_key = Column(String(256), nullable=False)
    row_kind = Column(String(64), nullable=False, server_default="")
    item_id = Column(
        Integer,
        ForeignKey("items.item_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sort_key = Column(String(256), nullable=True)
    payload = Column(CrossPlatformJSON, nullable=False, default=dict)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    snapshot = relationship("PlanningReadSnapshot", back_populates="rows")
    item = relationship("Item")
    root_members = relationship(
        "PlanningReadRootMember",
        back_populates="row",
        cascade="all, delete-orphan",
    )


class PlanningReadRootMember(Base):
    """Generic row-to-root membership for hierarchy and root filters."""

    __tablename__ = "planning_read_root_member"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "row_id", "root_key",
            name="uq_planning_read_root_member",
        ),
        Index(
            "ix_planning_read_root_member_snapshot_root",
            "snapshot_id", "root_key",
        ),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    snapshot_id = Column(
        BigInteger,
        ForeignKey("planning_read_snapshot.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    row_id = Column(
        BigInteger,
        ForeignKey("planning_read_row.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    root_key = Column(String(256), nullable=False)
    root_item_id = Column(
        Integer,
        ForeignKey("items.item_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    payload = Column(CrossPlatformJSON, nullable=False, default=dict)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    snapshot = relationship(
        "PlanningReadSnapshot", back_populates="root_members"
    )
    row = relationship("PlanningReadRow", back_populates="root_members")
    root_item = relationship("Item")


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
    # Item-ledger : third warehouse-policy label. A finished-goods warehouse
    # is a legitimate 1С warehouse whose SLE are mirrored and whose bin is kept,
    # but the planning pool never sums it (on_hand(P) excludes it) — finished
    # goods are produced straight onto it, outside the planning contour. Additive
    # in : no reader consults it yet (pool exclusion lands with ingest, ).
    is_finished_goods = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


class ProcessingContractorStock(Base):
    """Current 1C balance of customer-owned stock held by a processor."""
    __tablename__ = "processing_contractor_stock"
    __table_args__ = (
        UniqueConstraint(
            "item_id",
            "contractor_ref1c",
            "order_ref1c",
            "order_type",
            "transfer_type",
            name="uq_processing_contractor_stock_axis",
        ),
        CheckConstraint("qty >= 0", name="ck_processing_contractor_stock_qty_nonnegative"),
    )

    id = Column(Integer, primary_key=True)
    item_id = Column(
        Integer,
        ForeignKey("items.item_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contractor_ref1c = Column(String(36), nullable=False, default="", server_default="", index=True)
    order_ref1c = Column(String(36), nullable=False, default="", server_default="", index=True)
    order_type = Column(String(255), nullable=False, default="", server_default="")
    transfer_type = Column(String(255), nullable=False, default="", server_default="")
    qty = Column(DECIMAL(15, 3), nullable=False, default=0, server_default="0")
    synced_at = Column(TIMESTAMP, nullable=False, default=func.now(), server_default=func.now())


class ProcessingStockSyncState(Base):
    """One-row health record for the processing-stock snapshot."""
    __tablename__ = "processing_stock_sync_state"

    id = Column(Integer, primary_key=True, default=1)
    status = Column(String(20), nullable=False, default="never", server_default="never")
    last_attempt_at = Column(TIMESTAMP, nullable=True)
    last_success_at = Column(TIMESTAMP, nullable=True)
    rows_seen = Column(Integer, nullable=False, default=0, server_default="0")
    rows_stored = Column(Integer, nullable=False, default=0, server_default="0")
    unmatched_items = Column(Integer, nullable=False, default=0, server_default="0")
    last_error = Column(TEXT, nullable=True)


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
    spec_id = Column(
        Integer,
        ForeignKey('specifications.spec_id'),
        nullable=False,
        index=True,
    )
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


class ProductionOrder(Base):
    __tablename__ = "production_orders"

    order_id = Column(Integer, primary_key=True, index=True)
    order_number = Column(String(50), index=True)
    order_date = Column(DateTime, nullable=False)
    order_ref1c = Column(String(36), unique=True, index=True)
    is_posted = Column(Boolean, default=False)
    # 1C order state (we will use it later to filter active vs done)
    order_state_key = Column(String(36), nullable=True, index=True)
    order_state_name = Column(String(255), nullable=True)
    deletion_mark = Column(Boolean, default=False, nullable=False, index=True)
    # Source tagging: distinguishes internally created MRP orders ('mrp') from
    # 1C-synced ones ('1c'). source_run_id ties an internal order back to the
    # planning_run it was generated from.
    source = Column(String(16), nullable=False, default="1c", server_default="1c", index=True)
    source_run_id = Column(Integer, ForeignKey('planning_run.run_id', ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())

    # Relationship для загрузки продукции заказа
    products = relationship("ProductionProduct", back_populates="order", lazy="select")


class ProductionProduct(Base):
    __tablename__ = "production_products"

    __table_args__ = (
        UniqueConstraint("order_id", "line_number", name="ux_production_products_order_line"),
        # Partial unique index (NULLs not constrained) enforced at the DB layer
        # via migration 20260520_01. Declared here for ORM autogenerate parity.
        Index(
            "ux_production_products_source_planned_order",
            "source_planned_order_id",
            unique=True,
            postgresql_where=text("source_planned_order_id IS NOT NULL"),
            sqlite_where=text("source_planned_order_id IS NOT NULL"),
        ),
    )

    product_id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey('production_orders.order_id'), nullable=False)
    item_id = Column(Integer, ForeignKey('items.item_id'), nullable=False)
    # 1C line normalization
    line_number = Column(Integer, nullable=True, index=True)
    characteristic_ref1c = Column(String(36), nullable=True)
    destination_warehouse_ref1c = Column(String(36), nullable=True, index=True)
    quantity = Column(DECIMAL(10, 3), nullable=False)
    # Fact tracking (from 1C Assembly/Сборка запасов)
    produced_qty = Column(DECIMAL(10, 3), default=0.0, nullable=False)
    remaining_qty = Column(DECIMAL(10, 3), nullable=False)
    spec_id = Column(Integer, ForeignKey('specifications.spec_id'), nullable=True)
    stage_id = Column(Integer, ForeignKey('production_stages.stage_id'), nullable=True)
    # When this line was generated from MRP (rather than 1C-synced), points to
    # the planned_order row it came from. NULL for 1C-synced lines. Partial
    # unique index above prevents the same planned_order being duplicated.
    source_planned_order_id = Column(
        Integer,
        ForeignKey('planned_order.order_id', ondelete="SET NULL"),
        nullable=True,
    )
    # When this line was generated from a period-plan MRP snapshot, points to
    # the mrp_requirement row it satisfies. NULL for 1C-synced and legacy-MRP
    # planned_order lines. Added by migration 20260522_06.
    source_mrp_requirement_id = Column(
        Integer,
        ForeignKey('mrp_requirement.id', ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Opaque idempotency key for period-plan order allocation. Added by
    # migration 20260522_06.
    source_mrp_allocation_key = Column(String(100), nullable=True, index=True)
    # Generation that owned creation of this planning proposal. NULL is kept
    # only for migrated/legacy and unrelated writers; Ledger readers must
    # always filter by an explicit generation id.
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())

    # Relationship для обратного доступа к заказу
    order = relationship("ProductionOrder", back_populates="products")
    item = relationship("Item", back_populates="production_products")
    control_state = relationship("ProductionOrderLineState", back_populates="product", uselist=False)


class ProductionOrderLineState(Base):
    __tablename__ = "production_order_line_states"
    __table_args__ = (
        UniqueConstraint("product_id", name="ux_production_order_line_states_product"),
    )

    state_id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("production_products.product_id", ondelete="CASCADE"), nullable=False)
    # Production journal status set. Legacy technical values are still accepted
    # and mapped by the service layer to the compact workshop-facing labels.
    status = Column(String(32), nullable=False, default="shortage", server_default="shortage", index=True)
    workshop_id = Column(Integer, ForeignKey("production_resources.resource_id"), nullable=True, index=True)
    workshop_id_source = Column(String(16), nullable=True, index=True)
    workshop_id_set_at = Column(TIMESTAMP, nullable=True)
    planned_start_date = Column(Date, nullable=True, index=True)
    planned_finish_date = Column(Date, nullable=True, index=True)
    opened_at = Column(TIMESTAMP, nullable=True)
    route_sheet_printed_at = Column(TIMESTAMP, nullable=True)
    issue_status = Column(String(32), nullable=False, default="not_requested", index=True)
    comment = Column(TEXT, nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now(), nullable=False)

    product = relationship("ProductionProduct", back_populates="control_state")
    workshop = relationship("ProductionResource")


class WorkshopWarehouseBinding(Base):
    """
    Plan rule: workshop warehouse settings. One workshop maps to at most one
    settings row (enforced by UNIQUE on workshop_id).

    warehouse_ref1c is the workshop warehouse used as material transfer
    destination and production-order reserve warehouse. production_warehouse_ref1c
    is the finished-product recipient used in 1C production orders.
    """
    __tablename__ = "workshop_warehouse_bindings"
    __table_args__ = (
        UniqueConstraint("workshop_id", name="ux_workshop_warehouse_bindings_workshop"),
    )

    binding_id = Column(Integer, primary_key=True, index=True)
    workshop_id = Column(
        Integer,
        ForeignKey("production_resources.resource_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    warehouse_ref1c = Column(String(36), nullable=False, index=True)
    production_warehouse_ref1c = Column(String(36), nullable=True, index=True)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False)

    workshop = relationship("ProductionResource")


class IgnoredWarehouse(Base):
    """
    Plan rule: "список игнорируемых складов" — чтобы не задавать лишние
    вопросы по остаткам, например если компонент лежит в изоляторе брака.
    """
    __tablename__ = "ignored_warehouses"

    warehouse_ref1c = Column(String(36), primary_key=True, nullable=False)
    warehouse_name = Column(String(255), nullable=True)
    reason = Column(TEXT, nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)


class ProductionMaterialIssue(Base):
    __tablename__ = "production_material_issues"
    __table_args__ = (
        # At most one ACTIVE (draft|requested) outgoing material issue per
        # production line and source warehouse. A line can legitimately need
        # several outgoing transfers when its components are stored in
        # different warehouses.
        Index(
            "ux_production_material_issues_active_per_product",
            "product_id",
            "source_warehouse_ref1c",
            unique=True,
            postgresql_where=text(
                "status IN ('draft', 'requested') AND direction = 'issue'"
            ),
            sqlite_where=text(
                "status IN ('draft', 'requested') AND direction = 'issue'"
            ),
        ),
    )

    issue_id = Column(Integer, primary_key=True, index=True)
    document_number = Column(String(50), nullable=False, unique=True, index=True)
    product_id = Column(Integer, ForeignKey("production_products.product_id", ondelete="CASCADE"), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey("production_orders.order_id"), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="draft", index=True)
    # 'issue'  — outgoing transfer source -> workshop (the original use case).
    # 'return' — workshop -> source, для leftover-компонентов при частичном
    # выпуске. Both are emitted as Document_ПеремещениеЗапасов with the
    # warehouse columns interpreted at face value.
    direction = Column(
        String(16), nullable=False, default="issue", server_default="issue"
    )
    # For direction='issue': destination warehouse (workshop's bound warehouse).
    # For direction='return': original source warehouse (where leftover goes back).
    # Used as "СкладПолучатель_Key" in the 1C payload.
    warehouse_ref1c = Column(String(36), nullable=True, index=True)
    # For direction='issue': source warehouse (where materials sit).
    # For direction='return': workshop warehouse (where leftover currently is).
    # Used as "СкладОтправитель_Key".
    source_warehouse_ref1c = Column(String(36), nullable=True, index=True)
    # Nullable only for historical documents created before the Item Ledger
    # boundary. New operational issues are always pinned to accepted truth.
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    initiated_by = Column(String(100), nullable=True)
    exported_ref1c = Column(String(36), nullable=True, index=True)
    exported_at = Column(TIMESTAMP, nullable=True)
    export_error = Column(TEXT, nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now(), nullable=False)

    product = relationship("ProductionProduct")
    order = relationship("ProductionOrder")
    ledger_generation = relationship("LedgerGeneration")
    lines = relationship("ProductionMaterialIssueLine", back_populates="issue", cascade="all, delete-orphan")


class ProductionMaterialIssueLine(Base):
    __tablename__ = "production_material_issue_lines"

    line_id = Column(Integer, primary_key=True, index=True)
    issue_id = Column(Integer, ForeignKey("production_material_issues.issue_id", ondelete="CASCADE"), nullable=False)
    component_item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    required_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0)
    issued_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0)
    unit = Column(String(50), nullable=True)
    source_spec_id = Column(Integer, ForeignKey("specifications.spec_id"), nullable=True)
    line_status = Column(String(32), nullable=False, default="planned", index=True)
    custody_event_revision = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(TIMESTAMP, default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now(), nullable=False)

    issue = relationship("ProductionMaterialIssue", back_populates="lines")
    component_item = relationship("Item")


class ProductionMaterialCustodyEvent(Base):
    __tablename__ = "production_material_custody_event"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="ux_production_material_custody_event_idempotency",
        ),
        CheckConstraint(
            "location_kind IN ('transit', 'workshop')",
            name="ck_production_material_custody_event_location",
        ),
        CheckConstraint(
            "source_kind IN ('baseline', 'issue_created', 'transfer_posted', 'transfer_returned', 'consumed', 'terminal_release')",
            name="ck_production_material_custody_event_source_kind",
        ),
        CheckConstraint(
            "delta_qty != 0",
            name="ck_production_material_custody_event_nonzero_delta",
        ),
        Index(
            "ix_production_material_custody_event_effective",
            "effective_at",
            "id",
        ),
        Index("ix_production_material_custody_event_product", "product_id", "component_item_id"),
        Index("ix_production_material_custody_event_idempotency", "idempotency_key"),
    )

    id = Column(BigIntPK, primary_key=True, index=True)
    issue_id = Column(
        Integer,
        ForeignKey("production_material_issues.issue_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    product_id = Column(
        Integer,
        ForeignKey("production_products.product_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    component_item_id = Column(
        Integer,
        ForeignKey("items.item_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_kind = Column(
        String(32), nullable=False, default="issue_created", server_default="issue_created"
    )
    source_sle_id = Column(
        BigInteger,
        nullable=True,
        index=True,
    )
    effective_at = Column(TIMESTAMP, nullable=False, index=True)
    location_kind = Column(
        String(16), nullable=False, default="transit", server_default="transit"
    )
    warehouse_ref1c = Column(String(36), nullable=False)
    source_ref1c = Column(String(36), nullable=True)
    source_ref2c = Column(String(64), nullable=True)
    delta_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    idempotency_key = Column(String(140), nullable=False)
    document_number = Column(String(64), nullable=True)
    document_line_no = Column(String(16), nullable=True)
    created_at = Column(
        TIMESTAMP,
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )

    issue = relationship("ProductionMaterialIssue")
    product = relationship("ProductionProduct")
    component_item = relationship("Item")


class ProductionMaterialCustodyProjectionManifest(Base):
    __tablename__ = "production_material_custody_projection_manifest"
    __table_args__ = (
        CheckConstraint(
            "status IN ('complete', 'building')",
            name="ck_production_material_custody_projection_manifest_status",
        ),
        Index(
            "ix_production_material_custody_projection_manifest_cutoff",
            "cutoff",
        ),
    )

    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    baseline_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    cutoff = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(16), nullable=False, default="building", server_default="building")
    is_baseline = Column(Boolean, nullable=False, default=False, server_default="false")
    source_event_high_watermark_id = Column(BigInteger, nullable=False, default=0)
    observed_at = Column(
        TIMESTAMP,
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )
    built_at = Column(
        TIMESTAMP,
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )

    ledger_generation = relationship("LedgerGeneration", foreign_keys=[ledger_generation_id])


class ProductionMaterialCustodyProjection(Base):
    __tablename__ = "production_material_custody_projection"
    __table_args__ = (
        UniqueConstraint(
            "ledger_generation_id", "product_id", "component_item_id", "location_kind", "warehouse_ref1c",
            name="ux_production_material_custody_projection_cell",
        ),
        CheckConstraint(
            "location_kind IN ('transit', 'workshop')",
            name="ck_production_material_custody_projection_location",
        ),
        CheckConstraint(
            "reserved_qty >= 0",
            name="ck_production_material_custody_projection_qty_nonnegative",
        ),
        CheckConstraint(
            "source_event_high_watermark_id >= 0",
            name="ck_pm_custody_projection_event_hwm_nonnegative",
        ),
        Index(
            "ix_production_material_custody_projection_generation_product",
            "ledger_generation_id",
            "product_id",
        ),
    )

    id = Column(BigIntPK, primary_key=True, index=True)
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    product_id = Column(
        Integer,
        ForeignKey("production_products.product_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    component_item_id = Column(
        Integer,
        ForeignKey("items.item_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    location_kind = Column(
        String(16), nullable=False, default="workshop", server_default="workshop"
    )
    warehouse_ref1c = Column(String(36), nullable=False)
    reserved_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    source_event_high_watermark_id = Column(BigInteger, nullable=False, default=0)
    built_at = Column(
        TIMESTAMP,
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )

    product = relationship("ProductionProduct")
    component_item = relationship("Item")
    ledger_generation = relationship("LedgerGeneration")


class ProductionManufacture(Base):
    """
    A record of one "Произвести" click on a production_products line. Local
    counterpart of 1C Document_СборкаЗапасов. Multiple manufactures per
    product line are allowed (partial production across shifts/days).
    """
    __tablename__ = "production_manufactures"

    manufacture_id = Column(Integer, primary_key=True, index=True)
    product_id = Column(
        Integer,
        ForeignKey("production_products.product_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    order_id = Column(
        Integer,
        ForeignKey("production_orders.order_id"),
        nullable=False,
        index=True,
    )
    qty = Column(DECIMAL(15, 3), nullable=False)
    executor = Column(String(100), nullable=True)
    comment = Column(TEXT, nullable=True)
    # draft -> exported (1C document created) -> cancelled (admin reversal)
    status = Column(
        String(32), nullable=False, default="draft", server_default="draft", index=True
    )
    exported_ref1c = Column(String(36), nullable=True, index=True)
    exported_at = Column(TIMESTAMP, nullable=True)
    export_error = Column(TEXT, nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False)

    product = relationship("ProductionProduct")
    order = relationship("ProductionOrder")
    operations = relationship(
        "ProductionManufactureOperation",
        back_populates="manufacture",
        cascade="all, delete-orphan",
    )


class ProductionManufactureOperation(Base):
    """
    Per-operation executor selected for a ProductionManufacture.
    Drives Document_СдельныйНаряд with ПоложениеИсполнителя=ВТабличнойЧасти.
    """
    __tablename__ = "production_manufacture_operations"

    id = Column(Integer, primary_key=True, index=True)
    manufacture_id = Column(
        Integer,
        ForeignKey("production_manufactures.manufacture_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    spec_operation_id = Column(Integer, ForeignKey("spec_operations.spec_operation_id"), nullable=True, index=True)
    operation_id = Column(Integer, ForeignKey("operations.operation_id"), nullable=False, index=True)
    line_number = Column(Integer, nullable=False)
    employee_ref1c = Column(String(36), nullable=False, index=True)
    employee_name = Column(String(255), nullable=False)
    employee_type = Column(String(20), nullable=False, default="employee", server_default="employee")
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False)

    manufacture = relationship("ProductionManufacture", back_populates="operations")
    spec_operation = relationship("SpecOperation")
    operation = relationship("Operation")


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
    operation_key = Column(String(36), nullable=True, index=True)
    operation_name = Column(String(100), nullable=True, index=True)
    processing_transfer_date = Column(DateTime, nullable=True)
    processing_report_date = Column(DateTime, nullable=True)
    order_state_key = Column(String(36), nullable=True, index=True)
    order_state_name = Column(String(255), nullable=True)
    deletion_mark = Column(Boolean, default=False, nullable=False, index=True)
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
    line_number = Column(Integer, nullable=True, index=True)
    characteristic_ref1c = Column(String(36), nullable=True)
    destination_warehouse_ref1c = Column(String(36), nullable=True, index=True)
    quantity = Column(DECIMAL(10, 3), nullable=False)
    received_qty = Column(DECIMAL(10, 3), default=0.0, nullable=False)
    remaining_qty = Column(DECIMAL(10, 3), nullable=False)
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


# ===== Weekly production report: day close + global work calendar =====


class WorkCalendarDay(Base):
    __tablename__ = "work_calendar_day"

    date = Column(Date, primary_key=True)
    is_workday = Column(Boolean, nullable=False, default=True)
    comment = Column(TEXT, nullable=True)


class ProductionDayClose(Base):
    __tablename__ = "production_day_close"
    __table_args__ = (
        UniqueConstraint("close_date", name="ux_production_day_close_close_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    close_date = Column(Date, nullable=False, index=True)
    status = Column(String(20), nullable=False, default="OPEN")  # OPEN | CLOSED
    target_date = Column(Date, nullable=True)
    closed_at = Column(TIMESTAMP, nullable=True)
    closed_by = Column(String(100), nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now(), nullable=False)


class ProductionDayCloseItem(Base):
    __tablename__ = "production_day_close_item"
    __table_args__ = (
        UniqueConstraint("day_close_id", "item_id", name="ux_production_day_close_item_day_item"),
    )

    id = Column(Integer, primary_key=True, index=True)
    day_close_id = Column(Integer, ForeignKey("production_day_close.id", ondelete="CASCADE"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False)

    planned_qty_snapshot = Column(DECIMAL(15, 3), nullable=False, default=0.0)
    fact_qty_snapshot = Column(DECIMAL(15, 3), nullable=False, default=0.0)
    carry_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0)
    applied_to_date = Column(Date, nullable=True)
    # Additional fields for improved carry tracking
    original_planned_qty_before_carry = Column(DECIMAL(15, 3), nullable=True)
    planned_qty_after_carry = Column(DECIMAL(15, 3), nullable=True)
    carry_status = Column(String(20), nullable=True)

    # Связи
    day_close = relationship("ProductionDayClose")
    item = relationship("Item")


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
    config = Column(CrossPlatformJSON, nullable=False)
    comment = Column(TEXT, nullable=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(TIMESTAMP, default=func.now())


class ProductionPlanHeader(Base):
    __tablename__ = "production_plan_header"
    __table_args__ = (
        CheckConstraint("period_to >= period_from", name="ck_production_plan_header_period"),
        CheckConstraint(
            "status in ('draft', 'fixed', 'closed')",
            name="ck_production_plan_header_status",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    period_from = Column(Date, nullable=False, index=True)
    period_to = Column(Date, nullable=False, index=True)
    status = Column(String(20), nullable=False, default="draft", index=True)
    comment = Column(TEXT, nullable=True)
    created_by = Column(String(100), nullable=True)
    fixed_by = Column(String(100), nullable=True)
    fixed_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now(), nullable=False)

    lines = relationship("ProductionPlanLine", back_populates="plan", cascade="all, delete-orphan")


class ProductionPlanLine(Base):
    __tablename__ = "production_plan_line"
    __table_args__ = (
        UniqueConstraint("plan_id", "item_id", "bucket_date", name="ux_production_plan_line_plan_item_bucket"),
    )

    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("production_plan_header.id", ondelete="CASCADE"), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    bucket_date = Column(Date, nullable=False, index=True)
    qty = Column(DECIMAL(15, 3), nullable=False, default=0.0)
    locked_by_run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(TIMESTAMP, default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now(), nullable=False)

    plan = relationship("ProductionPlanHeader", back_populates="lines")
    item = relationship("Item")
    locked_by_run = relationship("PlanningRun", foreign_keys=[locked_by_run_id])


class PlanningRun(Base):
    __tablename__ = "planning_run"
    # A source plan may have only one *open candidate* for one immutable Ledger
    # generation. Historical FIXED/SUPERSEDED rows intentionally retain the
    # same lineage, so they must not participate in this identity. Both
    # columns also stay nullable for legacy rows.
    __table_args__ = (
        # A non-null plan must never have more than one published fixed snapshot
        # across all ledger generations.
        Index(
            "uq_planning_run_fixed_snapshot_source_plan",
            "source_plan_id",
            unique=True,
            postgresql_where=text(
                "status = 'FIXED_SNAPSHOT' AND source_plan_id IS NOT NULL"
            ),
            sqlite_where=text(
                "status = 'FIXED_SNAPSHOT' AND source_plan_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_planning_run_generation_source_plan",
            "ledger_generation_id",
            "source_plan_id",
            unique=True,
            postgresql_where=text(
                "status = 'BUILDING_SNAPSHOT' AND ledger_generation_id IS NOT NULL "
                "AND source_plan_id IS NOT NULL"
            ),
            sqlite_where=text(
                "status = 'BUILDING_SNAPSHOT' AND ledger_generation_id IS NOT NULL "
                "AND source_plan_id IS NOT NULL"
            ),
        ),
    )

    run_id = Column(Integer, primary_key=True, index=True)
    started_at = Column(TIMESTAMP, default=func.now(), nullable=False)
    finished_at = Column(TIMESTAMP, nullable=True)
    status = Column(String(20), nullable=False, default="PENDING")
    started_by = Column(String(100), nullable=True)
    horizon_days = Column(Integer, nullable=True)
    # Флаг закрепления прогона от авто‑очистки
    pinned = Column(Boolean, nullable=False, default=False)
    config_version_id = Column(Integer, ForeignKey("planning_config_versions.id"), nullable=True)
    config_snapshot = Column(CrossPlatformJSON, nullable=False)
    warnings = Column(CrossPlatformJSON, nullable=True)
    kpi = Column(CrossPlatformJSON, nullable=True)
    # Period plan link
    source_plan_id = Column(Integer, ForeignKey("production_plan_header.id", ondelete="SET NULL"), nullable=True, index=True)
    period_from = Column(Date, nullable=True, index=True)
    period_to = Column(Date, nullable=True, index=True)
    fixed_at = Column(TIMESTAMP, nullable=True)
    prior_run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="SET NULL"), nullable=True, index=True)
    # Ledger v2 (, additive) — currently active baseline version for
    # this run. Default-1 semantics arrive with the freeze writer (later
    # increment); nullable now so existing rows are untouched.
    active_freeze_version = Column(Integer, nullable=True)
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    ledger_cutoff = Column(DateTime(timezone=True), nullable=True)

    prior_run = relationship("PlanningRun", remote_side=[run_id])
    ledger_generation = relationship("LedgerGeneration")


class PlannedOrder(Base):
    __tablename__ = "planned_order"

    order_id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False)
    requested_qty = Column(DECIMAL(15, 3), nullable=False)
    planned_qty = Column(DECIMAL(15, 3), nullable=False)
    qty = Column(DECIMAL(15, 3), nullable=False)
    need_date = Column(Date, nullable=False)
    start_date = Column(Date, nullable=True)
    finish_date = Column(Date, nullable=True)
    route_ref = Column(String(255), nullable=True)
    priority_index = Column(DECIMAL(10, 4), nullable=True)
    bucket_date = Column(Date, nullable=False)
    demand_ref = Column(TEXT, nullable=True)
    demand_date = Column(Date, nullable=True)
    # Nullable for migration compatibility; Ledger-bound readers fail closed
    # for new proposals without this immutable generation identity.
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    ledger_generation = relationship("LedgerGeneration")


class PlannedOrderStage(Base):
    __tablename__ = "planned_order_stage"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=False)
    order_id = Column(Integer, ForeignKey("planned_order.order_id", ondelete="CASCADE"), nullable=False)
    stage_id = Column(Integer, ForeignKey("production_stages.stage_id"), nullable=False)
    area_id = Column(Integer, ForeignKey("production_resources.resource_id"), nullable=True)
    bucket_date = Column(Date, nullable=False)
    hours = Column(DECIMAL(12, 3), nullable=False, default=0.0)


class PlannedPurchase(Base):
    __tablename__ = "planned_purchase"

    purchase_id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False)
    requested_qty = Column(DECIMAL(15, 3), nullable=False)
    planned_qty = Column(DECIMAL(15, 3), nullable=False)
    qty = Column(DECIMAL(15, 3), nullable=False)
    need_date = Column(Date, nullable=False)
    order_date = Column(Date, nullable=False)
    lead_time_days = Column(Integer, nullable=False)
    priority_index = Column(DECIMAL(10, 4), nullable=True)
    bucket_date = Column(Date, nullable=False)
    supplier_ref1c = Column(String(255), nullable=True)
    source_mrp_requirement_id = Column(
        Integer,
        ForeignKey("mrp_requirement.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Nullable for migration compatibility; mandatory for every new
    # Ledger-driven reconciliation proposal.
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    ledger_generation = relationship("LedgerGeneration")


class PurchaseExportLineAllocation(Base):
    """Immutable split of one exported 1C supplier-order line across proposals.

    Export may coalesce several ``planned_purchase`` rows into one 1C line.
    This table preserves the exact reverse mapping instead of inferring it from
    item/date/quantity after the document has been created.
    """

    __tablename__ = "purchase_export_line_allocation"
    __table_args__ = (
        UniqueConstraint(
            "ledger_generation_id",
            "supplier_order_ref",
            "supplier_order_line_no",
            "planned_purchase_id",
            name="uq_purchase_export_line_allocation",
        ),
        CheckConstraint(
            "allocated_qty > 0",
            name="ck_purchase_export_line_allocation_qty_positive",
        ),
        Index(
            "ix_purchase_export_line_order",
            "supplier_order_ref",
            "supplier_order_line_no",
        ),
        Index(
            "ix_purchase_export_line_generation_order",
            "ledger_generation_id",
            "supplier_order_ref",
            "supplier_order_line_no",
        ),
        UniqueConstraint(
            "ledger_generation_id",
            "supplier_order_ref",
            "request_line_token",
            "planned_purchase_id",
            name="uq_purchase_export_line_allocation_token",
        ),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    supplier_order_ref = Column(String(64), nullable=False)
    supplier_order_line_no = Column(String(32), nullable=False)
    planned_purchase_id = Column(
        Integer,
        ForeignKey("planned_purchase.purchase_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    allocated_qty = Column(DECIMAL(15, 3), nullable=False)
    # Immutable 1C ``КлючСвязи`` and the canonical payload it represents.
    # Both are nullable only for allocations written before migration 13.
    request_line_token = Column(BigInteger, nullable=True)
    export_line_payload_hash = Column(String(64), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    ledger_generation = relationship("LedgerGeneration")
    planned_purchase = relationship("PlannedPurchase")


class PurchaseExportBatch(Base):
    """Immutable boundary for planning-control export materialization."""

    __tablename__ = "purchase_export_batch"
    __table_args__ = (
        CheckConstraint(
            "status IN ('building', 'completed', 'failed', 'aborted')",
            name="ck_purchase_export_batch_status",
        ),
        UniqueConstraint("idempotency_key", name="uq_purchase_export_batch_idempotency_key"),
        Index(
            "ix_purchase_export_batch_ledger_generation_id",
            "ledger_generation_id",
        ),
        Index(
            "ix_purchase_export_batch_planning_read_snapshot_id",
            "planning_read_snapshot_id",
        ),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    planning_read_snapshot_id = Column(
        BigInteger,
        ForeignKey("planning_read_snapshot.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key = Column(String(128), nullable=False)
    status = Column(String(24), nullable=False, server_default="building")
    payload_hash = Column(String(64), nullable=True)
    request_payload = Column(CrossPlatformJSON, nullable=True)
    result_payload = Column(CrossPlatformJSON, nullable=True)
    reason = Column(TEXT, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )
    completed_at = Column(DateTime(timezone=True), nullable=True)

    ledger_generation = relationship("LedgerGeneration")
    planning_read_snapshot = relationship("PlanningReadSnapshot")


class PurchaseExportObligationAllocation(Base):
    """Materialization allocation from one planning reservation to one 1C line."""

    __tablename__ = "purchase_export_obligation_allocation"
    __table_args__ = (
        CheckConstraint(
            "allocated_qty > 0",
            name="ck_purchase_export_obligation_allocation_qty_positive",
        ),
        UniqueConstraint(
            "batch_id",
            "supplier_order_ref",
            "supplier_order_line_no",
            "reservation_id",
            name="uq_purchase_export_obligation_allocation",
        ),
        Index(
            "ix_purchase_export_obligation_allocation_batch_reservation",
            "batch_id",
            "reservation_id",
        ),
        Index(
            "ix_purchase_export_obligation_allocation_batch_supplier_line",
            "batch_id",
            "supplier_order_ref",
            "supplier_order_line_no",
        ),
        Index(
            "ix_purchase_export_obligation_allocation_planned_purchase",
            "planned_purchase_id",
            "batch_id",
        ),
        Index(
            "ix_purchase_export_obligation_allocation_ledger_generation_id",
            "ledger_generation_id",
        ),
        Index(
            "ix_purchase_export_obligation_allocation_item_id",
            "item_id",
        ),
        Index(
            "ix_purchase_export_obligation_allocation_planning_stock_pool",
            "planning_stock_pool",
        ),
        Index(
            "ix_purchase_export_obligation_alloc_destination_wh",
            "destination_warehouse_ref1c",
        ),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    batch_id = Column(
        BigInteger,
        ForeignKey("purchase_export_batch.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reservation_id = Column(
        BigInteger,
        ForeignKey("reservation_entry.id", ondelete="RESTRICT"),
        nullable=False,
    )
    supplier_order_ref = Column(String(64), nullable=False)
    supplier_order_line_no = Column(String(32), nullable=False)
    line_token = Column(BigInteger, nullable=True)
    line_hash = Column(String(64), nullable=True)
    allocated_qty = Column(DECIMAL(15, 3), nullable=False)
    planned_purchase_id = Column(
        Integer,
        ForeignKey("planned_purchase.purchase_id", ondelete="SET NULL"),
        nullable=True,
    )
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=True,
    )
    item_id = Column(
        Integer,
        ForeignKey("items.item_id", ondelete="RESTRICT"),
        nullable=True,
    )
    planning_stock_pool = Column(String(64), nullable=True)
    destination_warehouse_ref1c = Column(String(36), nullable=True)
    eta_date = Column(Date, nullable=True)

    batch = relationship("PurchaseExportBatch")
    reservation = relationship("ReservationEntry")
    planned_purchase = relationship("PlannedPurchase")
    ledger_generation = relationship("LedgerGeneration")
    item = relationship("Item")


class PlannedRework(Base):
    __tablename__ = "planned_rework"

    rework_id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False)
    spec_id = Column(Integer, ForeignKey("specifications.spec_id"), nullable=True)
    requested_qty = Column(DECIMAL(15, 3), nullable=False)
    planned_qty = Column(DECIMAL(15, 3), nullable=False)
    qty = Column(DECIMAL(15, 3), nullable=False)
    need_date = Column(Date, nullable=False)
    order_date = Column(Date, nullable=False)
    lead_time_days = Column(Integer, nullable=False)
    priority_index = Column(DECIMAL(10, 4), nullable=True)
    bucket_date = Column(Date, nullable=False)
    component_limit = Column(DECIMAL(15, 3), nullable=True)
    component_blocked = Column(Boolean, nullable=False, default=False)
    component_partial = Column(Boolean, nullable=False, default=False)
    shortage = Column(CrossPlatformJSON, nullable=True)
    # Nullable for migration compatibility; mandatory for Ledger-derived
    # proposals and verified by consumers before use.
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    ledger_generation = relationship("LedgerGeneration")


class MrpRequirement(Base):
    __tablename__ = "mrp_requirement"
    __table_args__ = (
        UniqueConstraint("run_id", "item_id", name="ux_mrp_requirement_run_item"),
    )

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    total_required_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0)
    net_required_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0)
    period_from = Column(Date, nullable=False, index=True)
    period_to = Column(Date, nullable=False, index=True)
    bom_level = Column(Integer, nullable=False, default=0)
    status = Column(String(20), nullable=False, default="open", server_default="open", index=True)
    closed_at = Column(TIMESTAMP, nullable=True)
    freeze_version = Column(Integer, nullable=True)
    characteristic_ref = Column(String(36), nullable=True)
    organization_ref = Column(String(36), nullable=True)
    planning_stock_pool = Column(String(64), nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now(), nullable=False)

    run = relationship("PlanningRun")
    item = relationship("Item")
    buckets = relationship("MrpRequirementBucket", back_populates="requirement", cascade="all, delete-orphan")


class MrpRequirementBucket(Base):
    __tablename__ = "mrp_requirement_bucket"
    __table_args__ = (
        UniqueConstraint("requirement_id", "bucket_date", name="ux_mrp_requirement_bucket_req_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    requirement_id = Column(Integer, ForeignKey("mrp_requirement.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    bucket_date = Column(Date, nullable=False, index=True)
    gross_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0)
    net_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0)
    created_at = Column(TIMESTAMP, default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now(), nullable=False)

    requirement = relationship("MrpRequirement", back_populates="buckets")
    run = relationship("PlanningRun")
    item = relationship("Item")


class CapacityLoad(Base):
    __tablename__ = "capacity_load"
    __table_args__ = (
        UniqueConstraint('run_id','area_id','bucket_date', name='ux_capacity_load_run_area_date'),
    )

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=False)
    area_id = Column(Integer, ForeignKey("production_resources.resource_id"), nullable=False)
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


class SyncLink(Base):
    """
    Idempotency table for PRODPLAN <-> 1C document exchange. See
    .docs/one_c_export_from_prodplan.md ("Идемпотентность"). Each export
    service upserts one row per (source_doctype, source_id, target_entity)
    and stores the 1C Ref_Key on success so a re-run is a no-op.
    """
    __tablename__ = "sync_link"
    __table_args__ = (
        UniqueConstraint(
            "source_system",
            "source_doctype",
            "source_id",
            "target_entity",
            name="ux_sync_link_source_target",
        ),
    )

    link_id = Column(Integer, primary_key=True, index=True)
    source_system = Column(String(50), nullable=False, default="PRODPLAN", server_default="PRODPLAN")
    source_doctype = Column(String(50), nullable=False)
    source_id = Column(Integer, nullable=False)
    target_system = Column(String(50), nullable=False, default="1C", server_default="1C")
    target_entity = Column(String(100), nullable=False, index=True)
    target_ref_key = Column(String(36), nullable=True, index=True)
    target_number = Column(String(50), nullable=True)
    payload_hash = Column(String(64), nullable=True)
    # New planning exports are always tied to one accepted Ledger generation.
    # Null is retained solely for historical links.
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    # planned | success | error | cancelled
    status = Column(String(20), nullable=False, default="planned", server_default="planned", index=True)
    last_error = Column(TEXT, nullable=True)
    last_synced_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False)


# ---------------------------------------------------------------------------
# Assembly takt master retained for the canonical drum scheduler.
# The historical table name stays unchanged until its data is migrated.
# ---------------------------------------------------------------------------


class AssemblyRate(Base):
    """
    Assembly takt: how many units of an SKU one unit of resource capacity
    yields per day. Mirrors ERPNext prodflow `ProdFlow Assembly Rate`.
    """
    __tablename__ = "dbr_assembly_rate"
    __table_args__ = (
        UniqueConstraint("resource_id", "item_id", name="ux_dbr_assembly_rate_resource_item"),
    )

    id = Column(Integer, primary_key=True, index=True)
    resource_id = Column(
        Integer,
        ForeignKey("production_resources.resource_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_id = Column(
        Integer,
        ForeignKey("items.item_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    qty_per_capacity = Column(DECIMAL(12, 3), nullable=False)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False)

    resource = relationship("ProductionResource")
    item = relationship("Item")


class AssemblyQueueLine(Base):
    """Canonical normalized queue line for FIXED Snapshot live plans."""

    __tablename__ = "assembly_queue_line"
    __table_args__ = (
        UniqueConstraint(
            "ledger_generation_id",
            "plan_line_id",
            name="uq_assembly_queue_line_generation_plan_line",
        ),
        CheckConstraint("planned_output_qty >= 0", name="ck_assembly_queue_line_planned_qty_nonnegative"),
        CheckConstraint(
            "accepted_plan_output_qty >= 0",
            name="ck_assembly_queue_line_accepted_qty_nonnegative",
        ),
        CheckConstraint(
            "assembly_remaining_qty >= 0",
            name="ck_assembly_queue_line_remaining_qty_nonnegative",
        ),
        CheckConstraint(
            "period_to >= period_from",
            name="ck_assembly_queue_line_period",
        ),
        Index(
            "ix_assembly_queue_line_generation_status",
            "ledger_generation_id",
            "line_status",
        ),
        Index(
            "ix_assembly_queue_line_generation_sort",
            "ledger_generation_id",
            "sort_key",
        ),
        Index(
            "ix_assembly_queue_line_plan_line",
            "plan_id",
            "plan_line_id",
        ),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True, index=True)
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    planning_run_id = Column(
        Integer,
        ForeignKey("planning_run.run_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    plan_id = Column(
        Integer,
        ForeignKey("production_plan_header.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    plan_line_id = Column(
        Integer,
        ForeignKey("production_plan_line.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    item_id = Column(
        Integer,
        ForeignKey("items.item_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    bucket_date = Column(Date, nullable=False)
    period_from = Column(Date, nullable=False, index=True)
    period_to = Column(Date, nullable=False, index=True)
    planned_output_qty = Column(DECIMAL(15, 3), nullable=False)
    accepted_plan_output_qty = Column(DECIMAL(15, 3), nullable=False)
    assembly_remaining_qty = Column(DECIMAL(15, 3), nullable=False)
    eligible_from = Column(DateTime(timezone=True), nullable=True)
    original_priority = Column(CrossPlatformJSON, nullable=False, default=list, server_default=text("'[]'"))
    sort_key = Column(String(128), nullable=False)
    line_status = Column(String(20), nullable=False, default="open", server_default="open")
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    planning_run = relationship("PlanningRun")
    plan = relationship("ProductionPlanHeader")
    plan_line = relationship("ProductionPlanLine")
    item = relationship("Item")
    ledger_generation = relationship("LedgerGeneration")


class DrumSchedule(Base):
    __tablename__ = "drum_schedule"
    __table_args__ = (
        UniqueConstraint(
            "ledger_generation_id",
            name="uq_drum_schedule_generation",
        ),
        CheckConstraint("slot_row_count >= 0", name="ck_drum_schedule_slot_row_count_nonnegative"),
        CheckConstraint("gap_row_count >= 0", name="ck_drum_schedule_gap_row_count_nonnegative"),
        CheckConstraint("total_open_qty >= 0", name="ck_drum_schedule_total_open_qty_nonnegative"),
        CheckConstraint("total_slot_qty >= 0", name="ck_drum_schedule_total_slot_qty_nonnegative"),
        CheckConstraint("total_gap_qty >= 0", name="ck_drum_schedule_total_gap_qty_nonnegative"),
        Index("ix_drum_schedule_generation", "ledger_generation_id"),
        Index("ix_drum_schedule_algorithm", "algorithm_version"),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True, index=True)
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status = Column(String(20), nullable=False, default="completed", server_default="completed")
    algorithm_version = Column(String(64), nullable=False)
    schedule_from = Column(Date, nullable=False)
    schedule_to = Column(Date, nullable=False)
    queue_signature = Column(String(64), nullable=False)
    slot_signature = Column(String(64), nullable=False)
    gap_signature = Column(String(64), nullable=False)
    slot_row_count = Column(Integer, nullable=False, default=0, server_default="0")
    gap_row_count = Column(Integer, nullable=False, default=0, server_default="0")
    total_open_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default=text("0"))
    total_slot_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default=text("0"))
    total_gap_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default=text("0"))
    metrics = Column(CrossPlatformJSON, nullable=False, default=dict, server_default=text("'{}'"))
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)

    ledger_generation = relationship("LedgerGeneration")


class DrumSlot(Base):
    __tablename__ = "drum_slot"
    __table_args__ = (
        UniqueConstraint(
            "drum_schedule_id",
            "assembly_queue_line_id",
            "slot_ordinal",
            name="uq_drum_slot_schedule_line_ordinal",
        ),
        CheckConstraint("slot_qty > 0", name="ck_drum_slot_qty_positive"),
        CheckConstraint("slot_ordinal >= 0", name="ck_drum_slot_ordinal_nonnegative"),
        Index("ix_drum_slot_schedule_date", "drum_schedule_id", "slot_date"),
        Index("ix_drum_slot_resource_date", "resource_id", "slot_date"),
        Index("ix_drum_slot_item", "item_id"),
        Index("ix_drum_slot_plan", "plan_id", "plan_line_id"),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True, index=True)
    drum_schedule_id = Column(
        BigIntPK,
        ForeignKey("drum_schedule.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    assembly_queue_line_id = Column(
        BigInteger,
        ForeignKey("assembly_queue_line.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    plan_id = Column(
        Integer,
        ForeignKey("production_plan_header.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    plan_line_id = Column(
        Integer,
        ForeignKey("production_plan_line.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    item_id = Column(
        Integer,
        ForeignKey("items.item_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    resource_id = Column(
        Integer,
        ForeignKey("production_resources.resource_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    slot_date = Column(Date, nullable=False)
    slot_qty = Column(DECIMAL(15, 3), nullable=False)
    planned_output_qty = Column(DECIMAL(15, 3), nullable=False)
    slot_ordinal = Column(Integer, nullable=False, default=0, server_default="0")
    original_priority = Column(CrossPlatformJSON, nullable=False, default=list, server_default=text("'[]'"))
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)

    drum_schedule = relationship("DrumSchedule")
    queue_line = relationship("AssemblyQueueLine")


class DrumCapacityGap(Base):
    __tablename__ = "drum_capacity_gap"
    __table_args__ = (
        UniqueConstraint(
            "drum_schedule_id",
            "assembly_queue_line_id",
            "resource_id",
            "gap_date",
            name="uq_drum_gap_schedule_line_resource_date",
        ),
        CheckConstraint("required_qty >= 0", name="ck_drum_gap_required_qty_nonnegative"),
        CheckConstraint("available_capacity >= 0", name="ck_drum_gap_available_capacity_nonnegative"),
        CheckConstraint("gap_qty >= 0", name="ck_drum_gap_qty_nonnegative"),
        Index("ix_drum_gap_schedule_resource_date", "drum_schedule_id", "resource_id", "gap_date"),
        Index("ix_drum_gap_schedule_item", "drum_schedule_id", "item_id"),
        Index("ix_drum_gap_item_resource", "item_id", "resource_id"),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True, index=True)
    drum_schedule_id = Column(
        BigIntPK,
        ForeignKey("drum_schedule.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    assembly_queue_line_id = Column(
        BigInteger,
        ForeignKey("assembly_queue_line.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    plan_id = Column(
        Integer,
        ForeignKey("production_plan_header.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    plan_line_id = Column(
        Integer,
        ForeignKey("production_plan_line.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    resource_id = Column(
        Integer,
        ForeignKey("production_resources.resource_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    item_id = Column(
        Integer,
        ForeignKey("items.item_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    gap_date = Column(Date, nullable=False)
    required_qty = Column(DECIMAL(15, 3), nullable=False)
    available_capacity = Column(DECIMAL(15, 3), nullable=False)
    gap_qty = Column(DECIMAL(15, 3), nullable=False)
    original_priority = Column(CrossPlatformJSON, nullable=False, default=list, server_default=text("'[]'"))
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)

    drum_schedule = relationship("DrumSchedule")
    queue_line = relationship("AssemblyQueueLine")


class ShelfPolicy(Base):
    """Stable physical shelf configuration; never plan-generated."""

    __tablename__ = "shelf_policy"
    __table_args__ = (
        UniqueConstraint("item_id", "warehouse_ref1c", name="uq_shelf_policy_item_warehouse"),
        CheckConstraint("replenishment_time_days >= 0", name="ck_shelf_policy_replenishment_nonnegative"),
        CheckConstraint("review_cycle_days >= 0", name="ck_shelf_policy_review_nonnegative"),
        CheckConstraint("safety_days >= 0", name="ck_shelf_policy_safety_nonnegative"),
        CheckConstraint("batch_multiple > 0", name="ck_shelf_policy_batch_positive"),
        Index("ix_shelf_policy_active_item", "active", "item_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("items.item_id", ondelete="RESTRICT"), nullable=False, index=True)
    warehouse_ref1c = Column(String(36), nullable=False, index=True)
    replenishment_time_days = Column(Integer, nullable=False, default=0, server_default="0")
    review_cycle_days = Column(Integer, nullable=False, default=0, server_default="0")
    safety_days = Column(Integer, nullable=False, default=0, server_default="0")
    batch_multiple = Column(DECIMAL(15, 3), nullable=False, default=1, server_default="1")
    active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now())

    item = relationship("Item")


class ShelfProjection(Base):
    """Immutable generation-bound timing projection of existing MRP demand."""

    __tablename__ = "shelf_projection"
    __table_args__ = (
        UniqueConstraint(
            "ledger_generation_id",
            "shelf_policy_id",
            name="uq_shelf_projection_generation_policy",
        ),
        CheckConstraint("target_qty >= 0", name="ck_shelf_projection_target_nonnegative"),
        CheckConstraint("gap_qty >= 0", name="ck_shelf_projection_gap_nonnegative"),
        CheckConstraint("transfer_qty >= 0", name="ck_shelf_projection_transfer_nonnegative"),
        CheckConstraint("pull_qty >= 0", name="ck_shelf_projection_pull_nonnegative"),
        CheckConstraint("materialized_qty >= 0", name="ck_shelf_projection_materialized_nonnegative"),
        CheckConstraint(
            "materialized_qty <= unlaunched_mrp_qty",
            name="ck_shelf_projection_materialized_within_mrp",
        ),
        Index("ix_shelf_projection_generation_priority", "ledger_generation_id", "latest_start_date"),
        Index("ix_shelf_projection_item", "item_id"),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True, index=True)
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    shelf_policy_id = Column(
        Integer,
        ForeignKey("shelf_policy.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    item_id = Column(Integer, ForeignKey("items.item_id", ondelete="RESTRICT"), nullable=False, index=True)
    warehouse_ref1c = Column(String(36), nullable=False)
    as_of_date = Column(Date, nullable=False)
    protection_until = Column(Date, nullable=False)
    target_qty = Column(DECIMAL(15, 3), nullable=False)
    shelf_physical_qty = Column(DECIMAL(15, 3), nullable=False)
    other_stock_qty = Column(DECIMAL(15, 3), nullable=False)
    confirmed_open_production_qty = Column(DECIMAL(15, 3), nullable=False)
    projected_qty = Column(DECIMAL(15, 3), nullable=False)
    gap_qty = Column(DECIMAL(15, 3), nullable=False)
    transfer_qty = Column(DECIMAL(15, 3), nullable=False)
    unlaunched_mrp_qty = Column(DECIMAL(15, 3), nullable=False)
    pull_qty = Column(DECIMAL(15, 3), nullable=False)
    materialized_qty = Column(DECIMAL(15, 3), nullable=False)
    first_shortage_date = Column(Date, nullable=True)
    latest_start_date = Column(Date, nullable=True)
    demand_manifest = Column(CrossPlatformJSON, nullable=False, default=list, server_default=text("'[]'"))
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now())

    generation = relationship("LedgerGeneration")
    policy = relationship("ShelfPolicy")
    item = relationship("Item")


class PaintWeldPair(Base):
    """
    Связка «окрашенная ↔ сварная (неокрашенная)» деталь для семейства
    «… после покраски». Строится автоматически из default-спек (source='auto'),
    ручные правки допустимы (source='manual'). См. .docs/paint_weld_chain_logic.md.

    Одна окрашенная деталь = одна сварная (painted_item_id UNIQUE). Сварная может
    участвовать в нескольких парах теоретически, поэтому только индекс.
    """

    __tablename__ = "paint_weld_pairs"
    __table_args__ = (
        UniqueConstraint("painted_item_id", name="ux_paint_weld_pairs_painted"),
        CheckConstraint("source IN ('auto', 'manual')", name="ck_paint_weld_pairs_source"),
    )

    id = Column(Integer, primary_key=True, index=True)
    painted_item_id = Column(
        Integer, ForeignKey("items.item_id"), nullable=False
    )
    welded_item_id = Column(
        Integer, ForeignKey("items.item_id"), nullable=False, index=True
    )
    source = Column(String(10), nullable=False, default="auto", server_default="auto")
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    painted_item = relationship("Item", foreign_keys=[painted_item_id])
    welded_item = relationship("Item", foreign_keys=[welded_item_id])


class PaintWeldChainLink(Base):
    """
    Локальная связь «окрасочный → сварочный» заказ (цепочка открытия, этап 2).

    В 1С сварочный документ несёт штатное основание
    (`ЗаказНаПроизводствоОснование_Key` + `ДокументОснование`/`_Type`,
    см. .docs/odata.md). Локальная запись — источник истины на стороне
    PRODPLAN и якорь идемпотентности повторного открытия цепочки.

    Один окрасочный заказ = один сварочный (painted_order_id UNIQUE) — это якорь
    идемпотентности повторного открытия цепочки.
    """

    __tablename__ = "paint_weld_chain_links"
    __table_args__ = (
        UniqueConstraint("painted_order_id", name="ux_paint_weld_chain_links_painted_order"),
    )

    id = Column(Integer, primary_key=True, index=True)
    painted_order_id = Column(
        Integer, ForeignKey("production_orders.order_id"), nullable=False
    )
    welded_order_id = Column(
        Integer, ForeignKey("production_orders.order_id"), nullable=False, index=True
    )
    pair_id = Column(Integer, ForeignKey("paint_weld_pairs.id"), nullable=False)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)


# ---------------------------------------------------------------------------
# MRP Execution Ledger v2 —  (ADDITIVE SCHEMA ONLY).
#
# See .docs/reservation-replenishment-core.md. The remaining tables model the
# immutable frozen-plan baseline and its allocation/BOM snapshot.
#
# Pool qualification (v2 ): pool_key = (item_id, characteristic_ref,
# organization_ref, planning_stock_pool). Pool columns are nullable here for
# additive consistency with MrpRequirement's new pool columns; an empty
# characteristic is a distinct key value, not a wildcard (v2 ). See the
# INCREMENT-1 report note on normalizing empty pool keys before these tables
# carry data (owner decision).
# ---------------------------------------------------------------------------


class MrpFreezeBaseline(Base):
    """Run-scoped, versioned frozen snapshot of a pool's supply position at
    freeze time (v2 ). Immutable versions: refreeze = INSERT version+1;
    refreezing run A never touches run B."""

    __tablename__ = "mrp_freeze_baseline"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "freeze_version",
            "item_id",
            "characteristic_ref",
            "organization_ref",
            "planning_stock_pool",
            name="ux_mrp_freeze_baseline_pool_version",
        ),
        Index("ix_mrp_freeze_baseline_run_version", "run_id", "freeze_version"),
        Index(
            "ix_mrp_freeze_baseline_pool",
            "item_id",
            "characteristic_ref",
            "organization_ref",
            "planning_stock_pool",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=False, index=True)
    freeze_version = Column(Integer, nullable=False)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    characteristic_ref = Column(String(36), nullable=True)
    organization_ref = Column(String(36), nullable=True)
    planning_stock_pool = Column(String(64), nullable=True)
    frozen_at = Column(TIMESTAMP, nullable=False, default=func.now(), server_default=func.now())
    baseline_at = Column(TIMESTAMP, nullable=True)
    physical_import_batch_id = Column(
        BigInteger,
        ForeignKey("physical_import_batch.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    stock_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    produced_total = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    received_total = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    unit_coef = Column(DECIMAL(15, 3), nullable=False, default=1.0, server_default="1")
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)

    run = relationship("PlanningRun")
    item = relationship("Item")


class MrpFreezeAllocation(Base):
    """Coverage-carrying frozen allocation (v2 ): binds a requirement to a
    supply source (stock / supplier_order / wip_order). alloc_qty and source
    are immutable; realized_qty / evaporated_qty are rewritten each cycle by
    verify_frozen_supply. Prevents double net deduction."""

    __tablename__ = "mrp_freeze_allocation"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "freeze_version",
            "requirement_id",
            "source_type",
            "source_ref",
            "source_line_ref",
            name="ux_mrp_freeze_allocation_source",
        ),
        Index("ix_mrp_freeze_allocation_run_version", "run_id", "freeze_version"),
        Index("ix_mrp_freeze_allocation_requirement", "requirement_id"),
        Index(
            "ix_mrp_freeze_allocation_pool",
            "item_id",
            "characteristic_ref",
            "organization_ref",
            "planning_stock_pool",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=False, index=True)
    freeze_version = Column(Integer, nullable=False)
    requirement_id = Column(Integer, ForeignKey("mrp_requirement.id", ondelete="CASCADE"), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    characteristic_ref = Column(String(36), nullable=True)
    organization_ref = Column(String(36), nullable=True)
    planning_stock_pool = Column(String(64), nullable=True)
    # source_type ∈ {stock, supplier_order, wip_order}. Empty string = distinct
    # key value (not wildcard); NOT NULL keeps the UNIQUE key deterministic.
    source_type = Column(String(32), nullable=False, server_default="")
    source_ref = Column(String(64), nullable=False, server_default="")
    source_line_ref = Column(String(64), nullable=False, server_default="")
    alloc_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    fact_at_freeze = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    realized_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    evaporated_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)

    run = relationship("PlanningRun")
    item = relationship("Item")
    requirement = relationship("MrpRequirement")


class MrpFreezeComponent(Base):
    """Frozen BOM / consumption norms (v2 ). Writer = freeze; reader = drift
    only (a spec/norm change after freeze does NOT create drift)."""

    __tablename__ = "mrp_freeze_component"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "freeze_version",
            "parent_item_id",
            "component_item_id",
            "spec_ref",
            name="ux_mrp_freeze_component_spec",
        ),
        Index("ix_mrp_freeze_component_run_version", "run_id", "freeze_version"),
        Index("ix_mrp_freeze_component_parent", "parent_item_id"),
        Index("ix_mrp_freeze_component_component", "component_item_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=False, index=True)
    freeze_version = Column(Integer, nullable=False)
    parent_item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    parent_characteristic_ref = Column(String(36), nullable=True)
    parent_organization_ref = Column(String(36), nullable=True)
    parent_planning_stock_pool = Column(String(64), nullable=True)
    component_item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    component_characteristic_ref = Column(String(36), nullable=True)
    component_organization_ref = Column(String(36), nullable=True)
    component_planning_stock_pool = Column(String(64), nullable=True)
    spec_ref = Column(String(36), nullable=False, server_default="")
    spec_version = Column(String(50), nullable=True)
    norm_qty_per_unit = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    unit_coef = Column(DECIMAL(15, 3), nullable=False, default=1.0, server_default="1")
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)

    run = relationship("PlanningRun")
    parent_item = relationship("Item", foreign_keys=[parent_item_id])
    component_item = relationship("Item", foreign_keys=[component_item_id])


class MrpFreezeComponentCumulative(Base):
    """Cumulative frozen BOM norms per frozen root item (v2 ).

    Reader/consumer: shelf projection and any other contour that needs
    root-level component demand in a single step, without re-expanding the
    hierarchy at read time.
    """

    __tablename__ = "mrp_freeze_component_cumulative"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "freeze_version",
            "root_item_id",
            "component_item_id",
            name="ux_mrp_freeze_component_cumulative_root",
        ),
        CheckConstraint(
            "cumulative_norm_qty_per_root_unit >= 0",
            name="ck_mrp_freeze_component_cumulative_norm_non_negative",
        ),
        Index("ix_mrp_freeze_component_cumulative_run_version", "run_id", "freeze_version"),
        Index("ix_mrp_freeze_component_cumulative_root", "root_item_id"),
        Index("ix_mrp_freeze_component_cumulative_component", "component_item_id"),
    )

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=False)
    freeze_version = Column(Integer, nullable=False)
    root_item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False)
    component_item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False)
    cumulative_norm_qty_per_root_unit = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)

    run = relationship("PlanningRun")
    root_item = relationship("Item", foreign_keys=[root_item_id])
    component_item = relationship("Item", foreign_keys=[component_item_id])


# ---------------------------------------------------------------------------
# ITEM LEDGER: physical quantity tape plus immutable planning reservations.
#
# Two append-only ledgers whose fold gives an item's state (–):
#   Ledger-1 (physical movements, mirror of 1С AccumulationRegister): keyed
#     physically (item, characteristic, organization, warehouse) — stock_ledger_
#     entry (signed qty + running qty_after), stock_bin (on_hand fold),
#     stock_recorder_pull (pull idempotency), stock_ledger_anchor (seed/S0).
#   Reservation tape (PRODPLAN-owned): reservation_entry stores one frozen
#     obligation per requirement; reservation_event is its append-only physical
#     replenishment journal. Pool/key columns are NOT NULL default '': an
# empty characteristic/organization is a distinct key value, never a wildcard.
# ---------------------------------------------------------------------------


class StockLedgerEntry(Base):
    """Ledger-1 append-only physical movement ( / stock-doc ).
    Signed ``qty`` (receipt > 0, expense < 0, base UoM); ``qty_after`` is the
    running balance projection (R-A). One row per (recorder, line); replacement
    is by-recorder (delete+reinsert), never UPDATE of an applied row (И5)."""

    __tablename__ = "stock_ledger_entry"
    __table_args__ = (
        UniqueConstraint(
            "recorder_type",
            "recorder_ref",
            "line_no",
            "source_content_hash",
            "ingest_batch_id",
            name="ux_stock_ledger_entry_recorder_line",
        ),
        Index(
            "ix_stock_ledger_entry_ledger_key",
            "item_id",
            "characteristic_ref",
            "organization_ref",
            "warehouse_ref1c",
            "posting_at",
        ),
        Index("ix_stock_ledger_entry_posting_at", "posting_at"),
        Index("ix_stock_ledger_entry_recorder", "recorder_type", "recorder_ref"),
    )

    id = Column(BigIntPK, primary_key=True, index=True)
    ingest_batch_id = Column(
        BigInteger,
        ForeignKey("physical_import_batch.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_content_hash = Column(String(64), nullable=False)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    characteristic_ref = Column(String(36), nullable=False, server_default="")
    organization_ref = Column(String(36), nullable=False, server_default="")
    warehouse_ref1c = Column(String(36), nullable=False, server_default="")
    # Signed quantity in base UoM (RecordType Receipt → +, Expense → −).
    qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    # Running balance after this row within its ledger key (rebuild_running_balance).
    qty_after = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    posting_at = Column(TIMESTAMP, nullable=False, default=func.now(), server_default=func.now())
    # RecordType raw ∈ {Receipt, Expense} from 1С (sign source, kept for trace).
    record_type = Column(String(16), nullable=False, server_default="")
    # movement_kind ∈ {receipt, expense, assembly_in, assembly_out, transfer_in,
    # transfer_out, writeoff, adjustment, seed}. Empty = distinct key value.
    movement_kind = Column(String(32), nullable=False, server_default="")
    # recorder identity: 1С document type / GUID / LineNumber (string, ).
    recorder_type = Column(String(64), nullable=False, server_default="")
    recorder_ref = Column(String(64), nullable=False, server_default="")
    line_no = Column(String(32), nullable=False, server_default="")
    # ingest_source ∈ {pull, balance_reconcile, seed, adjustment}.
    ingest_source = Column(String(32), nullable=False, server_default="")
    active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)

    item = relationship("Item")
    ingest_batch = relationship("PhysicalImportBatch")


class StockLedgerSupplierReceiptProvenance(Base):
    """Generation-scoped, immutable business match for a supplier receipt.

    The copied receipt identity makes the decision auditable without parsing a
    mutable external document. ``match_status`` is deliberately explicit:
    ambiguous and unmatched evidence is retained and cannot masquerade as an
    exact supplier-order-line match.
    """

    __tablename__ = "stock_ledger_supplier_receipt_provenance"
    __table_args__ = (
        UniqueConstraint(
            "ledger_generation_id",
            "stock_ledger_entry_id",
            name="uq_supplier_receipt_provenance_generation_sle",
        ),
        CheckConstraint(
            "match_status IN ('exact', 'ambiguous', 'unmatched', "
            "'excluded_non_supplier')",
            name="ck_supplier_receipt_provenance_match_status",
        ),
        CheckConstraint(
            "operation_kind IN ('supplier_receipt', 'correction', "
            "'supplier_return', 'transfer', 'non_supplier_expense', 'unknown')",
            name="ck_supplier_receipt_provenance_operation_kind",
        ),
        CheckConstraint(
            "ambiguity_count >= 0",
            name="ck_supplier_receipt_provenance_ambiguity_count",
        ),
        CheckConstraint(
            "(match_status = 'exact' AND supplier_order_ref IS NOT NULL "
            "AND supplier_order_line_no IS NOT NULL AND ambiguity_count = 0) "
            "OR (match_status = 'ambiguous' AND ambiguity_count > 1 "
            "AND reason IS NOT NULL) "
            "OR (match_status = 'unmatched' AND ambiguity_count = 0 "
            "AND reason IS NOT NULL) "
            "OR (match_status = 'excluded_non_supplier' AND supplier_order_ref IS NULL "
            "AND supplier_order_line_no IS NULL AND ambiguity_count = 0 "
            "AND operation_kind = 'non_supplier_expense' "
            "AND operation_key IS NOT NULL AND operation_name IS NOT NULL "
            "AND reason IS NOT NULL)",
            name="ck_supplier_receipt_provenance_match_evidence",
        ),
        Index(
            "ix_supplier_receipt_provenance_order",
            "supplier_order_ref",
            "supplier_order_line_no",
        ),
        Index(
            "ix_supplier_receipt_provenance_generation_status",
            "ledger_generation_id",
            "match_status",
        ),
        Index(
            "ix_supplier_receipt_provenance_generation_kind",
            "ledger_generation_id",
            "operation_kind",
        ),
        Index(
            "ix_supplier_receipt_provenance_receipt_line",
            "ledger_generation_id",
            "receipt_doc_type",
            "receipt_doc_ref",
            "receipt_doc_line_no",
        ),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    stock_ledger_entry_id = Column(
        BigInteger,
        ForeignKey("stock_ledger_entry.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    receipt_doc_type = Column(String(64), nullable=False)
    receipt_doc_ref = Column(String(64), nullable=False)
    receipt_doc_line_no = Column(String(32), nullable=False)
    supplier_order_ref = Column(String(64), nullable=True)
    supplier_order_line_no = Column(String(32), nullable=True)
    operation_kind = Column(
        String(32), nullable=False, default="unknown", server_default="unknown"
    )
    operation_key = Column(String(128), nullable=True)
    operation_name = Column(String(128), nullable=True)
    correction_receipt_ref = Column(String(64), nullable=True)
    evidence_hash = Column(String(64), nullable=False, default="", server_default="")
    evidence_payload = Column(
        CrossPlatformJSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    match_rule = Column(String(64), nullable=False)
    match_status = Column(String(32), nullable=False)
    ambiguity_count = Column(Integer, nullable=False, server_default="0")
    reason = Column(TEXT, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    ledger_generation = relationship("LedgerGeneration")
    stock_ledger_entry = relationship("StockLedgerEntry")


class AssemblyOutputFactDecision(Base):
    """Decision table for mapping one physical assembly output to plan provenance.

    The canonical path is exact match to a production plan line; only when exact
    provenance is absent the fallback is FIFO by the caller-provided queue order.
    One decision can be ambiguous or invalid to preserve provenance integrity.
    """

    __tablename__ = "assembly_output_fact_decision"
    __table_args__ = (
        UniqueConstraint(
            "ledger_generation_id",
            "stock_ledger_entry_id",
            name="uq_assembly_output_fact_decision_generation_sle",
        ),
        CheckConstraint(
            "decision_status IN ('allocatable', 'ambiguous', 'invalid')",
            name="ck_assembly_output_fact_decision_status",
        ),
        CheckConstraint(
            "link_kind IN ('exact_plan_line', 'planned_order', 'order_ref', 'none')",
            name="ck_assembly_output_fact_decision_link_kind",
        ),
        CheckConstraint(
            "surplus_qty >= 0",
            name="ck_assembly_output_fact_decision_surplus_nonnegative",
        ),
        CheckConstraint(
            "length(source_content_hash) = 64",
            name="ck_assembly_output_fact_decision_source_content_hash_len",
        ),
        Index(
            "ix_assembly_output_fact_decision_generation",
            "ledger_generation_id",
        ),
        Index(
            "ix_assembly_output_fact_decision_stock_ledger_entry",
            "stock_ledger_entry_id",
        ),
        Index(
            "ix_assembly_output_fact_decision_generation_status",
            "ledger_generation_id",
            "decision_status",
        ),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    stock_ledger_entry_id = Column(
        BigInteger,
        ForeignKey("stock_ledger_entry.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    decision_status = Column(String(16), nullable=False)
    link_kind = Column(String(24), nullable=False)
    reason = Column(TEXT, nullable=True)
    evidence_payload = Column(
        CrossPlatformJSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    source_content_hash = Column(String(64), nullable=False)
    surplus_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")

    ledger_generation = relationship("LedgerGeneration")
    stock_ledger_entry = relationship("StockLedgerEntry")


class AssemblyOutputAllocation(Base):
    """Concrete per-plan-line allocations emitted by canonical physical output logic."""

    __tablename__ = "assembly_output_allocation"
    __table_args__ = (
        UniqueConstraint(
            "ledger_generation_id",
            "stock_ledger_entry_id",
            "allocation_ordinal",
            name="uq_assembly_output_allocation_generation_sle_ordinal",
        ),
        UniqueConstraint(
            "ledger_generation_id",
            "stock_ledger_entry_id",
            "plan_line_id",
            name="uq_assembly_output_allocation_generation_sle_plan_line",
        ),
        CheckConstraint(
            "allocated_qty > 0",
            name="ck_assembly_output_allocation_qty_positive",
        ),
        CheckConstraint(
            "match_rule IN ('exact', 'fifo')",
            name="ck_assembly_output_allocation_match_rule",
        ),
        CheckConstraint(
            "allocation_ordinal >= 0",
            name="ck_assembly_output_allocation_ordinal_nonnegative",
        ),
        Index(
            "ix_assembly_output_allocation_generation",
            "ledger_generation_id",
        ),
        Index(
            "ix_assembly_output_allocation_stock_ledger_entry",
            "stock_ledger_entry_id",
        ),
        Index(
            "ix_assembly_output_allocation_plan",
            "plan_id",
        ),
        Index(
            "ix_assembly_output_allocation_plan_line",
            "plan_line_id",
        ),
        Index(
            "ix_assembly_output_allocation_generation_match_rule",
            "ledger_generation_id",
            "match_rule",
        ),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    stock_ledger_entry_id = Column(
        BigInteger,
        ForeignKey("stock_ledger_entry.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    plan_id = Column(
        Integer,
        ForeignKey("production_plan_header.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    plan_line_id = Column(
        Integer,
        ForeignKey("production_plan_line.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    allocated_qty = Column(DECIMAL(15, 3), nullable=False)
    match_rule = Column(String(8), nullable=False)
    allocation_ordinal = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    ledger_generation = relationship("LedgerGeneration")
    stock_ledger_entry = relationship("StockLedgerEntry")
    plan = relationship("ProductionPlanHeader")
    plan_line = relationship("ProductionPlanLine")


class StockLedgerFactSupersession(Base):
    """Append-only replacement edge preserving earlier accepted fact prefixes."""
    __tablename__ = "stock_ledger_fact_supersession"
    __table_args__ = (
        UniqueConstraint(
            "import_batch_id", "old_sle_id",
            name="uq_stock_ledger_supersession_transition",
        ),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    old_sle_id = Column(
        BigInteger,
        ForeignKey("stock_ledger_entry.id", ondelete="RESTRICT"),
        nullable=False,
    )
    new_sle_id = Column(
        BigInteger,
        ForeignKey("stock_ledger_entry.id", ondelete="RESTRICT"),
        nullable=True,
    )
    import_batch_id = Column(
        BigInteger,
        ForeignKey("physical_import_batch.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    old_sle = relationship("StockLedgerEntry", foreign_keys=[old_sle_id])
    new_sle = relationship("StockLedgerEntry", foreign_keys=[new_sle_id])
    import_batch = relationship("PhysicalImportBatch")


class StockBin(Base):
    """Ledger-1 fold cache: on_hand per physical key (). on_hand =
    Σ qty over the key's SLE (= last qty_after). reconcile_pending_qty holds a
    debounced Balance-vs-ledger delta (written by the reconcile step, )."""

    __tablename__ = "stock_bin"
    __table_args__ = (
        UniqueConstraint(
            "ledger_generation_id",
            "item_id",
            "characteristic_ref",
            "organization_ref",
            "warehouse_ref1c",
            name="ux_stock_bin_ledger_key",
        ),
        Index(
            "ix_stock_bin_ledger_key",
            "item_id",
            "characteristic_ref",
            "organization_ref",
            "warehouse_ref1c",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    characteristic_ref = Column(String(36), nullable=False, server_default="")
    organization_ref = Column(String(36), nullable=False, server_default="")
    warehouse_ref1c = Column(String(36), nullable=False, server_default="")
    on_hand = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    # Debounced Balance-vs-ledger delta (  step 3): a non-zero value means
    # a drift was seen last sweep and is awaiting a confirming second sweep.
    reconcile_pending_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    # When this key was last confirmed reconciled against 1С /Balance (|delta|≤EPS
    # matched, or an adjustment-SLE applied). NULL = never reconciled ().
    last_reconciled_at = Column(TIMESTAMP, nullable=True)
    last_entry_id = Column(BigInteger, nullable=True)
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now(), server_default=func.now(), nullable=False)

    item = relationship("Item")
    ledger_generation = relationship("LedgerGeneration")


class StockRecorderPull(Base):
    """Ledger-1 pull idempotency ledger (). One row per 1С recorder
    already mirrored, so a re-pull of the same document is a no-op ()."""

    __tablename__ = "stock_recorder_pull"
    __table_args__ = (
        UniqueConstraint("recorder_type", "recorder_ref", name="ux_stock_recorder_pull_recorder"),
        Index("ix_stock_recorder_pull_pulled_at", "pulled_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    recorder_type = Column(String(64), nullable=False, server_default="")
    recorder_ref = Column(String(64), nullable=False, server_default="")
    line_count = Column(Integer, nullable=False, default=0, server_default="0")
    # status ∈ {pending, done, empty, error} (). A hook enqueues 'pending';
    # the puller sets done/empty/error. 'pulled' remains the  legacy default.
    status = Column(String(20), nullable=False, server_default="pulled")
    # What put the recorder on the queue (e.g. 'manufacture_export',
    # 'stock_transfer_export', 'reconcile'); diagnostic only.
    source = Column(String(64), nullable=False, default="", server_default="")
    # Retry bookkeeping for process_pending_pulls (attempt cap) + last error text.
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    last_error = Column(TEXT, nullable=True)
    # Producing-order GUID captured from the recorder's document HEADER at pull
    # time (СборкаЗапасов.ЗаказНаПроизводство_Key / ПеремещениеЗапасов.
    # ДокументОснование of type ЗаказНаПроизводство). Second source for the
    # SLE→reservation matching chain (after sync_link); NULL = header carried
    # no order basis. Migration 20260723_01.
    order_ref = Column(String(36), nullable=True)
    pulled_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now(), server_default=func.now(), nullable=False)


class StockLedgerAnchor(Base):
    """Ledger-1 anchor ( / stock-doc ): a per-key seed/S0 point.
    After the anchor the running balance is authoritative; the anchor records
    the Balance-derived opening quantity and the period it belongs to."""

    __tablename__ = "stock_ledger_anchor"
    __table_args__ = (
        UniqueConstraint(
            "item_id",
            "characteristic_ref",
            "organization_ref",
            "warehouse_ref1c",
            "anchor_period",
            name="ux_stock_ledger_anchor_key_period",
        ),
        Index(
            "ix_stock_ledger_anchor_ledger_key",
            "item_id",
            "characteristic_ref",
            "organization_ref",
            "warehouse_ref1c",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    ingest_batch_id = Column(
        BigInteger,
        ForeignKey("physical_import_batch.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    characteristic_ref = Column(String(36), nullable=False, server_default="")
    organization_ref = Column(String(36), nullable=False, server_default="")
    warehouse_ref1c = Column(String(36), nullable=False, server_default="")
    anchor_period = Column(Date, nullable=False)
    anchor_at = Column(TIMESTAMP, nullable=False, default=func.now(), server_default=func.now())
    balance_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    # source ∈ {balance_seed, s0_freeze}.
    source = Column(String(32), nullable=False, server_default="balance_seed")
    entry_id = Column(BigInteger, nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)

    item = relationship("Item")
    ingest_batch = relationship("PhysicalImportBatch")


class ReservationEntry(Base):
    """One immutable plan reservation plus its FIFO replenishment progress."""

    __tablename__ = "reservation_entry"
    __table_args__ = (
        UniqueConstraint(
            "ledger_generation_id", "requirement_id",
            name="ux_reservation_entry_requirement",
        ),
        CheckConstraint(
            "realization_mode IN ('make', 'buy', 'rework')",
            name="ck_reservation_entry_replenishment_flow",
        ),
        Index(
            "ix_reservation_entry_pool",
            "item_id",
            "characteristic_ref",
            "organization_ref",
            "planning_stock_pool",
            "lifecycle_status",
        ),
        Index("ix_reservation_entry_run_version", "run_id", "freeze_version"),
        Index("ix_reservation_entry_requirement", "requirement_id"),
    )

    def __init__(self, **kwargs):
        # Direct builders must state the immutable replenishment denominator.
        # For the common all-missing case it equals the full reserve.
        reserved = Decimal(str(kwargs.get("reserved_qty", 0) or 0))
        required = kwargs.get("replenishment_required_qty")
        if required is None:
            required = reserved
            kwargs["replenishment_required_qty"] = required
        if "covered_from_stock_at_freeze_qty" not in kwargs:
            kwargs["covered_from_stock_at_freeze_qty"] = max(
                reserved - Decimal(str(required or 0)),
                Decimal("0"),
            )
        if "replenishment_received_qty" not in kwargs:
            kwargs["replenishment_received_qty"] = min(
                max(Decimal(str(kwargs.get("realized_qty", 0) or 0)), Decimal("0")),
                max(Decimal(str(required or 0)), Decimal("0")),
            )
        super().__init__(**kwargs)

    id = Column(BigIntPK, primary_key=True, index=True)
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    characteristic_ref = Column(String(36), nullable=False, server_default="")
    organization_ref = Column(String(36), nullable=False, server_default="")
    planning_stock_pool = Column(String(64), nullable=False, server_default="default")
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=True, index=True)
    freeze_version = Column(Integer, nullable=False, server_default="0")
    requirement_id = Column(Integer, ForeignKey("mrp_requirement.id", ondelete="CASCADE"), nullable=False)
    priority_period_from = Column(Date, nullable=False)
    priority_period_to = Column(Date, nullable=False)
    # Compatibility storage name. ``rework`` is a known, executor-less
    # rework reserve: it is not materialized into a working journal but can be
    # closed by an accepted physical assembly output.
    realization_mode = Column(String(10), nullable=False, server_default="make")
    reserved_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    covered_from_stock_at_freeze_qty = Column(
        DECIMAL(15, 3), nullable=False, default=0.0, server_default="0"
    )
    replenishment_required_qty = Column(
        DECIMAL(15, 3), nullable=False, default=0.0, server_default="0"
    )
    replenishment_received_qty = Column(
        DECIMAL(15, 3), nullable=False, default=0.0, server_default="0"
    )
    # Compatibility cache during the code migration. It mirrors
    # replenishment_received_qty and is not an independent source.
    realized_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    # Active rows participate in FIFO; released rows are historical only.
    lifecycle_status = Column(String(20), nullable=False, server_default="active")
    opened_at = Column(TIMESTAMP, nullable=True)
    closed_at = Column(TIMESTAMP, nullable=True)
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now(), server_default=func.now(), nullable=False)

    item = relationship("Item")
    run = relationship("PlanningRun")
    requirement = relationship("MrpRequirement")
    ledger_generation = relationship("LedgerGeneration")


class ReservationEvent(Base):
    """Ledger-2 append-only reservation journal (). Fold by
    reservation gives reserved_qty/realized_qty; fold by item-key gives the
    pool's reserved_soft. Never UPDATE/DELETE (analogue of И5). idempotency_key
    makes a re-run non-duplicating."""

    __tablename__ = "reservation_event"
    __table_args__ = (
        UniqueConstraint(
            "ledger_generation_id", "idempotency_key",
            name="ux_reservation_event_idempotency",
        ),
        Index("ix_reservation_event_reservation", "reservation_id"),
        Index(
            "ix_reservation_event_pool",
            "item_id",
            "characteristic_ref",
            "organization_ref",
            "planning_stock_pool",
        ),
        Index("ix_reservation_event_sle", "sle_id"),
    )

    id = Column(BigIntPK, primary_key=True, index=True)
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reservation_id = Column(BigInteger, ForeignKey("reservation_entry.id", ondelete="CASCADE"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    characteristic_ref = Column(String(36), nullable=False, server_default="")
    organization_ref = Column(String(36), nullable=False, server_default="")
    planning_stock_pool = Column(String(64), nullable=False, server_default="default")
    # event_kind ∈ {open, amend, realize, unrealize, cancel, release,
    # carry_out, carry_in, close, reopen} ().
    event_kind = Column(String(20), nullable=False, server_default="")
    reserved_delta = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    realized_delta = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    sle_id = Column(BigInteger, ForeignKey("stock_ledger_entry.id", ondelete="SET NULL"), nullable=True)
    fact_ref = Column(String(64), nullable=False, server_default="")
    fact_line_ref = Column(String(64), nullable=False, server_default="")
    # match_rule ∈ {pegged, fifo, manual} ().
    match_rule = Column(String(20), nullable=False, server_default="")
    cycle_id = Column(String(64), nullable=False, server_default="")
    idempotency_key = Column(String(120), nullable=False)
    event_at = Column(TIMESTAMP, nullable=False, default=func.now(), server_default=func.now())
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)

    reservation = relationship("ReservationEntry")
    item = relationship("Item")
    ledger_generation = relationship("LedgerGeneration")


class ReservationConsumptionAllocation(Base):
    """One immutable generation-scoped physical consumption assignment for a reservation.

    Consumption logic consumes against this table in §16 instead of rewriting
    legacy coverage columns, preserving a full historical trail per accepted
    generation.
    """

    __tablename__ = "reservation_consumption_allocation"
    __table_args__ = (
        UniqueConstraint(
            "ledger_generation_id",
            "idempotency_key",
            name="uq_reservation_consumption_allocation_generation_idempotency",
        ),
        UniqueConstraint(
            "ledger_generation_id",
            "sle_id",
            "reservation_id",
            name="uq_res_consumption_generation_sle_reservation",
        ),
        CheckConstraint(
            "match_rule IN ('pegged', 'fifo')",
            name="ck_reservation_consumption_allocation_match_rule",
        ),
        CheckConstraint(
            "allocated_qty > 0",
            name="ck_reservation_consumption_allocation_qty_positive",
        ),
        Index(
            "ix_reservation_consumption_allocation_generation",
            "ledger_generation_id",
        ),
        Index(
            "ix_reservation_consumption_allocation_reservation",
            "reservation_id",
        ),
        Index("ix_reservation_consumption_allocation_sle", "sle_id"),
        Index(
            "ix_reservation_consumption_allocation_requirement",
            "requirement_id",
        ),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reservation_id = Column(
        BigInteger,
        ForeignKey("reservation_entry.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sle_id = Column(
        BigInteger,
        ForeignKey("stock_ledger_entry.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requirement_id = Column(
        Integer,
        ForeignKey("mrp_requirement.id", ondelete="RESTRICT"),
        nullable=False,
    )
    allocated_qty = Column(DECIMAL(15, 3), nullable=False)
    match_rule = Column(String(16), nullable=False)
    fact_ref = Column(String(64), nullable=False, server_default="")
    fact_line_ref = Column(String(64), nullable=False, server_default="")
    item_id = Column(
        Integer,
        ForeignKey("items.item_id", ondelete="RESTRICT"),
        nullable=False,
    )
    characteristic_ref = Column(String(36), nullable=False, server_default="")
    organization_ref = Column(String(36), nullable=False, server_default="")
    planning_stock_pool = Column(String(64), nullable=False, server_default="default")
    idempotency_key = Column(String(160), nullable=False)
    event_at = Column(TIMESTAMP, nullable=False, default=func.now(), server_default=func.now())
    ingested_at = Column(
        TIMESTAMP, nullable=False, default=func.now(), server_default=func.now(),
    )

    ledger_generation = relationship("LedgerGeneration")
    reservation = relationship("ReservationEntry")
    stock_ledger_entry = relationship("StockLedgerEntry")
    requirement = relationship("MrpRequirement")
    item = relationship("Item")


class ReplenishmentWorkItem(Base):
    """One immutable unified replenishment journal line (make/buy).

    Each active reservation with positive replenishment requirement maps to
    exactly one work item in one generation. Idempotency is guaranteed by
    generation + reservation uniqueness and deterministic rebuild.
    """

    __tablename__ = "replenishment_work_item"
    __table_args__ = (
        UniqueConstraint(
            "ledger_generation_id", "reservation_id",
            name="uq_replenishment_work_item_generation_reservation",
        ),
        CheckConstraint(
            "replenishment_method IN ('make', 'buy')",
            name="ck_replenishment_work_item_method",
        ),
        CheckConstraint(
            "replenishment_required_qty >= 0",
            name="ck_replenishment_work_item_required_nonnegative",
        ),
        CheckConstraint(
            "replenishment_fulfilled_qty >= 0",
            name="ck_replenishment_work_item_fulfilled_nonnegative",
        ),
        CheckConstraint(
            "replenishment_remaining_qty >= 0",
            name="ck_replenishment_work_item_remaining_nonnegative",
        ),
        CheckConstraint(
            "replenishment_required_qty >= replenishment_fulfilled_qty",
            name="ck_replenishment_work_item_fulfilled_le_required",
        ),
        CheckConstraint(
            "replenishment_required_qty >= replenishment_remaining_qty",
            name="ck_replenishment_work_item_remaining_le_required",
        ),
        CheckConstraint(
            "replenishment_remaining_qty = "
            "replenishment_required_qty - replenishment_fulfilled_qty",
            name="ck_replenishment_work_item_remaining_exact",
        ),
        Index("ix_replenishment_work_item_generation", "ledger_generation_id"),
        Index("ix_replenishment_work_item_plan", "plan_id"),
        Index("ix_replenishment_work_item_run", "run_id"),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    ledger_generation_id = Column(
        BigInteger,
        ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reservation_id = Column(
        BigInteger,
        ForeignKey("reservation_entry.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan_id = Column(
        Integer,
        ForeignKey("production_plan_header.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    run_id = Column(
        Integer,
        ForeignKey("planning_run.run_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    requirement_id = Column(
        Integer,
        ForeignKey("mrp_requirement.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    item_id = Column(
        Integer,
        ForeignKey("items.item_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    replenishment_method = Column(String(10), nullable=False)
    replenishment_required_qty = Column(
        DECIMAL(15, 3),
        nullable=False,
        default=0.0,
        server_default="0",
    )
    replenishment_fulfilled_qty = Column(
        DECIMAL(15, 3),
        nullable=False,
        default=0.0,
        server_default="0",
    )
    replenishment_remaining_qty = Column(
        DECIMAL(15, 3),
        nullable=False,
        default=0.0,
        server_default="0",
    )
    # Optional execution lineage is preserved for downstream consumers.
    execution_document_kind = Column(String(24), nullable=False, server_default="")
    execution_document_id = Column(BigInteger, nullable=True)
    execution_document_state = Column(String(24), nullable=False, server_default="")
    execution_document_payload = Column(
        CrossPlatformJSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    ledger_generation = relationship("LedgerGeneration")
    reservation = relationship("ReservationEntry")
    plan = relationship("ProductionPlanHeader")
    run = relationship("PlanningRun")
    requirement = relationship("MrpRequirement")
    item = relationship("Item")
