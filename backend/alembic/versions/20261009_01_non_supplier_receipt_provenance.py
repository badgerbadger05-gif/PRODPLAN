"""Type processing returns as non-supplier receipts without changing stock."""
from alembic import op
import sqlalchemy as sa

revision = "20261009_01"
down_revision = "20260925_01"
branch_labels = None
depends_on = None


def _replace_checks(include_receipts: bool) -> None:
    kinds = "'supplier_receipt', 'correction', 'supplier_return', 'transfer', 'non_supplier_expense', 'unknown'"
    exclusions = "'non_supplier_expense'"
    if include_receipts:
        kinds += ", 'non_supplier_receipt'"
        exclusions += ", 'non_supplier_receipt'"
    with op.batch_alter_table("stock_ledger_supplier_receipt_provenance") as batch:
        batch.drop_constraint("ck_supplier_receipt_provenance_operation_kind", type_="check")
        batch.drop_constraint("ck_supplier_receipt_provenance_match_evidence", type_="check")
        batch.create_check_constraint("ck_supplier_receipt_provenance_operation_kind", f"operation_kind IN ({kinds})")
        batch.create_check_constraint("ck_supplier_receipt_provenance_match_evidence",
            "(match_status = 'exact' AND supplier_order_ref IS NOT NULL AND supplier_order_line_no IS NOT NULL AND ambiguity_count = 0) "
            "OR (match_status = 'ambiguous' AND ambiguity_count > 1 AND reason IS NOT NULL) "
            "OR (match_status = 'unmatched' AND ambiguity_count = 0 AND reason IS NOT NULL) "
            "OR (match_status = 'excluded_non_supplier' AND supplier_order_ref IS NULL AND supplier_order_line_no IS NULL AND ambiguity_count = 0 "
            f"AND operation_kind IN ({exclusions}) AND operation_key IS NOT NULL AND operation_name IS NOT NULL AND reason IS NOT NULL)")


def upgrade() -> None:
    _replace_checks(True)


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM stock_ledger_supplier_receipt_provenance WHERE operation_kind = 'non_supplier_receipt' LIMIT 1")).first():
        raise RuntimeError("Cannot downgrade while typed non-supplier receipt evidence exists")
    _replace_checks(False)
