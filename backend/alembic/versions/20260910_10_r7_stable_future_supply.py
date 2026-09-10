"""Make future-supply source identities stable across generation refreshes."""

from alembic import op
import sqlalchemy as sa


revision = "20260910_10"
down_revision = "20260910_09"
branch_labels = None
depends_on = None


def _identity(row) -> str:
    status = str(row["evidence_status"] or "")
    if status == "exact":
        return ":".join(
            str(row[key] or "").strip()
            for key in ("supply_kind", "source_ref", "source_line_ref", "source_local_id")
        )
    return f"{str(row['supply_kind'] or '').strip()}:rejected:{str(row['source_content_hash'] or '').strip()}"


def upgrade() -> None:
    op.add_column(
        "ledger_future_supply",
        sa.Column("current_identity", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "ledger_future_supply",
        sa.Column("is_current", sa.Boolean(), nullable=True),
    )
    bind = op.get_bind()
    pointer_id = bind.execute(sa.text(
        "SELECT current_generation_id FROM planning_truth_state WHERE id = 1"
    )).scalar()
    rows = bind.execute(sa.text(
        """SELECT id, ledger_generation_id, supply_kind, source_ref,
                         source_line_ref, source_local_id, source_content_hash,
                         evidence_status
                    FROM ledger_future_supply
                   ORDER BY id"""
    )).mappings().all()
    current_identities = set()
    for row in rows:
        identity = _identity(row)
        if not identity:
            raise RuntimeError(f"R7 future-supply row {row['id']} has no stable identity")
        is_current = pointer_id is not None and int(row["ledger_generation_id"]) == int(pointer_id)
        if is_current and identity in current_identities:
            raise RuntimeError(
                f"R7 future-supply active identity collision: {identity}"
            )
        if is_current:
            current_identities.add(identity)
        bind.execute(
            sa.text(
                """UPDATE ledger_future_supply
                       SET current_identity = :identity, is_current = :is_current
                     WHERE id = :id"""
            ),
            {"identity": identity, "is_current": is_current},
        )
    with op.batch_alter_table("ledger_future_supply") as batch:
        batch.alter_column(
            "current_identity", existing_type=sa.String(length=256),
            nullable=False, server_default="",
        )
        batch.alter_column(
            "is_current", existing_type=sa.Boolean(),
            nullable=False, server_default=sa.text("false"),
        )
    op.create_index(
        "ux_ledger_future_supply_current_identity",
        "ledger_future_supply",
        ["current_identity"],
        unique=True,
        postgresql_where=sa.text("is_current = true AND current_identity <> ''"),
        sqlite_where=sa.text("is_current = 1 AND current_identity <> ''"),
    )


def downgrade() -> None:
    op.drop_index(
        "ux_ledger_future_supply_current_identity",
        table_name="ledger_future_supply",
    )
    with op.batch_alter_table("ledger_future_supply") as batch:
        batch.drop_column("is_current")
        batch.drop_column("current_identity")
