"""Add custody event stream, projection manifest, and projection tables."""

from alembic import op
import sqlalchemy as sa


revision = "20260731_05"
down_revision = "20260731_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "production_material_custody_event" not in inspector.get_table_names():
        op.create_table(
            "production_material_custody_event",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column(
                "issue_id",
                sa.Integer(),
                sa.ForeignKey("production_material_issues.issue_id", ondelete="SET NULL"),
                nullable=True,
                index=True,
            ),
            sa.Column(
                "product_id",
                sa.Integer(),
                sa.ForeignKey("production_products.product_id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "component_item_id",
                sa.Integer(),
                sa.ForeignKey("items.item_id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "source_kind",
                sa.String(32),
                nullable=False,
                server_default="issue_created",
            ),
            sa.Column("source_sle_id", sa.BigInteger(), nullable=True),
            sa.Column(
                "effective_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "location_kind",
                sa.String(16),
                nullable=False,
                server_default="transit",
            ),
            sa.Column("warehouse_ref1c", sa.String(36), nullable=False),
            sa.Column("source_ref1c", sa.String(36), nullable=True),
            sa.Column("source_ref2c", sa.String(64), nullable=True),
            sa.Column("delta_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("idempotency_key", sa.String(140), nullable=False),
            sa.Column("document_number", sa.String(64), nullable=True),
            sa.Column("document_line_no", sa.String(16), nullable=True),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "idempotency_key",
                name="ux_production_material_custody_event_idempotency",
            ),
            sa.CheckConstraint(
                "location_kind IN ('transit', 'workshop')",
                name="ck_production_material_custody_event_location",
            ),
            sa.CheckConstraint(
                "source_kind IN ('baseline', 'issue_created', 'transfer_posted', 'transfer_returned', 'consumed', 'terminal_release')",
                name="ck_production_material_custody_event_source_kind",
            ),
            sa.CheckConstraint(
                "delta_qty != 0",
                name="ck_production_material_custody_event_nonzero_delta",
            ),
        )
        op.create_index(
            "ix_production_material_custody_event_effective",
            "production_material_custody_event",
            ["effective_at", "id"],
        )
        op.create_index(
            "ix_production_material_custody_event_product",
            "production_material_custody_event",
            ["product_id", "component_item_id"],
        )
        op.create_index(
            "ix_production_material_custody_event_idempotency",
            "production_material_custody_event",
            ["idempotency_key"],
        )

    if "production_material_custody_projection_manifest" not in inspector.get_table_names():
        op.create_table(
            "production_material_custody_projection_manifest",
            sa.Column(
                "ledger_generation_id",
                sa.BigInteger(),
                sa.ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
                nullable=False,
                primary_key=True,
            ),
            sa.Column(
                "baseline_generation_id",
                sa.BigInteger(),
                sa.ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
                nullable=True,
                index=True,
            ),
            sa.Column("cutoff", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "status",
                sa.String(16),
                nullable=False,
                server_default="building",
            ),
            sa.Column(
                "is_baseline",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column(
                "source_event_high_watermark_id",
                sa.BigInteger,
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "observed_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "built_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "status IN ('complete', 'building')",
                name="ck_production_material_custody_projection_manifest_status",
            ),
        )
        op.create_index(
            "ix_production_material_custody_projection_manifest_cutoff",
            "production_material_custody_projection_manifest",
            ["cutoff"],
        )

    if "production_material_custody_projection" not in inspector.get_table_names():
        op.create_table(
            "production_material_custody_projection",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column(
                "ledger_generation_id",
                sa.BigInteger(),
                sa.ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "product_id",
                sa.Integer(),
                sa.ForeignKey("production_products.product_id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "component_item_id",
                sa.Integer(),
                sa.ForeignKey("items.item_id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "location_kind",
                sa.String(16),
                nullable=False,
                server_default="workshop",
            ),
            sa.Column("warehouse_ref1c", sa.String(36), nullable=False),
            sa.Column("reserved_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column(
                "source_event_high_watermark_id",
                sa.BigInteger,
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "built_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "ledger_generation_id",
                "product_id",
                "component_item_id",
                "location_kind",
                "warehouse_ref1c",
                name="ux_production_material_custody_projection_cell",
            ),
            sa.CheckConstraint(
                "location_kind IN ('transit', 'workshop')",
                name="ck_production_material_custody_projection_location",
            ),
            sa.CheckConstraint(
                "reserved_qty >= 0",
                name="ck_production_material_custody_projection_qty_nonnegative",
            ),
            sa.CheckConstraint(
                "source_event_high_watermark_id >= 0",
                name="ck_pm_custody_projection_event_hwm_nonnegative",
            ),
        )
        op.create_index(
            "ix_production_material_custody_projection_generation_product",
            "production_material_custody_projection",
            ["ledger_generation_id", "product_id"],
        )



def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in [
        "production_material_custody_projection",
        "production_material_custody_projection_manifest",
        "production_material_custody_event",
    ]:
        if table in inspector.get_table_names():
            op.drop_table(table)
