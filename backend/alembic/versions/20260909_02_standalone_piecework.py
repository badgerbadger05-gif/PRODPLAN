"""Persist labor-only commands independently of production.
Revision ID: 20260909_02
Revises: 20260909_01
"""
from alembic import op
import sqlalchemy as sa
from app.models import CrossPlatformJSON
revision = "20260909_02"
down_revision = "20260909_01"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("production_piecework_commands",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("production_products.product_id"), nullable=False),
        sa.Column("request_key", sa.String(100), nullable=False, unique=True),
        sa.Column("target_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("operation_executors", CrossPlatformJSON(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False))

def downgrade():
    op.drop_table("production_piecework_commands")
