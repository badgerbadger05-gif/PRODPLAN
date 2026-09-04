"""Add cumulative assembly readiness curve and action provenance.

Revision ID: 20260903_03
Revises: 20260903_02
"""

from alembic import op
import sqlalchemy as sa


revision = "20260903_03"
down_revision = "20260903_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("assembly_readiness") as batch:
        batch.drop_constraint("ck_assembly_readiness_status", type_="check")
        batch.create_check_constraint(
            "ck_assembly_readiness_status",
            "status IN ('ready', 'recoverable', 'partial', 'blocked', 'unavailable')",
        )
        batch.add_column(sa.Column("transferable_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"))
        batch.add_column(sa.Column("kitting_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"))
        batch.add_column(sa.Column("committed_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"))
        batch.add_column(sa.Column("launchable_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"))
        batch.add_column(sa.Column("readiness_date", sa.Date(), nullable=True))
        batch.add_column(sa.Column("readiness_curve", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        batch.add_column(sa.Column("action_manifest", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        batch.add_column(sa.Column("unavailable_reasons", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    with op.batch_alter_table("drum_slot") as batch:
        batch.drop_constraint("ck_drum_slot_readiness_phase", type_="check")
        batch.create_check_constraint(
            "ck_drum_slot_readiness_phase",
            "readiness_phase IN ('now', 'transfer', 'kitting', 'committed', 'launch', 'blocked', 'unavailable')",
        )
        batch.add_column(sa.Column("readiness_date", sa.Date(), nullable=True))
        batch.add_column(sa.Column("readiness_curve", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        batch.add_column(sa.Column("action_manifest", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        batch.add_column(sa.Column("unavailable_reasons", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        batch.add_column(sa.Column("blocking_manifest", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    with op.batch_alter_table("drum_capacity_gap") as batch:
        batch.drop_constraint("ck_drum_gap_readiness_phase", type_="check")
        batch.create_check_constraint(
            "ck_drum_gap_readiness_phase",
            "readiness_phase IN ('now', 'transfer', 'kitting', 'committed', 'launch', 'blocked', 'unavailable', 'mixed')",
        )


def downgrade() -> None:
    with op.batch_alter_table("drum_capacity_gap") as batch:
        batch.drop_constraint("ck_drum_gap_readiness_phase", type_="check")
        batch.create_check_constraint(
            "ck_drum_gap_readiness_phase",
            "readiness_phase IN ('ready', 'blocked', 'unavailable', 'mixed')",
        )
    with op.batch_alter_table("drum_slot") as batch:
        batch.drop_column("blocking_manifest")
        batch.drop_column("unavailable_reasons")
        batch.drop_column("action_manifest")
        batch.drop_column("readiness_curve")
        batch.drop_column("readiness_date")
        batch.drop_constraint("ck_drum_slot_readiness_phase", type_="check")
        batch.create_check_constraint(
            "ck_drum_slot_readiness_phase",
            "readiness_phase IN ('ready', 'blocked', 'unavailable')",
        )
    with op.batch_alter_table("assembly_readiness") as batch:
        batch.drop_column("unavailable_reasons")
        batch.drop_column("action_manifest")
        batch.drop_column("readiness_curve")
        batch.drop_column("readiness_date")
        batch.drop_column("launchable_qty")
        batch.drop_column("committed_qty")
        batch.drop_column("kitting_qty")
        batch.drop_column("transferable_qty")
        batch.drop_constraint("ck_assembly_readiness_status", type_="check")
        batch.create_check_constraint(
            "ck_assembly_readiness_status",
            "status IN ('ready', 'partial', 'blocked', 'unavailable')",
        )
