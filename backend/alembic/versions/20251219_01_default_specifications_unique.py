"""enforce uniqueness for default_specifications by (item_id, characteristic_id)

Revision ID: 20251219_01
Revises: 20251205_10
Create Date: 2025-12-19

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20251219_01"
down_revision = "20251205_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1) Best-effort normalize "empty characteristic" representation.
    # Some datasets store an all-zero GUID as "empty"; treat it as empty string for uniqueness.
    try:
        conn.execute(
            sa.text(
                """
                UPDATE default_specifications
                   SET characteristic_id = NULL
                 WHERE characteristic_id = '00000000-0000-0000-0000-000000000000'
                """
            )
        )
    except Exception:
        pass

    # 2) Deduplicate existing rows keeping the newest (updated_at/created_at) then max(id)
    # Partition key uses COALESCE(characteristic_id,'') to treat NULL as "empty".
    try:
        conn.execute(
            sa.text(
                """
                WITH ranked AS (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY item_id, COALESCE(characteristic_id, '')
                               ORDER BY COALESCE(updated_at, created_at) DESC NULLS LAST, id DESC
                           ) AS rn
                    FROM default_specifications
                )
                DELETE FROM default_specifications d
                USING ranked r
                WHERE d.id = r.id
                  AND r.rn > 1
                """
            )
        )
    except Exception:
        # If window functions are unavailable (unlikely on PG), skip dedup.
        pass

    # 3) Create unique index on (item_id, COALESCE(characteristic_id,''))
    # We use raw SQL here because Alembic's create_index() doesn't express COALESCE portably.
    try:
        conn.execute(
            sa.text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_default_specifications_item_char
                ON default_specifications (item_id, COALESCE(characteristic_id, ''))
                """
            )
        )
    except Exception:
        # Fallback without IF NOT EXISTS
        conn.execute(
            sa.text(
                """
                CREATE UNIQUE INDEX ux_default_specifications_item_char
                ON default_specifications (item_id, COALESCE(characteristic_id, ''))
                """
            )
        )


def downgrade() -> None:
    conn = op.get_bind()
    try:
        conn.execute(sa.text("DROP INDEX IF EXISTS ux_default_specifications_item_char"))
    except Exception:
        try:
            conn.execute(sa.text("DROP INDEX ux_default_specifications_item_char"))
        except Exception:
            pass

