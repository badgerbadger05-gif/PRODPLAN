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


class PlanningComparisonBatch(Base):
    """One immutable comparison attempt between stable and shadow contours."""
    __tablename__ = "planning_comparison_batch"
    __table_args__ = (
        UniqueConstraint("capture_key", name="uq_planning_comparison_batch_capture_key"),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    capture_key = Column(String(128), nullable=False)
    stable_base_url = Column(String(512), nullable=False)
    cutoff_grade = Column(String(16), nullable=False)
    cutoff_reason = Column(TEXT, nullable=True)
    stable_run_key = Column(String(128), nullable=True)
    shadow_run_key = Column(String(128), nullable=True)
    metrics = Column(CrossPlatformJSON, nullable=False, default=dict)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now())


class PlanningComparisonEvent(Base):
    """Append-only audit trail for capture lifecycle."""
    __tablename__ = "planning_comparison_event"

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    batch_id = Column(BigInteger, ForeignKey("planning_comparison_batch.id", ondelete="RESTRICT"), nullable=False, index=True)
    event_type = Column(String(64), nullable=False)
    payload = Column(CrossPlatformJSON, nullable=False, default=dict)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now())


class PlanningComparisonSnapshot(Base):
    """Raw, hashed source payload retained for reproducibility."""
    __tablename__ = "planning_comparison_snapshot"
    __table_args__ = (
        UniqueConstraint("batch_id", "contour", "snapshot_kind", name="uq_planning_comparison_snapshot_axis"),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    batch_id = Column(BigInteger, ForeignKey("planning_comparison_batch.id", ondelete="RESTRICT"), nullable=False, index=True)
    contour = Column(String(16), nullable=False)
    snapshot_kind = Column(String(32), nullable=False)
    raw_payload_hash = Column(String(64), nullable=False)
    payload = Column(CrossPlatformJSON, nullable=False)
    captured_at = Column(TIMESTAMP, nullable=False, server_default=func.now())


class PlanningComparisonRow(Base):
    """Canonical result row; never uses a contour-local numeric identifier."""
    __tablename__ = "planning_comparison_row"
    __table_args__ = (
        UniqueConstraint("batch_id", "contour", "result_kind", "canonical_key", name="uq_planning_comparison_row_axis"),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    batch_id = Column(BigInteger, ForeignKey("planning_comparison_batch.id", ondelete="RESTRICT"), nullable=False, index=True)
    contour = Column(String(16), nullable=False)
    result_kind = Column(String(16), nullable=False)
    canonical_key = Column(String(768), nullable=False)
    item_key = Column(String(128), nullable=False, index=True)
    bucket_date = Column(Date, nullable=True)
    quantity = Column(DECIMAL(24, 6), nullable=False)
    raw_payload_hash = Column(String(64), nullable=False)
    payload = Column(CrossPlatformJSON, nullable=False)


class PlanningComparisonDiff(Base):
    __tablename__ = "planning_comparison_diff"
    __table_args__ = (
        UniqueConstraint("batch_id", "result_kind", "canonical_key", name="uq_planning_comparison_diff_axis"),
    )

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    batch_id = Column(BigInteger, ForeignKey("planning_comparison_batch.id", ondelete="RESTRICT"), nullable=False, index=True)
    result_kind = Column(String(16), nullable=False)
    canonical_key = Column(String(768), nullable=False)
    item_key = Column(String(128), nullable=False, index=True)
    stable_quantity = Column(DECIMAL(24, 6), nullable=False)
    shadow_quantity = Column(DECIMAL(24, 6), nullable=False)
    delta_quantity = Column(DECIMAL(24, 6), nullable=False)
    classification = Column(String(24), nullable=False)


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
    # Item-ledger §2.5: third warehouse-policy label. A finished-goods warehouse
    # is a legitimate 1С warehouse whose SLE are mirrored and whose bin is kept,
    # but the planning pool never sums it (on_hand(P) excludes it) — finished
    # goods are produced straight onto it, outside the planning contour. Additive
    # in Inc1: no reader consults it yet (pool exclusion lands with ingest, inc2+).
    is_finished_goods = Column(Boolean, nullable=False, default=False, server_default="false")
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
        Index(
            "ux_production_products_source_dbr_signal",
            "source_dbr_signal_id",
            unique=True,
            postgresql_where=text("source_dbr_signal_id IS NOT NULL"),
            sqlite_where=text("source_dbr_signal_id IS NOT NULL"),
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
    # DBR feeder signals materialize into the same local production journal.
    # The partial unique index makes that projection idempotent.
    source_dbr_signal_id = Column(
        Integer,
        ForeignKey('dbr_feeder_signal.id', ondelete="SET NULL"),
        nullable=True,
        index=True,
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
    material_coverage_status = Column(String(32), nullable=True, index=True)
    material_coverage_label = Column(String(64), nullable=True)
    material_coverage_calculated_at = Column(TIMESTAMP, nullable=True)
    material_coverage_snapshot = Column(CrossPlatformJSON, nullable=True)
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
    initiated_by = Column(String(100), nullable=True)
    exported_ref1c = Column(String(36), nullable=True, index=True)
    exported_at = Column(TIMESTAMP, nullable=True)
    export_error = Column(TEXT, nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now(), nullable=False)

    product = relationship("ProductionProduct")
    order = relationship("ProductionOrder")
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
    created_at = Column(TIMESTAMP, default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now(), nullable=False)

    issue = relationship("ProductionMaterialIssue", back_populates="lines")
    component_item = relationship("Item")


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
        CheckConstraint("status in ('draft', 'fixed', 'archived')", name="ck_production_plan_header_status"),
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
    # Ledger v2 (Increment 1, additive) — currently active baseline version for
    # this run. Default-1 semantics arrive with the freeze writer (later
    # increment); nullable now so existing rows are untouched.
    active_freeze_version = Column(Integer, nullable=True)

    prior_run = relationship("PlanningRun", remote_side=[run_id])


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
    covered_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0)
    remaining_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0)
    period_from = Column(Date, nullable=False, index=True)
    period_to = Column(Date, nullable=False, index=True)
    bom_level = Column(Integer, nullable=False, default=0)
    # Phase-1 execution-ledger columns. NB (ledger v2): executed_qty and
    # initial_snapshot_stock become *derived caches* in later increments
    # (executed_qty = Σ mrp_execution_allocation rows with kind='execution');
    # no behavior change here, kept as-is for backward compatibility.
    executed_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    carried_remaining = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    initial_snapshot_stock = Column(DECIMAL(15, 3), nullable=True)
    status = Column(String(20), nullable=False, default="open", server_default="open", index=True)
    closed_at = Column(TIMESTAMP, nullable=True)
    # Ledger v2 (Increment 1, additive) — freeze/pool qualification. Unread by
    # logic yet; defaults keep existing rows/behavior unchanged.
    freeze_version = Column(Integer, nullable=True)
    drift_adjustment_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
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


# --- Forced/Manual planning (separate from main MRP run results) ---


class ForcedOrderRequest(Base):
    __tablename__ = "forced_order_request"

    id = Column(Integer, primary_key=True, index=True)
    # Optional linkage to a planning run for context (warnings, horizon, etc.)
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="SET NULL"), nullable=True, index=True)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    need_date = Column(Date, nullable=False, index=True)
    requested_qty = Column(DECIMAL(15, 3), nullable=False)
    # Who/why
    created_by = Column(String(100), nullable=True)
    reason = Column(TEXT, nullable=True)
    # Status tracking
    status = Column(String(20), nullable=False, default="PENDING")  # PENDING | PROCESSED | FAILED
    error = Column(TEXT, nullable=True)
    # Diagnostics snapshot (optional)
    meta = Column(CrossPlatformJSON, nullable=True)
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())


