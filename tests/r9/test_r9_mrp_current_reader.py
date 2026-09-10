from datetime import datetime, timezone

import pytest

from app import models
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    publish_current_obligation_views_from_generation,
)
from app.services.mrp_result_snapshot import (
    read_mrp_result_manifest,
    read_mrp_result_rows,
)
from app.routers.plan import get_planning_result_summary


def _generation(db):
    cutoff = datetime(2026, 9, 10, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(
        batch_key="r9-mrp-reader-batch",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="r9-mrp-reader-generation",
        status="accepted",
        cutoff=cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True, "reservation_replay": True},
        physical_import_batch=batch,
        algorithm_version="r9-test",
        replay_version="r9-test",
        accepted_at=cutoff,
    )
    db.add(generation)
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
    db.flush()
    return generation


def _mrp_snapshot(db, generation):
    snapshot = models.PlanningReadSnapshot(
        consumer="mrp_result",
        snapshot_key="run:41:v1",
        ledger_generation_id=generation.id,
        cutoff=generation.cutoff,
        truth_status="accepted",
        payload={"summary": {"planned": 7}},
        published_at=generation.cutoff,
    )
    db.add(snapshot)
    db.flush()
    db.add(models.PlanningReadRow(
        snapshot_id=snapshot.id,
        row_key="req:41:1",
        row_kind="production",
        item_id=10,
        sort_key="2026-09-10|0001",
        payload={"item_id": 10, "qty": 7, "run_id": 41, "row_kind": "production"},
    ))
    db.flush()


def test_mrp_reader_uses_persisted_current_rows_and_manifest(db_session):
    generation = _generation(db_session)
    _mrp_snapshot(db_session, generation)
    db_session.commit()
    publish_current_obligation_views_from_generation(db_session, generation.id)
    db_session.commit()

    result = read_mrp_result_rows(db_session, 41, row_kind="production")
    manifest = read_mrp_result_manifest(db_session, 41)

    assert result["rows"][0].items() >= {
        "item_id": 10,
        "qty": 7,
        "run_id": 41,
        "row_kind": "production",
    }.items()
    assert result["current_identity"]
    assert result["source_revision"].startswith("accepted:g")
    assert manifest["current_identity"] == result["current_identity"]


def test_mrp_reader_fails_closed_even_when_legacy_snapshot_exists(db_session):
    generation = _generation(db_session)
    _mrp_snapshot(db_session, generation)
    db_session.commit()

    with pytest.raises(CurrentExecutionUnavailable):
        read_mrp_result_rows(db_session, 41, row_kind="production")


def test_mrp_http_reader_maps_missing_current_to_503(db_session):
    generation = _generation(db_session)
    _mrp_snapshot(db_session, generation)
    db_session.commit()

    with pytest.raises(Exception) as caught:
        import asyncio
        asyncio.run(get_planning_result_summary(41, db=db_session))
    assert getattr(caught.value, "status_code", None) == 503


def test_mrp_reader_rejects_unknown_run_and_keeps_date_to_inclusive(db_session):
    generation = _generation(db_session)
    snapshot = models.PlanningReadSnapshot(
        consumer="mrp_result",
        snapshot_key="run:51:v1",
        ledger_generation_id=generation.id,
        cutoff=generation.cutoff,
        truth_status="accepted",
        payload={"summary": {"row_counts": {"production": 1}, "total_qty": {"production": 3}}},
        published_at=generation.cutoff,
    )
    db_session.add(snapshot)
    db_session.flush()
    db_session.add(models.PlanningReadRow(
        snapshot_id=snapshot.id,
        row_key="req:51:1",
        row_kind="production",
        item_id=10,
        sort_key="2026-09-10|0001",
        payload={"item_id": 10, "qty": 3, "run_id": 51, "row_kind": "production"},
    ))
    db_session.commit()
    publish_current_obligation_views_from_generation(db_session, generation.id)
    db_session.commit()

    assert read_mrp_result_rows(
        db_session, 51, row_kind="production", date_to="2026-09-10"
    )["total"] == 1
    with pytest.raises(CurrentExecutionUnavailable):
        read_mrp_result_manifest(db_session, 999)


def test_mrp_reader_uses_per_run_summary_and_keeps_identity_tie_ascending(db_session):
    generation = _generation(db_session)
    for run_id, total in ((61, 2), (62, 9)):
        snapshot = models.PlanningReadSnapshot(
            consumer="mrp_result",
            snapshot_key=f"run:{run_id}:v1",
            ledger_generation_id=generation.id,
            cutoff=generation.cutoff,
            truth_status="accepted",
            payload={"summary": {"row_counts": {"production": 1}, "total_qty": {"production": total}}},
            published_at=generation.cutoff,
        )
        db_session.add(snapshot)
        db_session.flush()
        for key in ("b", "a"):
            db_session.add(models.PlanningReadRow(
                snapshot_id=snapshot.id,
                row_key=key,
                row_kind="production",
                item_id=10,
                sort_key="2026-09-10|same",
                payload={"item_id": 10, "qty": 1, "run_id": run_id, "row_kind": "production"},
            ))
    db_session.commit()
    publish_current_obligation_views_from_generation(db_session, generation.id)
    db_session.commit()

    manifest = read_mrp_result_manifest(db_session, 62)
    rows = read_mrp_result_rows(db_session, 62, row_kind="production", sort_dir="desc")
    assert manifest["snapshot_total_qty"] == {"production": 9}
    assert [row["current_identity"] for row in rows["rows"]] == [
        "run:62:v1:production:a", "run:62:v1:production:b"
    ]


def test_mrp_root_filter_uses_persisted_current_membership(db_session):
    generation = _generation(db_session)
    snapshot = models.PlanningReadSnapshot(
        consumer="mrp_result",
        snapshot_key="run:71:v1",
        ledger_generation_id=generation.id,
        cutoff=generation.cutoff,
        truth_status="accepted",
        payload={"summary": {"row_counts": {"production": 1}, "total_qty": {"production": 4}}},
        published_at=generation.cutoff,
    )
    db_session.add(snapshot)
    db_session.flush()
    row = models.PlanningReadRow(
        snapshot_id=snapshot.id,
        row_key="req:71:1",
        row_kind="production",
        item_id=10,
        sort_key="2026-09-10|0001",
        payload={"item_id": 10, "qty": 4, "run_id": 71, "row_kind": "production"},
    )
    db_session.add(row)
    db_session.flush()
    db_session.add(models.PlanningReadRootMember(
        snapshot_id=snapshot.id,
        row_id=row.id,
        root_key="root:99",
        root_item_id=99,
    ))
    db_session.commit()
    publish_current_obligation_views_from_generation(db_session, generation.id)
    db_session.commit()

    assert read_mrp_result_rows(
        db_session, 71, row_kind="production", root_item_id=99
    )["total"] == 1
    assert read_mrp_result_rows(
        db_session, 71, row_kind="production", root_item_id=100
    )["total"] == 0
