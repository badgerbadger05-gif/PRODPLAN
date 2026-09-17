"""R3 stable identity, source completeness and explicit live-MRP pointers."""

from alembic import op
import sqlalchemy as sa


revision = "20260910_01"
down_revision = "20260909_02"
branch_labels = None
depends_on = None


def backfill_live_pointers(bind) -> None:
    """Populate explicit current-MRP pointers from unambiguous fixed runs."""
    duplicate_plan = bind.execute(
        sa.text(
            "SELECT source_plan_id FROM planning_run "
            "WHERE status = 'FIXED_SNAPSHOT' AND source_plan_id IS NOT NULL "
            "GROUP BY source_plan_id HAVING count(*) > 1 LIMIT 1"
        )
    ).scalar()
    if duplicate_plan is not None:
        raise RuntimeError(
            "R3 migration refuses ambiguous current MRP pointer backfill for "
            f"plan {duplicate_plan}"
        )
    bind.execute(
        sa.text(
            "INSERT INTO planning_live_pointer (plan_id, run_id, status) "
            "SELECT pr.source_plan_id, pr.run_id, 'active' "
            "FROM planning_run pr "
            "JOIN production_plan_header p ON p.id = pr.source_plan_id "
            "WHERE pr.status = 'FIXED_SNAPSHOT' "
            "AND pr.source_plan_id IS NOT NULL "
            "AND p.status = 'fixed'"
        )
    )


