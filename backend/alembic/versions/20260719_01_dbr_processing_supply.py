"""DBR фаза 4: тип снабжения «Переработка» (давальческий питатель №3)

Revision ID: 20260719_01
Revises: 20260718_01

- dbr_settings: rt_processing_days (RT давальческой цепочки, дефолт 25) и
  processing_trip_interval_days (рейс-интервал, дефолт 7);
- dbr_supermarket_position: supply_type допускает 'processing', rt_source
  допускает 'chain'.

Guard pattern (зеркалит 20260717_08): каждый шаг сначала инспектируется, так
что повторный прогон или схема, уже построенная create_all(), — no-op. SQLite
не умеет ALTER DROP CONSTRAINT — свап чеков через batch_alter_table.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260719_01"
down_revision = "20260718_01"
branch_labels = None
depends_on = None

_SUPPLY_WITH_PROCESSING = "supply_type IN ('manufacture', 'purchase', 'processing')"
_SUPPLY_LEGACY = "supply_type IN ('manufacture', 'purchase')"
_RT_SOURCE_WITH_CHAIN = "rt_source IN ('class', 'lead_time', 'chain')"
_RT_SOURCE_LEGACY = "rt_source IN ('class', 'lead_time')"


def _swap_check(dialect: str, name: str, definition: str) -> None:
    if dialect == "postgresql":
        op.drop_constraint(name, "dbr_supermarket_position", type_="check")
        op.create_check_constraint(name, "dbr_supermarket_position", definition)
    else:
        with op.batch_alter_table("dbr_supermarket_position", schema=None) as batch_op:
            batch_op.drop_constraint(name, type_="check")
            batch_op.create_check_constraint(name, definition)


def upgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    dialect = bind.dialect.name
    inspector = None if offline else sa.inspect(bind)

    settings_columns = set() if inspector is None else {
        row["name"] for row in inspector.get_columns("dbr_settings")
    }
    if inspector is None or "rt_processing_days" not in settings_columns:
        op.add_column(
            "dbr_settings",
            sa.Column("rt_processing_days", sa.Integer(), nullable=False, server_default="25"),
        )
    if inspector is None or "processing_trip_interval_days" not in settings_columns:
        op.add_column(
            "dbr_settings",
            sa.Column(
                "processing_trip_interval_days", sa.Integer(), nullable=False, server_default="7"
            ),
        )

    checks = {} if inspector is None else {
        row.get("name"): str(row.get("sqltext", ""))
        for row in inspector.get_check_constraints("dbr_supermarket_position")
    }
    supply_sql = checks.get("ck_dbr_supermarket_position_supply_type_allowed", "")
    if inspector is None or "processing" not in supply_sql:
        _swap_check(
            dialect, "ck_dbr_supermarket_position_supply_type_allowed", _SUPPLY_WITH_PROCESSING
        )
    rt_source_sql = checks.get("ck_dbr_supermarket_position_rt_source_allowed", "")
    if inspector is None or "chain" not in rt_source_sql:
        _swap_check(
            dialect, "ck_dbr_supermarket_position_rt_source_allowed", _RT_SOURCE_WITH_CHAIN
        )


def downgrade() -> None:
    # Переработка сворачивается в закупку до восстановления узкого чека —
    # иначе constraint нарушится на существующих строках.
    op.execute(
        "UPDATE dbr_supermarket_position SET supply_type = 'purchase', rt_source = 'lead_time' "
        "WHERE supply_type = 'processing'"
    )
    dialect = op.get_bind().dialect.name
    _swap_check(dialect, "ck_dbr_supermarket_position_rt_source_allowed", _RT_SOURCE_LEGACY)
    _swap_check(dialect, "ck_dbr_supermarket_position_supply_type_allowed", _SUPPLY_LEGACY)
    op.drop_column("dbr_settings", "processing_trip_interval_days")
    op.drop_column("dbr_settings", "rt_processing_days")
