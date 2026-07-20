"""add paint↔weld pairs (окраска ↔ сварка chain, stage 1)

Revision ID: 20260717_09
Revises: 20260717_08

Справочник пар «окрашенная ↔ сварная» для семейства «… после покраски».
См. .docs/paint_weld_chain_logic.md (этап 1: без записи в 1С).

Guard pattern (mirrors 20260717_03/06/07/08): the table is inspected first so a
re-run — or a schema already built by create_all() — is a no-op. SQLite-safe.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260717_09"
down_revision = "20260717_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(bind)
    table_exists = bool(
        inspector is not None and "paint_weld_pairs" in inspector.get_table_names()
    )
    if not table_exists:
        op.create_table(
            "paint_weld_pairs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("painted_item_id", sa.Integer(), nullable=False),
            sa.Column("welded_item_id", sa.Integer(), nullable=False),
            sa.Column("source", sa.String(length=10), server_default="auto", nullable=False),
            sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.CheckConstraint("source IN ('auto', 'manual')", name="ck_paint_weld_pairs_source"),
            sa.ForeignKeyConstraint(["painted_item_id"], ["items.item_id"]),
            sa.ForeignKeyConstraint(["welded_item_id"], ["items.item_id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("painted_item_id", name="ux_paint_weld_pairs_painted"),
        )

    inspector = None if offline else sa.inspect(bind)
    existing_indexes = (
        set()
        if inspector is None
        else {row["name"] for row in inspector.get_indexes("paint_weld_pairs")}
    )
    for column in ("id", "welded_item_id"):
        name = f"ix_paint_weld_pairs_{column}"
        if name not in existing_indexes:
            op.create_index(name, "paint_weld_pairs", [column])


def downgrade() -> None:
    op.drop_table("paint_weld_pairs")
