"""Manifest-first accepted-generation GC and reclaim contracts."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from tools.accepted_generation_gc import (
    GcBlocked,
    RECLAIM_TABLES,
    _assert_backup_ready,
    _write_json_atomic,
    build_gc_manifest,
)


REPO = Path(__file__).resolve().parents[2]


def _base_engine():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE ledger_generation ("
            "id INTEGER PRIMARY KEY, status TEXT NOT NULL, accepted_at TEXT)"
        ))
        connection.execute(text(
            "CREATE TABLE planning_truth_state (id INTEGER PRIMARY KEY, current_generation_id INTEGER)"
        ))
        connection.execute(text("INSERT INTO planning_truth_state VALUES (1, 3)"))
        connection.execute(text(
            "INSERT INTO ledger_generation(id,status,accepted_at) VALUES "
            "(1,'accepted','2026-01-01'),(2,'accepted','2026-01-02'),"
            "(3,'accepted','2026-01-03'),(4,'building','2026-01-04')"
        ))
    return engine


def test_dry_run_preserves_current_building_and_explicit_retention_policy():
    engine = _base_engine()
    report = build_gc_manifest(engine, retain_accepted=1)
    assert report["status"] == "ready"
    assert report["current_generation_id"] == 3
    assert report["building_generation_ids"] == [4]
    assert report["policy_retained_accepted_generation_ids"] == [3]
    assert report["candidate_generation_ids"] == [1, 2]
    assert report["preserved_generation_ids"] == [3, 4]
    assert report["unknown_dependencies"] == []
    assert report["fingerprint"]


def test_unknown_inbound_dependency_blocks_manifest_without_cascade():
    engine = _base_engine()
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE mystery_dependency ("
            "id INTEGER PRIMARY KEY, generation_id INTEGER NOT NULL, "
            "FOREIGN KEY(generation_id) REFERENCES ledger_generation(id))"
        ))
        connection.execute(text("INSERT INTO mystery_dependency VALUES (1,1)"))
    report = build_gc_manifest(engine, retain_accepted=1)
    assert report["status"] == "ready"
    assert report["metadata_status"] == "blocked"
    assert report["unknown_dependencies"]
    assert report["unknown_dependencies"][0]["table"] == "mystery_dependency"
    assert report["deletable_generation_ids"] == [2]
    assert report["retained_metadata_generation_ids"] == [1]


def _manifest_with_unknown_rows(insert_order):
    engine = _base_engine()
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE mystery_dependency ("
            "id INTEGER PRIMARY KEY, generation_id INTEGER NOT NULL, "
            "FOREIGN KEY(generation_id) REFERENCES ledger_generation(id))"
        ))
        for row in insert_order:
            connection.execute(
                text("INSERT INTO mystery_dependency(id, generation_id) VALUES (:id, :generation_id)"),
                {"id": row[0], "generation_id": row[1]},
            )
    return build_gc_manifest(engine, retain_accepted=1)


def test_manifest_fingerprint_is_stable_when_dependency_rows_are_returned_in_different_order():
    first = _manifest_with_unknown_rows([(1, 1), (2, 2)])
    second = _manifest_with_unknown_rows([(2, 2), (1, 1)])

    assert first["fingerprint"] == second["fingerprint"]
    assert first["dependency_blockers"] == second["dependency_blockers"]
    assert first["unknown_dependencies"] == second["unknown_dependencies"]
    assert first["metadata_blockers_by_generation"] == second["metadata_blockers_by_generation"]


def test_gc_owned_fk_does_not_block_evidence_cleanup_or_metadata():
    engine = _base_engine()
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE assembly_queue_line ("
            "id INTEGER PRIMARY KEY, ledger_generation_id INTEGER NOT NULL, "
            "FOREIGN KEY(ledger_generation_id) REFERENCES ledger_generation(id))"
        ))
        connection.execute(text(
            "CREATE TABLE assembly_readiness ("
            "id INTEGER PRIMARY KEY, ledger_generation_id INTEGER NOT NULL, "
            "assembly_queue_line_id INTEGER NOT NULL, "
            "FOREIGN KEY(ledger_generation_id) REFERENCES ledger_generation(id), "
            "FOREIGN KEY(assembly_queue_line_id) REFERENCES assembly_queue_line(id))"
        ))
        connection.execute(text("INSERT INTO assembly_queue_line VALUES (1,1)"))
        connection.execute(text("INSERT INTO assembly_readiness VALUES (1,1,1)"))
    report = build_gc_manifest(engine, retain_accepted=1)
    assert report["status"] == "ready"
    assert report["metadata_status"] == "ready"
    assert report["deletable_generation_ids"] == [1, 2]
    assert report["evidence_delete_generation_ids"]["assembly_queue_line"] == [1, 2]


def test_archive_is_set_based_idempotent_and_does_not_touch_current_events():
    source = (REPO / "tools/accepted_generation_gc.py").read_text(encoding="utf-8")
    archive = source[source.index("def _archive_events"):source.index("def apply_gc_manifest")]
    assert "reservation_event_archive" in archive
    assert "ON CONFLICT (business_identity, event_identity) DO UPDATE" in archive
    assert "coalesce(event.is_current, false) = false" in archive
    assert "coalesce(entry.is_current, false) = false" in archive
    assert "md5(concat_ws" in archive
    assert "latest" not in archive.lower()


def test_backup_guard_is_postgres_session_only():
    class _Bind:
        class dialect:
            name = "sqlite"

    with pytest.raises(GcBlocked, match="PostgreSQL backup guard"):
        _assert_backup_ready(_Bind())


def test_reclaim_allowlist_is_explicit_and_separate_from_gc_apply():
    assert "reservation_event" in RECLAIM_TABLES
    assert "stock_bin" in RECLAIM_TABLES
    assert "ledger_build_batch" in RECLAIM_TABLES
    source = (REPO / "tools/accepted_generation_gc.py").read_text(encoding="utf-8")
    assert 'choices=("dry-run", "apply", "reclaim-plan", "reclaim")' in source
    assert "VACUUM (FULL, ANALYZE)" in source
    assert "pg_repack" in source
    assert "--manifest" in source
    assert "--plan" in source
    assert "--output" in source
    assert "--available-free-bytes" in source
    assert "disk_usage" not in source
    assert "required = max(" in source
    assert '"fingerprint"' in source


def test_manifest_output_is_atomic(tmp_path):
    path = tmp_path / "gc.json"
    _write_json_atomic(str(path), {"status": "ready", "candidate": [1]})
    assert '"status": "ready"' in path.read_text(encoding="utf-8")
    assert not (tmp_path / "gc.json.tmp").exists()


def test_pointer_without_its_own_provenance_blocks_evidence_deletion(monkeypatch):
    """GC may not remove the last copy of an accepted receipt's evidence.

    The pointer generation is preserved either way, but the evidence GC
    deletes from every *other* generation is the only copy of anything the
    pointer does not own.  The guard used to be advisory: it never reached
    ``status``, never removed the table from the deletable set, and swallowed
    every error.
    """
    import tools.accepted_generation_gc as gc

    blocker = {
        "table": "stock_ledger_supplier_receipt_provenance",
        "referred_table": "stock_ledger_supplier_receipt_provenance",
        "classification": "pointer-evidence-incomplete",
        "generation_id": 7,
        "reason": "pointer generation 7 does not own supplier receipt provenance",
        "sample_stock_ledger_entry_ids": [11, 12],
    }
    monkeypatch.setattr(
        gc, "_pointer_provenance_blockers", lambda engine, current_id: [blocker],
    )
    engine = _base_engine()

    manifest = gc.build_gc_manifest(engine, retain_accepted=0)

    assert manifest["status"] == "blocked"
    assert manifest["evidence_delete_generation_ids"].get(
        "stock_ledger_supplier_receipt_provenance", []
    ) == []
    assert any(
        row.get("classification") == "pointer-evidence-incomplete"
        for row in manifest["dependency_blockers"]
    )


def test_pointer_provenance_probe_does_not_swallow_errors(monkeypatch):
    """A probe that cannot run must say so, not return "nothing to block"."""
    import tools.accepted_generation_gc as gc

    engine = _base_engine()

    def _explode(session, **kwargs):
        raise RuntimeError("probe is broken")

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_supplier_evidence."
        "lost_supplier_receipt_provenance_sle_ids",
        _explode,
    )
    # The reduced fixture schema is detected before the probe runs, so no
    # exception escapes and no verdict is invented either.
    assert gc._pointer_provenance_blockers(engine, 7) == []
