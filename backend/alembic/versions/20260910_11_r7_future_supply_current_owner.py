"""Add the stable current future-supply owner and change audit."""

from alembic import op
import sqlalchemy as sa


revision = "20260910_11"
down_revision = "20260910_10"
branch_labels = None
depends_on = None


_CURRENT_COLUMNS = """
current_identity, source_generation_id, source_capture_batch_id,
supply_kind, item_id, characteristic_ref, organization_ref,
planning_stock_pool, destination_warehouse_ref1c, source_ref,
source_line_ref, source_local_id, source_requirement_id,
ordered_qty_at_cutoff, realized_qty_at_cutoff, open_qty_at_cutoff,
eta_date, source_state_key, source_updated_at, capture_cutoff,
source_content_hash, evidence_status, reason
"""


def _accepted_scope(alias: str = "lfs") -> str:
    """Exact accepted-pointer scope; never infer a latest generation."""
    return f"""
      {alias}.ledger_generation_id = pts.current_generation_id
      AND lg.id = pts.current_generation_id
      AND lg.status = 'accepted'
    """


def _backfill_current_owner(bind) -> None:
    """Copy only the accepted pointer source, then prune legacy history.

    The source table can contain tens of millions of historical rows.  The
    validation, insert, and prune are all set-based and run in the migration's
    transaction.  No Python row materialization is permitted here: if the
    accepted source is malformed or collides, the migration fails before any
    legacy row is removed.
    """
    pointer_id = bind.execute(sa.text(
        "SELECT current_generation_id FROM planning_truth_state WHERE id = 1"
    )).scalar()
    if pointer_id is None:
        # There is no accepted source to publish.  Leave every legacy row
        # untouched; deleting it would destroy recoverable evidence without a
        # current owner.
        issue = bind.execute(sa.text(
            """
            SELECT lfs.id
              FROM ledger_future_supply AS lfs
              JOIN ledger_generation AS lg ON lg.id = lfs.ledger_generation_id
             WHERE lg.status = 'building'
               AND trim(coalesce(lfs.current_identity, '')) = ''
             LIMIT 1
            """
        )).first()
        if issue is not None:
            raise RuntimeError(
                "R7 future-supply BUILDING staging has no stable identity: "
                f"row {issue[0]}"
            )
        bind.execute(sa.text(
            """
            UPDATE ledger_future_supply
               SET is_current = false
             WHERE is_current IS NULL
               AND EXISTS (
                   SELECT 1 FROM ledger_generation AS lg
                    WHERE lg.id = ledger_future_supply.ledger_generation_id
                      AND lg.status = 'building'
               )
            """
        ))
        return

    validation = bind.execute(sa.text(
        f"""
        SELECT lfs.current_identity, count(*) AS row_count,
               max(length(trim(lfs.current_identity))) AS max_length
          FROM ledger_future_supply AS lfs
          JOIN ledger_generation AS lg ON lg.id = lfs.ledger_generation_id
          JOIN planning_truth_state AS pts ON pts.id = 1
         WHERE {_accepted_scope('lfs')}
         GROUP BY lfs.current_identity
        HAVING trim(coalesce(lfs.current_identity, '')) = ''
            OR count(*) > 1
            OR max(length(trim(lfs.current_identity))) > 256
         LIMIT 1
        """
    )).first()
    if validation is not None:
        raise RuntimeError(
            "R7 future-supply current backfill has empty/too-long/colliding "
            f"identity: {validation[0]!r}"
        )

    bind.execute(sa.text(
        f"""
        INSERT INTO ledger_future_supply_current ({_CURRENT_COLUMNS})
        SELECT lfs.current_identity, lfs.ledger_generation_id,
               lfs.capture_batch_id, lfs.supply_kind, lfs.item_id,
               lfs.characteristic_ref, lfs.organization_ref,
               lfs.planning_stock_pool, lfs.destination_warehouse_ref1c,
               lfs.source_ref, lfs.source_line_ref, lfs.source_local_id,
               lfs.source_requirement_id, lfs.ordered_qty_at_cutoff,
               lfs.realized_qty_at_cutoff, lfs.open_qty_at_cutoff,
               lfs.eta_date, lfs.source_state_key, lfs.source_updated_at,
               lfs.capture_cutoff, lfs.source_content_hash,
               lfs.evidence_status, lfs.reason
          FROM ledger_future_supply AS lfs
          JOIN ledger_generation AS lg ON lg.id = lfs.ledger_generation_id
          JOIN planning_truth_state AS pts ON pts.id = 1
         WHERE {_accepted_scope('lfs')}
           AND trim(coalesce(lfs.current_identity, '')) <> ''
        """
    ))

    staging_issue = bind.execute(sa.text(
        """
        SELECT lfs.id
          FROM ledger_future_supply AS lfs
          JOIN ledger_generation AS lg ON lg.id = lfs.ledger_generation_id
         WHERE lg.status = 'building'
           AND trim(coalesce(lfs.current_identity, '')) = ''
         LIMIT 1
        """
    )).first()
    if staging_issue is not None:
        raise RuntimeError(
            "R7 future-supply BUILDING staging has no stable identity: "
            f"row {staging_issue[0]}"
        )
    # Existing BUILDING staging is bounded, so normalizing its legacy marker is
    # safe and does not scan/copy the retired accepted history.
    bind.execute(sa.text(
        """
        UPDATE ledger_future_supply
           SET is_current = false
         WHERE is_current IS NULL
           AND EXISTS (
               SELECT 1 FROM ledger_generation AS lg
                WHERE lg.id = ledger_future_supply.ledger_generation_id
                  AND lg.status = 'building'
           )
        """
    ))

    # Only after the compact owner has been populated successfully may old
    # accepted/stale/rejected generation copies be removed.  BUILDING staging
    # remains bounded and recoverable for an in-flight build.  This destructive
    # prune requires the existing pre-migration backup/restore rehearsal; the
    # downgrade below intentionally fails closed once legacy evidence is gone.
    bind.execute(sa.text(
        """
        DELETE FROM ledger_future_supply
         WHERE EXISTS (
             SELECT 1
               FROM ledger_generation AS lg
              WHERE lg.id = ledger_future_supply.ledger_generation_id
                AND lg.status <> 'building'
         )
        """
    ))


