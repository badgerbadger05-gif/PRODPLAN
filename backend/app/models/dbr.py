from sqlalchemy import Column, Integer, String, DECIMAL, TIMESTAMP, ForeignKey, TEXT, Boolean, DateTime, Date, CheckConstraint, JSON, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text
from ..database import Base
from .types import CrossPlatformJSON


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
