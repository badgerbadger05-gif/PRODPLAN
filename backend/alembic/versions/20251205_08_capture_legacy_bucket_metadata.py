"""capture legacy weekly/bucket_type metadata before cleanup

Revision ID: 20251205_08
Revises: 20251119_01
Create Date: 2025-12-05 08:00:00

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20251205_08"
down_revision = "20251119_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Archive tables to preserve legacy weekly/bucket_type information

    # planning_run_bucket_modes
    op.create_table(
        "planning_run_bucket_modes",
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("planning_run.run_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("use_weekly", sa.Boolean(), nullable=False),
        sa.Column("legacy_bucket_types", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("weekly_rows", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "captured_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # mrp_bucket_type_legacy
    op.create_table(
        "mrp_bucket_type_legacy",
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("record_id", sa.Integer(), nullable=False),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("planning_run.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("bucket_type", sa.Text(), nullable=False),
        sa.Column("bucket_date", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("entity", "record_id", name="pk_mrp_bucket_type_legacy"),
    )
    op.create_index(
        "ix_mrp_bucket_legacy_run_entity",
        "mrp_bucket_type_legacy",
        ["run_id", "entity"],
        unique=False,
    )

    conn = op.get_bind()

    # Шаги 2-4 — перенос исторических данных сырым PostgreSQL-SQL
    # (BOOL_OR / TO_JSONB / ON CONFLICT / несколько операторов в одном execute).
    # На чистой БД переносить нечего, поэтому на других диалектах (SQLite в
    # тесте воспроизводимости схемы) шаги пропускаются: состав схемы от них
    # не зависит.
    if conn.dialect.name != "postgresql":
        return

    # 2) Populate planning_run_bucket_modes
    # Derive legacy bucket usage per run:
    # - use_weekly = TRUE if any of the detail tables has bucket_type <> 'daily' for that run
    # - legacy_bucket_types = JSONB array of distinct bucket_type values across all four tables for the run (NULL if none)
    # - weekly_rows = SUM of rows with bucket_type='weekly' across all four tables
    conn.execute(
        sa.text(
            """
            WITH btypes AS (
                SELECT run_id, bucket_type FROM planned_order
                UNION ALL
                SELECT run_id, bucket_type FROM planned_order_stage
                UNION ALL
                SELECT run_id, bucket_type FROM planned_purchase
                UNION ALL
                SELECT run_id, bucket_type FROM capacity_load
            ),
            agg AS (
                SELECT
                    pr.run_id,
                    COALESCE(SUM(CASE WHEN b.bucket_type = 'weekly' THEN 1 ELSE 0 END), 0) AS weekly_rows,
                    BOOL_OR(CASE WHEN b.bucket_type IS NOT NULL AND b.bucket_type <> 'daily' THEN TRUE ELSE FALSE END) AS has_non_daily,
                    CASE
                        WHEN COUNT(b.bucket_type) = 0 THEN NULL
                        ELSE TO_JSONB(ARRAY(SELECT DISTINCT bt.bucket_type FROM btypes bt WHERE bt.run_id = pr.run_id))
                    END AS legacy_bucket_types
                FROM planning_run pr
                LEFT JOIN btypes b ON b.run_id = pr.run_id
                GROUP BY pr.run_id
            )
            INSERT INTO planning_run_bucket_modes (run_id, use_weekly, legacy_bucket_types, weekly_rows)
            SELECT run_id,
                   COALESCE(has_non_daily, FALSE) AS use_weekly,
                   legacy_bucket_types,
                   COALESCE(weekly_rows, 0) AS weekly_rows
            FROM agg
            ON CONFLICT (run_id) DO NOTHING
            """
        )
    )

    # 3) Populate mrp_bucket_type_legacy with all non-daily rows from each table
    conn.execute(
        sa.text(
            """
            INSERT INTO mrp_bucket_type_legacy (entity, record_id, run_id, bucket_type, bucket_date)
            SELECT 'planned_order' AS entity, po.order_id AS record_id, po.run_id, po.bucket_type, po.bucket_date
            FROM planned_order po
            WHERE po.bucket_type <> 'daily';

            INSERT INTO mrp_bucket_type_legacy (entity, record_id, run_id, bucket_type, bucket_date)
            SELECT 'planned_order_stage' AS entity, pos.id AS record_id, pos.run_id, pos.bucket_type, pos.bucket_date
            FROM planned_order_stage pos
            WHERE pos.bucket_type <> 'daily';

            INSERT INTO mrp_bucket_type_legacy (entity, record_id, run_id, bucket_type, bucket_date)
            SELECT 'planned_purchase' AS entity, pp.purchase_id AS record_id, pp.run_id, pp.bucket_type, pp.bucket_date
            FROM planned_purchase pp
            WHERE pp.bucket_type <> 'daily';

            INSERT INTO mrp_bucket_type_legacy (entity, record_id, run_id, bucket_type, bucket_date)
            SELECT 'capacity_load' AS entity, cl.id AS record_id, cl.run_id, cl.bucket_type, cl.bucket_date
            FROM capacity_load cl
            WHERE cl.bucket_type <> 'daily';
            """
        )
    )

    # 4) Sanity checks: total 'weekly' rows in source tables vs legacy table
    src_weekly = conn.execute(
        sa.text(
            """
            SELECT
                (SELECT COUNT(*) FROM planned_order WHERE bucket_type = 'weekly')
              + (SELECT COUNT(*) FROM planned_order_stage WHERE bucket_type = 'weekly')
              + (SELECT COUNT(*) FROM planned_purchase WHERE bucket_type = 'weekly')
              + (SELECT COUNT(*) FROM capacity_load WHERE bucket_type = 'weekly') AS cnt
            """
        )
    ).scalar() or 0

    legacy_weekly = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM mrp_bucket_type_legacy WHERE bucket_type = 'weekly'"
        )
    ).scalar() or 0

    if int(src_weekly) != int(legacy_weekly):
        # Not a hard failure, but raise explicit error to highlight mismatch during upgrade
        raise RuntimeError(
            f"Legacy capture mismatch: source weekly={src_weekly}, archived weekly={legacy_weekly}"
        )


def downgrade() -> None:
    op.drop_index("ix_mrp_bucket_legacy_run_entity", table_name="mrp_bucket_type_legacy")
    op.drop_table("mrp_bucket_type_legacy")
    op.drop_table("planning_run_bucket_modes")