def upgrade() -> None:
    op.create_table(
        "ledger_future_supply_current",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("current_identity", sa.String(length=256), nullable=False),
        sa.Column("source_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("source_capture_batch_id", sa.BigInteger(), nullable=False),
        sa.Column("supply_kind", sa.String(length=32), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("characteristic_ref", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("organization_ref", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("planning_stock_pool", sa.String(length=128), nullable=False),
        sa.Column("destination_warehouse_ref1c", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("source_ref", sa.String(length=64), nullable=True),
        sa.Column("source_line_ref", sa.String(length=64), nullable=True),
        sa.Column("source_local_id", sa.String(length=128), nullable=True),
        sa.Column("source_requirement_id", sa.Integer(), nullable=True),
        sa.Column("ordered_qty_at_cutoff", sa.Numeric(15, 3), nullable=False),
        sa.Column("realized_qty_at_cutoff", sa.Numeric(15, 3), nullable=False),
        sa.Column("open_qty_at_cutoff", sa.Numeric(15, 3), nullable=False),
        sa.Column("eta_date", sa.Date(), nullable=True),
        sa.Column("source_state_key", sa.String(length=64), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("capture_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_content_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["source_generation_id"], ["ledger_generation.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_capture_batch_id"], ["ledger_build_batch.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
        sa.ForeignKeyConstraint(["source_requirement_id"], ["mrp_requirement.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("current_identity", name="ux_ledger_future_supply_current_identity_v2"),
        sa.CheckConstraint("supply_kind IN ('wip_order', 'supplier_order')", name="ck_ledger_future_supply_current_kind"),
        sa.CheckConstraint("evidence_status IN ('exact', 'ambiguous', 'unmatched', 'rejected')", name="ck_ledger_future_supply_current_evidence_status"),
        sa.CheckConstraint("ordered_qty_at_cutoff >= 0 AND realized_qty_at_cutoff >= 0 AND open_qty_at_cutoff >= 0", name="ck_ledger_future_supply_current_quantities_nonnegative"),
    )
    op.create_index("ix_ledger_future_supply_current_item", "ledger_future_supply_current", ["item_id"])
    op.create_index("ix_ledger_future_supply_current_generation", "ledger_future_supply_current", ["source_generation_id"])

    op.create_table(
        "ledger_future_supply_current_change",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("current_identity", sa.String(length=256), nullable=False),
        sa.Column("current_row_id", sa.BigInteger(), nullable=True),
        sa.Column("source_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("before_payload", sa.JSON(), nullable=True),
        sa.Column("after_payload", sa.JSON(), nullable=True),
        sa.Column("source_content_hash", sa.String(length=64), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["source_generation_id"], ["ledger_generation.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_ledger_future_supply_current_change_identity", "ledger_future_supply_current_change", ["current_identity"])

    bind = op.get_bind()
    _backfill_current_owner(bind)
    with op.batch_alter_table("ledger_future_supply") as batch:
        batch.alter_column(
            "current_identity", existing_type=sa.String(length=256),
            nullable=False, server_default="",
        )
        batch.alter_column(
            "is_current", existing_type=sa.Boolean(),
            nullable=False, server_default=sa.text("false"),
        )


def downgrade() -> None:
    # The compact owner is the runtime source of truth after this revision;
    # simply dropping it would make a subsequent upgrade lose every published
    # row because new staging captures intentionally no longer toggle the
    # legacy ``is_current`` flag.  Restore the legacy marker only for an
    # unambiguous source row (identity + generation + capture batch), and fail
    # closed rather than guessing among historical copies.
    bind = op.get_bind()
    current_rows = bind.execute(sa.text(
        """
        SELECT current_identity, source_generation_id, source_capture_batch_id
          FROM ledger_future_supply_current
         ORDER BY id
        """
    )).mappings().all()
    bind.execute(sa.text("UPDATE ledger_future_supply SET is_current = false"))
    for row in current_rows:
        matches = bind.execute(sa.text(
            """
            SELECT id
              FROM ledger_future_supply
             WHERE current_identity = :identity
               AND ledger_generation_id = :generation_id
               AND capture_batch_id = :capture_batch_id
             ORDER BY id
            """
        ), {
            "identity": row["current_identity"],
            "generation_id": row["source_generation_id"],
            "capture_batch_id": row["source_capture_batch_id"],
        }).scalars().all()
        if len(matches) != 1:
            raise RuntimeError(
                "R7 future-supply downgrade cannot restore legacy current "
                f"identity {row['current_identity']!r}: expected one source "
                f"row, found {len(matches)}"
            )
        bind.execute(sa.text(
            "UPDATE ledger_future_supply SET is_current = true WHERE id = :id"
        ), {"id": matches[0]})
    op.drop_index("ix_ledger_future_supply_current_change_identity", table_name="ledger_future_supply_current_change")
    op.drop_table("ledger_future_supply_current_change")
    op.drop_index("ix_ledger_future_supply_current_generation", table_name="ledger_future_supply_current")
    op.drop_index("ix_ledger_future_supply_current_item", table_name="ledger_future_supply_current")
    op.drop_table("ledger_future_supply_current")
