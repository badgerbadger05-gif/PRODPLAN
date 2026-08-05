"""Persist plan execution and run-owned MRP roots.

Revision ID: 20260805_01
Revises: 20260804_01
"""

from decimal import Decimal

import sqlalchemy as sa
from alembic import op


revision = "20260805_01"
down_revision = "20260804_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("production_plan_line") as batch_op:
        batch_op.add_column(
            sa.Column(
                "accepted_output_qty",
                sa.DECIMAL(15, 3),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column("remaining_output_qty", sa.DECIMAL(15, 3), nullable=True)
        )

    with op.batch_alter_table("assembly_output_allocation") as batch_op:
        batch_op.add_column(sa.Column("run_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_assembly_output_allocation_run",
            "planning_run",
            ["run_id"],
            ["run_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            "ix_assembly_output_allocation_run_id", ["run_id"], unique=False
        )

    op.create_table(
        "mrp_run_root",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("plan_line_id", sa.Integer(), nullable=False),
        sa.Column("planned_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column(
            "accepted_qty", sa.DECIMAL(15, 3), server_default="0", nullable=False
        ),
        sa.Column("remaining_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("planned_qty >= 0", name="ck_mrp_run_root_planned_nonnegative"),
        sa.CheckConstraint("accepted_qty >= 0", name="ck_mrp_run_root_accepted_nonnegative"),
        sa.CheckConstraint("remaining_qty >= 0", name="ck_mrp_run_root_remaining_nonnegative"),
        sa.ForeignKeyConstraint(["plan_line_id"], ["production_plan_line.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["planning_run.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "plan_line_id", name="uq_mrp_run_root_run_plan_line"),
    )
    op.create_index("ix_mrp_run_root_run", "mrp_run_root", ["run_id"])
    op.create_index("ix_mrp_run_root_plan_line", "mrp_run_root", ["plan_line_id"])

    op.create_table(
        "production_plan_execution_fact",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("stock_ledger_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("plan_line_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("allocated_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("match_rule", sa.String(length=8), nullable=False),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "allocated_qty > 0", name="ck_production_plan_execution_fact_qty_positive"
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["production_plan_header.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["plan_line_id"], ["production_plan_line.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["planning_run.run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["stock_ledger_entry_id"], ["stock_ledger_entry.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stock_ledger_entry_id",
            "plan_line_id",
            name="uq_production_plan_execution_fact_sle_line",
        ),
    )
    op.create_index(
        "ix_production_plan_execution_fact_run",
        "production_plan_execution_fact",
        ["run_id"],
    )
    op.create_index(
        "ix_production_plan_execution_fact_plan",
        "production_plan_execution_fact",
        ["plan_id", "plan_line_id"],
    )

    bind = op.get_bind()
    meta = sa.MetaData()
    plan_line = sa.Table("production_plan_line", meta, autoload_with=bind)
    planning_run = sa.Table("planning_run", meta, autoload_with=bind)
    truth = sa.Table("planning_truth_state", meta, autoload_with=bind)
    allocation = sa.Table("assembly_output_allocation", meta, autoload_with=bind)
    queue = sa.Table("assembly_queue_line", meta, autoload_with=bind)
    run_root = sa.Table("mrp_run_root", meta, autoload_with=bind)
    execution_fact = sa.Table("production_plan_execution_fact", meta, autoload_with=bind)

    generation_id = bind.execute(
        sa.select(truth.c.current_generation_id).where(truth.c.id == 1)
    ).scalar_one_or_none()
    accepted_by_line: dict[int, Decimal] = {}
    if generation_id is not None:
        for line_id, accepted in bind.execute(
            sa.select(
                allocation.c.plan_line_id,
                sa.func.coalesce(sa.func.sum(allocation.c.allocated_qty), 0),
            )
            .where(allocation.c.ledger_generation_id == int(generation_id))
            .group_by(allocation.c.plan_line_id)
        ):
            accepted_by_line[int(line_id)] = Decimal(str(accepted or 0))

    line_rows = list(bind.execute(sa.select(plan_line.c.id, plan_line.c.qty)))
    for line_id, qty in line_rows:
        planned = max(Decimal(str(qty or 0)), Decimal("0"))
        accepted = min(
            max(accepted_by_line.get(int(line_id), Decimal("0")), Decimal("0")),
            planned,
        )
        bind.execute(
            plan_line.update()
            .where(plan_line.c.id == int(line_id))
            .values(
                accepted_output_qty=accepted,
                remaining_output_qty=max(planned - accepted, Decimal("0")),
            )
        )

    if generation_id is None:
        return

    active_runs = {
        int(plan_id): int(run_id)
        for run_id, plan_id in bind.execute(
            sa.select(planning_run.c.run_id, planning_run.c.source_plan_id).where(
                planning_run.c.status == "FIXED_SNAPSHOT",
                planning_run.c.source_plan_id.is_not(None),
            )
        )
    }
    queue_rows = list(
        bind.execute(
            sa.select(
                queue.c.planning_run_id,
                queue.c.plan_line_id,
                queue.c.planned_output_qty,
                queue.c.accepted_plan_output_qty,
                queue.c.assembly_remaining_qty,
            ).where(queue.c.ledger_generation_id == int(generation_id))
        )
    )
    queued_lines: set[int] = set()
    for run_id, line_id, planned, accepted, remaining in queue_rows:
        bind.execute(
            run_root.insert().values(
                run_id=int(run_id),
                plan_line_id=int(line_id),
                planned_qty=planned,
                accepted_qty=accepted,
                remaining_qty=remaining,
            )
        )
        queued_lines.add(int(line_id))

    for line_id, plan_id, qty, accepted, remaining in bind.execute(
        sa.select(
            plan_line.c.id,
            plan_line.c.plan_id,
            plan_line.c.qty,
            plan_line.c.accepted_output_qty,
            plan_line.c.remaining_output_qty,
        )
    ):
        run_id = active_runs.get(int(plan_id))
        if run_id is None or int(line_id) in queued_lines:
            continue
        bind.execute(
            run_root.insert().values(
                run_id=run_id,
                plan_line_id=int(line_id),
                planned_qty=qty,
                accepted_qty=accepted,
                remaining_qty=remaining,
            )
        )

    queue_run_by_line = {
        int(line_id): int(run_id) for run_id, line_id, *_ in queue_rows
    }
    for line_id, run_id in queue_run_by_line.items():
        bind.execute(
            allocation.update()
            .where(
                allocation.c.ledger_generation_id == int(generation_id),
                allocation.c.plan_line_id == int(line_id),
            )
            .values(run_id=int(run_id))
        )
    for row in bind.execute(
        sa.select(
            allocation.c.stock_ledger_entry_id,
            allocation.c.plan_id,
            allocation.c.plan_line_id,
            allocation.c.allocated_qty,
            allocation.c.match_rule,
        ).where(allocation.c.ledger_generation_id == int(generation_id))
    ):
        run_id = queue_run_by_line.get(int(row.plan_line_id)) or active_runs.get(
            int(row.plan_id)
        )
        if run_id is None:
            continue
        bind.execute(
            execution_fact.insert().values(
                stock_ledger_entry_id=int(row.stock_ledger_entry_id),
                plan_id=int(row.plan_id),
                plan_line_id=int(row.plan_line_id),
                run_id=int(run_id),
                allocated_qty=row.allocated_qty,
                match_rule=row.match_rule,
            )
        )


def downgrade() -> None:
    op.drop_index(
        "ix_production_plan_execution_fact_plan",
        table_name="production_plan_execution_fact",
    )
    op.drop_index(
        "ix_production_plan_execution_fact_run",
        table_name="production_plan_execution_fact",
    )
    op.drop_table("production_plan_execution_fact")
    op.drop_index("ix_mrp_run_root_plan_line", table_name="mrp_run_root")
    op.drop_index("ix_mrp_run_root_run", table_name="mrp_run_root")
    op.drop_table("mrp_run_root")
    with op.batch_alter_table("assembly_output_allocation") as batch_op:
        batch_op.drop_index("ix_assembly_output_allocation_run_id")
        batch_op.drop_constraint(
            "fk_assembly_output_allocation_run", type_="foreignkey"
        )
        batch_op.drop_column("run_id")
    with op.batch_alter_table("production_plan_line") as batch_op:
        batch_op.drop_column("remaining_output_qty")
        batch_op.drop_column("accepted_output_qty")
