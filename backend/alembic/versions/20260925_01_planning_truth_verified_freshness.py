"""Verified-freshness columns on the planning truth pointer (decision §57).

Freshness of the accepted generation is the time of the last successful
reconciliation with 1C, not the time of the last publication.  A physical
refresh that read 1C up to a new cutoff, converged on balances and found no
semantic delta proves the accepted pointer is still true up to that cutoff,
yet it deliberately publishes nothing: an equivalent import has no successor.

The proof therefore has nowhere to live on the generation - generation lineage
is immutable and the verified generation is the one already accepted - so it is
recorded on the singleton pointer instead: which generation was verified, up to
which cutoff, and when.  The reader uses it only while
``verified_generation_id`` still equals ``current_generation_id``; any pointer
move makes the verification irrelevant without any row having to be cleared.

No foreign key on ``verified_generation_id`` on purpose: it is an equality
witness, not a reference.  A RESTRICT edge from the pointer would hold every
verified generation against R8 accepted-generation GC long after the pointer
moved away from it.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260925_01"
down_revision = "20260924_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "planning_truth_state",
        sa.Column("verified_generation_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "planning_truth_state",
        sa.Column("verified_cutoff", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "planning_truth_state",
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("planning_truth_state", "verified_at")
    op.drop_column("planning_truth_state", "verified_cutoff")
    op.drop_column("planning_truth_state", "verified_generation_id")
