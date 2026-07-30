"""Index specification component lookups by parent specification.

Revision ID: 20260730_02
Revises: 20260730_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_02"
down_revision = "20260730_01"
branch_labels = None
depends_on = None

TABLE_NAME = "spec_components"
INDEX_NAME = "ix_spec_components_spec_id"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE_NAME not in inspector.get_table_names():
        return
    indexes = {index["name"] for index in inspector.get_indexes(TABLE_NAME)}
    if INDEX_NAME not in indexes:
        op.create_index(INDEX_NAME, TABLE_NAME, ["spec_id"], unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE_NAME not in inspector.get_table_names():
        return
    indexes = {index["name"] for index in inspector.get_indexes(TABLE_NAME)}
    if INDEX_NAME in indexes:
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
