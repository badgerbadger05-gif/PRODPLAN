from sqlalchemy import Column, Integer, String, DECIMAL, TIMESTAMP, ForeignKey, TEXT, Boolean, DateTime, Date, CheckConstraint, JSON, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text
from ..database import Base
from .types import CrossPlatformJSON


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
