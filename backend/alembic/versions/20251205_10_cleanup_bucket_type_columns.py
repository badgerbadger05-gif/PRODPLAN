"""remove bucket_type columns and related constraints/indexes; reindex without bucket_type

Revision ID: 20251205_10
Revises: 20251205_09
Create Date: 2025-12-05 08:20:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20251205_10"
down_revision = "20251205_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Optional sanity check: count non-daily rows before dropping columns (best-effort)
    try:
        res = conn.execute(
            sa.text(
                """
                SELECT
                  (SELECT COUNT(*) FROM planned_order WHERE bucket_type <> 'daily')
                + (SELECT COUNT(*) FROM planned_order_stage WHERE bucket_type <> 'daily')
                + (SELECT COUNT(*) FROM planned_purchase WHERE bucket_type <> 'daily')
                + (SELECT COUNT(*) FROM capacity_load WHERE bucket_type <> 'daily') AS non_daily_rows
                """
            )
        ).scalar()
        # Not raising here; just informational
    except Exception:
        pass

    # ---- planned_order ----
    try:
        op.drop_constraint("ck_planned_order_bucket_type", "planned_order", type_="check")
    except Exception:
        pass
    try:
        op.drop_index("idx_planned_order_bucket", table_name="planned_order")
    except Exception:
        pass
    # Drop column
    try:
        conn.execute(sa.text("ALTER TABLE planned_order DROP COLUMN IF EXISTS bucket_type"))
    except Exception:
        try:
            conn.execute(sa.text("ALTER TABLE planned_order DROP COLUMN bucket_type"))
        except Exception:
            pass
    # New indexes without bucket_type
    try:
        op.create_index(
            "ix_planned_order_run_bucket_date",
            "planned_order",
            ["run_id", "bucket_date"],
            unique=False,
        )
    except Exception:
        pass
    try:
        op.create_index(
            "ix_planned_order_bucket_date",
            "planned_order",
            ["bucket_date"],
            unique=False,
        )
    except Exception:
        pass

    # ---- planned_order_stage ----
    try:
        op.drop_constraint("ck_planned_order_stage_bucket_type", "planned_order_stage", type_="check")
    except Exception:
        pass
    for idx in ("idx_pos_bucket", "idx_pos_area_bucket"):
        try:
            op.drop_index(idx, table_name="planned_order_stage")
        except Exception:
            pass
    # Drop column
    try:
        conn.execute(sa.text("ALTER TABLE planned_order_stage DROP COLUMN IF EXISTS bucket_type"))
    except Exception:
        try:
            conn.execute(sa.text("ALTER TABLE planned_order_stage DROP COLUMN bucket_type"))
        except Exception:
            pass
    # New indexes without bucket_type
    try:
        op.create_index(
            "ix_pos_run_bucket_date",
            "planned_order_stage",
            ["run_id", "bucket_date"],
            unique=False,
        )
    except Exception:
        pass
    try:
        op.create_index(
            "ix_pos_area_bucket_date",
            "planned_order_stage",
            ["area_id", "bucket_date"],
            unique=False,
        )
    except Exception:
        pass

    # ---- planned_purchase ----
    try:
        op.drop_constraint("ck_planned_purchase_bucket_type", "planned_purchase", type_="check")
    except Exception:
        pass
    try:
        op.drop_index("idx_planned_purchase_bucket", table_name="planned_purchase")
    except Exception:
        pass
    # Drop column
    try:
        conn.execute(sa.text("ALTER TABLE planned_purchase DROP COLUMN IF EXISTS bucket_type"))
    except Exception:
        try:
            conn.execute(sa.text("ALTER TABLE planned_purchase DROP COLUMN bucket_type"))
        except Exception:
            pass
    # New indexes without bucket_type
    try:
        op.create_index(
            "ix_planned_purchase_run_bucket_date",
            "planned_purchase",
            ["run_id", "bucket_date"],
            unique=False,
        )
    except Exception:
        pass
    try:
        op.create_index(
            "ix_planned_purchase_bucket_date",
            "planned_purchase",
            ["bucket_date"],
            unique=False,
        )
    except Exception:
        pass

    # ---- capacity_load ----
    # Before dropping bucket_type and adding a new unique constraint, we must remove non-daily rows
    # to avoid duplicates on (run_id, area_id, bucket_date) that would violate the new uniqueness.
    try:
        conn.execute(sa.text("DELETE FROM capacity_load WHERE bucket_type <> 'daily'"))
    except Exception:
        pass
    # Also deduplicate any remaining 'daily' rows that collide on (run_id, area_id, bucket_date)
    # Keep the smallest id per group and delete others.
    try:
        conn.execute(sa.text("""
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (PARTITION BY run_id, area_id, bucket_date ORDER BY id) AS rn
                FROM capacity_load
                WHERE bucket_type = 'daily'
            )
            DELETE FROM capacity_load cl
            USING ranked r
            WHERE cl.id = r.id
              AND r.rn > 1
        """))
    except Exception:
        pass
    try:
        op.drop_constraint("ck_capacity_load_bucket_type", "capacity_load", type_="check")
    except Exception:
        pass
    # unique index/constraint included bucket_type earlier; drop and recreate without it
    try:
        op.drop_constraint("ux_capacity_load", "capacity_load", type_="unique")
    except Exception:
        # if created as index instead of constraint, try drop index
        try:
            op.drop_index("ux_capacity_load", table_name="capacity_load")
        except Exception:
            pass
    # Drop column
    try:
        conn.execute(sa.text("ALTER TABLE capacity_load DROP COLUMN IF EXISTS bucket_type"))
    except Exception:
        try:
            conn.execute(sa.text("ALTER TABLE capacity_load DROP COLUMN bucket_type"))
        except Exception:
            pass
    # New unique without bucket_type
    try:
        op.create_unique_constraint(
            "ux_capacity_load_run_area_date",
            "capacity_load",
            ["run_id", "area_id", "bucket_date"],
        )
    except Exception:
        pass


def downgrade() -> None:
    conn = op.get_bind()

    # ---- planned_order ----
    try:
        conn.execute(
            sa.text(
                "ALTER TABLE planned_order ADD COLUMN IF NOT EXISTS bucket_type VARCHAR(10) NOT NULL DEFAULT 'daily'"
            )
        )
    except Exception:
        conn.execute(
            sa.text(
                "ALTER TABLE planned_order ADD COLUMN bucket_type VARCHAR(10) NOT NULL DEFAULT 'daily'"
            )
        )
    try:
        op.create_check_constraint(
            "ck_planned_order_bucket_type",
            "planned_order",
            "bucket_type IN ('daily','weekly')",
        )
    except Exception:
        pass
    # Recreate original bucket index
    try:
        op.create_index(
            "idx_planned_order_bucket",
            "planned_order",
            ["bucket_type", "bucket_date"],
            unique=False,
        )
    except Exception:
        pass
    # Drop new indexes created in upgrade
    for idx in ("ix_planned_order_run_bucket_date", "ix_planned_order_bucket_date"):
        try:
            op.drop_index(idx, table_name="planned_order")
        except Exception:
            pass

    # Backfill from legacy archive
    try:
        conn.execute(
            sa.text(
                """
                UPDATE planned_order po
                   SET bucket_type = l.bucket_type
                  FROM mrp_bucket_type_legacy l
                 WHERE l.entity = 'planned_order'
                   AND l.record_id = po.order_id
                """
            )
        )
    except Exception:
        pass

    # ---- planned_order_stage ----
    try:
        conn.execute(
            sa.text(
                "ALTER TABLE planned_order_stage ADD COLUMN IF NOT EXISTS bucket_type VARCHAR(10) NOT NULL DEFAULT 'daily'"
            )
        )
    except Exception:
        conn.execute(
            sa.text(
                "ALTER TABLE planned_order_stage ADD COLUMN bucket_type VARCHAR(10) NOT NULL DEFAULT 'daily'"
            )
        )
    try:
        op.create_check_constraint(
            "ck_planned_order_stage_bucket_type",
            "planned_order_stage",
            "bucket_type IN ('daily','weekly')",
        )
    except Exception:
        pass
    # Recreate original indexes
    for name, cols in (
        ("idx_pos_bucket", ["bucket_type", "bucket_date"]),
        ("idx_pos_area_bucket", ["area_id", "bucket_type", "bucket_date"]),
    ):
        try:
            op.create_index(name, "planned_order_stage", cols, unique=False)
        except Exception:
            pass
    # Drop new indexes created in upgrade
    for idx in ("ix_pos_run_bucket_date", "ix_pos_area_bucket_date"):
        try:
            op.drop_index(idx, table_name="planned_order_stage")
        except Exception:
            pass
    # Backfill from legacy
    try:
        conn.execute(
            sa.text(
                """
                UPDATE planned_order_stage pos
                   SET bucket_type = l.bucket_type
                  FROM mrp_bucket_type_legacy l
                 WHERE l.entity = 'planned_order_stage'
                   AND l.record_id = pos.id
                """
            )
        )
    except Exception:
        pass

    # ---- planned_purchase ----
    try:
        conn.execute(
            sa.text(
                "ALTER TABLE planned_purchase ADD COLUMN IF NOT EXISTS bucket_type VARCHAR(10) NOT NULL DEFAULT 'daily'"
            )
        )
    except Exception:
        conn.execute(
            sa.text(
                "ALTER TABLE planned_purchase ADD COLUMN bucket_type VARCHAR(10) NOT NULL DEFAULT 'daily'"
            )
        )
    try:
        op.create_check_constraint(
            "ck_planned_purchase_bucket_type",
            "planned_purchase",
            "bucket_type IN ('daily','weekly')",
        )
    except Exception:
        pass
    try:
        op.create_index(
            "idx_planned_purchase_bucket",
            "planned_purchase",
            ["bucket_type", "bucket_date"],
            unique=False,
        )
    except Exception:
        pass
    # Drop new indexes created in upgrade
    for idx in ("ix_planned_purchase_run_bucket_date", "ix_planned_purchase_bucket_date"):
        try:
            op.drop_index(idx, table_name="planned_purchase")
        except Exception:
            pass
    # Backfill from legacy
    try:
        conn.execute(
            sa.text(
                """
                UPDATE planned_purchase pp
                   SET bucket_type = l.bucket_type
                  FROM mrp_bucket_type_legacy l
                 WHERE l.entity = 'planned_purchase'
                   AND l.record_id = pp.purchase_id
                """
            )
        )
    except Exception:
        pass

    # ---- capacity_load ----
    try:
        conn.execute(
            sa.text(
                "ALTER TABLE capacity_load ADD COLUMN IF NOT EXISTS bucket_type VARCHAR(10) NOT NULL DEFAULT 'daily'"
            )
        )
    except Exception:
        conn.execute(
            sa.text(
                "ALTER TABLE capacity_load ADD COLUMN bucket_type VARCHAR(10) NOT NULL DEFAULT 'daily'"
            )
        )
    try:
        op.create_check_constraint(
            "ck_capacity_load_bucket_type",
            "capacity_load",
            "bucket_type IN ('daily','weekly')",
        )
    except Exception:
        pass
    # Drop new unique constraint (without bucket_type) and restore original unique including bucket_type
    try:
        op.drop_constraint("ux_capacity_load_run_area_date", "capacity_load", type_="unique")
    except Exception:
        try:
            op.drop_index("ux_capacity_load_run_area_date", table_name="capacity_load")
        except Exception:
            pass
    try:
        op.create_unique_constraint(
            "ux_capacity_load",
            "capacity_load",
            ["run_id", "area_id", "bucket_type", "bucket_date"],
        )
    except Exception:
        pass
    # Backfill from legacy
    try:
        conn.execute(
            sa.text(
                """
                UPDATE capacity_load cl
                   SET bucket_type = l.bucket_type
                  FROM mrp_bucket_type_legacy l
                 WHERE l.entity = 'capacity_load'
                   AND l.record_id = cl.id
                """
            )
        )
    except Exception:
        pass