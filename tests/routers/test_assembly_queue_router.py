"""Contract tests for /api/v1/production-control/assembly-queue."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.routers.production_control import (
    get_assembly_queue,
    get_drum_schedule,
    get_shelf_projections,
    router as production_control_router,
)
from app.services import planning_truth


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session):
    app = FastAPI()
    app.include_router(production_control_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _accepted_generation(db, *, with_assembly_queue_capability: bool = True):
    cutoff = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="assembly-queue-router-physical",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="assembly-queue-router-generation",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        capabilities={},
        source_watermarks={},
        physical_import_batch=physical,
        algorithm_version="tests/assembly-queue-router",
    )
    db.add_all([physical, generation])
    db.flush()
    generation.capabilities = {
        planning_truth.CAPABILITY_PHYSICAL_LEDGER: True,
        planning_truth.CAPABILITY_RESERVATION_REPLAY: True,
        planning_truth.CAPABILITY_PLANNING_SNAPSHOTS: True,
        planning_truth.CAPABILITY_ASSEMBLY_QUEUE: with_assembly_queue_capability,
        planning_truth.CAPABILITY_DRUM_SCHEDULE: True,
        planning_truth.CAPABILITY_SHELF_PROJECTION: True,
    }
    planning_truth.publish_generation(db, generation)
    return generation, cutoff


def _publish_snapshot(db, payload):
    planning_truth.publish_read_snapshot(
        db,
        consumer="assembly_queue",
        snapshot_key="current:v1",
        payload=payload,
        required_capabilities=(
            planning_truth.CAPABILITY_PHYSICAL_LEDGER,
            planning_truth.CAPABILITY_RESERVATION_REPLAY,
            planning_truth.CAPABILITY_PLANNING_SNAPSHOTS,
            planning_truth.CAPABILITY_ASSEMBLY_QUEUE,
        ),
    )
    db.flush()


def test_assembly_queue_returns_strict_payload_for_accepted_snapshot(client, db_session):
    generation, _ = _accepted_generation(db_session)
    payload = {
        "rows": [
            {
                "run_id": 3001,
                "plan_id": 4001,
                "plan_line_id": 4101,
                "item_id": 5001,
                "item_code": "FG-1",
                "item_name": "Finished good 1",
                "bucket_date": "2026-08-03",
                "period_from": "2026-08-01",
                "period_to": "2026-08-31",
                "planned_output_qty": 10.0,
                "accepted_plan_output_qty": 3.0,
                "assembly_remaining_qty": 7.0,
                "priority_key": ["2026-08-01", "2026-08-31", 4001, 4101],
                "sort_key": "2026-08-01|2026-08-31|0000004001|0000004101",
                "eligible_from": "2026-08-01T00:00:00.000000Z",
            },
            {
                "run_id": 3002,
                "plan_id": 4002,
                "plan_line_id": 4102,
                "item_id": 5002,
                "item_code": "FG-2",
                "item_name": "Finished good 2",
                "bucket_date": "2026-09-05",
                "period_from": "2026-09-01",
                "period_to": "2026-09-30",
                "planned_output_qty": 20.0,
                "accepted_plan_output_qty": 5.0,
                "assembly_remaining_qty": 15.0,
                "priority_key": ["2026-09-01", "2026-09-30", 4002, 4102],
                "sort_key": "2026-09-01|2026-09-30|0000004002|0000004102",
                "eligible_from": "2026-09-01T00:00:00.000000Z",
            },
        ],
        "total_rows": 2,
        "total_queue_qty": 22.0,
    }
    _publish_snapshot(db_session, payload)
    db_session.commit()

    response = client.get("/api/v1/production-control/assembly-queue")
    assert response.status_code == 200
    assert response.json() == {
        **payload,
        "limit": 1000,
        "offset": 0,
        "truth_meta": {
            "ledger_generation": int(generation.id),
            "cutoff": generation.cutoff.isoformat(),
            "truth_status": "accepted",
            "truth_reason": None,
        },
    }


def test_assembly_queue_pages_rows_but_keeps_whole_queue_totals(client, db_session):
    _accepted_generation(db_session)
    rows = [
        {
            "run_id": 3000 + index,
            "plan_id": 4000 + index,
            "plan_line_id": 4100 + index,
            "item_id": 5000 + index,
            "item_code": f"FG-{index}",
            "item_name": f"Finished good {index}",
            "bucket_date": "2026-08-03",
            "period_from": "2026-08-01",
            "period_to": "2026-08-31",
            "planned_output_qty": 10.0,
            "accepted_plan_output_qty": 0.0,
            "assembly_remaining_qty": 10.0,
            "priority_key": ["2026-08-01", "2026-08-31", 4000 + index, 4100 + index],
            "sort_key": (
                f"2026-08-01|2026-08-31|{4000 + index:010d}|{4100 + index:010d}"
            ),
            "eligible_from": "2026-08-01T00:00:00.000000Z",
        }
        for index in range(5)
    ]
    _publish_snapshot(
        db_session,
        {"rows": rows, "total_rows": len(rows), "total_queue_qty": 50.0},
    )
    db_session.commit()

    page = client.get(
        "/api/v1/production-control/assembly-queue", params={"limit": 2, "offset": 3}
    ).json()

    assert [row["plan_line_id"] for row in page["rows"]] == [4103, 4104]
    assert page["total_rows"] == 5
    assert page["total_queue_qty"] == 50.0
    assert page["limit"] == 2
    assert page["offset"] == 3

    assert (
        client.get(
            "/api/v1/production-control/assembly-queue", params={"limit": 0}
        ).status_code
        == 422
    )


def test_assembly_queue_router_rejects_missing_assembly_queue_capability(
    db_session,
):
    _accepted_generation(db_session, with_assembly_queue_capability=False)
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        get_assembly_queue(db=db_session)
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "planning_truth_unavailable"
    assert exc.value.detail["ready"] is False


def test_assembly_queue_router_rejects_missing_snapshot_even_with_capability(
    db_session,
):
    _accepted_generation(db_session)
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        get_assembly_queue(db=db_session)
    detail = exc.value.detail
    assert detail["code"] == "assembly_queue_unavailable"


def test_missing_snapshot_detail_keeps_its_own_reason(db_session):
    """The readiness projection must not erase the explicit diagnosis.

    The truth pointer is perfectly healthy here, so ``readiness.reason`` is
    ``None``; unpacking it last used to overwrite the one sentence that says
    what is actually missing, and the caller received ``reason: null``.
    """
    generation, _ = _accepted_generation(db_session)
    db_session.commit()
    assert planning_truth.get_truth_state(db_session).reason is None

    with pytest.raises(HTTPException) as queue_exc:
        get_assembly_queue(db=db_session)
    queue_detail = queue_exc.value.detail
    assert queue_detail["code"] == "assembly_queue_unavailable"
    assert queue_detail["reason"] == (
        "assembly queue snapshot is missing for accepted generation"
    )
    # The readiness projection still travels alongside it.
    assert queue_detail["truth_status"] == "accepted"
    assert queue_detail["ledger_generation"] == int(generation.id)

    with pytest.raises(HTTPException) as drum_exc:
        get_drum_schedule(db=db_session)
    drum_detail = drum_exc.value.detail
    assert drum_detail["code"] == "drum_schedule_unavailable"
    assert drum_detail["reason"] == (
        "drum schedule is missing for accepted generation"
    )
    assert drum_detail["ledger_generation"] == int(generation.id)


def test_drum_router_reads_only_persisted_accepted_schedule(client, db_session):
    generation, cutoff = _accepted_generation(db_session)
    db_session.add(
        models.DrumSchedule(
            ledger_generation_id=generation.id,
            status="completed",
            algorithm_version="tests/1",
            schedule_from=cutoff.date(),
            schedule_to=cutoff.date(),
            queue_signature="q" * 64,
            slot_signature="s" * 64,
            gap_signature="g" * 64,
            slot_row_count=0,
            gap_row_count=0,
            total_open_qty=0,
            total_slot_qty=0,
            total_gap_qty=0,
            metrics={},
        )
    )
    db_session.commit()

    response = client.get("/api/v1/production-control/drum")

    assert response.status_code == 200
    assert response.json() == {
        "schedule_from": cutoff.date().isoformat(),
        "schedule_to": cutoff.date().isoformat(),
        "slots": [],
        "gaps": [],
        "total_open_qty": 0.0,
        "total_slot_qty": 0.0,
        "total_gap_qty": 0.0,
        "total_slots": 0,
        "total_gaps": 0,
        "limit": 1000,
        "offset": 0,
        "truth_meta": {
            "ledger_generation": generation.id,
            "cutoff": generation.cutoff.isoformat(),
            "truth_status": "accepted",
            "truth_reason": None,
        },
    }


def test_drum_router_fails_closed_without_persisted_schedule(db_session):
    _accepted_generation(db_session)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        get_drum_schedule(db=db_session)

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "drum_schedule_unavailable"


def _drum_schedule_with_slots(
    db, generation, cutoff, *, slot_count: int, item_id: int = 7
):
    schedule = models.DrumSchedule(
        ledger_generation_id=generation.id,
        status="completed",
        algorithm_version="tests/1",
        schedule_from=cutoff.date(),
        schedule_to=cutoff.date(),
        queue_signature="q" * 64,
        slot_signature="s" * 64,
        gap_signature="g" * 64,
        slot_row_count=slot_count,
        gap_row_count=0,
        total_open_qty=slot_count,
        total_slot_qty=slot_count,
        total_gap_qty=0,
        metrics={},
    )
    db.add(schedule)
    db.flush()
    for ordinal in range(slot_count):
        db.add(
            models.DrumSlot(
                drum_schedule_id=schedule.id,
                assembly_queue_line_id=900 + ordinal,
                plan_id=1,
                plan_line_id=1,
                item_id=item_id,
                resource_id=3,
                slot_date=cutoff.date(),
                slot_qty=1,
                planned_output_qty=1,
                slot_ordinal=ordinal,
                original_priority=[],
            )
        )
    db.flush()
    return schedule


def test_drum_router_pages_slots_and_reports_totals(client, db_session):
    generation, cutoff = _accepted_generation(db_session)
    _drum_schedule_with_slots(db_session, generation, cutoff, slot_count=5)
    db_session.commit()

    page = client.get(
        "/api/v1/production-control/drum", params={"limit": 2, "offset": 2}
    ).json()

    assert len(page["slots"]) == 2
    assert page["total_slots"] == 5
    assert page["total_gaps"] == 0
    assert page["limit"] == 2
    assert page["offset"] == 2
    # Schedule-wide totals never shrink with the page.
    assert page["total_slot_qty"] == 5.0
    # No Item row behind this id: the labels are additive and stay nullable.
    assert page["slots"][0]["item_code"] is None
    assert page["slots"][0]["item_name"] is None

    full = client.get("/api/v1/production-control/drum").json()
    assert len(full["slots"]) == 5


def test_drum_router_exposes_item_labels_on_slots(client, db_session):
    """The drum board must not be left rendering bare item ids."""
    generation, cutoff = _accepted_generation(db_session)
    item = models.Item(item_code="DRUM-7", item_name="Drum item 7")
    db_session.add(item)
    db_session.flush()
    _drum_schedule_with_slots(
        db_session, generation, cutoff, slot_count=2, item_id=item.item_id
    )
    db_session.commit()

    body = client.get("/api/v1/production-control/drum").json()

    assert [row["item_code"] for row in body["slots"]] == ["DRUM-7", "DRUM-7"]
    assert [row["item_name"] for row in body["slots"]] == [
        "Drum item 7",
        "Drum item 7",
    ]
    # Backwards compatible: every pre-existing slot field is untouched.
    assert body["slots"][0]["item_id"] == int(item.item_id)
    assert body["slots"][0]["slot_ordinal"] == 0
    assert body["slots"][0]["slot_qty"] == 1.0


def test_shelves_router_reads_empty_persisted_projection(client, db_session):
    generation, _ = _accepted_generation(db_session)
    db_session.commit()

    response = client.get("/api/v1/production-control/shelves")

    assert response.status_code == 200
    assert response.json()["rows"] == []
    assert response.json()["total_rows"] == 0
    assert response.json()["truth_meta"]["ledger_generation"] == generation.id


def test_shelves_router_exposes_item_labels_manifest_and_paging(client, db_session):
    generation, cutoff = _accepted_generation(db_session)
    item = models.Item(item_code="COMP-9", item_name="Shelf component 9")
    db_session.add(item)
    db_session.flush()
    policy = models.ShelfPolicy(
        item_id=item.item_id,
        warehouse_ref1c="SHELF",
        replenishment_time_days=3,
        review_cycle_days=1,
        safety_days=0,
        batch_multiple=1,
    )
    db_session.add(policy)
    db_session.flush()
    manifest = [
        {
            "need_date": "2026-08-05",
            "qty": "4",
            "priority": ["2026-08-01", "2026-08-31", 1, 1],
            "drum_slot_id": 11,
        }
    ]
    db_session.add(
        models.ShelfProjection(
            ledger_generation_id=generation.id,
            shelf_policy_id=policy.id,
            item_id=item.item_id,
            warehouse_ref1c="SHELF",
            as_of_date=cutoff.date(),
            protection_until=cutoff.date(),
            target_qty=4,
            shelf_physical_qty=0,
            other_stock_qty=0,
            confirmed_open_production_qty=0,
            projected_qty=0,
            gap_qty=4,
            transfer_qty=0,
            unlaunched_mrp_qty=4,
            pull_qty=4,
            materialized_qty=4,
            first_shortage_date=None,
            latest_start_date=None,
            demand_manifest=manifest,
        )
    )
    db_session.commit()

    body = client.get("/api/v1/production-control/shelves").json()

    assert body["total_rows"] == 1
    assert body["limit"] == 1000
    assert body["offset"] == 0
    row = body["rows"][0]
    assert row["item_code"] == "COMP-9"
    assert row["item_name"] == "Shelf component 9"
    assert row["demand_manifest"] == manifest

    empty_page = client.get(
        "/api/v1/production-control/shelves", params={"limit": 1, "offset": 1}
    ).json()
    assert empty_page["rows"] == []
    assert empty_page["total_rows"] == 1


def test_shelves_router_fails_closed_without_capability(db_session):
    generation, _ = _accepted_generation(db_session)
    generation.capabilities = {
        **generation.capabilities,
        planning_truth.CAPABILITY_SHELF_PROJECTION: False,
    }
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        get_shelf_projections(db=db_session)

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "planning_truth_unavailable"