class ForcedOrderResult(Base):
    __tablename__ = "forced_order_result"

    id = Column(Integer, primary_key=True, index=True)
    request_id = Column(Integer, ForeignKey("forced_order_request.id", ondelete="CASCADE"), nullable=False, unique=True)
    # Computed quantities
    planned_qty = Column(DECIMAL(15, 3), nullable=False)
    normalized_qty = Column(DECIMAL(15, 3), nullable=True)
    horizon_limit = Column(DECIMAL(15, 3), nullable=True)
    component_limit = Column(DECIMAL(15, 3), nullable=True)
    # Component shortage breakdown / warnings in a stable JSON shape
    shortage = Column(CrossPlatformJSON, nullable=True)
    created_at = Column(TIMESTAMP, default=func.now())


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
    # planned | success | error | cancelled
    status = Column(String(20), nullable=False, default="planned", server_default="planned", index=True)
    last_error = Column(TEXT, nullable=True)
    last_synced_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False)


# ---------------------------------------------------------------------------
# DBR (Drum-Buffer-Rope) parallel planning module — settings/config tables.
# Ported from ERPNext prodflow (ProdFlow Planning Settings / Assembly Rate /
# child supply-risk settings). See .docs/dbr_parallel_module_roadmap.md §3.
# Read-only against shared tables; only these dbr_* tables are module-owned.
# ---------------------------------------------------------------------------


class DbrSettings(Base):
    """
    Singleton row (id=1) of DBR planning settings. Mirrors ERPNext prodflow
    `ProdFlow Planning Settings`. Warehouse roles reference
    stock_warehouses.warehouse_ref1c by value (no hard FK — warehouses are
    synced from 1C and may be (re)created independently).
    """
    __tablename__ = "dbr_settings"

    id = Column(Integer, primary_key=True, index=True)
    # Barabar / gate horizon
    frozen_days = Column(Integer, nullable=False, default=3, server_default="3")
    gate_horizon_workdays = Column(Integer, nullable=False, default=10, server_default="10")
    shelf_threshold_qty = Column(DECIMAL(12, 3), nullable=False, default=5, server_default="5")
    # Replenishment-time classes (days)
    rt_machining_days = Column(Integer, nullable=False, default=7, server_default="7")
    rt_welding_days = Column(Integer, nullable=False, default=15, server_default="15")
    rt_painting_days = Column(Integer, nullable=False, default=21, server_default="21")
    # Batch (green-zone) days per operation kind
    batch_days_turning = Column(Integer, nullable=False, default=10, server_default="10")
    batch_days_bending = Column(Integer, nullable=False, default=7, server_default="7")
    batch_days_welding = Column(Integer, nullable=False, default=5, server_default="5")
    batch_days_paint_black = Column(Integer, nullable=False, default=2, server_default="2")
    batch_days_paint_color = Column(Integer, nullable=False, default=3, server_default="3")
    # Feeder chain
    feeder_chain_enabled = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    feeder_load_horizon_weeks = Column(Integer, nullable=False, default=4, server_default="4")
    # Давальческая переработка (питатель №3, фаза 4): RT всей цепочки
    # (мехцех → ожидание рейса → кругорейс → приёмка) и рейс-интервал (квант
    # партии = ADU × интервал). См. питатель-3-гальваника-round-trip.md.
    rt_processing_days = Column(Integer, nullable=False, default=25, server_default="25")
    processing_trip_interval_days = Column(Integer, nullable=False, default=7, server_default="7")
    # Порог алерта просроченного кругорейса: открытый заказ переработчику
    # старше N дней = партия у подрядчика дольше нормы (дока §5).
    processing_roundtrip_days = Column(Integer, nullable=False, default=14, server_default="14")
    # Shelf warehouses (roles): №2 (mechshop WIP), №3 (painted), №4 (hull #2).
    # FK-semantics on stock_warehouses.warehouse_ref1c, but no hard FK.
    w2_warehouse_ref1c = Column(String(36), nullable=True)
    w3_warehouse_ref1c = Column(String(36), nullable=True)
    w4_warehouse_ref1c = Column(String(36), nullable=True)
    # Fastener (метизы) item-category names excluded from the kit as free-issue.
    # JSON list of ItemCategory.category_name values; empty list = nobody is a
    # fastener. Mirrors ERPNext prodflow FASTENER_ITEM_GROUPS.
    fastener_categories = Column(CrossPlatformJSON, nullable=False, default=list, server_default=text("'[]'"))
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False)


