"""Bootstrap the stable reservation current owner for the accepted pointer.

This revision deliberately does not compact history.  A generation is a build
boundary/provenance value, never a reservation identity.  Only exact accepted
pointer rows receive current ownership/event metadata; historical rows remain
legacy until a later bounded GC revision proves all dependency cutovers.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260914_01"
down_revision = "20260911_03"
branch_labels = None
depends_on = None


_IDENTITY_SQL = """
    'reservation:req:' || CAST(requirement_id AS TEXT) ||
    ':mode:' || trim(coalesce(realization_mode, ''))
"""

# Keep this byte-for-byte equivalent to
# ``reservation_event_identity`` in the runtime service: plain pipe-separated
# fields, fixed three-decimal quantities, and no generation/cycle labels.
_EVENT_IDENTITY_SQL = """
    concat(
        owner.current_identity, '|',
        coalesce(event.event_kind, ''), '|',
        coalesce(CAST(event.sle_id AS TEXT), ''), '|',
        trim(coalesce(event.fact_ref, '')), '|',
        trim(coalesce(event.fact_line_ref, '')), '|',
        to_char(coalesce(event.reserved_delta, 0), 'FM999999999999990.000'), '|',
        to_char(coalesce(event.realized_delta, 0), 'FM999999999999990.000'), '|',
        trim(coalesce(event.match_rule, ''))
    )
"""

_EVENT_ORIGIN_SQL = """
    CASE
        WHEN event.sle_id IS NULL AND coalesce(event.realized_delta, 0) = 0
            THEN 'obligation'
        WHEN event.event_kind = 'unrealize' OR coalesce(event.realized_delta, 0) < 0
            THEN 'correction'
        ELSE 'factual'
    END
