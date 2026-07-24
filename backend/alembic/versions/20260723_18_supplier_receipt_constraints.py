"""Align supplier receipt provenance constraints with persisted non-supplier exclusions.

Revision ID: 20260723_18
Revises: 20260723_17
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_18"
down_revision = "20260723_17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("stock_ledger_supplier_receipt_provenance") as batch_op:
        batch_op.alter_column(
            "match_status",
            existing_type=sa.String(length=16),
            type_=sa.String(length=32),
            existing_nullable=False,
        )
        batch_op.drop_constraint(
            "ck_supplier_receipt_provenance_match_status",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_supplier_receipt_provenance_match_status",
            "match_status IN ('exact', 'ambiguous', 'unmatched', "
            "'excluded_non_supplier')",
        )
        batch_op.drop_constraint(
            "ck_supplier_receipt_provenance_operation_kind",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_supplier_receipt_provenance_operation_kind",
            "operation_kind IN ('supplier_receipt', 'correction', "
            "'supplier_return', 'transfer', 'non_supplier_expense', 'unknown')",
        )
        batch_op.drop_constraint(
            "ck_supplier_receipt_provenance_match_evidence",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_supplier_receipt_provenance_match_evidence",
            "(match_status = 'exact' AND supplier_order_ref IS NOT NULL "
            "AND supplier_order_line_no IS NOT NULL AND ambiguity_count = 0) "
            "OR (match_status = 'ambiguous' AND ambiguity_count > 1 "
            "AND reason IS NOT NULL) "
            "OR (match_status = 'unmatched' AND ambiguity_count = 0 "
            "AND reason IS NOT NULL) "
            "OR (match_status = 'excluded_non_supplier' AND supplier_order_ref IS NULL "
            "AND supplier_order_line_no IS NULL AND ambiguity_count = 0 "
            "AND operation_kind = 'non_supplier_expense' "
            "AND operation_key IS NOT NULL AND operation_name IS NOT NULL "
            "AND reason IS NOT NULL)",
        )


def downgrade() -> None:
    with op.batch_alter_table("stock_ledger_supplier_receipt_provenance") as batch_op:
        batch_op.drop_constraint(
            "ck_supplier_receipt_provenance_match_evidence",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_supplier_receipt_provenance_match_evidence",
            "(match_status = 'exact' AND supplier_order_ref IS NOT NULL "
            "AND supplier_order_line_no IS NOT NULL AND ambiguity_count = 0) "
            "OR (match_status = 'ambiguous' AND ambiguity_count > 1 "
            "AND reason IS NOT NULL) "
            "OR (match_status = 'unmatched' AND ambiguity_count = 0 "
            "AND reason IS NOT NULL)",
        )
        batch_op.drop_constraint(
            "ck_supplier_receipt_provenance_operation_kind",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_supplier_receipt_provenance_operation_kind",
            "operation_kind IN ('supplier_receipt', 'correction', "
            "'supplier_return', 'transfer', 'unknown')",
        )
        batch_op.drop_constraint(
            "ck_supplier_receipt_provenance_match_status",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_supplier_receipt_provenance_match_status",
            "match_status IN ('exact', 'ambiguous', 'unmatched')",
        )
        batch_op.alter_column(
            "match_status",
            existing_type=sa.String(length=32),
            type_=sa.String(length=16),
            existing_nullable=False,
        )
