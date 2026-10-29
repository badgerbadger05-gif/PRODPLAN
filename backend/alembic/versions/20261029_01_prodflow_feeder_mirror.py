"""Add accepted ProdFlow feeder assignment mirror."""

from alembic import op
import sqlalchemy as sa


revision = "20261029_01"
down_revision = "20261023_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prodflow_feeder_manifest",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_revision", sa.String(length=64), nullable=False),
        sa.Column("row_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_name", sa.String(length=100), server_default="ProdFlowSupermarketPosition", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "prodflow_feeder_assignment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_ref1c", sa.String(length=36), nullable=False),
        sa.Column("source_item_code", sa.String(length=100), nullable=False),
        sa.Column("route_class", sa.String(length=100), nullable=True),
        sa.Column("source_revision", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["items.item_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("item_id", name="uq_prodflow_feeder_assignment_item"),
    )
    op.create_index("ix_prodflow_feeder_assignment_item_id", "prodflow_feeder_assignment", ["item_id"])
    op.create_index("ix_prodflow_feeder_assignment_source_revision", "prodflow_feeder_assignment", ["source_revision"])
    op.create_index("ix_prodflow_feeder_assignment_warehouse", "prodflow_feeder_assignment", ["warehouse_ref1c"])


def downgrade() -> None:
    op.drop_index("ix_prodflow_feeder_assignment_warehouse", table_name="prodflow_feeder_assignment")
    op.drop_index("ix_prodflow_feeder_assignment_source_revision", table_name="prodflow_feeder_assignment")
    op.drop_index("ix_prodflow_feeder_assignment_item_id", table_name="prodflow_feeder_assignment")
    op.drop_table("prodflow_feeder_assignment")
    op.drop_table("prodflow_feeder_manifest")
