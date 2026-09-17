"""Bounded cutover for generation-scoped execution projections.

The current execution owner is published by 20260910_12/13 and the accepted
compatibility rows are still needed by manual drum actions, work-item
navigation and supplier provenance validation.  This revision therefore does
not rewrite or migrate those rows: it proves the exact accepted pointer has a
complete current execution manifest, then removes only retired non-building
generation copies.  Active BUILDING staging and the exact current generation
remain available to compatibility readers.

No ledger generation is removed here.  Reservation/history GC is a later
revision after all dependent readers have cut over and backup prerequisites
have been verified.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260914_02"
down_revision = "20260914_01"
branch_labels = None
depends_on = None


_REQUIRED_SCOPES = (
    ("assembly_queue", "assembly:all-live-plans"),
    ("assembly_readiness", "assembly:all-live-plans"),
    ("drum_schedule", "drum:all-live-plans"),
    ("drum_slot", "drum:all-live-plans"),
    ("drum_gap", "drum:all-live-plans"),
    ("drum_excluded", "drum:all-live-plans"),
    ("shelf_projection", "shelf:all-live-mrps"),
)


# FK-safe order: drum children, queue/readiness parents, then independent
# generation-scoped compatibility projections.  Every statement excludes the
# exact accepted pointer and explicitly preserves active BUILDING rows.
_RETENTION_STATEMENTS = (
    """
    DELETE FROM drum_capacity_gap
     WHERE EXISTS (
               SELECT 1 FROM drum_schedule ds
                WHERE ds.id = drum_capacity_gap.drum_schedule_id
                  AND ds.ledger_generation_id <> :generation_id
           )
       AND NOT EXISTS (
               SELECT 1 FROM drum_schedule ds
               JOIN ledger_generation lg ON lg.id = ds.ledger_generation_id
                WHERE ds.id = drum_capacity_gap.drum_schedule_id
                  AND lg.status = 'building'
           )
       AND NOT EXISTS (
               SELECT 1 FROM assembly_queue_line q
               JOIN ledger_generation lg ON lg.id = q.ledger_generation_id
                WHERE q.id = drum_capacity_gap.assembly_queue_line_id
                  AND lg.status = 'building'
           )
    """,
    """
    DELETE FROM drum_slot
     WHERE EXISTS (
               SELECT 1 FROM drum_schedule ds
                WHERE ds.id = drum_slot.drum_schedule_id
                  AND ds.ledger_generation_id <> :generation_id
           )
       AND NOT EXISTS (
               SELECT 1 FROM drum_schedule ds
               JOIN ledger_generation lg ON lg.id = ds.ledger_generation_id
                WHERE ds.id = drum_slot.drum_schedule_id
                  AND lg.status = 'building'
           )
       AND NOT EXISTS (
               SELECT 1 FROM assembly_queue_line q
               JOIN ledger_generation lg ON lg.id = q.ledger_generation_id
                WHERE q.id = drum_slot.assembly_queue_line_id
                  AND lg.status = 'building'
           )
    """,
    """
    DELETE FROM assembly_readiness
     WHERE ledger_generation_id <> :generation_id
       AND NOT EXISTS (
               SELECT 1 FROM ledger_generation lg
                WHERE lg.id = assembly_readiness.ledger_generation_id
                  AND lg.status = 'building'
           )
    """,
    """
    DELETE FROM drum_schedule
     WHERE ledger_generation_id <> :generation_id
       AND NOT EXISTS (
               SELECT 1 FROM ledger_generation lg
                WHERE lg.id = drum_schedule.ledger_generation_id
                  AND lg.status = 'building'
           )
    """,
    """
    DELETE FROM assembly_queue_line
     WHERE ledger_generation_id <> :generation_id
       AND NOT EXISTS (
               SELECT 1 FROM ledger_generation lg
                WHERE lg.id = assembly_queue_line.ledger_generation_id
                  AND lg.status = 'building'
           )
    """,
    """
    DELETE FROM shelf_projection
     WHERE ledger_generation_id <> :generation_id
       AND NOT EXISTS (
               SELECT 1 FROM ledger_generation lg
                WHERE lg.id = shelf_projection.ledger_generation_id
                  AND lg.status = 'building'
           )
    """,
    """
    DELETE FROM assembly_output_allocation
     WHERE ledger_generation_id <> :generation_id
       AND NOT EXISTS (
               SELECT 1 FROM ledger_generation lg
                WHERE lg.id = assembly_output_allocation.ledger_generation_id
                  AND lg.status = 'building'
           )
    """,
    """
    DELETE FROM assembly_output_fact_decision
     WHERE ledger_generation_id <> :generation_id
       AND NOT EXISTS (
               SELECT 1 FROM ledger_generation lg
                WHERE lg.id = assembly_output_fact_decision.ledger_generation_id
                  AND lg.status = 'building'
           )
    """,
    """
    DELETE FROM stock_ledger_supplier_receipt_provenance
     WHERE ledger_generation_id <> :generation_id
       AND NOT EXISTS (
               SELECT 1 FROM ledger_generation lg
                WHERE lg.id = stock_ledger_supplier_receipt_provenance.ledger_generation_id
                  AND lg.status = 'building'
           )
    """,
    """
    DELETE FROM replenishment_work_item
     WHERE ledger_generation_id <> :generation_id
       AND NOT EXISTS (
               SELECT 1 FROM ledger_generation lg
                WHERE lg.id = replenishment_work_item.ledger_generation_id
                  AND lg.status = 'building'
           )
    """,
)


def _current_generation(bind) -> int:
    row = bind.execute(sa.text(
        """
        SELECT pts.current_generation_id
          FROM planning_truth_state pts
          JOIN ledger_generation lg ON lg.id = pts.current_generation_id
         WHERE pts.id = 1 AND lg.status = 'accepted'
        """
    )).scalar()
    if row is None:
        raise RuntimeError(
            "execution projection cutover requires an accepted truth pointer"
        )
    return int(row)


def _assert_manifest(bind, generation_id: int) -> None:
    rows = bind.execute(sa.text(
        """
        SELECT entity_kind, scope_key
          FROM current_execution_scope
         WHERE source_generation_id = :generation_id
           AND result_ready = true
        """
    ), {"generation_id": generation_id}).all()
    covered = {(str(row[0]), str(row[1])) for row in rows}
    missing = [scope for scope in _REQUIRED_SCOPES if scope not in covered]
    if missing:
        raise RuntimeError(
            "execution projection cutover has incomplete current manifest: "
            + ", ".join(f"{kind}/{scope}" for kind, scope in missing)
        )


def _assert_backup_ready(bind) -> None:
    """Require an operator-verified backup in the migration session."""
    if bind.dialect.name != "postgresql":
        raise RuntimeError(
            "execution projection cutover requires PostgreSQL backup guard"
        )
    ready = bind.execute(sa.text(
        "SELECT current_setting('prodplan.execution_projection_backup_ready', true)"
    )).scalar()
    if str(ready or "").strip().lower() != "on":
        raise RuntimeError(
            "execution projection cutover requires verified backup: set "
            "prodplan.execution_projection_backup_ready=on in this session"
        )


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite is used only for disposable schema reproducibility.  It has no
    # accepted current contour and cannot provide the PostgreSQL session
    # backup guard required before this destructive cutover.
    if bind.dialect.name == "sqlite":
        return
    _assert_backup_ready(bind)
    generation_id = _current_generation(bind)
    _assert_manifest(bind, generation_id)
    params = {"generation_id": generation_id}
    for statement in _RETENTION_STATEMENTS:
        bind.execute(sa.text(statement), params)


def downgrade() -> None:
    # Deleted retired projection rows cannot be reconstructed without the
    # external backup.  There is no schema object to reverse in this revision;
    # restoration must use the verified backup before a downgrade attempt.
    pass
