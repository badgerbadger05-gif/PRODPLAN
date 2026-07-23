"""Persist normalized supplier receipt operation evidence.

Revision ID: 20260723_12
Revises: 20260723_11
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260723_12"
down_revision = "20260723_11"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    table = "stock_ledger_supplier_receipt_provenance"
    op.add_column(
        table,
        sa.Column(
            "operation_kind",
            sa.String(length=32),
            server_default="unknown",
            nullable=False,
        ),
    )
    op.add_column(
        table,
        sa.Column("operation_key", sa.String(length=128), nullable=True),
    )
    op.add_column(
        table,
        sa.Column("operation_name", sa.String(length=128), nullable=True),
    )
    op.add_column(
        table,
        sa.Column("correction_receipt_ref", sa.String(length=64), nullable=True),
    )
    op.add_column(
        table,
        sa.Column(
            "evidence_hash",
            sa.String(length=64),
            server_default="",
            nullable=False,
        ),
    )
    op.add_column(
        table,
        sa.Column(
            "evidence_payload",
            _json_type(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_supplier_receipt_provenance_operation_kind",
        table,
        "operation_kind IN ('supplier_receipt', 'correction', "
        "'supplier_return', 'transfer', 'unknown')",
    )
    op.create_index(
        "ix_supplier_receipt_provenance_generation_kind",
        table,
        ["ledger_generation_id", "operation_kind"],
        unique=False,
    )


def downgrade() -> None:
    table = "stock_ledger_supplier_receipt_provenance"
    op.drop_index(
        "ix_supplier_receipt_provenance_generation_kind",
        table_name=table,
    )
    op.drop_constraint(
        "ck_supplier_receipt_provenance_operation_kind",
        table,
        type_="check",
    )
    for column in (
        "evidence_payload",
        "evidence_hash",
        "correction_receipt_ref",
        "operation_name",
        "operation_key",
        "operation_kind",
    ):
        op.drop_column(table, column)
