from pathlib import Path
import re
import sqlite3
import subprocess
import sys

from app import models


REPO = Path(__file__).resolve().parents[1]
CLEAR_SQL = REPO / "tools" / "sql" / "clear_rebuildable_ledger_projections.sql"
VERIFY_SQL = REPO / "tools" / "sql" / "verify_ledger_rebuild.sql"
ALEMBIC_SQLITE_UPGRADE = REPO / "tests" / "alembic_sqlite_upgrade.py"


def _truncate_targets(source: str) -> set[str]:
    match = re.search(
        r"\bTRUNCATE\s+(.*?)\s+RESTART\s+IDENTITY\s*;",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert match is not None
    return {
        token.strip()
        for token in match.group(1).split(",")
        if token.strip()
    }


def _detached_fk_specs(source: str) -> set[tuple[str, str, str]]:
    block = re.search(
        r"DO\s+\$detach_rebuildable_fks\$.*?\bVALUES\b(.*?)"
        r"\)\s+AS\s+expected_fk\(table_name,\s*column_name,\s*target_table\)",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert block is not None
    return {
        (table_name, column_name, target_table)
        for table_name, column_name, target_table in re.findall(
            r"\('([^']+)',\s*'([^']+)',\s*'([^']+)'\)",
            block.group(1),
        )
    }


def _restored_fk_specs(source: str) -> set[tuple[str, str, str]]:
    return {
        (table_name, column_name, target_table)
        for table_name, _constraint, column_name, target_table in re.findall(
            r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+CONSTRAINT\s+(\w+)\s+"
            r"FOREIGN\s+KEY\s*\(\s*(\w+)\s*\)\s+"
            r"REFERENCES\s+(\w+)\s*\(",
            source,
            flags=re.IGNORECASE | re.DOTALL,
        )
    }


def _migrated_fk_specs(db_path: Path) -> set[tuple[str, str, str]]:
    connection = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        specs: set[tuple[str, str, str]] = set()
        for table_name in tables:
            quoted_table = table_name.replace('"', '""')
            for row in connection.execute(
                f'PRAGMA foreign_key_list("{quoted_table}")'
            ):
                specs.add((table_name, row[3], row[2]))
        return specs
    finally:
        connection.close()


def _count_invariant_query(source: str, alias: str) -> str:
    end_match = re.search(
        rf"\)\s+AS\s+{re.escape(alias)}\s*;",
        source,
        flags=re.IGNORECASE,
    )
    assert end_match is not None
    start = source.rfind("SELECT count(*)", 0, end_match.start())
    assert start >= 0
    match = re.search(
        rf"SELECT\s+count\(\*\)\s+INTO\s+v_count\s+FROM\s+\((.*?)\)"
        rf"\s+AS\s+{re.escape(alias)}\s*;",
        source[start : end_match.end()],
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert match is not None
    return (
        f"SELECT count(*) FROM ({match.group(1)}) AS {alias}"
        .replace("v_generation_id", ":generation_id")
    )


def test_clear_script_is_guarded_and_preserves_non_rebuildable_inputs():
    source = CLEAR_SQL.read_text(encoding="utf-8")
    targets = _truncate_targets(source)

    assert "\\set ON_ERROR_STOP on" in source
    assert "CLEAR_REBUILDABLE_LEDGER_PROJECTIONS" in source
    assert "current_database()" in source
    assert "expected_generation_key" in source
    assert "other_client_sessions" in source
    assert "No CASCADE" in source
    assert "CASCADE" not in re.search(
        r"\bTRUNCATE\s+(.*?)\s+RESTART\s+IDENTITY\s*;",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    ).group(0)

    assert {
        "ledger_generation",
        "stock_ledger_entry",
        "reservation_entry",
        "reservation_event",
        "mrp_requirement",
        "planning_run",
        "planning_read_snapshot",
        "assembly_queue_line",
        "drum_schedule",
        "shelf_projection",
    } <= targets

    assert {
        "items",
        "specifications",
        "production_plan_header",
        "production_plan_line",
        "sync_link",
        "shelf_policy",
        "dbr_assembly_rate",
        "forced_order_request",
    }.isdisjoint(targets)


def test_clear_script_detaches_and_restores_every_current_keep_to_clear_fk():
    source = CLEAR_SQL.read_text(encoding="utf-8")
    targets = _truncate_targets(source)
    metadata_specs: set[tuple[str, str, str]] = set()
    for table in models.Base.metadata.tables.values():
        if table.name in targets:
            continue
        for constraint in table.foreign_key_constraints:
            assert len(constraint.elements) == 1
            element = constraint.elements[0]
            target_table = element.target_fullname.split(".", 1)[0]
            if target_table in targets:
                metadata_specs.add(
                    (table.name, element.parent.name, target_table)
                )

    assert _detached_fk_specs(source) == metadata_specs
    assert _restored_fk_specs(source) == metadata_specs
    assert "constraint_row.conrelid = expected.table_name::regclass" in source
    assert "constraint_row.confrelid = expected.target_table::regclass" in source
    assert "local_column.attname = expected.column_name" in source
    assert "ALTER TABLE %I DROP CONSTRAINT %I" in source


def test_truncate_set_is_closed_over_migrated_schema_foreign_keys(tmp_path):
    source = CLEAR_SQL.read_text(encoding="utf-8")
    targets = _truncate_targets(source)
    db_path = tmp_path / "migrated-schema.sqlite"
    result = subprocess.run(
        [sys.executable, str(ALEMBIC_SQLITE_UPGRADE), str(db_path)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    migrated_specs = _migrated_fk_specs(db_path)
    assert {
        ("planning_run_bucket_modes", "run_id", "planning_run"),
        ("mrp_bucket_type_legacy", "run_id", "planning_run"),
    } <= migrated_specs
    escaping_specs = {
        spec
        for spec in migrated_specs
        if spec[2] in targets and spec[0] not in targets
    }
    assert escaping_specs <= _detached_fk_specs(source)
    assert {
        "planning_run_bucket_modes",
        "mrp_bucket_type_legacy",
    } <= targets


def test_clear_script_clears_current_material_coverage_projection_payload():
    source = CLEAR_SQL.read_text(encoding="utf-8")
    column = models.ProductionOrderLineState.__table__.c[
        "material_coverage_ledger_generation_id"
    ]
    foreign_key = next(iter(column.foreign_keys))
    assert foreign_key.target_fullname == "ledger_generation.id"
    assert foreign_key.ondelete == "RESTRICT"

    update = re.search(
        r"UPDATE\s+production_order_line_states\s+SET(.*?)\s+WHERE",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert update is not None
    for field in (
        "material_coverage_ledger_generation_id",
        "material_coverage_status",
        "material_coverage_label",
        "material_coverage_calculated_at",
        "material_coverage_snapshot",
    ):
        assert re.search(rf"\b{field}\s*=\s*NULL\b", update.group(1))

    assert (
        "'production_order_line_states', "
        "(SELECT count(*) FROM production_order_line_states)"
    ) in source
    assert (
        "WHEN 'production_order_line_states' "
        "THEN (SELECT count(*) FROM production_order_line_states)"
    ) in source


def test_verifier_is_read_only_fail_closed_and_covers_publication_invariants():
    source = VERIFY_SQL.read_text(encoding="utf-8")

    assert "\\set ON_ERROR_STOP on" in source
    assert "BEGIN TRANSACTION READ ONLY;" in source
    assert source.rstrip().endswith("ROLLBACK;")
    assert "expected_database" in source
    assert "expected_generation_key" in source
    assert "expected_cutoff" in source
    assert "expected_fixed_run_count" in source
    assert "expected_opening_baseline_at" in source

    for evidence in (
        "planning_truth_state",
        "physical_import_batch",
        "mrp_freeze_baseline",
        "mrp_requirement",
        "mrp_requirement_bucket",
        "reservation_event",
        "stock_ledger_entry",
        "stock_ledger_fact_supersession",
        "purchase_control_journal",
        "assembly_queue_line",
        "drum_schedule",
        "drum_slot",
        "drum_capacity_gap",
        "production_control_journal",
        "period_plan_execution",
        "mrp_result",
        "assembly_queue",
    ):
        assert evidence in source


def test_verifier_accepts_fifo_split_but_rejects_overallocation_and_duplicate_pair():
    source = VERIFY_SQL.read_text(encoding="utf-8")
    conservation = _count_invariant_query(source, "overallocated_sle")
    uniqueness = _count_invariant_query(source, "duplicated_allocation")
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            """
            CREATE TABLE stock_ledger_entry (
                id INTEGER PRIMARY KEY,
                qty NUMERIC NOT NULL
            );
            CREATE TABLE reservation_event (
                ledger_generation_id INTEGER NOT NULL,
                sle_id INTEGER,
                reservation_id INTEGER NOT NULL,
                realized_delta NUMERIC NOT NULL
            );
            INSERT INTO stock_ledger_entry(id, qty) VALUES
                (1, 10), (2, 5), (3, 10);
            -- Legitimate FIFO split: one physical SLE, two reservations.
            INSERT INTO reservation_event VALUES
                (7, 1, 101, 4),
                (7, 1, 102, 6);
            """
        )
        assert connection.execute(
            conservation,
            {"generation_id": 7},
        ).fetchone()[0] == 0
        assert connection.execute(
            uniqueness,
            {"generation_id": 7},
        ).fetchone()[0] == 0

        connection.executescript(
            """
            -- Same-sign sum 6 exceeds physical quantity 5.
            INSERT INTO reservation_event VALUES
                (7, 2, 201, 3),
                (7, 2, 202, 3);
            -- Same SLE/reservation allocation repeated, although total is safe.
            INSERT INTO reservation_event VALUES
                (7, 3, 301, 2),
                (7, 3, 301, 2);
            """
        )
        assert connection.execute(
            conservation,
            {"generation_id": 7},
        ).fetchone()[0] == 1
        assert connection.execute(
            uniqueness,
            {"generation_id": 7},
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_verifier_requires_exactly_all_five_planning_snapshot_consumers():
    source = VERIFY_SQL.read_text(encoding="utf-8")
    block = re.search(
        r"VALUES\s+(.*?)\)\s+AS\s+required_consumer\(consumer\)",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert block is not None
    consumers = set(re.findall(r"\('([^']+)'\)", block.group(1)))
    assert consumers == {
        "mrp_result",
        "period_plan_execution",
        "assembly_queue",
        "purchase_control_journal",
        "production_control_journal",
    }
    assert "snapshot.snapshot_key = 'run:' || run.run_id::text" in source
    assert "'plan=' || run.source_plan_id::text" in source
    assert "snapshot_key = 'current:v1'" in source
    assert "snapshot.consumer = 'production_control_journal'" in source
    assert "row.row_kind = 'production_order'" in source
