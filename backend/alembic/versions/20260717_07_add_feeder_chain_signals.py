"""add child chain feeder signals (parent pegging, depth, Цепочка type)

Revision ID: 20260717_07
Revises: 20260717_06
"""

from alembic import op
import sqlalchemy as sa

revision = "20260717_07"
down_revision = "20260717_06"
branch_labels = None
depends_on = None


def _swap_type_check(dialect: str, definition: str) -> None:
    """Recreate ck_dbr_feeder_signal_type. SQLite has no ALTER DROP CONSTRAINT,
    so it goes through batch_alter_table (table copy); Postgres keeps DDL."""
    if dialect == "postgresql":
        op.drop_constraint("ck_dbr_feeder_signal_type", "dbr_feeder_signal", type_="check")
        op.create_check_constraint(
            "ck_dbr_feeder_signal_type", "dbr_feeder_signal", definition
        )
    else:
        with op.batch_alter_table("dbr_feeder_signal", schema=None) as batch_op:
            batch_op.drop_constraint("ck_dbr_feeder_signal_type", type_="check")
            batch_op.create_check_constraint("ck_dbr_feeder_signal_type", definition)


def upgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    dialect = bind.dialect.name
    inspector = None if offline else sa.inspect(bind)
    columns = set() if inspector is None else {
        row["name"] for row in inspector.get_columns("dbr_feeder_signal")
    }

    if inspector is None or "parent_signal_id" not in columns:
        # Add the plain column first; SQLite cannot ALTER-add a foreign key, so
        # the self-referential FK is created separately on Postgres only. SQLite
        # dev/test schemas come from create_all(), which already carries the FK.
        op.add_column(
            "dbr_feeder_signal",
            sa.Column("parent_signal_id", sa.Integer(), nullable=True),
        )
        if dialect == "postgresql":
            op.create_foreign_key(
                "fk_dbr_feeder_signal_parent",
                "dbr_feeder_signal",
                "dbr_feeder_signal",
                ["parent_signal_id"],
                ["id"],
                ondelete="CASCADE",
            )
    if inspector is None or "chain_depth" not in columns:
        op.add_column(
            "dbr_feeder_signal",
            sa.Column("chain_depth", sa.Integer(), server_default="0", nullable=False),
        )

    # Chain signals are pegged to a parent, not a shelf — the position becomes
    # nullable for that family.  Postgres alters in place; SQLite copies.
    position_col = None if inspector is None else {
        row["name"]: row for row in inspector.get_columns("dbr_feeder_signal")
    }.get("supermarket_position_id")
    if inspector is None or (position_col is not None and not position_col["nullable"]):
        if dialect == "postgresql":
            op.alter_column(
                "dbr_feeder_signal",
                "supermarket_position_id",
                existing_type=sa.Integer(),
                nullable=True,
            )
        else:
            with op.batch_alter_table("dbr_feeder_signal", schema=None) as batch_op:
                batch_op.alter_column(
                    "supermarket_position_id", existing_type=sa.Integer(), nullable=True
                )

    checks = {} if inspector is None else {
        row.get("name"): row.get("sqltext", "")
        for row in inspector.get_check_constraints("dbr_feeder_signal")
    }
    type_sql = str(checks.get("ck_dbr_feeder_signal_type", ""))
    if inspector is None or "Цепочка" not in type_sql:
        _swap_type_check(dialect, "signal_type IN ('Пополнение', 'Под график', 'Цепочка')")

    inspector = None if offline else sa.inspect(bind)
    indexes = set() if inspector is None else {
        row["name"] for row in inspector.get_indexes("dbr_feeder_signal")
    }
    name = "ix_dbr_feeder_signal_parent_signal_id"
    if inspector is None or name not in indexes:
        op.create_index(name, "dbr_feeder_signal", ["parent_signal_id"])


def downgrade() -> None:
    op.execute("DELETE FROM dbr_feeder_signal WHERE signal_type = 'Цепочка'")
    _swap_type_check(op.get_bind().dialect.name, "signal_type IN ('Пополнение', 'Под график')")
    op.drop_index("ix_dbr_feeder_signal_parent_signal_id", table_name="dbr_feeder_signal")
    op.drop_column("dbr_feeder_signal", "chain_depth")
    op.drop_column("dbr_feeder_signal", "parent_signal_id")
