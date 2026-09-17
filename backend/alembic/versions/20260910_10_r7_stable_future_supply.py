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


def _identity_sql(alias: str = "lfs") -> str:
    """The SQL form of :func:`_identity`, preserving empty exact segments."""
    return (
        f"CASE WHEN {alias}.evidence_status = 'exact' THEN "
        f"concat(btrim(coalesce({alias}.supply_kind, '')), ':', "
        f"btrim(coalesce({alias}.source_ref, '')), ':', "
        f"btrim(coalesce({alias}.source_line_ref, '')), ':', "
        f"btrim(coalesce({alias}.source_local_id, ''))) "
        f"ELSE concat(btrim(coalesce({alias}.supply_kind, '')), ':rejected:', "
        f"btrim(coalesce({alias}.source_content_hash, ''))) END"
    )


def _raise_identity_issue(issue) -> None:
    if issue is None:
        return
    mapping = getattr(issue, "_mapping", None)
    code = mapping["error_code"] if mapping is not None else issue[0]
    row_id = mapping["row_id"] if mapping is not None else issue[1]
    identity = mapping["business_identity"] if mapping is not None else issue[2]
    if code == "collision":
        raise RuntimeError(
            "R7 future-supply active identity collision: "
            f"{identity} (row {row_id})"
        )
    if code == "too_long":
        raise RuntimeError(
            "R7 future-supply identity exceeds 256 characters: "
            f"{identity} (row {row_id})"
        )
    raise RuntimeError(
        "R7 future-supply row has no stable identity: "
        f"{identity!r} (row {row_id})"
    )


def _backfill_postgresql(bind, pointer_id) -> None:
    """Backfill only accepted-pointer rows and bounded BUILDING staging."""
    identity = _identity_sql("lfs")
    issue = bind.execute(
        sa.text(
            f"""
            WITH identities AS (
                SELECT lfs.id AS row_id, lfs.ledger_generation_id,
                       {identity} AS business_identity
                  FROM ledger_future_supply lfs
                  JOIN ledger_generation lg ON lg.id = lfs.ledger_generation_id
                  JOIN planning_truth_state pts ON pts.id = 1
                 WHERE lg.status = 'building'
                    OR (pts.current_generation_id IS NOT NULL
                        AND lg.id = pts.current_generation_id
                        AND lg.status = 'accepted')
            ), invalid AS (
                SELECT 'empty' AS error_code, row_id, business_identity
                  FROM identities
                 WHERE business_identity = ''
                UNION ALL
                SELECT 'too_long' AS error_code, row_id, business_identity
                  FROM identities
                 WHERE char_length(business_identity) > 256
                UNION ALL
                SELECT 'collision' AS error_code,
                       min(row_id) AS row_id, business_identity
                  FROM identities
                 WHERE :pointer_id IS NOT NULL
                   AND ledger_generation_id = :pointer_id
                 GROUP BY business_identity
                HAVING count(*) > 1
            )
            SELECT error_code, row_id, business_identity
              FROM invalid
             ORDER BY error_code, row_id
             LIMIT 1
            """
        ),
        {"pointer_id": pointer_id},
    ).first()
    _raise_identity_issue(issue)

    bind.execute(
        sa.text(
            f"""
            WITH identities AS (
                SELECT lfs.id AS row_id, lfs.ledger_generation_id,
                       {identity} AS business_identity
                  FROM ledger_future_supply lfs
                  JOIN ledger_generation lg ON lg.id = lfs.ledger_generation_id
                  JOIN planning_truth_state pts ON pts.id = 1
                 WHERE lg.status = 'building'
                    OR (pts.current_generation_id IS NOT NULL
                        AND lg.id = pts.current_generation_id
                        AND lg.status = 'accepted')
            )
            UPDATE ledger_future_supply AS lfs
               SET current_identity = identities.business_identity,
                   is_current = CASE
                       WHEN :pointer_id IS NULL THEN false
                       ELSE identities.ledger_generation_id = :pointer_id
                   END
              FROM identities
             WHERE lfs.id = identities.row_id
            """
        ),
        {"pointer_id": pointer_id},
    )


def _backfill_compat(bind, pointer_id) -> None:
    """SQLite fallback limited to accepted-pointer/building rows."""
    rows = bind.execute(sa.text(
        """SELECT id, ledger_generation_id, supply_kind, source_ref,
                         source_line_ref, source_local_id, source_content_hash,
                         evidence_status
                    FROM ledger_future_supply
                   WHERE ledger_generation_id = :pointer_id
                      OR ledger_generation_id IN (
                           SELECT id FROM ledger_generation WHERE status = 'building'
                       )
                   ORDER BY id"""
    ), {"pointer_id": pointer_id}).mappings().all()
    current_identities = set()
    for row in rows:
        identity = _identity(row)
        if not identity or len(identity) > 256:
            raise RuntimeError(
                f"R7 future-supply row {row['id']} has invalid stable identity"
            )
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
            {"identity": identity, "is_current": is_current, "id": row["id"]},
        )


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
    if bind.dialect.name == "postgresql":
        _backfill_postgresql(bind, pointer_id)
    else:
        _backfill_compat(bind, pointer_id)
    # Historical accepted rows are pruned by 20260910_11 after their accepted
    # pointer source is published.  Keep these migration columns nullable for
    # that bounded cutover; 20260910_11 tightens the remaining BUILDING
    # staging shape after fail-closed validation.
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
