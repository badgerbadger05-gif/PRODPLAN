from pathlib import Path
import re


REPO = Path(__file__).resolve().parents[1]
CLEAR_SQL = REPO / "tools" / "sql" / "clear_rebuildable_ledger_projections.sql"
VERIFY_SQL = REPO / "tools" / "sql" / "verify_ledger_rebuild.sql"


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
    ):
        assert evidence in source