class DbrAssemblyRate(Base):
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


class DbrCategorySupplyRisk(Base):
    """
    Per-category (1С item group) supply-risk safety percentage and the
    warehouse where the category is received. Mirrors ERPNext prodflow child
    "category supply risk" settings.
    """
    __tablename__ = "dbr_category_supply_risk"
    __table_args__ = (
        UniqueConstraint("item_group", name="ux_dbr_category_supply_risk_group"),
    )

    id = Column(Integer, primary_key=True, index=True)
    item_group = Column(String(255), nullable=False, index=True)
    receipt_warehouse_ref1c = Column(String(36), nullable=True)
    supply_risk_pct = Column(DECIMAL(6, 2), nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False)


class DbrSupermarketPosition(Base):
    """Static Phase-2 buffer sizing for one item at one destination shelf."""

    __tablename__ = "dbr_supermarket_position"
    __table_args__ = (
        UniqueConstraint(
            "item_id", "warehouse_ref1c", name="ux_dbr_supermarket_position_item_warehouse"
        ),
        CheckConstraint("adu >= 0", name="ck_dbr_supermarket_position_adu_nonnegative"),
        CheckConstraint("commonality >= 0", name="ck_dbr_supermarket_position_commonality_nonnegative"),
        CheckConstraint("rt_days >= 0", name="ck_dbr_supermarket_position_rt_nonnegative"),
        CheckConstraint("batch_days >= 0", name="ck_dbr_supermarket_position_batch_nonnegative"),
        CheckConstraint("q_batch >= 0", name="ck_dbr_supermarket_position_q_batch_nonnegative"),
        CheckConstraint("k_var >= 0", name="ck_dbr_supermarket_position_k_var_nonnegative"),
        CheckConstraint("k_var <= 1", name="ck_dbr_supermarket_position_k_var_bounded"),
        CheckConstraint("supply_risk_pct >= 0", name="ck_dbr_supermarket_position_supply_risk_nonnegative"),
        CheckConstraint("supply_type IN ('manufacture', 'purchase', 'processing')", name="ck_dbr_supermarket_position_supply_type_allowed"),
        CheckConstraint("mode IN ('shelf', 'under_schedule')", name="ck_dbr_supermarket_position_mode_allowed"),
        CheckConstraint("rt_source IN ('class', 'lead_time', 'chain')", name="ck_dbr_supermarket_position_rt_source_allowed"),
        CheckConstraint("red_qty >= 0 AND yellow_qty >= 0 AND green_qty >= 0 AND target_qty >= 0", name="ck_dbr_supermarket_position_zones_nonnegative"),
    )

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("items.item_id", ondelete="CASCADE"), nullable=False, index=True)
    warehouse_ref1c = Column(String(36), nullable=False, index=True)
    supply_type = Column(String(20), nullable=False)
    mode = Column(String(20), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true", index=True)
    is_stale = Column(Boolean, nullable=False, default=False, server_default="false", index=True)
    adu = Column(DECIMAL(16, 4), nullable=False)
    commonality = Column(Integer, nullable=False)
    route_class = Column(String(40), nullable=True)
    rt_days = Column(DECIMAL(10, 3), nullable=False)
    rt_source = Column(String(20), nullable=False, default="class", server_default="class")
    batch_days = Column(DECIMAL(10, 3), nullable=False)
    q_batch = Column(DECIMAL(16, 3), nullable=False)
    k_var = Column(DECIMAL(6, 3), nullable=False)
    supply_risk_pct = Column(DECIMAL(8, 3), nullable=False, default=0, server_default="0")
    red_qty = Column(DECIMAL(16, 3), nullable=False)
    yellow_qty = Column(DECIMAL(16, 3), nullable=False)
    green_qty = Column(DECIMAL(16, 3), nullable=False)
    target_qty = Column(DECIMAL(16, 3), nullable=False)
    source_schedule_id = Column(Integer, ForeignKey("dbr_drum_schedule.id", ondelete="SET NULL"), nullable=True, index=True)
    data_quality = Column(CrossPlatformJSON, nullable=False, default=list, server_default=text("'[]'"))
    calculation_snapshot = Column(CrossPlatformJSON, nullable=False, default=dict, server_default=text("'{}'"))
    calculated_at = Column(TIMESTAMP, nullable=False, default=func.now(), server_default=func.now())
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False)

    item = relationship("Item")
    source_schedule = relationship("DbrDrumSchedule")


