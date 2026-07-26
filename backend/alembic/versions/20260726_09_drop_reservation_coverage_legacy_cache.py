"""Drop legacy reservation coverage cache from canonical reservation entries.

Revision ID: 20260726_09
Revises: 20260726_08
"""

from alembic import op
import sqlalchemy as sa


revision = "20260726_09"
down_revision = "20260726_08"
branch_labels = None
depends_on = None


def _has_column(inspector: sa.Inspector, table: str, column: str) -> bool:
    return column in {row["name"] for row in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(bind)

    if inspector is None or inspector.has_table("reservation_coverage"):
        op.drop_table("reservation_coverage")

    if inspector is None or inspector.has_table("reservation_entry"):
        with op.batch_alter_table("reservation_entry") as batch_op:
            for name in [
                "covered_on_hand_qty",
                "covered_incoming_supplier_qty",
                "covered_incoming_wip_qty",
                "uncovered_qty",
                "coverage_state",
            ]:
                if inspector is None or _has_column(inspector, "reservation_entry", name):
                    batch_op.drop_column(name)


def downgrade() -> None:
    op.create_table(
        "reservation_coverage",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("reservation_id", sa.BigInteger(), nullable=False),
        sa.Column("source_kind", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("source_ref", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("source_line_ref", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("pin_kind", sa.String(length=10), nullable=False, server_default="floating"),
        sa.Column("alloc_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
        sa.Column("fact_at_freeze", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
        sa.Column("covered_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
        sa.Column("realized_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
        sa.Column("evaporated_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
        sa.Column("cycle_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("computed_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["reservation_id"], ["reservation_entry.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "reservation_id", "source_kind", "source_ref", "source_line_ref", "pin_kind",
            name="ux_reservation_coverage_source",
        ),
    )
    op.create_index("ix_reservation_coverage_reservation", "reservation_coverage", ["reservation_id"])

    bind = op.get_bind()
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(bind)

    if inspector is None or inspector.has_table("reservation_entry"):
        with op.batch_alter_table("reservation_entry") as batch_op:
            if inspector is None or not _has_column(inspector, "reservation_entry", "covered_on_hand_qty"):
                batch_op.add_column(
                    sa.Column(
                        "covered_on_hand_qty",
                        sa.DECIMAL(15, 3),
                        nullable=False,
                        server_default="0",
                    )
                )
            if inspector is None or not _has_column(inspector, "reservation_entry", "covered_incoming_supplier_qty"):
                batch_op.add_column(
                    sa.Column(
                        "covered_incoming_supplier_qty",
                        sa.DECIMAL(15, 3),
                        nullable=False,
                        server_default="0",
                    )
                )
            if inspector is None or not _has_column(inspector, "reservation_entry", "covered_incoming_wip_qty"):
                batch_op.add_column(
                    sa.Column(
                        "covered_incoming_wip_qty",
                        sa.DECIMAL(15, 3),
                        nullable=False,
                        server_default="0",
                    )
                )
            if inspector is None or not _has_column(inspector, "reservation_entry", "uncovered_qty"):
                batch_op.add_column(
                    sa.Column(
                        "uncovered_qty",
                        sa.DECIMAL(15, 3),
                        nullable=False,
                        server_default="0",
                    )
                )
            if inspector is None or not _has_column(inspector, "reservation_entry", "coverage_state"):
                batch_op.add_column(
                    sa.Column(
                        "coverage_state",
                        sa.String(20),
                        nullable=False,
                        server_default="uncovered",
                    )
                )
