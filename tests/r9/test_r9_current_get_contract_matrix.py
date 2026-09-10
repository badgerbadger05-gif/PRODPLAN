"""Behavioral R9 inventory for current readers: read-only and fail-closed."""

from __future__ import annotations

import sqlalchemy as sa
import pytest
from datetime import datetime, timezone

from app import models
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    load_current_execution_coherent,
    publish_current_execution_scope,
)


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
    snapshot = models.PlanningReadSnapshot(
        consumer="mrp_result",
        snapshot_key="r9-legacy-only",
        ledger_generation_id=generation.id,
        cutoff=generation.cutoff,
        published_at=generation.cutoff,
        truth_status="accepted",
        payload={"rows": [{"row_key": "legacy", "qty": 99}]},
    )
    db_session.add(snapshot)
    db_session.commit()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy replay/builder was called by a current GET")

    # These are the old heavy/snapshot boundaries.  The current loader must
    # fail before either can be consulted.
    monkeypatch.setattr("app.services.mrp_result_snapshot._read_snapshot_rows", forbidden, raising=False)
    monkeypatch.setattr("app.services.mrp_result_snapshot.preview_make_work_item_materials", forbidden, raising=False)
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
