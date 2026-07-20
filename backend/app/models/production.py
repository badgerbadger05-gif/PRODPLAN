from sqlalchemy import Column, Integer, String, DECIMAL, TIMESTAMP, ForeignKey, TEXT, Boolean, DateTime, Date, CheckConstraint, JSON, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text
from ..database import Base
from .types import CrossPlatformJSON


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
