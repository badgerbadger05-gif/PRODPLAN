"""Drop forced order request/result tables."""

from alembic import op
import sqlalchemy as sa


revision = "20260731_01"
down_revision = "20260730_02"
branch_labels = None
depends_on = None


def _drop_forced_order_indexes(bind, table_name, index_names):
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return
    indexes = {idx["name"] for idx in inspector.get_indexes(table_name)}
    for index_name in index_names:
        if index_name in indexes:
            op.drop_index(index_name, table_name=table_name)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = {name for name in inspector.get_table_names()}

    # Drop in FK-safe order: children first.
    if "forced_order_result" in table_names:
        _drop_forced_order_indexes(bind, "forced_order_result", {"ix_forced_order_result_request_id"})
        op.drop_table("forced_order_result")

    if "forced_order_request" in table_names:
        _drop_forced_order_indexes(bind, "forced_order_request", {
            "ix_forced_order_request_run_id",
            "ix_forced_order_request_item_id",
            "ix_forced_order_request_need_date",
        })
        op.drop_table("forced_order_request")


def downgrade() -> None:
    # Schema compatibility is restored for an older checkout; deleted rows are
    # intentionally not fabricated and require backup restoration.
    op.create_table(
        "forced_order_request",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("planning_run.run_id", ondelete="SET NULL"), nullable=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.item_id"), nullable=False),
        sa.Column("need_date", sa.Date(), nullable=False),
        sa.Column("requested_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_forced_order_request_run_id", "forced_order_request", ["run_id"])
    op.create_index("ix_forced_order_request_item_id", "forced_order_request", ["item_id"])
    op.create_index("ix_forced_order_request_need_date", "forced_order_request", ["need_date"])
    op.create_table(
        "forced_order_result",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("forced_order_request.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("planned_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("normalized_qty", sa.DECIMAL(15, 3), nullable=True),
        sa.Column("horizon_limit", sa.DECIMAL(15, 3), nullable=True),
        sa.Column("component_limit", sa.DECIMAL(15, 3), nullable=True),
        sa.Column("shortage", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_forced_order_result_request_id", "forced_order_result", ["request_id"], unique=True)
