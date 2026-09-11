"""Drop the retired generic planning-read storage after a current-only proof."""

from alembic import op
import sqlalchemy as sa


revision = "20260911_03"
down_revision = "20260911_02"
branch_labels = None
depends_on = None

_LEGACY_TABLES = (
    "planning_read_root_member",
    "planning_read_row",
    "planning_read_snapshot",
)
_REQUIRED_SCOPES = {
    "production_control_journal": "production:all-live-orders",
    "purchase_control_journal": "purchase:all-live-plans",
    "mrp_result": "mrp:all-live-plans",
    "period_plan_execution": "period-plan:all-live-plans",
}


def _present_legacy_tables(connection) -> set[str]:
    inspector = sa.inspect(connection)
    return {
        table
        for table in _LEGACY_TABLES
        if table in set(inspector.get_table_names())
    }


def _reject_unknown_inbound_foreign_keys(connection, present: set[str]) -> None:
    inspector = sa.inspect(connection)
    for table in inspector.get_table_names():
        if table in present:
            continue
        for foreign_key in inspector.get_foreign_keys(table):
            if foreign_key.get("referred_table") in present:
                raise RuntimeError(
                    "unknown external foreign key references legacy planning-read storage: "
                    f"{table}.{foreign_key.get('name') or '<unnamed>'}"
                )


def _lock_legacy_tables(connection, present: set[str]) -> None:
    if connection.dialect.name == "postgresql":
        for table in _LEGACY_TABLES:
            if table in present:
                connection.execute(
                    sa.text(
                        f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE NOWAIT"
                    )
                )


def _has_rows(connection, table: str) -> bool:
    return bool(
        connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first()
    )


def _validate_current_truth(connection) -> None:
    required_tables = {
        "ledger_generation",
        "planning_truth_state",
        "current_execution_scope",
    }
    available = set(sa.inspect(connection).get_table_names())
    if required_tables - available:
        raise RuntimeError(
            "legacy planning-read data requires accepted current truth and scopes"
        )

    pointer = connection.execute(
        sa.text(
            "SELECT current_generation_id FROM planning_truth_state "
            "WHERE id = 1"
        )
    ).scalar_one_or_none()
    if pointer is None:
        raise RuntimeError("legacy planning-read data lacks the current truth pointer")
    generation = connection.execute(
        sa.text(
            "SELECT status FROM ledger_generation WHERE id = :generation_id"
        ),
        {"generation_id": pointer},
    ).scalar_one_or_none()
    if generation != "accepted":
        raise RuntimeError("legacy planning-read data requires an accepted truth generation")

    rows = connection.execute(
        sa.text(
            "SELECT entity_kind, scope_key, source_generation_id, source_revision, result_ready "
            "FROM current_execution_scope"
        )
    ).mappings().all()
    expected = {
        (kind, scope): (f"accepted:g{int(pointer)}:{kind}")
        for kind, scope in _REQUIRED_SCOPES.items()
    }
    found: set[tuple[str, str]] = set()
    for row in rows:
        key = (str(row["entity_kind"]), str(row["scope_key"]))
        if key not in expected:
            continue
        if (
            int(row["source_generation_id"] or 0) != int(pointer)
            or str(row["source_revision"] or "") != expected[key]
            or not bool(row["result_ready"])
        ):
            raise RuntimeError(
                "legacy planning-read data has an incomplete current obligation scope"
            )
        found.add(key)
    if found != set(expected):
        raise RuntimeError(
            "legacy planning-read data requires the exact four ready current obligation scopes"
        )


def upgrade() -> None:
    connection = op.get_bind()
    present = _present_legacy_tables(connection)
    if not present:
        return
    if present != set(_LEGACY_TABLES):
        raise RuntimeError(
            "legacy planning-read storage is partial; refusing an unsafe drop"
        )

    _lock_legacy_tables(connection, present)
    _reject_unknown_inbound_foreign_keys(connection, present)

    available = set(sa.inspect(connection).get_table_names())
    if "ledger_generation" in available:
        building = connection.execute(
            sa.text(
                "SELECT 1 FROM ledger_generation "
                "WHERE lower(COALESCE(status, '')) = 'building' LIMIT 1"
            )
        ).first()
        if building is not None:
            raise RuntimeError(
                "BUILDING LedgerGeneration blocks legacy planning-read table drop"
            )

    if any(_has_rows(connection, table) for table in _LEGACY_TABLES):
        _validate_current_truth(connection)

    for table in _LEGACY_TABLES:
        connection.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))


def downgrade() -> None:
    raise RuntimeError(
        "20260911_03 planning-read table drop is irreversible; restore from an explicit backup"
    )
