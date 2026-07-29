"""legacy pre-Alembic baseline: catalogue/production tables

Исторически эти таблицы никогда не создавались миграциями: они появлялись
только из ``Base.metadata.create_all(bind=engine)`` в ``app/main.py``.
Из-за этого схема не воспроизводилась из Alembic: на чистой БД цепочка
падала уже на ``ALTER TABLE specifications``, а десяток таблиц
(units, default_specifications, production_stages, production_components,
production_operations, production_plan_entries, resource_stages,
root_products, spec_operations, item_embeddings) не создавался вовсе.

Ревизия ставится КОРНЕМ цепочки (down_revision = None), а бывший корень
``20250925_01`` переключён на неё. Для уже существующих БД (они проштампованы
поздним head) ревизия не выполняется вообще; для БД, поднятых через
``create_all`` и не проштампованных, каждая таблица создаётся только если её
нет — миграция идемпотентна.

Таблицы, которые в дальнейшем ALTER-ятся другими миграциями
(items, item_categories*, operations, production_orders, production_products,
production_resources, spec_components, specifications), создаются здесь в
ИСХОДНОМ виде — без колонок, которые добавляют более поздние ревизии.
Итоговая форма после ``upgrade head`` совпадает с ``app/models.py``.

(* item_categories поздними ревизиями не меняется и создаётся сразу целиком.)

Revision ID: 20250924_00
Revises:
Create Date: 2025-09-24 00:00:00
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20250924_00"
down_revision = None
branch_labels = None
depends_on = None


# Порядок создания учитывает внешние ключи.
_TABLES_IN_CREATE_ORDER = (
    "item_categories",
    "items",
    "units",
    "production_stages",
    "specifications",
    "spec_components",
    "operations",
    "spec_operations",
    "production_orders",
    "production_products",
    "production_components",
    "production_operations",
    "default_specifications",
    "root_products",
    "production_resources",
    "resource_stages",
    "production_plan_entries",
    "item_embeddings",
)


def _timestamps():
    return (
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
    )


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    def missing(name: str) -> bool:
        return name not in existing

    # --- item_categories -------------------------------------------------
    if missing("item_categories"):
        op.create_table(
            "item_categories",
            sa.Column("category_id", sa.Integer(), primary_key=True),
            sa.Column("category_code", sa.String(length=50), nullable=True),
            sa.Column("category_name", sa.String(length=255), nullable=False),
            sa.Column("category_ref1c", sa.String(length=36), nullable=True),
            sa.Column("parent_id", sa.Integer(), nullable=True),
            sa.Column("is_folder", sa.Boolean(), nullable=True),
            sa.Column("predefined", sa.Boolean(), nullable=True),
            sa.Column("predefined_name", sa.String(length=100), nullable=True),
            sa.Column("data_version", sa.String(length=50), nullable=True),
            sa.Column("deletion_mark", sa.Boolean(), nullable=True),
            *_timestamps(),
            sa.ForeignKeyConstraint(
                ["parent_id"],
                ["item_categories.category_id"],
                name="fk_item_categories_parent_id",
            ),
        )
        op.create_index("ix_item_categories_category_id", "item_categories", ["category_id"])
        op.create_index("ix_item_categories_category_code", "item_categories", ["category_code"])
        op.create_index(
            "ix_item_categories_category_ref1c",
            "item_categories",
            ["category_ref1c"],
            unique=True,
        )

    # --- items -----------------------------------------------------------
    # Без optimal_batch (20251007_06), category_id (20260313_01)
    # и supplier_ref1c (20260519_01).
    if missing("items"):
        op.create_table(
            "items",
            sa.Column("item_id", sa.Integer(), primary_key=True),
            sa.Column("item_code", sa.String(length=50), nullable=False),
            sa.Column("item_name", sa.Text(), nullable=False),
            sa.Column("item_article", sa.String(length=100), nullable=True),
            sa.Column("item_ref1c", sa.String(length=36), nullable=True),
            sa.Column("replenishment_method", sa.String(length=50), nullable=True),
            sa.Column("replenishment_time", sa.Integer(), nullable=True),
            sa.Column("unit", sa.String(length=50), nullable=True),
            sa.Column("stock_qty", sa.Numeric(10, 3), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=True),
            *_timestamps(),
        )
        op.create_index("ix_items_item_id", "items", ["item_id"])
        op.create_index("ix_items_item_code", "items", ["item_code"], unique=True)
        op.create_index("ix_items_item_article", "items", ["item_article"])
        op.create_index("ix_items_item_ref1c", "items", ["item_ref1c"])

    # --- units -----------------------------------------------------------
    if missing("units"):
        op.create_table(
            "units",
            sa.Column("unit_id", sa.Integer(), primary_key=True),
            sa.Column("unit_ref1c", sa.String(length=36), nullable=True),
            sa.Column("unit_code", sa.String(length=50), nullable=True),
            sa.Column("unit_name", sa.String(length=255), nullable=False),
            sa.Column("unit_full_name", sa.String(length=255), nullable=True),
            sa.Column("short_name", sa.String(length=50), nullable=True),
            sa.Column("iso_code", sa.String(length=50), nullable=True),
            sa.Column("base_unit_ref1c", sa.String(length=36), nullable=True),
            sa.Column("ratio", sa.Numeric(18, 6), nullable=True),
            sa.Column("precision", sa.Integer(), nullable=True),
            *_timestamps(),
        )
        op.create_index("ix_units_unit_id", "units", ["unit_id"])
        op.create_index("ix_units_unit_ref1c", "units", ["unit_ref1c"], unique=True)
        op.create_index("ix_units_unit_code", "units", ["unit_code"])

    # --- production_stages ------------------------------------------------
    if missing("production_stages"):
        op.create_table(
            "production_stages",
            sa.Column("stage_id", sa.Integer(), primary_key=True),
            sa.Column("stage_name", sa.String(length=255), nullable=False),
            sa.Column("stage_order", sa.Integer(), nullable=True),
            sa.Column("stage_ref1c", sa.String(length=36), nullable=True),
            *_timestamps(),
            sa.UniqueConstraint("stage_name", name="uq_production_stages_stage_name"),
        )
        op.create_index("ix_production_stages_stage_id", "production_stages", ["stage_id"])

    # --- specifications ---------------------------------------------------
    # Без production_kind_id (20251002_04).
    if missing("specifications"):
        op.create_table(
            "specifications",
            sa.Column("spec_id", sa.Integer(), primary_key=True),
            sa.Column("spec_code", sa.String(length=50), nullable=True),
            sa.Column("spec_name", sa.Text(), nullable=False),
            sa.Column("spec_ref1c", sa.String(length=36), nullable=True),
            *_timestamps(),
        )
        op.create_index("ix_specifications_spec_id", "specifications", ["spec_id"])
        op.create_index("ix_specifications_spec_code", "specifications", ["spec_code"])
        op.create_index(
            "ix_specifications_spec_ref1c", "specifications", ["spec_ref1c"], unique=True
        )

    # --- spec_components --------------------------------------------------
    # Без component_spec_ref1c (20260624_01).
    if missing("spec_components"):
        op.create_table(
            "spec_components",
            sa.Column("component_id", sa.Integer(), primary_key=True),
            sa.Column("spec_id", sa.Integer(), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("quantity", sa.Numeric(10, 3), nullable=False),
            sa.Column("stage_id", sa.Integer(), nullable=True),
            sa.Column("component_type", sa.String(length=50), nullable=True),
            *_timestamps(),
            sa.ForeignKeyConstraint(
                ["spec_id"], ["specifications.spec_id"], name="fk_spec_components_spec_id"
            ),
            sa.ForeignKeyConstraint(
                ["item_id"], ["items.item_id"], name="fk_spec_components_item_id"
            ),
            sa.ForeignKeyConstraint(
                ["stage_id"],
                ["production_stages.stage_id"],
                name="fk_spec_components_stage_id",
            ),
        )
        op.create_index("ix_spec_components_component_id", "spec_components", ["component_id"])

    # --- operations -------------------------------------------------------
    # Без operation_price (20260605_02).
    if missing("operations"):
        op.create_table(
            "operations",
            sa.Column("operation_id", sa.Integer(), primary_key=True),
            sa.Column("operation_ref1c", sa.String(length=36), nullable=True),
            sa.Column("operation_name", sa.String(length=255), nullable=True),
            sa.Column("time_norm", sa.Numeric(10, 4), nullable=True),
            *_timestamps(),
        )
        op.create_index("ix_operations_operation_id", "operations", ["operation_id"])
        op.create_index(
            "ix_operations_operation_ref1c", "operations", ["operation_ref1c"], unique=True
        )

    # --- spec_operations --------------------------------------------------
    if missing("spec_operations"):
        op.create_table(
            "spec_operations",
            sa.Column("spec_operation_id", sa.Integer(), primary_key=True),
            sa.Column("spec_id", sa.Integer(), nullable=False),
            sa.Column("operation_id", sa.Integer(), nullable=False),
            sa.Column("stage_id", sa.Integer(), nullable=True),
            sa.Column("time_norm", sa.Numeric(10, 4), nullable=True),
            *_timestamps(),
            sa.ForeignKeyConstraint(
                ["spec_id"], ["specifications.spec_id"], name="fk_spec_operations_spec_id"
            ),
            sa.ForeignKeyConstraint(
                ["operation_id"],
                ["operations.operation_id"],
                name="fk_spec_operations_operation_id",
            ),
            sa.ForeignKeyConstraint(
                ["stage_id"],
                ["production_stages.stage_id"],
                name="fk_spec_operations_stage_id",
            ),
        )
        op.create_index(
            "ix_spec_operations_spec_operation_id", "spec_operations", ["spec_operation_id"]
        )

    # --- production_orders ------------------------------------------------
    # Без order_state_key / order_state_name / deletion_mark (20260213_01)
    # и source / source_run_id (20260520_01).
    if missing("production_orders"):
        op.create_table(
            "production_orders",
            sa.Column("order_id", sa.Integer(), primary_key=True),
            sa.Column("order_number", sa.String(length=50), nullable=True),
            sa.Column("order_date", sa.DateTime(), nullable=False),
            sa.Column("order_ref1c", sa.String(length=36), nullable=True),
            sa.Column("is_posted", sa.Boolean(), nullable=True),
            *_timestamps(),
        )
        op.create_index("ix_production_orders_order_id", "production_orders", ["order_id"])
        op.create_index(
            "ix_production_orders_order_number", "production_orders", ["order_number"]
        )
        op.create_index(
            "ix_production_orders_order_ref1c",
            "production_orders",
            ["order_ref1c"],
            unique=True,
        )

    # --- production_products ----------------------------------------------
    # Без line_number / characteristic_ref1c (20260213_01),
    # produced_qty / remaining_qty (20260226_01),
    # source_planned_order_id (20260520_01),
    # source_mrp_requirement_id / source_mrp_allocation_key (20260522_06),
    # destination_warehouse_ref1c (20260717_02),
    # ledger_generation_id (20260723_07).
    if missing("production_products"):
        op.create_table(
            "production_products",
            sa.Column("product_id", sa.Integer(), primary_key=True),
            sa.Column("order_id", sa.Integer(), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("quantity", sa.Numeric(10, 3), nullable=False),
            sa.Column("spec_id", sa.Integer(), nullable=True),
            sa.Column("stage_id", sa.Integer(), nullable=True),
            *_timestamps(),
            sa.ForeignKeyConstraint(
                ["order_id"],
                ["production_orders.order_id"],
                name="fk_production_products_order_id",
            ),
            sa.ForeignKeyConstraint(
                ["item_id"], ["items.item_id"], name="fk_production_products_item_id"
            ),
            sa.ForeignKeyConstraint(
                ["spec_id"],
                ["specifications.spec_id"],
                name="fk_production_products_spec_id",
            ),
            sa.ForeignKeyConstraint(
                ["stage_id"],
                ["production_stages.stage_id"],
                name="fk_production_products_stage_id",
            ),
        )
        op.create_index(
            "ix_production_products_product_id", "production_products", ["product_id"]
        )

    # --- production_components --------------------------------------------
    if missing("production_components"):
        op.create_table(
            "production_components",
            sa.Column("component_id", sa.Integer(), primary_key=True),
            sa.Column("order_id", sa.Integer(), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("quantity", sa.Numeric(10, 3), nullable=False),
            sa.Column("spec_id", sa.Integer(), nullable=True),
            sa.Column("stage_id", sa.Integer(), nullable=True),
            *_timestamps(),
            sa.ForeignKeyConstraint(
                ["order_id"],
                ["production_orders.order_id"],
                name="fk_production_components_order_id",
            ),
            sa.ForeignKeyConstraint(
                ["item_id"], ["items.item_id"], name="fk_production_components_item_id"
            ),
            sa.ForeignKeyConstraint(
                ["spec_id"],
                ["specifications.spec_id"],
                name="fk_production_components_spec_id",
            ),
            sa.ForeignKeyConstraint(
                ["stage_id"],
                ["production_stages.stage_id"],
                name="fk_production_components_stage_id",
            ),
        )
        op.create_index(
            "ix_production_components_component_id", "production_components", ["component_id"]
        )

    # --- production_operations ---------------------------------------------
    if missing("production_operations"):
        op.create_table(
            "production_operations",
            sa.Column("operation_id", sa.Integer(), primary_key=True),
            sa.Column("order_id", sa.Integer(), nullable=False),
            sa.Column("operation_id_ref", sa.Integer(), nullable=False),
            sa.Column("planned_quantity", sa.Numeric(10, 3), nullable=True),
            sa.Column("time_norm", sa.Numeric(10, 4), nullable=True),
            sa.Column("standard_hours", sa.Numeric(10, 4), nullable=True),
            sa.Column("stage_id", sa.Integer(), nullable=True),
            *_timestamps(),
            sa.ForeignKeyConstraint(
                ["order_id"],
                ["production_orders.order_id"],
                name="fk_production_operations_order_id",
            ),
            sa.ForeignKeyConstraint(
                ["operation_id_ref"],
                ["operations.operation_id"],
                name="fk_production_operations_operation_id_ref",
            ),
            sa.ForeignKeyConstraint(
                ["stage_id"],
                ["production_stages.stage_id"],
                name="fk_production_operations_stage_id",
            ),
        )
        op.create_index(
            "ix_production_operations_operation_id", "production_operations", ["operation_id"]
        )

    # --- default_specifications --------------------------------------------
    if missing("default_specifications"):
        op.create_table(
            "default_specifications",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("characteristic_id", sa.String(length=36), nullable=True),
            sa.Column("spec_id", sa.Integer(), nullable=False),
            *_timestamps(),
            sa.ForeignKeyConstraint(
                ["item_id"], ["items.item_id"], name="fk_default_specifications_item_id"
            ),
            sa.ForeignKeyConstraint(
                ["spec_id"],
                ["specifications.spec_id"],
                name="fk_default_specifications_spec_id",
            ),
        )
        op.create_index("ix_default_specifications_id", "default_specifications", ["id"])

    # --- root_products ------------------------------------------------------
    if missing("root_products"):
        op.create_table(
            "root_products",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("item_id", sa.Integer(), nullable=False),
            *_timestamps(),
            sa.ForeignKeyConstraint(
                ["item_id"], ["items.item_id"], name="fk_root_products_item_id"
            ),
            sa.UniqueConstraint("item_id", name="uq_root_products_item_id"),
        )
        op.create_index("ix_root_products_id", "root_products", ["id"])

    # --- production_resources -----------------------------------------------
    # Без buffer_days (20251007_06); capacity / work_hours_per_day становятся
    # NOT NULL только в 20251009_07.
    if missing("production_resources"):
        op.create_table(
            "production_resources",
            sa.Column("resource_id", sa.Integer(), primary_key=True),
            sa.Column("resource_name", sa.String(length=255), nullable=False),
            sa.Column("planning_offset", sa.Integer(), nullable=True),
            sa.Column("planning_horizon", sa.Integer(), nullable=True),
            sa.Column("capacity", sa.Numeric(10, 2), nullable=True),
            sa.Column("work_schedule", sa.String(length=100), nullable=True),
            sa.Column("work_hours_per_day", sa.Numeric(4, 2), nullable=True),
            *_timestamps(),
        )
        op.create_index(
            "ix_production_resources_resource_id", "production_resources", ["resource_id"]
        )

    # --- resource_stages ----------------------------------------------------
    if missing("resource_stages"):
        op.create_table(
            "resource_stages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("resource_id", sa.Integer(), nullable=False),
            sa.Column("stage_id", sa.Integer(), nullable=False),
            *_timestamps(),
            sa.ForeignKeyConstraint(
                ["resource_id"],
                ["production_resources.resource_id"],
                name="fk_resource_stages_resource_id",
            ),
            sa.ForeignKeyConstraint(
                ["stage_id"],
                ["production_stages.stage_id"],
                name="fk_resource_stages_stage_id",
            ),
        )
        op.create_index("ix_resource_stages_id", "resource_stages", ["id"])

    # --- production_plan_entries --------------------------------------------
    if missing("production_plan_entries"):
        op.create_table(
            "production_plan_entries",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("stage_id", sa.Integer(), nullable=True),
            sa.Column("date", sa.DateTime(), nullable=False),
            sa.Column("planned_qty", sa.Numeric(10, 3), nullable=True),
            sa.Column("completed_qty", sa.Numeric(10, 3), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            *_timestamps(),
            sa.ForeignKeyConstraint(
                ["item_id"], ["items.item_id"], name="fk_production_plan_entries_item_id"
            ),
            sa.ForeignKeyConstraint(
                ["stage_id"],
                ["production_stages.stage_id"],
                name="fk_production_plan_entries_stage_id",
            ),
        )
        op.create_index("ix_production_plan_entries_id", "production_plan_entries", ["id"])

    # --- item_embeddings -----------------------------------------------------
    # Таблица оставлена под будущее честное решение по семантическому поиску:
    # псевдо-эмбеддинги (md5) из сервиса поиска убраны, модель в models.py жива.
    if missing("item_embeddings"):
        op.create_table(
            "item_embeddings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("embedding_vector", sa.Text(), nullable=False),
            sa.Column("model_name", sa.String(length=100), nullable=False),
            *_timestamps(),
            sa.ForeignKeyConstraint(
                ["item_id"], ["items.item_id"], name="fk_item_embeddings_item_id"
            ),
            sa.UniqueConstraint("item_id", name="uq_item_embeddings_item_id"),
        )
        op.create_index("ix_item_embeddings_id", "item_embeddings", ["id"])


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table_name in reversed(_TABLES_IN_CREATE_ORDER):
        if table_name in existing:
            op.drop_table(table_name)