def upgrade() -> None:
    op.add_column("physical_import_batch", sa.Column("expected_page_count", sa.Integer(), nullable=True))
    op.add_column("physical_import_batch", sa.Column("received_page_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("physical_import_batch", sa.Column("source_complete", sa.Boolean(), nullable=False, server_default=sa.true()))

    op.create_table(
        "physical_import_page",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("import_batch_id", sa.BigInteger(), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("page_token", sa.String(length=256), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["import_batch_id"], ["physical_import_batch.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("import_batch_id", "page_no", name="uq_physical_import_page_number"),
        sa.UniqueConstraint("import_batch_id", "page_token", name="uq_physical_import_page_token"),
        sa.CheckConstraint("page_no > 0", name="ck_physical_import_page_positive"),
    )
    op.create_index("ix_physical_import_page_batch", "physical_import_page", ["import_batch_id"])

    op.add_column("stock_ledger_entry", sa.Column("business_identity", sa.String(length=256), nullable=True))
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # The disposable schema-reproducibility database exercises this chain
        # with SQLite. Keep the production PostgreSQL identity expression
        # below unchanged, but use SQLite JSON1 and correlated UPDATE syntax
        # here instead of PostgreSQL JSON operators/casts/functions.
        missing_cutoff_hashes = bind.execute(sa.text(
            "SELECT count(*) FROM stock_ledger_entry e "
            "LEFT JOIN physical_import_batch b ON b.id = e.ingest_batch_id "
            "WHERE e.recorder_type = 'cutoff_balance_adjustment' "
            "AND nullif(json_extract(b.source_watermarks, '$.content_hash'), '') IS NULL"
        )).scalar()
    else:
        missing_cutoff_hashes = bind.execute(
            sa.text(
                "SELECT count(*) FROM stock_ledger_entry e "
                "LEFT JOIN physical_import_batch b ON b.id = e.ingest_batch_id "
                "WHERE e.recorder_type = 'cutoff_balance_adjustment' "
                "AND nullif(b.source_watermarks ->> 'content_hash', '') IS NULL"
            )
        ).scalar()
    if int(missing_cutoff_hashes or 0):
        raise RuntimeError(
            "R3 migration refuses cutoff adjustments without physical snap "
            f"content hash ({int(missing_cutoff_hashes)} rows)"
        )
    if bind.dialect.name == "sqlite":
        bind.execute(sa.text(
            "UPDATE stock_ledger_entry "
            "SET business_identity = CASE "
            "WHEN recorder_type = 'cutoff_balance_adjustment' THEN "
            "'movement:cutoff_balance_adjustment:snap:' || "
            "coalesce((SELECT json_extract(b.source_watermarks, '$.content_hash') "
            "FROM physical_import_batch b WHERE b.id = stock_ledger_entry.ingest_batch_id), '') || "
            "':ref:' || coalesce(recorder_ref, '') || ':line:' || "
            "coalesce(line_no, '') || ':cell:' || coalesce(item_id, '') || ':' || "
            "coalesce(characteristic_ref, '') || ':' || coalesce(organization_ref, '') || ':' || "
            "coalesce(warehouse_ref1c, '') "
            "ELSE 'movement:' || coalesce(recorder_type, '') || ':' || "
            "coalesce(recorder_ref, '') || ':' || coalesce(line_no, '') END "
            "WHERE business_identity IS NULL"
        ))
    else:
        op.execute(
            sa.text(
                "UPDATE stock_ledger_entry SET business_identity = CASE "
                "WHEN recorder_type = 'cutoff_balance_adjustment' THEN "
                "'movement:cutoff_balance_adjustment:snap:' || "
                "coalesce(batch.source_watermarks ->> 'content_hash', '') || "
                "':ref:' || coalesce(recorder_ref, '') || ':line:' || "
                "coalesce(line_no, '') || ':cell:' || md5(concat_ws(chr(31), "
                "coalesce(item_id::text, ''), coalesce(characteristic_ref, ''), "
                "coalesce(organization_ref, ''), coalesce(warehouse_ref1c, ''))) "
                "ELSE 'movement:' || coalesce(recorder_type, '') || ':' || "
                "coalesce(recorder_ref, '') || ':' || coalesce(line_no, '') END "
                "FROM physical_import_batch batch "
                "WHERE stock_ledger_entry.ingest_batch_id = batch.id "
                "AND stock_ledger_entry.business_identity IS NULL"
            )
        )
    duplicate = op.get_bind().execute(
        sa.text(
            "SELECT business_identity FROM stock_ledger_entry "
            "WHERE active = true GROUP BY business_identity "
            "HAVING count(*) > 1 LIMIT 1"
        )
    ).scalar()
    if duplicate is not None:
        raise RuntimeError(
            "R3 migration refuses ambiguous active duplicate business identity "
            f"{duplicate}"
        )
    op.alter_column("stock_ledger_entry", "business_identity", nullable=False, server_default="")
    op.create_index(
        "uq_stock_ledger_entry_active_business_identity",
        "stock_ledger_entry", ["business_identity"], unique=True,
        postgresql_where=sa.text("active = true"),
    )
    op.create_table(
        "stock_ledger_business_identity_map",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("business_identity", sa.String(length=256), nullable=False),
        sa.Column("stock_ledger_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("mapping_reason", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["stock_ledger_entry_id"], ["stock_ledger_entry.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("business_identity", "stock_ledger_entry_id", name="uq_stock_ledger_identity_map_edge"),
    )
    op.execute(
        sa.text(
            "INSERT INTO stock_ledger_business_identity_map "
            "(business_identity, stock_ledger_entry_id, mapping_reason) "
            "SELECT business_identity, id, 'legacy-explicit-backfill' FROM stock_ledger_entry"
        )
    )

    op.create_table(
        "planning_live_pointer",
        sa.Column("plan_id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["plan_id"], ["production_plan_header.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["planning_run.run_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("run_id", name="uq_planning_live_pointer_run"),
        sa.CheckConstraint("status IN ('active', 'retired')", name="ck_planning_live_pointer_status"),
    )
    op.create_index("ix_planning_live_pointer_run_id", "planning_live_pointer", ["run_id"])
    # Existing fixed plans must not emerge from this migration with an empty
    # pointer table.  The source-plan uniqueness constraint makes this mapping
    # deterministic; an ambiguity is diagnosed before any pointer is written.
    backfill_live_pointers(op.get_bind())
    op.create_table(
        "planning_run_successor",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("predecessor_run_id", sa.Integer(), nullable=False),
        sa.Column("successor_run_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["plan_id"], ["production_plan_header.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["predecessor_run_id"], ["planning_run.run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["successor_run_id"], ["planning_run.run_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("plan_id", "predecessor_run_id", "successor_run_id", name="uq_planning_run_successor_edge"),
    )
    op.create_index("ix_planning_run_successor_plan_id", "planning_run_successor", ["plan_id"])

    op.add_column("mrp_freeze_baseline", sa.Column("frozen_basis_generation_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_mrp_freeze_baseline_frozen_basis_generation_id", "mrp_freeze_baseline", ["frozen_basis_generation_id"])
    op.create_foreign_key(
        "fk_mrp_freeze_baseline_frozen_basis_generation",
        "mrp_freeze_baseline", "ledger_generation",
        ["frozen_basis_generation_id"], ["id"], ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_mrp_freeze_baseline_frozen_basis_generation", "mrp_freeze_baseline", type_="foreignkey")
    op.drop_index("ix_mrp_freeze_baseline_frozen_basis_generation_id", table_name="mrp_freeze_baseline")
    op.drop_column("mrp_freeze_baseline", "frozen_basis_generation_id")
    op.drop_index("ix_planning_run_successor_plan_id", table_name="planning_run_successor")
    op.drop_table("planning_run_successor")
    op.drop_index("ix_planning_live_pointer_run_id", table_name="planning_live_pointer")
    op.drop_table("planning_live_pointer")
    op.drop_table("stock_ledger_business_identity_map")
    op.drop_index("uq_stock_ledger_entry_active_business_identity", table_name="stock_ledger_entry")
    op.drop_column("stock_ledger_entry", "business_identity")
    op.drop_index("ix_physical_import_page_batch", table_name="physical_import_page")
    op.drop_table("physical_import_page")
    op.drop_column("physical_import_batch", "source_complete")
    op.drop_column("physical_import_batch", "received_page_count")
    op.drop_column("physical_import_batch", "expected_page_count")
