"""Add accepted production mutation coordination epoch.

Revision ID: 20261030_02
Revises: 20261030_01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261030_02"
down_revision = "20261030_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(
        postgresql.JSONB(astext_type=sa.Text()), "postgresql"
    )
    op.create_table(
        "accepted_production_mutation_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_issued_epoch", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_consumed_epoch", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "accepted_production_mutation",
        sa.Column("epoch", sa.BigInteger(), primary_key=True),
        sa.Column("accepted_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("mutation_kind", sa.String(64), nullable=False),
        sa.Column("product_ids", json_type, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("item_ids", json_type, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("business_identities", json_type, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("full_scope", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["accepted_generation_id"], ["ledger_generation.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_accepted_production_mutation_generation_epoch",
        "accepted_production_mutation", ["accepted_generation_id", "epoch"],
    )
    op.execute(
        "INSERT INTO accepted_production_mutation_state "
        "(id, last_issued_epoch, last_consumed_epoch) VALUES (1, 0, 0)"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_accepted_production_mutation_generation_epoch",
        table_name="accepted_production_mutation",
    )
    op.drop_table("accepted_production_mutation")
    op.drop_table("accepted_production_mutation_state")
