"""Persist operator completion intent and retry identity.
Revision ID: 20260909_01
Revises: 20260904_06
"""
from alembic import op
import sqlalchemy as sa
revision = "20260909_01"
down_revision = "20260904_06"
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table("production_manufactures") as batch:
        batch.add_column(sa.Column("complete_order", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("request_key", sa.String(100), nullable=True))
        batch.create_unique_constraint("uq_manufacture_product_request", ["product_id", "request_key"])

def downgrade():
    with op.batch_alter_table("production_manufactures") as batch:
        batch.drop_constraint("uq_manufacture_product_request", type_="unique")
        batch.drop_column("request_key")
        batch.drop_column("complete_order")
