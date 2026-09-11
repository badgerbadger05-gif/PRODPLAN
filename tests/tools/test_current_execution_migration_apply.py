"""R10 apply/postflight contracts for the legacy obligation migration.

These tests intentionally use a small local schema and a patched canonical
publisher.  They prove transaction/selection policy without invoking a real
destructive migration or a live worker.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

from tools.current_execution_migration import (
    EXPECTED_CURRENT_SCOPES,
    PreflightBlocked,
    PostflightBlocked,
    apply_current_obligation_migration,
    postflight_manifest,
)


def _engine():
    return create_engine("sqlite:///:memory:")


def _schema(engine):
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE planning_truth_state ("
            "id INTEGER PRIMARY KEY, current_generation_id INTEGER)"
        ))
        connection.execute(text(
            "CREATE TABLE ledger_generation ("
            "id INTEGER PRIMARY KEY, status TEXT NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE planning_read_snapshot ("
            "id INTEGER PRIMARY KEY, consumer TEXT NOT NULL, snapshot_key TEXT NOT NULL, "
            "ledger_generation_id INTEGER NOT NULL, truth_status TEXT NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE planning_run ("
            "run_id INTEGER PRIMARY KEY, source_plan_id INTEGER, "
            "ledger_generation_id INTEGER, status TEXT NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE current_execution_scope ("
            "id INTEGER PRIMARY KEY, entity_kind TEXT NOT NULL, scope_key TEXT NOT NULL, "
            "source_generation_id INTEGER, source_revision TEXT NOT NULL, "
            "result_ready BOOLEAN NOT NULL, content_hash TEXT NOT NULL, summary TEXT NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE current_execution_row ("
            "id INTEGER PRIMARY KEY, entity_kind TEXT NOT NULL, scope_key TEXT NOT NULL, "
            "business_identity TEXT NOT NULL, source_revision TEXT NOT NULL, "
            "source_generation_id INTEGER, result_status TEXT NOT NULL, result_ready BOOLEAN NOT NULL, "
            "content_hash TEXT NOT NULL, payload TEXT NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE current_execution_change ("
            "id INTEGER PRIMARY KEY, current_row_id INTEGER, entity_kind TEXT NOT NULL, "
            "business_identity TEXT NOT NULL, scope_key TEXT NOT NULL, source_revision TEXT NOT NULL, "
            "operation TEXT NOT NULL, reason TEXT NOT NULL, before_payload TEXT, after_payload TEXT)"
        ))
        connection.execute(text(
            "CREATE TABLE purchase_export_batch ("
            "id INTEGER PRIMARY KEY, ledger_generation_id INTEGER NOT NULL, "
            "planning_read_snapshot_id INTEGER, current_execution_scope_id INTEGER, "
            "current_execution_source_revision TEXT, idempotency_key TEXT NOT NULL)"
        ))


def _seed_truth(engine, *, generation_id=7, status="accepted", pointer=None):
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO ledger_generation (id, status) VALUES (:id, :status)"),
            {"id": generation_id, "status": status},
        )
        connection.execute(
            text("INSERT INTO planning_truth_state (id, current_generation_id) VALUES (1, :id)"),
            {"id": generation_id if pointer is None else pointer},
        )
        connection.execute(text(
            "INSERT INTO planning_run (run_id, source_plan_id, ledger_generation_id, status) "
            "VALUES (41, 7, :generation_id, 'FIXED_SNAPSHOT')"
        ), {"generation_id": generation_id})
        evidence = (
            (1, "production_control_journal", "journal:v1"),
            (2, "purchase_control_journal", "journal:v1"),
            (3, "mrp_result", "run:41"),
            (4, "period_plan_execution", "plan=7;run=41"),
        )
        for evidence_id, consumer, snapshot_key in evidence:
            connection.execute(text(
                "INSERT INTO planning_read_snapshot "
                "(id, consumer, snapshot_key, ledger_generation_id, truth_status) "
                "VALUES (:id, :consumer, :snapshot_key, :generation_id, 'accepted')"
            ), {
                "id": evidence_id, "consumer": consumer, "snapshot_key": snapshot_key,
                "generation_id": generation_id,
            })
        connection.execute(text(
            "INSERT INTO purchase_export_batch "
            "(id, ledger_generation_id, planning_read_snapshot_id, idempotency_key) "
            "VALUES (1, :generation_id, 2, 'r10-unit-export')"
        ), {"generation_id": generation_id})


def _publish_all(session, generation_id: int, *, duplicate=False):
    for index, (consumer, scope_key, entity_kind) in enumerate(EXPECTED_CURRENT_SCOPES, start=1):
        revision = f"accepted:g{generation_id}:{consumer}"
        session.execute(text(
            "INSERT INTO current_execution_scope "
            "(id, entity_kind, scope_key, source_generation_id, source_revision, result_ready, content_hash, summary) "
            "VALUES (:id, :kind, :scope, :generation, :revision, 1, 'scope-hash', '{}')"
        ), {
            "id": index, "kind": entity_kind, "scope": scope_key,
            "generation": generation_id, "revision": revision,
        })
        for row_offset, suffix in enumerate(
            ("a", "a") if duplicate and index == 1 else ("a",),
            start=1,
        ):
            session.execute(text(
                "INSERT INTO current_execution_row "
                "(id, entity_kind, scope_key, business_identity, source_revision, source_generation_id, "
                "result_status, result_ready, content_hash, payload) "
                "VALUES (:id, :kind, :scope, :identity, :revision, :generation, 'accepted', 1, 'row-hash', :payload)"
            ), {
                "id": index * 10 + row_offset,
                "kind": entity_kind, "scope": scope_key,
                "identity": f"{entity_kind}:identity:{suffix}",
                "revision": revision, "generation": generation_id,
                "payload": json.dumps({"value": suffix}),
            })
        session.execute(text(
            "INSERT INTO current_execution_change "
            "(id, current_row_id, entity_kind, business_identity, scope_key, source_revision, operation, reason) "
            "VALUES (:id, :row_id, :kind, :identity, :scope, :revision, 'insert', 'migration')"
        ), {
            "id": index, "row_id": index * 10 + 1, "kind": entity_kind,
            "identity": f"{entity_kind}:identity:a", "scope": scope_key, "revision": revision,
        })


def test_apply_requires_explicit_writers_stopped_before_any_dml():
    engine = _engine()
    _schema(engine)
    _seed_truth(engine)

    with pytest.raises(PreflightBlocked, match="writers-stopped"):
        apply_current_obligation_migration(engine, writers_stopped=False)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM current_execution_scope")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM current_execution_change")).scalar_one() == 0


def test_apply_uses_truth_pointer_and_rejects_pointer_to_nonaccepted_even_if_newer_is_accepted(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed_truth(engine, generation_id=9, status="accepted", pointer=8)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO ledger_generation (id, status) VALUES (8, 'building')"))

    called = []
    monkeypatch.setattr(
        "tools.current_execution_migration.publish_current_obligation_views_from_snapshots",
        lambda session, generation_id: called.append(generation_id),
    )

    with pytest.raises(PreflightBlocked, match="accepted"):
        apply_current_obligation_migration(engine, writers_stopped=True)
    assert called == []


def test_unknown_manifest_blocks_before_publisher(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed_truth(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE r2_tx_probe (id INTEGER PRIMARY KEY)"))

    called = []
    monkeypatch.setattr(
        "tools.current_execution_migration.publish_current_obligation_views_from_snapshots",
        lambda session, generation_id: called.append(generation_id),
    )

    with pytest.raises(PreflightBlocked, match="unknown"):
        apply_current_obligation_migration(engine, writers_stopped=True)
    assert called == []


def test_missing_obligation_source_evidence_blocks_before_publisher_and_dml(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed_truth(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "DELETE FROM planning_read_snapshot WHERE consumer = 'purchase_control_journal'"
        ))

    called = []
    monkeypatch.setattr(
        "tools.current_execution_migration.publish_current_obligation_views_from_snapshots",
        lambda session, generation_id: called.append(generation_id),
    )

    with pytest.raises(PreflightBlocked, match="purchase_control_journal"):
        apply_current_obligation_migration(engine, writers_stopped=True)
    assert called == []
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM current_execution_scope")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM current_execution_row")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM current_execution_change")).scalar_one() == 0


def test_near_match_snapshot_key_does_not_satisfy_exact_run_source(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed_truth(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE planning_read_snapshot SET snapshot_key = 'run:41:v1' "
            "WHERE consumer = 'mrp_result'"
        ))

    called = []
    monkeypatch.setattr(
        "tools.current_execution_migration.publish_current_obligation_views_from_snapshots",
        lambda session, generation_id: called.append(generation_id),
    )

    with pytest.raises(PreflightBlocked, match="mrp_result"):
        apply_current_obligation_migration(engine, writers_stopped=True)
    assert called == []


def test_partial_failure_rolls_back_scopes_rows_and_changes(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed_truth(engine)
    monkeypatch.setattr(
        "tools.current_execution_migration.publish_current_obligation_views_from_snapshots",
        _publish_all,
    )

    with pytest.raises(RuntimeError, match="fault injection"):
        apply_current_obligation_migration(
            engine, writers_stopped=True, fault_after_consumer="production_control_journal"
        )

    with engine.connect() as connection:
        for table in ("current_execution_scope", "current_execution_row", "current_execution_change"):
            assert connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0


def test_postflight_ambiguity_rolls_back_all_published_rows(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed_truth(engine)
    monkeypatch.setattr(
        "tools.current_execution_migration.publish_current_obligation_views_from_snapshots",
        lambda session, generation_id: _publish_all(session, generation_id, duplicate=True),
    )

    with pytest.raises(PostflightBlocked, match="duplicate"):
        apply_current_obligation_migration(engine, writers_stopped=True)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM current_execution_scope")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM current_execution_row")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM current_execution_change")).scalar_one() == 0


def test_second_apply_is_idempotent_and_adds_no_change_rows(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed_truth(engine)

    def idempotent_publisher(session, generation_id):
        if session.execute(text("SELECT count(*) FROM current_execution_scope")).scalar_one() == 0:
            _publish_all(session, generation_id)
        return {}

    monkeypatch.setattr(
        "tools.current_execution_migration.publish_current_obligation_views_from_snapshots",
        idempotent_publisher,
    )

    first = apply_current_obligation_migration(engine, writers_stopped=True)
    second = apply_current_obligation_migration(engine, writers_stopped=True)

    assert first["status"] == "ready"
    assert second["status"] == "ready"
    assert second["idempotent"] is True
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM current_execution_change")).scalar_one() == len(EXPECTED_CURRENT_SCOPES)


def test_postflight_reports_expected_scope_anchor_and_revision():
    engine = _engine()
    _schema(engine)
    _seed_truth(engine)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM purchase_export_batch"))
        # Build a complete accepted fixture through a direct transaction, as the
        # production publisher does; postflight itself must remain read-only.
        pass
    with engine.begin() as connection:
        connection.exec_driver_sql("BEGIN") if False else None

    from sqlalchemy.orm import Session
    with Session(engine) as session, session.begin():
        _publish_all(session, 7)

    report = postflight_manifest(engine, generation_id=7)
    assert report["status"] == "ready"
    assert report["generation_id"] == 7
    assert report["scopes"]["production_control_journal"]["source_revision"] == "accepted:g7:production_control_journal"


def test_apply_migrates_purchase_export_anchor_and_retry_is_noop(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed_truth(engine)
    def idempotent_publisher(session, generation_id):
        if session.execute(text("SELECT count(*) FROM current_execution_scope")).scalar_one() == 0:
            _publish_all(session, generation_id)
        return {}

    monkeypatch.setattr(
        "tools.current_execution_migration.publish_current_obligation_views_from_snapshots",
        idempotent_publisher,
    )

    first = apply_current_obligation_migration(engine, writers_stopped=True)
    with engine.connect() as connection:
        anchor = connection.execute(text(
            "SELECT planning_read_snapshot_id, current_execution_scope_id, current_execution_source_revision "
            "FROM purchase_export_batch WHERE id=1"
        )).one()
        change_count = connection.execute(text("SELECT count(*) FROM current_execution_change")).scalar_one()
    assert anchor[0] is None
    assert anchor[1] == 2
    assert anchor[2] == "accepted:g7:purchase_control_journal"
    assert first["postflight"]["purchase_export_anchors"] == {"legacy": 0, "current": 1}

    second = apply_current_obligation_migration(engine, writers_stopped=True)
    assert second["idempotent"] is True
    assert second["postflight"]["purchase_export_anchors"] == {"legacy": 0, "current": 1}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM current_execution_change")).scalar_one() == change_count


def test_wrong_purchase_export_snapshot_rolls_back_publication_and_anchor(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed_truth(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE purchase_export_batch SET planning_read_snapshot_id=1 WHERE id=1"
        ))
    monkeypatch.setattr(
        "tools.current_execution_migration.publish_current_obligation_views_from_snapshots",
        _publish_all,
    )

    with pytest.raises(PreflightBlocked, match="purchase export snapshot"):
        apply_current_obligation_migration(engine, writers_stopped=True)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM current_execution_scope")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM current_execution_row")).scalar_one() == 0
        assert connection.execute(text("SELECT planning_read_snapshot_id FROM purchase_export_batch WHERE id=1")).scalar_one() == 1