class DbrFeederSignal(Base):
    """Advisory replenishment signal; never an order or a launch command."""

    __tablename__ = "dbr_feeder_signal"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="ux_dbr_feeder_signal_dedup_key"),
        CheckConstraint("signal_type IN ('Пополнение', 'Под график', 'Цепочка')", name="ck_dbr_feeder_signal_type"),
        CheckConstraint(
            "status IN ('Open', 'Diagnostic', 'Order Created', 'In Work', 'Done', 'Cancelled')",
            name="ck_dbr_feeder_signal_status",
        ),
        CheckConstraint("suggested_qty >= 0", name="ck_dbr_feeder_signal_qty_nonnegative"),
    )

    id = Column(Integer, primary_key=True, index=True)
    dedup_key = Column(String(66), nullable=False, index=True)
    signal_type = Column(String(30), nullable=False, default="Пополнение", server_default="Пополнение")
    # Chain signals ("Цепочка") are pegged to a parent signal, not to a shelf
    # position — hence the position is nullable for that family.
    supermarket_position_id = Column(
        Integer,
        ForeignKey("dbr_supermarket_position.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    item_id = Column(Integer, ForeignKey("items.item_id", ondelete="CASCADE"), nullable=False, index=True)
    warehouse_ref1c = Column(String(36), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="Open", server_default="Open", index=True)
    suggested_qty = Column(DECIMAL(16, 3), nullable=False, default=0, server_default="0")
    priority = Column(DECIMAL(16, 6), nullable=False, default=0, server_default="0", index=True)
    zone = Column(String(20), nullable=True, index=True)
    nfp_snapshot = Column(DECIMAL(16, 3), nullable=True)
    target_qty_snapshot = Column(DECIMAL(16, 3), nullable=True)
    kit_force = Column(Boolean, nullable=False, default=False, server_default="false")
    kit_shortage_qty = Column(DECIMAL(16, 3), nullable=False, default=0, server_default="0")
    # Chain pegging (Фаза 3.2): child "Цепочка" signal points at the signal whose
    # kit deficit spawned it; chain_depth guards against pathological BOM trees.
    parent_signal_id = Column(
        Integer, ForeignKey("dbr_feeder_signal.id", ondelete="CASCADE"), nullable=True, index=True
    )
    chain_depth = Column(Integer, nullable=False, default=0, server_default="0")
    source_schedule_id = Column(Integer, ForeignKey("dbr_drum_schedule.id", ondelete="SET NULL"), nullable=True, index=True)
    drum_slot_id = Column(Integer, ForeignKey("dbr_drum_slot.id", ondelete="CASCADE"), nullable=True, index=True)
    need_date = Column(Date, nullable=True, index=True)
    required_date = Column(Date, nullable=True, index=True)
    raw_demand_qty = Column(DECIMAL(16, 3), nullable=True)
    raw_shortage_qty = Column(DECIMAL(16, 3), nullable=True)
    calculated_batch_qty = Column(DECIMAL(16, 3), nullable=True)
    data_quality = Column(CrossPlatformJSON, nullable=False, default=list, server_default=text("'[]'"))
    is_incomplete = Column(Boolean, nullable=False, default=False, server_default="false", index=True)
    reason_json = Column(CrossPlatformJSON, nullable=False, default=dict, server_default=text("'{}'"))
    # Materialization (Фаза 3): the 1С Document_ЗаказНаПроизводство created when
    # this signal was launched. Stamped by services/dbr/materialize_service.
    one_c_order_ref = Column(String(36), nullable=True, index=True)
    one_c_order_number = Column(String(50), nullable=True)
    refreshed_at = Column(TIMESTAMP, nullable=False, default=func.now(), server_default=func.now())
    cancelled_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False)

    position = relationship("DbrSupermarketPosition")
    item = relationship("Item")
    source_schedule = relationship("DbrDrumSchedule")
    drum_slot = relationship("DbrDrumSlot")
    parent_signal = relationship("DbrFeederSignal", remote_side=[id])


# --------------------------------------------------------------------------
# DBR Phase 1 — production program, drum schedule, slots, capacity gaps
# --------------------------------------------------------------------------


class DbrProductionProgram(Base):
    """Производственная программа выпуска (аналог ERPNext prodflow
    `ProdFlow Production Program`). Строки в dbr_production_program_item."""

    __tablename__ = "dbr_production_program"

    id = Column(Integer, primary_key=True, index=True)
    company = Column(String(255), nullable=True)
    title = Column(String(255), nullable=True)
    from_date = Column(Date, nullable=False)
    to_date = Column(Date, nullable=False)
    # draft / approved / closed / cancelled
    status = Column(String(20), nullable=False, default="draft", server_default="draft", index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False)

    items = relationship(
        "DbrProductionProgramItem",
        back_populates="program",
        cascade="all, delete-orphan",
    )


class DbrProductionProgramItem(Base):
    __tablename__ = "dbr_production_program_item"

    id = Column(Integer, primary_key=True, index=True)
    program_id = Column(
        Integer,
        ForeignKey("dbr_production_program.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    program_date = Column(Date, nullable=False, index=True)
    qty = Column(DECIMAL(14, 3), nullable=False)
    comment = Column(TEXT, nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False)

    program = relationship("DbrProductionProgram", back_populates="items")
    item = relationship("Item")


class DbrDrumSchedule(Base):
    """Катящийся график барабана (аналог `ProdFlow Drum Schedule`).

    config_snapshot фиксирует настройки DBR на момент расчёта, чтобы результат
    был воспроизводим независимо от последующих правок dbr_settings.
    """

    __tablename__ = "dbr_drum_schedule"
    __table_args__ = (
        Index(
            "ux_dbr_drum_schedule_one_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    period_from = Column(Date, nullable=False)
    period_to = Column(Date, nullable=False)
    source_program_id = Column(
        Integer,
        ForeignKey("dbr_production_program.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # draft / active / superseded / cancelled
    status = Column(String(20), nullable=False, default="draft", server_default="draft", index=True)
    config_snapshot = Column(CrossPlatformJSON, nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False)

    slots = relationship(
        "DbrDrumSlot",
        back_populates="schedule",
        cascade="all, delete-orphan",
    )
    capacity_gaps = relationship(
        "DbrDrumCapacityGap",
        back_populates="schedule",
        cascade="all, delete-orphan",
    )
    source_program = relationship("DbrProductionProgram")
    covered_programs = relationship(
        "DbrDrumScheduleProgram",
        back_populates="schedule",
        cascade="all, delete-orphan",
    )


class DbrDrumScheduleProgram(Base):
    """Idempotency marker for programs materialized into one drum schedule."""

    __tablename__ = "dbr_drum_schedule_program"
    __table_args__ = (
        UniqueConstraint(
            "schedule_id",
            "program_id",
            name="ux_dbr_drum_schedule_program",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    schedule_id = Column(
        Integer,
        ForeignKey("dbr_drum_schedule.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    program_id = Column(
        Integer,
        ForeignKey("dbr_production_program.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(
        TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False
    )

    schedule = relationship("DbrDrumSchedule", back_populates="covered_programs")
    program = relationship("DbrProductionProgram")


class DbrDrumSlot(Base):
    """Плитка барабана: одно изделие × участок × день.

    planned_date фиксируется при рождении плитки и не меняется; slot_date
    подвижна (перенос/roll-forward). Границы жизненного цикла — release_status.
    """

    __tablename__ = "dbr_drum_slot"

    id = Column(Integer, primary_key=True, index=True)
    schedule_id = Column(
        Integer,
        ForeignKey("dbr_drum_schedule.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    slot_date = Column(Date, nullable=False, index=True)
    planned_date = Column(Date, nullable=False)
    resource_id = Column(Integer, ForeignKey("production_resources.resource_id"), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    qty = Column(DECIMAL(14, 3), nullable=False)
    produced_qty = Column(DECIMAL(14, 3), nullable=False, default=0, server_default="0")
    # green / yellow / red / unknown
    kit_status = Column(String(10), nullable=False, default="unknown", server_default="unknown", index=True)
    shortage_json = Column(CrossPlatformJSON, nullable=True)
    # pending / released / completed
    release_status = Column(String(12), nullable=False, default="pending", server_default="pending", index=True)
    # Materialization (Фаза 3): the 1С Document_ЗаказНаПроизводство created when
    # this slot was released. Stamped by services/dbr/materialize_service.
    one_c_order_ref = Column(String(36), nullable=True, index=True)
    one_c_order_number = Column(String(50), nullable=True)
    source_program_id = Column(
        Integer,
        ForeignKey("dbr_production_program.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    position = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False)

    schedule = relationship("DbrDrumSchedule", back_populates="slots")
    resource = relationship("ProductionResource")
    item = relationship("Item")


class DbrDrumCapacityGap(Base):
    """Разрыв мощности: сколько штук изделия не влезло в такт участка в день."""

    __tablename__ = "dbr_drum_capacity_gap"

    id = Column(Integer, primary_key=True, index=True)
    schedule_id = Column(
        Integer,
        ForeignKey("dbr_drum_schedule.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    gap_date = Column(Date, nullable=True, index=True)
    resource_id = Column(Integer, ForeignKey("production_resources.resource_id"), nullable=True, index=True)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=True, index=True)
    required_qty = Column(DECIMAL(14, 3), nullable=False)
    takt_qty = Column(DECIMAL(14, 3), nullable=False)
    gap_qty = Column(DECIMAL(14, 3), nullable=False)
    resolution = Column(String(64), nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False)

    schedule = relationship("DbrDrumSchedule", back_populates="capacity_gaps")
    resource = relationship("ProductionResource")
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
# MRP Execution Ledger v2 — Increment 1 (ADDITIVE SCHEMA ONLY).
#
# See mrp-ledger-blueprint-v2.md §3. These tables model the frozen-plan
# baseline, its allocations/BOM snapshot, the rebuilt-each-cycle execution
# ledger, the carry trace and the drift-event log. NO logic reads them yet;
# creating them changes zero behavior. Persisted-immutable tables (baseline /
# freeze_allocation / freeze_component / carry) capture the moment-of-freeze
# snapshot; the rebuildable ones (execution_allocation / drift_event) are
# derived caches restored each cycle.
#
# Pool qualification (v2 §2): pool_key = (item_id, characteristic_ref,
# organization_ref, planning_stock_pool). Pool columns are nullable here for
# additive consistency with MrpRequirement's new pool columns; an empty
# characteristic is a distinct key value, not a wildcard (v2 §2). See the
# INCREMENT-1 report note on normalizing empty pool keys before these tables
# carry data (owner decision).
# ---------------------------------------------------------------------------


class MrpFreezeBaseline(Base):
    """Run-scoped, versioned frozen snapshot of a pool's supply position at
    freeze time (v2 §3). Immutable versions: refreeze = INSERT version+1;
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
    stock_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    produced_total = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    received_total = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    unit_coef = Column(DECIMAL(15, 3), nullable=False, default=1.0, server_default="1")
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)

    run = relationship("PlanningRun")
    item = relationship("Item")


class MrpFreezeAllocation(Base):
    """Coverage-carrying frozen allocation (v2 §3): binds a requirement to a
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
    """Frozen BOM / consumption norms (v2 §3). Writer = freeze; reader = drift
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


class MrpExecutionAllocation(Base):
    """Explainable read-ledger: fact → requirement (v2 §3). Fully rebuilt each
    cycle. kind='execution' consumes net (Σ over buckets = executed_qty);
    kind='coverage_realization' (a fact reshaping already-counted coverage,
    e.g. supplier→stock) does NOT count toward executed."""

    __tablename__ = "mrp_execution_allocation"
    __table_args__ = (
        UniqueConstraint(
            "requirement_id",
            "bucket_id",
            "fact_type",
            "fact_ref",
            "fact_line_ref",
            "allocation_kind",
            name="ux_mrp_execution_allocation_fact",
        ),
        Index("ix_mrp_execution_allocation_cycle", "cycle_id"),
        Index("ix_mrp_execution_allocation_requirement", "requirement_id"),
        Index("ix_mrp_execution_allocation_bucket", "bucket_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    cycle_id = Column(String(64), nullable=False, server_default="")
    requirement_id = Column(Integer, ForeignKey("mrp_requirement.id", ondelete="CASCADE"), nullable=False, index=True)
    bucket_id = Column(Integer, ForeignKey("mrp_requirement_bucket.id", ondelete="CASCADE"), nullable=True, index=True)
    # fact_type ∈ {linked_production, unlinked_production, supplier_receipt,
    # carry, manual_surplus, drift_surplus}; allocation_kind ∈ {execution,
    # coverage_realization}. Empty string = distinct key value (not wildcard).
    fact_type = Column(String(32), nullable=False, server_default="")
    allocation_kind = Column(String(32), nullable=False, server_default="")
    fact_ref = Column(String(64), nullable=False, server_default="")
    fact_line_ref = Column(String(64), nullable=False, server_default="")
    fact_date = Column(TIMESTAMP, nullable=True)
    allocated_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    freeze_allocation_id = Column(
        Integer, ForeignKey("mrp_freeze_allocation.id", ondelete="SET NULL"), nullable=True, index=True
    )
    origin_requirement_id = Column(
        Integer, ForeignKey("mrp_requirement.id", ondelete="SET NULL"), nullable=True, index=True
    )
    calculated_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)

    requirement = relationship("MrpRequirement", foreign_keys=[requirement_id])
    bucket = relationship("MrpRequirementBucket")
    freeze_allocation = relationship("MrpFreezeAllocation")
    origin_requirement = relationship("MrpRequirement", foreign_keys=[origin_requirement_id])


class MrpRequirementCarry(Base):
    """Carry trace (v2 §3): source_requirement → target_requirement. UNIQUE on
    source_requirement_id enforces once-only + idempotent carry."""

    __tablename__ = "mrp_requirement_carry"
    __table_args__ = (
        UniqueConstraint("source_requirement_id", name="ux_mrp_requirement_carry_source"),
        Index("ix_mrp_requirement_carry_target", "target_requirement_id"),
        Index(
            "ix_mrp_requirement_carry_pool",
            "item_id",
            "characteristic_ref",
            "organization_ref",
            "planning_stock_pool",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    source_requirement_id = Column(Integer, ForeignKey("mrp_requirement.id", ondelete="CASCADE"), nullable=False)
    target_requirement_id = Column(Integer, ForeignKey("mrp_requirement.id", ondelete="CASCADE"), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    characteristic_ref = Column(String(36), nullable=True)
    organization_ref = Column(String(36), nullable=True)
    planning_stock_pool = Column(String(64), nullable=True)
    carried_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    carried_at = Column(TIMESTAMP, nullable=True)
    operator = Column(String(100), nullable=True)
    source_run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="SET NULL"), nullable=True, index=True)
    target_run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)

    source_requirement = relationship("MrpRequirement", foreign_keys=[source_requirement_id])
    target_requirement = relationship("MrpRequirement", foreign_keys=[target_requirement_id])
    item = relationship("Item")


class MrpDriftEvent(Base):
    """Explainability of drift (v2 §3), rewritten each cycle. matured=false =
    pending_drift (visible, not materialized)."""

    __tablename__ = "mrp_drift_event"
    __table_args__ = (
        Index("ix_mrp_drift_event_cycle", "cycle_id"),
        Index(
            "ix_mrp_drift_event_pool",
            "item_id",
            "characteristic_ref",
            "organization_ref",
            "planning_stock_pool",
        ),
        Index("ix_mrp_drift_event_requirement", "requirement_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    cycle_id = Column(String(64), nullable=False, server_default="", index=True)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    characteristic_ref = Column(String(36), nullable=True)
    organization_ref = Column(String(36), nullable=True)
    planning_stock_pool = Column(String(64), nullable=True)
    # kind ∈ {shortfall, surplus, evaporation}.
    kind = Column(String(32), nullable=False, server_default="")
    drift_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    expected_stock = Column(DECIMAL(15, 3), nullable=True)
    actual_stock = Column(DECIMAL(15, 3), nullable=True)
    matured = Column(Boolean, nullable=False, default=False, server_default="false")
    first_seen_cycle_id = Column(String(64), nullable=True)
    requirement_id = Column(Integer, ForeignKey("mrp_requirement.id", ondelete="SET NULL"), nullable=True, index=True)
    details = Column(CrossPlatformJSON, nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)

    item = relationship("Item")
    requirement = relationship("MrpRequirement")


# ---------------------------------------------------------------------------
# ITEM-LEDGER (item-центричный двойной леджер) — Increment 1, additive schema.
#
# Two append-only ledgers whose fold gives an item's state (design §1–§2):
#   Ledger-1 (physical movements, mirror of 1С AccumulationRegister): keyed
#     physically (item, characteristic, organization, warehouse) — stock_ledger_
#     entry (signed qty + running qty_after), stock_bin (on_hand fold),
#     stock_recorder_pull (pull idempotency), stock_ledger_anchor (seed/S0).
#   Ledger-2 (soft reservations, PRODPLAN-owned): keyed by planning pool —
#     reservation_entry (materialized fold, per requirement×mode),
#     reservation_event (append-only journal, idempotency_key), reservation_
#     coverage (frozen pins + floating distribution projection).
#
# Inc1 is schema + pure fold/redistribute functions only: NO writer is wired
# into freeze/cycle/reconcile, and no reader consults these tables — zero
# behavior change. Pool/key columns are NOT NULL default '' (design §2.2): an
# empty characteristic/organization is a distinct key value, never a wildcard.
# ---------------------------------------------------------------------------


class StockLedgerEntry(Base):
    """Ledger-1 append-only physical movement (design §2.1 / stock-doc §2.1).
    Signed ``qty`` (receipt > 0, expense < 0, base UoM); ``qty_after`` is the
    running balance projection (R-A). One row per (recorder, line); replacement
    is by-recorder (delete+reinsert), never UPDATE of an applied row (И5)."""

    __tablename__ = "stock_ledger_entry"
    __table_args__ = (
        UniqueConstraint(
            "recorder_type",
            "recorder_ref",
            "line_no",
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
    # recorder identity: 1С document type / GUID / LineNumber (string, inc0).
    recorder_type = Column(String(64), nullable=False, server_default="")
    recorder_ref = Column(String(64), nullable=False, server_default="")
    line_no = Column(String(32), nullable=False, server_default="")
    # ingest_source ∈ {pull, balance_reconcile, seed, adjustment}.
    ingest_source = Column(String(32), nullable=False, server_default="")
    active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)

    item = relationship("Item")


class StockBin(Base):
    """Ledger-1 fold cache: on_hand per physical key (design §2.1). on_hand =
    Σ qty over the key's SLE (= last qty_after). reconcile_pending_qty holds a
    debounced Balance-vs-ledger delta (written by the reconcile step, inc3)."""

    __tablename__ = "stock_bin"
    __table_args__ = (
        UniqueConstraint(
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
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    characteristic_ref = Column(String(36), nullable=False, server_default="")
    organization_ref = Column(String(36), nullable=False, server_default="")
    warehouse_ref1c = Column(String(36), nullable=False, server_default="")
    on_hand = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    # Debounced Balance-vs-ledger delta (inc3 §3б step 3): a non-zero value means
    # a drift was seen last sweep and is awaiting a confirming second sweep.
    reconcile_pending_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    # When this key was last confirmed reconciled against 1С /Balance (|delta|≤EPS
    # matched, or an adjustment-SLE applied). NULL = never reconciled (inc3).
    last_reconciled_at = Column(TIMESTAMP, nullable=True)
    last_entry_id = Column(BigInteger, nullable=True)
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now(), server_default=func.now(), nullable=False)

    item = relationship("Item")


class StockRecorderPull(Base):
    """Ledger-1 pull idempotency ledger (design §2.1). One row per 1С recorder
    already mirrored, so a re-pull of the same document is a no-op (inc2)."""

    __tablename__ = "stock_recorder_pull"
    __table_args__ = (
        UniqueConstraint("recorder_type", "recorder_ref", name="ux_stock_recorder_pull_recorder"),
        Index("ix_stock_recorder_pull_pulled_at", "pulled_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    recorder_type = Column(String(64), nullable=False, server_default="")
    recorder_ref = Column(String(64), nullable=False, server_default="")
    line_count = Column(Integer, nullable=False, default=0, server_default="0")
    # status ∈ {pending, done, empty, error} (inc2). A hook enqueues 'pending';
    # the puller sets done/empty/error. 'pulled' remains the inc1 legacy default.
    status = Column(String(20), nullable=False, server_default="pulled")
    # What put the recorder on the queue (e.g. 'manufacture_export',
    # 'stock_transfer_export', 'reconcile'); diagnostic only.
    source = Column(String(64), nullable=False, default="", server_default="")
    # Retry bookkeeping for process_pending_pulls (attempt cap) + last error text.
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    last_error = Column(TEXT, nullable=True)
    pulled_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now(), server_default=func.now(), nullable=False)


class StockLedgerAnchor(Base):
    """Ledger-1 anchor (design §2.1 / stock-doc §5): a per-key seed/S0 point.
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


class ReservationEntry(Base):
    """Ledger-2 materialized reservation (design §2.2): one row per requirement
    × realization_mode. Caches (reserved_qty/realized_qty) are the fold of
    reservation_event; covered_*/uncovered_qty/coverage_state are the
    redistribute() projection. reserved_qty is the plan's draw on the pool, NOT
    the shortfall — shortfall is the derived uncovered_qty."""

    __tablename__ = "reservation_entry"
    __table_args__ = (
        UniqueConstraint("requirement_id", "realization_mode", name="ux_reservation_entry_req_mode"),
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

    id = Column(BigIntPK, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    characteristic_ref = Column(String(36), nullable=False, server_default="")
    organization_ref = Column(String(36), nullable=False, server_default="")
    planning_stock_pool = Column(String(64), nullable=False, server_default="default")
    run_id = Column(Integer, ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=True, index=True)
    freeze_version = Column(Integer, nullable=False, server_default="0")
    requirement_id = Column(Integer, ForeignKey("mrp_requirement.id", ondelete="CASCADE"), nullable=False)
    priority_period_from = Column(Date, nullable=False)
    priority_period_to = Column(Date, nullable=False)
    # realization_mode ∈ {consume, make} — the axis of realization (§3, §6).
    # consume outstanding drives reserved_soft; make contributes exactly 0.
    realization_mode = Column(String(10), nullable=False, server_default="consume")
    reserved_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    realized_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    covered_on_hand_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    covered_incoming_supplier_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    covered_incoming_wip_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    uncovered_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    # lifecycle_status ∈ {active, closed, released, carried, cancelled} (§6.2).
    lifecycle_status = Column(String(20), nullable=False, server_default="active")
    # coverage_state ∈ {covered, partial, uncovered} — derived from uncovered_qty.
    coverage_state = Column(String(20), nullable=False, server_default="uncovered")
    opened_at = Column(TIMESTAMP, nullable=True)
    closed_at = Column(TIMESTAMP, nullable=True)
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now(), server_default=func.now(), nullable=False)

    item = relationship("Item")
    run = relationship("PlanningRun")
    requirement = relationship("MrpRequirement")


class ReservationEvent(Base):
    """Ledger-2 append-only reservation journal (design §2.3). Fold by
    reservation gives reserved_qty/realized_qty; fold by item-key gives the
    pool's reserved_soft. Never UPDATE/DELETE (analogue of И5). idempotency_key
    makes a re-run non-duplicating."""

    __tablename__ = "reservation_event"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="ux_reservation_event_idempotency"),
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
    reservation_id = Column(BigInteger, ForeignKey("reservation_entry.id", ondelete="CASCADE"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.item_id"), nullable=False, index=True)
    characteristic_ref = Column(String(36), nullable=False, server_default="")
    organization_ref = Column(String(36), nullable=False, server_default="")
    planning_stock_pool = Column(String(64), nullable=False, server_default="default")
    # event_kind ∈ {open, amend, realize, unrealize, cancel, release,
    # carry_out, carry_in, close, reopen} (§6.2).
    event_kind = Column(String(20), nullable=False, server_default="")
    reserved_delta = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    realized_delta = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    sle_id = Column(BigInteger, ForeignKey("stock_ledger_entry.id", ondelete="SET NULL"), nullable=True)
    fact_ref = Column(String(64), nullable=False, server_default="")
    fact_line_ref = Column(String(64), nullable=False, server_default="")
    # match_rule ∈ {pegged, fifo, manual} (§6.3).
    match_rule = Column(String(20), nullable=False, server_default="")
    cycle_id = Column(String(64), nullable=False, server_default="")
    idempotency_key = Column(String(120), nullable=False)
    event_at = Column(TIMESTAMP, nullable=False, default=func.now(), server_default=func.now())
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)

    reservation = relationship("ReservationEntry")
    item = relationship("Item")


class ReservationCoverage(Base):
    """Ledger-2 coverage projection (design §2.4): who covers a reservation.
    frozen pins (pin_kind='frozen') carry the immutable freeze allocation (=
    MrpFreezeAllocation), realized/evaporated rewritten by verify_frozen_supply;
    floating rows (pin_kind='floating') are (re)written ONLY by redistribute()."""

    __tablename__ = "reservation_coverage"
    __table_args__ = (
        UniqueConstraint(
            "reservation_id",
            "source_kind",
            "source_ref",
            "source_line_ref",
            "pin_kind",
            name="ux_reservation_coverage_source",
        ),
        Index("ix_reservation_coverage_reservation", "reservation_id"),
    )

    id = Column(BigIntPK, primary_key=True, index=True)
    reservation_id = Column(BigInteger, ForeignKey("reservation_entry.id", ondelete="CASCADE"), nullable=False)
    # source_kind ∈ {on_hand, supplier_order, wip_order}.
    source_kind = Column(String(20), nullable=False, server_default="")
    source_ref = Column(String(64), nullable=False, server_default="")
    source_line_ref = Column(String(64), nullable=False, server_default="")
    # pin_kind ∈ {frozen, floating}.
    pin_kind = Column(String(10), nullable=False, server_default="floating")
    alloc_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    fact_at_freeze = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    covered_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    realized_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    evaporated_qty = Column(DECIMAL(15, 3), nullable=False, default=0.0, server_default="0")
    cycle_id = Column(String(64), nullable=False, server_default="")
    computed_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)

    reservation = relationship("ReservationEntry")