"""


def _validate_and_backfill(bind) -> None:
    """Bootstrap only the exact accepted pointer; GC owns history later."""
    pointer = bind.execute(sa.text(
        """
        SELECT pts.current_generation_id
          FROM planning_truth_state AS pts
          JOIN ledger_generation AS lg ON lg.id = pts.current_generation_id
         WHERE pts.id = 1 AND lg.status = 'accepted'
        """
    )).scalar()
    if pointer is None:
        raise RuntimeError(
            "reservation current-owner migration requires an accepted truth pointer"
        )

    params = {"generation_id": int(pointer)}
    duplicate = bind.execute(sa.text("""
        SELECT requirement_id, realization_mode, count(*)
          FROM reservation_entry
         WHERE ledger_generation_id = :generation_id
         GROUP BY requirement_id, realization_mode
        HAVING count(*) > 1
         LIMIT 1
    """), params).first()
    if duplicate is not None:
        raise RuntimeError(
            "reservation current-owner migration found accepted identity collision: "
            f"requirement={duplicate[0]} mode={duplicate[1]} rows={duplicate[2]}"
        )

    identity_collision = bind.execute(sa.text(f"""
        SELECT {_IDENTITY_SQL}, count(*)
          FROM reservation_entry
         WHERE ledger_generation_id = :generation_id
         GROUP BY {_IDENTITY_SQL}
        HAVING count(*) > 1
         LIMIT 1
    """), params).first()
    if identity_collision is not None:
        raise RuntimeError(
            "reservation current-owner migration found accepted current identity collision: "
            f"identity={identity_collision[0]!r} rows={identity_collision[1]}"
        )

    # Validate immutable obligation payload only for the accepted pointer.  A
    # later GC revision owns historical compaction and must not make bootstrap
    # depend on 7.8m legacy rows.
    conflict = bind.execute(sa.text(f"""
        SELECT current_identity, count(*) AS variants
          FROM (
                SELECT {_IDENTITY_SQL} AS current_identity,
                       item_id, characteristic_ref, organization_ref,
                       planning_stock_pool, run_id, freeze_version,
                       requirement_id, priority_period_from, priority_period_to,
                       realization_mode, reserved_qty,
                       covered_from_stock_at_freeze_qty,
                       replenishment_required_qty
                  FROM reservation_entry
                 WHERE ledger_generation_id = :generation_id
               ) AS variants
         GROUP BY current_identity
        HAVING count(DISTINCT (
            item_id, characteristic_ref, organization_ref,
            planning_stock_pool, run_id, freeze_version,
            requirement_id, priority_period_from, priority_period_to,
            realization_mode, reserved_qty,
            covered_from_stock_at_freeze_qty,
            replenishment_required_qty
        )) > 1
         LIMIT 1
    """), params).first()
    if conflict is not None:
        raise RuntimeError(
            "reservation current-owner migration found accepted immutable obligation collision: "
            f"identity={conflict[0]!r} variants={conflict[1]}"
        )

    # Exactly the accepted pointer is promoted.  Historical rows retain the
    # initial ``legacy`` metadata and are not rewritten or rebound here.
    bind.execute(sa.text(f"""
        UPDATE reservation_entry
           SET current_identity = {_IDENTITY_SQL},
               owner_kind = 'current',
               is_current = true
         WHERE ledger_generation_id = :generation_id
    """), params)

    event_identity_issue = bind.execute(sa.text(f"""
        SELECT event.id, length({_EVENT_IDENTITY_SQL})
          FROM reservation_event AS event
          JOIN reservation_entry AS owner ON owner.id = event.reservation_id
         WHERE owner.ledger_generation_id = :generation_id
           AND (trim(coalesce({_EVENT_IDENTITY_SQL}, '')) = ''
                OR length({_EVENT_IDENTITY_SQL}) > 320)
         LIMIT 1
    """), params).first()
    if event_identity_issue is not None:
        raise RuntimeError(
            "reservation current-owner migration found empty/oversized accepted event identity: "
            f"event={event_identity_issue[0]} length={event_identity_issue[1]}"
        )

    event_collision = bind.execute(sa.text(f"""
        SELECT event_identity, count(*)
          FROM (
                SELECT {_EVENT_IDENTITY_SQL} AS event_identity
                  FROM reservation_event AS event
                  JOIN reservation_entry AS owner ON owner.id = event.reservation_id
                 WHERE owner.ledger_generation_id = :generation_id
               ) AS identities
         GROUP BY event_identity
        HAVING count(*) > 1
         LIMIT 1
    """), params).first()
    if event_collision is not None:
        raise RuntimeError(
            "reservation current-owner migration found accepted event identity collision: "
            f"identity={event_collision[0]!r} rows={event_collision[1]}"
        )

    event_key_conflict = bind.execute(sa.text(f"""
        SELECT idempotency_key, count(DISTINCT event_identity)
          FROM (
                SELECT event.idempotency_key, {_EVENT_IDENTITY_SQL} AS event_identity
                  FROM reservation_event AS event
                  JOIN reservation_entry AS owner ON owner.id = event.reservation_id
                 WHERE owner.ledger_generation_id = :generation_id
               ) AS identities
         GROUP BY idempotency_key
        HAVING count(DISTINCT event_identity) > 1
         LIMIT 1
    """), params).first()
    if event_key_conflict is not None:
        raise RuntimeError(
            "reservation current-owner migration found accepted idempotency collision: "
            f"key={event_key_conflict[0]!r} variants={event_key_conflict[1]}"
        )

    accepted_obligation_issue = bind.execute(sa.text(f"""
        SELECT entry.current_identity, count(event.id)
          FROM reservation_entry AS entry
          LEFT JOIN reservation_event AS event
            ON event.reservation_id = entry.id
           AND ({_EVENT_ORIGIN_SQL}) = 'obligation'
         WHERE entry.ledger_generation_id = :generation_id
         GROUP BY entry.id, entry.current_identity
        HAVING count(event.id) <> 1
         LIMIT 1
    """), params).first()
    if accepted_obligation_issue is not None:
        raise RuntimeError(
            "reservation current-owner migration requires exactly one accepted obligation basis: "
            f"{accepted_obligation_issue[0]!r} count={accepted_obligation_issue[1]}"
        )

    # Only the exact accepted-pointer event set is promoted.  No reservation
    # dependency table, archive row, historical event, or historical entry is
    # touched by this bootstrap revision.
    bind.execute(sa.text(f"""
        UPDATE reservation_event AS event
           SET event_identity = {_EVENT_IDENTITY_SQL},
               origin_kind = {_EVENT_ORIGIN_SQL},
               is_current = true
          FROM reservation_entry AS owner
         WHERE event.reservation_id = owner.id
           AND owner.ledger_generation_id = :generation_id
    """), params)

    accepted_basis_conflict = bind.execute(sa.text("""
        SELECT entry.current_identity, count(event.id)
          FROM reservation_entry AS entry
          LEFT JOIN reservation_event AS event
            ON event.reservation_id = entry.id
           AND event.is_current = true
           AND event.origin_kind = 'obligation'
         WHERE entry.ledger_generation_id = :generation_id
         GROUP BY entry.id, entry.current_identity
        HAVING count(event.id) <> 1
         LIMIT 1
    """), params).first()
    if accepted_basis_conflict is not None:
        raise RuntimeError(
            "reservation current-owner migration did not produce exactly one accepted obligation basis: "
            f"{accepted_basis_conflict[0]!r} count={accepted_basis_conflict[1]}"
        )

    bind.execute(sa.text("""
        INSERT INTO reservation_current_change (
            current_identity, reservation_id, source_generation_id,
            operation, origin_kind, after_payload
        )
        SELECT entry.current_identity, entry.id, entry.ledger_generation_id,
               'insert', 'obligation',
               json_build_object('reserved_qty', entry.reserved_qty,
                                 'covered_qty', entry.covered_from_stock_at_freeze_qty,
                                 'required_qty', entry.replenishment_required_qty,
                                 'lifecycle_status', entry.lifecycle_status)
          FROM reservation_entry AS entry
         WHERE entry.ledger_generation_id = :generation_id
           AND entry.is_current = true
    """), params)


def upgrade() -> None:
    op.add_column("reservation_entry", sa.Column("current_identity", sa.String(256), nullable=False, server_default=""))
    # Existing history is legacy metadata.  Switch the default to BUILDING
    # only after the exact accepted pointer has been bootstrapped.
    op.add_column("reservation_entry", sa.Column("owner_kind", sa.String(16), nullable=False, server_default="legacy"))
    op.add_column("reservation_entry", sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("reservation_event", sa.Column("event_identity", sa.String(320), nullable=False, server_default=""))
    op.add_column("reservation_event", sa.Column("origin_kind", sa.String(16), nullable=False, server_default="legacy"))
    op.add_column("reservation_event", sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_check_constraint(
        "ck_reservation_entry_owner_kind",
        "reservation_entry",
        "owner_kind IN ('building', 'current', 'legacy')",
    )
    op.create_check_constraint(
        "ck_reservation_event_origin_kind",
        "reservation_event",
        "origin_kind IN ('obligation', 'factual', 'correction', 'replay', 'legacy')",
    )
    op.create_table(
        "reservation_current_change",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("current_identity", sa.String(256), nullable=False),
        sa.Column("reservation_id", sa.BigInteger(), nullable=False),
        sa.Column("source_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("origin_kind", sa.String(16), nullable=False),
        sa.Column("before_payload", sa.JSON(), nullable=True),
        sa.Column("after_payload", sa.JSON(), nullable=True),
        sa.Column("source_event_identity", sa.String(320), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["reservation_id"], ["reservation_entry.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "operation IN ('insert', 'update', 'close', 'reopen')",
            name="ck_reservation_current_change_operation",
        ),
        sa.CheckConstraint(
            "origin_kind IN ('obligation', 'factual', 'correction')",
            name="ck_reservation_current_change_origin_kind",
        ),
    )
    op.create_index("ix_reservation_current_change_identity", "reservation_current_change", ["current_identity"])
    op.create_index("ix_reservation_current_change_reservation", "reservation_current_change", ["reservation_id"])
    op.create_table(
        "reservation_event_archive",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("source_reservation_id", sa.BigInteger(), nullable=False),
        sa.Column("business_identity", sa.String(256), nullable=False),
        sa.Column("event_identity", sa.String(320), nullable=False),
        sa.Column("origin_kind", sa.String(16), nullable=False, server_default="legacy"),
        sa.Column("occurrence_count", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("first_source_generation_id", sa.BigInteger(), nullable=True),
        sa.Column("last_source_generation_id", sa.BigInteger(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("business_identity", "event_identity", name="uq_reservation_event_archive_identity"),
    )
    op.create_index("ix_reservation_event_archive_identity", "reservation_event_archive", ["business_identity"])
    op.create_index("ix_reservation_event_archive_source_generation", "reservation_event_archive", ["source_generation_id"])

    bind = op.get_bind()
    # The disposable SQLite schema has no planning data or truth pointer; it
    # is a shape-compatibility run, not a production backfill. PostgreSQL (and
    # SQLite fixtures that explicitly provide a pointer) still execute the
    # fail-closed accepted-pointer validation above.
    if bind.dialect.name != "sqlite" or bind.execute(sa.text(
        "SELECT pts.current_generation_id "
        "FROM planning_truth_state AS pts "
        "JOIN ledger_generation AS lg ON lg.id = pts.current_generation_id "
        "WHERE pts.id = 1 AND lg.status = 'accepted'"
    )).scalar() is not None:
        _validate_and_backfill(bind)
    op.alter_column("reservation_entry", "owner_kind", server_default="building")
    op.alter_column("reservation_event", "origin_kind", server_default="obligation")
    op.create_index(
        "ux_reservation_entry_current_identity",
        "reservation_entry", ["current_identity"], unique=True,
        postgresql_where=sa.text("is_current = true AND current_identity <> ''"),
        sqlite_where=sa.text("is_current = 1 AND current_identity <> ''"),
    )
    op.create_index(
        "ux_reservation_event_current_identity",
        "reservation_event", ["event_identity"], unique=True,
        postgresql_where=sa.text("event_identity <> '' AND is_current = true"),
        sqlite_where=sa.text("event_identity <> '' AND is_current = 1"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    remaining = bind.execute(sa.text(
        "SELECT count(*) FROM reservation_current_change"
    )).scalar()
    if int(remaining or 0):
        raise RuntimeError("reservation current-owner downgrade is irreversible after publication")
    op.drop_constraint("ck_reservation_event_origin_kind", "reservation_event", type_="check")
    op.drop_constraint("ck_reservation_entry_owner_kind", "reservation_entry", type_="check")
    op.drop_index("ux_reservation_event_current_identity", table_name="reservation_event")
    op.drop_index("ux_reservation_entry_current_identity", table_name="reservation_entry")
    op.drop_index("ix_reservation_event_archive_source_generation", table_name="reservation_event_archive")
    op.drop_index("ix_reservation_event_archive_identity", table_name="reservation_event_archive")
    op.drop_table("reservation_event_archive")
    op.drop_index("ix_reservation_current_change_reservation", table_name="reservation_current_change")
    op.drop_index("ix_reservation_current_change_identity", table_name="reservation_current_change")
    op.drop_table("reservation_current_change")
    op.drop_column("reservation_event", "is_current")
    op.drop_column("reservation_event", "origin_kind")
    op.drop_column("reservation_event", "event_identity")
    op.drop_column("reservation_entry", "is_current")
    op.drop_column("reservation_entry", "owner_kind")
    op.drop_column("reservation_entry", "current_identity")
