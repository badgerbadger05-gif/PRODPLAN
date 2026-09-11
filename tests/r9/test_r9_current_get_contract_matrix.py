"""Behavioral R9 inventory for current readers: read-only and fail-closed."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import sqlalchemy as sa
import pytest
from fastapi import HTTPException

from app import models
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    load_current_execution_coherent,
    publish_current_execution_scope,
)


def _create_legacy_snapshot_table(db_session):
    """Create an explicit poison table; runtime metadata no longer owns it."""
    db_session.execute(sa.text(
        "CREATE TABLE IF NOT EXISTS planning_read_snapshot ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, consumer VARCHAR(128) NOT NULL, "
        "snapshot_key VARCHAR(256) NOT NULL, ledger_generation_id INTEGER NOT NULL, "
        "cutoff DATETIME NOT NULL, truth_status VARCHAR(16) NOT NULL, "
        "payload JSON NOT NULL, published_at DATETIME NOT NULL)"
    ))


def _legacy_generation(db_session):
    batch = models.PhysicalImportBatch(
        batch_key="r9-get-matrix-legacy-batch", status="completed",
        cutoff=datetime(2026, 9, 11, tzinfo=timezone.utc), source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="r9-get-matrix-legacy-generation", status="accepted",
        cutoff=batch.cutoff, source_watermarks={}, capabilities={},
        physical_import_batch=batch, algorithm_version="r9-test",
        replay_version="r9-test",
    )
    db_session.add(generation)
    db_session.flush()
    _create_legacy_snapshot_table(db_session)
    db_session.execute(
        sa.text(
            "INSERT INTO planning_read_snapshot "
            "(consumer, snapshot_key, ledger_generation_id, cutoff, truth_status, payload, published_at) "
            "VALUES (:consumer, :snapshot_key, :generation_id, :cutoff, :truth_status, :payload, :published_at)"
        ),
        {
            "consumer": "mrp_result",
            "snapshot_key": "r9-get-matrix-legacy",
            "generation_id": generation.id,
            "cutoff": generation.cutoff,
            "truth_status": "accepted",
            "payload": json.dumps({"rows": []}),
            "published_at": generation.cutoff,
        },
    )
    db_session.commit()
    return generation


# This is the runtime GET inventory for the five R9 contours.  The shared
# coherent loader is the persisted read boundary used by their route/service
# adapters; keeping the table explicit prevents a new contour from silently
# falling back to snapshots or a builder.
CURRENT_GET_SCOPES = (
    ("production_control", "production_control_journal", "production:all-live-orders"),
    ("purchase_control", "purchase_control_journal", "purchase:all-live-plans"),
    ("period_plan_execution", "period_plan_execution", "period-plan:all-live-plans"),
    ("mrp_result", "mrp_result", "mrp:all-live-plans"),
    ("assembly_queue", "assembly_queue", "assembly:all-live-plans"),
    ("assembly_readiness", "assembly_readiness", "assembly:all-live-plans"),
    ("drum", "drum_schedule", "drum:all-live-plans"),
    ("shelf", "shelf_projection", "shelf:all-live-mrps"),
)


def _row(kind: str, scope: str) -> dict:
    return {
        "entity_kind": kind,
        "business_identity": f"r9-read:{kind}",
        "scope_key": scope,
        "payload": {"current_identity": f"r9-read:{kind}", "ready": True},
    }


def test_current_get_inventory_is_read_only_and_does_not_replay(db_session):
    """Every current contour reads compact rows without DML or reconstruction."""
    for _name, kind, scope in CURRENT_GET_SCOPES:
        publish_current_execution_scope(
            db_session,
            source_revision="r9-get-matrix",
            scope_key=scope,
            rows=[_row(kind, scope)],
            entity_kinds=(kind,),
        )
    db_session.commit()

    writes: list[str] = []
    bind = db_session.get_bind()

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    sa.event.listen(bind, "before_cursor_execute", observe)
    try:
        for _name, kind, scope in CURRENT_GET_SCOPES:
            manifest, rows = load_current_execution_coherent(
                db_session, entity_kind=kind, scope_key=scope,
            )
            assert manifest.result_ready is True
            assert len(rows) == 1
    finally:
        sa.event.remove(bind, "before_cursor_execute", observe)
    assert writes == []


def test_missing_current_does_not_fallback_to_legacy_snapshot_or_heavy_replay(db_session, monkeypatch):
    """A legacy snapshot never makes an absent current GET available."""
    batch = models.PhysicalImportBatch(
        batch_key="r9-get-legacy-batch", status="completed", source_watermarks={}
    )
    generation = models.LedgerGeneration(
        generation_key="r9-get-legacy-generation", status="accepted",
        source_watermarks={}, capabilities={}, physical_import_batch=batch,
        algorithm_version="r9-test", replay_version="r9-test",
        cutoff=datetime(2026, 9, 11, tzinfo=timezone.utc),
    )
    db_session.add(generation)
    db_session.flush()
    _create_legacy_snapshot_table(db_session)
    db_session.execute(
        sa.text(
            "INSERT INTO planning_read_snapshot "
            "(consumer, snapshot_key, ledger_generation_id, cutoff, truth_status, payload, published_at) "
            "VALUES (:consumer, :snapshot_key, :generation_id, :cutoff, :truth_status, :payload, :published_at)"
        ),
        {
            "consumer": "mrp_result",
            "snapshot_key": "r9-legacy-only",
            "generation_id": generation.id,
            "cutoff": generation.cutoff,
            "truth_status": "accepted",
            "payload": json.dumps({"rows": [{"row_key": "legacy", "qty": 99}]}),
            "published_at": generation.cutoff,
        },
    )
    db_session.commit()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy replay/builder was called by a current GET")

    # These are the old heavy/snapshot boundaries.  The current loader must
    # fail before either can be consulted.
    monkeypatch.setattr("app.services.mrp_result_projection._read_snapshot_rows", forbidden, raising=False)
    monkeypatch.setattr("app.services.mrp_result_projection.preview_make_work_item_materials", forbidden, raising=False)
    with pytest.raises(CurrentExecutionUnavailable):
        load_current_execution_coherent(
            db_session, entity_kind="mrp_result", scope_key="mrp:all-live-plans",
        )


def test_route_sheet_get_mark_printed_is_still_read_only(monkeypatch):
    from app.routers import production_control

    monkeypatch.setattr(
        production_control,
        "_current_route_sheet_payloads",
        lambda *args, **kwargs: [{"product_id": 77, "route": []}],
    )
    monkeypatch.setattr(
        production_control,
        "render_route_sheets_from_snapshots",
        lambda payloads, **kwargs: "<html>current</html>",
    )
    writes: list[object] = []
    monkeypatch.setattr(
        production_control,
        "mark_route_sheets_printed_by_members",
        lambda *args, **kwargs: writes.append(True),
    )
    response = production_control.print_route_sheets(
        product_ids="77",
        current_identities="production:77",
        expected_source_revision="r9-get-matrix",
        mark_printed=True,
        db=object(),
    )
    assert response.body == b"<html>current</html>"
    assert writes == []


def test_mrp_route_get_export_matrix_fails_closed_before_snapshot_helpers(
    db_session, monkeypatch,
):
    """All MRP GET/group/export adapters reject missing current before legacy IO."""
    generation = _legacy_generation(db_session)
    from pathlib import Path

    projection_source = (
        Path(__file__).parents[2]
        / "backend"
        / "app"
        / "services"
        / "mrp_result_projection.py"
    ).read_text(encoding="utf-8")
    assert "_read_mrp_snapshot_rows" not in projection_source
    assert "_resolve_snapshot" not in projection_source
    from app.routers.plan import (
        export_planning_result_production,
        export_planning_result_purchases,
        export_planning_result_rework,
        get_planning_result_production,
        get_planning_result_production_grouped,
        get_planning_result_purchases,
        get_planning_result_purchases_grouped,
        get_planning_result_purchases_grouped_by_category,
        get_planning_result_rework,
        get_planning_result_rework_grouped,
        get_planning_result_rework_grouped_by_category,
        get_planning_result_capacity,
    )
    endpoints = [
        (get_planning_result_production, {"item_id": None, "root_item_id": None, "bucket_type": None, "date_from": None, "date_to": None, "limit": 100, "offset": 0, "sort_by": None, "sort_dir": None, "current_scope_id": None}),
        (get_planning_result_production_grouped, {"item_id": None, "date_from": None, "date_to": None, "limit": 100, "offset": 0, "sort_by": None, "sort_dir": None}),
        (get_planning_result_purchases, {"item_id": None, "root_item_id": None, "bucket_type": None, "supplier_ref1c": None, "category_id": None, "category_ref1c": None, "date_from": None, "date_to": None, "limit": 100, "offset": 0, "sort_by": None, "sort_dir": None, "current_scope_id": None}),
        (get_planning_result_purchases_grouped, {"date_from": None, "date_to": None, "limit": 100, "offset": 0}),
        (get_planning_result_rework, {"item_id": None, "root_item_id": None, "bucket_type": None, "date_from": None, "date_to": None, "limit": 100, "offset": 0, "sort_by": None, "sort_dir": None, "current_scope_id": None}),
        (get_planning_result_rework_grouped, {"item_id": None, "date_from": None, "date_to": None, "limit": 100, "offset": 0, "sort_by": None, "sort_dir": None}),
        (get_planning_result_purchases_grouped_by_category, {"item_id": None, "date_from": None, "date_to": None, "limit": 100, "offset": 0, "sort_by": None, "sort_dir": None}),
        (get_planning_result_rework_grouped_by_category, {"item_id": None, "date_from": None, "date_to": None, "limit": 100, "offset": 0, "sort_by": None, "sort_dir": None}),
        (get_planning_result_capacity, {"area_id": None, "bucket_type": None, "date_from": None, "date_to": None, "limit": 200, "offset": 0, "current_scope_id": None}),
        (export_planning_result_production, {"format": "csv", "root_item_id": None, "bucket_type": None, "date_from": None, "date_to": None, "sort_by": None, "sort_dir": None, "current_scope_id": None}),
        (export_planning_result_purchases, {"format": "csv", "root_item_id": None, "bucket_type": None, "supplier_ref1c": None, "category_id": None, "category_ref1c": None, "date_from": None, "date_to": None, "sort_by": None, "sort_dir": None, "current_scope_id": None}),
        (export_planning_result_rework, {"format": "csv", "root_item_id": None, "bucket_type": None, "date_from": None, "date_to": None, "sort_by": None, "sort_dir": None, "current_scope_id": None}),
    ]
    writes: list[str] = []
    bind = db_session.get_bind()

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    sa.event.listen(bind, "before_cursor_execute", observe)
    try:
        for endpoint, kwargs in endpoints:
            with pytest.raises(Exception) as caught:
                asyncio.run(endpoint(run_id=41, db=db_session, **kwargs))
            assert getattr(caught.value, "status_code", None) == 503, endpoint.__name__
    finally:
        sa.event.remove(bind, "before_cursor_execute", observe)
    assert writes == []


def test_representative_current_routes_fail_closed_without_legacy_reads_or_dml(
    db_session, monkeypatch,
):
    """Production, purchase, period and queue adapters share the same gate."""
    generation = _legacy_generation(db_session)
    generation.capabilities = {
        "physical_ledger": True, "reservation_replay": True,
        "assembly_queue": True, "assembly_readiness": True,
    }
    generation.accepted_at = generation.cutoff
    db_session.commit()

    import app.routers.plan as plan_router
    import app.routers.production_control as production_router
    import app.routers.purchase_control as purchase_router

    # These are real legacy entry symbols.  Reaching one would prove a
    # current request fell through before its current-manifest gate.
    forbidden = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("legacy reader reached before current gate")
    )
    monkeypatch.setattr(production_router, "read_production_control_journal_current", forbidden, raising=True)
    monkeypatch.setattr(purchase_router, "list_journal", forbidden, raising=True)
    monkeypatch.setattr(plan_router, "get_period_plan_execution_journal", forbidden, raising=True)
    monkeypatch.setattr(production_router.planning_truth, "require_accepted_truth", lambda *a, **k: object())
    monkeypatch.setattr(production_router, "build_truth_meta", lambda *_a, **_k: {})

    writes: list[str] = []
    bind = db_session.get_bind()

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    sa.event.listen(bind, "before_cursor_execute", observe)
    try:
        calls = (
            lambda: production_router.get_orders_journal(db=db_session),
            lambda: production_router.get_assembly_queue(db=db_session),
            lambda: purchase_router.get_orders(active_only=False, db=db_session),
            lambda: asyncio.run(plan_router.period_plans_execution_journal(plan_id=1, db=db_session)),
        )
        for call in calls:
            with pytest.raises(HTTPException) as caught:
                call()
            assert caught.value.status_code == 503
    finally:
        sa.event.remove(bind, "before_cursor_execute", observe)
    assert writes == []
