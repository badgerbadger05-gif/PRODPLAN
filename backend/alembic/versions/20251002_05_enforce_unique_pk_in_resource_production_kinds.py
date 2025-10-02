"""enforce unique production_kind_id in resource_production_kinds

Revision ID: 20251002_05
Revises: 20251002_04
Create Date: 2025-10-02 09:00:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20251002_05'
down_revision = '20251002_04'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Pre-check for duplicates to avoid migration failure halfway
    duplicates = list(bind.execute(sa.text("""
        SELECT production_kind_id
        FROM resource_production_kinds
        GROUP BY production_kind_id
        HAVING COUNT(*) > 1
    """)))
    if duplicates:
        dup_list = ", ".join(str(row[0]) for row in duplicates)
        raise Exception(
            "Cannot create unique constraint on resource_production_kinds.production_kind_id; "
            f"duplicates exist for production_kind_id(s): {dup_list}. "
            "Please resolve duplicates before applying this migration."
        )

    op.create_unique_constraint(
        'uq_resource_production_kinds_kind_unique',
        'resource_production_kinds',
        ['production_kind_id']
    )


def downgrade() -> None:
    op.drop_constraint(
        'uq_resource_production_kinds_kind_unique',
        'resource_production_kinds',
        type_='unique'
    )