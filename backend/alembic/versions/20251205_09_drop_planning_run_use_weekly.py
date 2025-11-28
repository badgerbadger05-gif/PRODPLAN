"""drop planning_run.use_weekly (archived in planning_run_bucket_modes)

Revision ID: 20251205_09
Revises: 20251205_08
Create Date: 2025-12-05 08:10:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20251205_09"
down_revision = "20251205_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Ensure archive table exists (best-effort; migration order guarantees this)
    # Proceed to drop column; use IF EXISTS for idempotency where supported.
    try:
        conn.execute(sa.text("ALTER TABLE planning_run DROP COLUMN IF EXISTS use_weekly"))
    except Exception:
        # Fallback without IF EXISTS (older PG or other dialects)
        try:
            conn.execute(sa.text("ALTER TABLE planning_run DROP COLUMN use_weekly"))
        except Exception:
            # Column may already be absent — ignore
            pass


def downgrade() -> None:
    conn = op.get_bind()

    # Recreate column with historical default TRUE
    try:
        conn.execute(
            sa.text(
                "ALTER TABLE planning_run ADD COLUMN IF NOT EXISTS use_weekly BOOLEAN NOT NULL DEFAULT TRUE"
            )
        )
    except Exception:
        # Fallback without IF NOT EXISTS
        conn.execute(
            sa.text(
                "ALTER TABLE planning_run ADD COLUMN use_weekly BOOLEAN NOT NULL DEFAULT TRUE"
            )
        )

    # Backfill from archive if present
    try:
        conn.execute(
            sa.text(
                """
                UPDATE planning_run pr
                   SET use_weekly = pm.use_weekly
                  FROM planning_run_bucket_modes pm
                 WHERE pm.run_id = pr.run_id
                """
            )
        )
    except Exception:
        # If archive missing, leave defaults (TRUE)
        pass