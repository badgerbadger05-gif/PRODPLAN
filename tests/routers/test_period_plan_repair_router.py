"""Admin repair for plans poisoned by the old fixation TOCTOU race.

Two concurrent «Зафиксировать» calls could both publish a FIXED_SNAPSHOT run for
the same plan.  Such a plan is permanently unusable — every later snapshot
attempt answers «План имеет несколько текущих зафиксированных MRP-снимков», and
``uq_planning_run_fixed_snapshot_source_plan`` cannot even be created on that
database.  The race is closed; these tests cover the cleanup path for the rows
it already left behind.

The unique index is dropped inside the tests on purpose: a database that still
needs the repair is by definition one where that index does not exist yet.
"""

import datetime
from datetime import date, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import get_db
from app.models import (
    Item,
    LedgerGeneration,
    PhysicalImportBatch,
    PlanningReadSnapshot,
    PlanningRun,
    PlanningTruthState,
    ProductionPlanHeader,
    ProductionPlanLine,
)
from app.routers.plan import router as plan_router


CUTOFF = datetime.datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
_FIXED_SNAPSHOT_INDEX = "uq_planning_run_fixed_snapshot_source_plan"


@pytest.fixture()
def client(db_session):
    app = FastAPI()
    app.include_router(plan_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def accepted_generation(db_session):
    batch = PhysicalImportBatch(
        batch_key="repair-ledger", status="completed", cutoff=CUTOFF,
        source_watermarks={}, completed_at=CUTOFF,
    )
    generation = LedgerGeneration(
        generation_key="repair-ledger", status="accepted", cutoff=CUTOFF,
        accepted_at=CUTOFF,
        source_watermarks={"replay_from": "2026-07-01T00:00:00+00:00"},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
            "planning_snapshots": True,
        },
        physical_import_batch=batch, algorithm_version="test",
    )
    db_session.add(generation)
    db_session.flush()
    db_session.add(PlanningTruthState(id=1, current_generation_id=generation.id))
    db_session.commit()
    return generation


def _fixed_plan(db, *, code="REPAIR"):
    item = Item(
        item_code=code, item_name="Деталь", unit="шт", replenishment_method="Покупка", replenishment_time=2, status="active",
    )
    db.add(item)
    db.flush()
    plan = ProductionPlanHeader(
        name="Август", period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
        status="fixed", created_by="test",
    )
    db.add(plan)
    db.flush()
    db.add(ProductionPlanLine(
        plan_id=plan.id, item_id=item.item_id, bucket_date=date(2026, 8, 7), qty=4.0,
    ))
    db.commit()
    return plan


def _fixed_run(db, plan, generation):
    run = PlanningRun(
        status="FIXED_SNAPSHOT", ledger_generation_id=generation.id,
        source_plan_id=plan.id, period_from=plan.period_from,
        period_to=plan.period_to, config_snapshot={}, horizon_days=30,
        started_at=CUTOFF, fixed_at=CUTOFF, finished_at=CUTOFF,
        pinned=True, active_freeze_version=1,
    )
    db.add(run)
    db.flush()
    return run


def _mrp_result_snapshot(db, run, generation):
    db.add(PlanningReadSnapshot(
        consumer="mrp_result", snapshot_key=f"run:{int(run.run_id)}",
        ledger_generation_id=generation.id, cutoff=CUTOFF, truth_status="accepted",
        payload={"run_id": int(run.run_id)}, published_at=CUTOFF,
    ))
    db.flush()


def _poison(db, plan, generation, *, snapshot_on_first=True):
    """Two current FIXED_SNAPSHOT runs, only one of which owns a read snapshot."""
    db.execute(text(f"DROP INDEX IF EXISTS {_FIXED_SNAPSHOT_INDEX}"))
    first = _fixed_run(db, plan, generation)
    second = _fixed_run(db, plan, generation)
    if snapshot_on_first:
        _mrp_result_snapshot(db, first, generation)
    db.commit()
    return first, second


def test_repair_keeps_the_run_with_a_valid_snapshot_not_the_newest_id(
    client, db_session, accepted_generation
):
    plan = _fixed_plan(db_session)
    survivor, loser = _poison(db_session, plan, accepted_generation)
    assert int(loser.run_id) > int(survivor.run_id)

    response = client.post(
        f"/api/v1/plan/period-plans/{plan.id}/repair-duplicate-snapshots",
        json={"repaired_by": "admin"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["repaired"] is True
    assert body["survivor_run_id"] == int(survivor.run_id)
    assert body["superseded_run_ids"] == [int(loser.run_id)]
    db_session.expire_all()
    assert str(db_session.get(PlanningRun, survivor.run_id).status) == "FIXED_SNAPSHOT"
    assert str(db_session.get(PlanningRun, loser.run_id).status) == "SUPERSEDED"
    # The plan is now healthy enough for the singleton index the migration adds.
    db_session.execute(text(
        f"CREATE UNIQUE INDEX {_FIXED_SNAPSHOT_INDEX} ON planning_run (source_plan_id) "
        "WHERE status = 'FIXED_SNAPSHOT' AND source_plan_id IS NOT NULL"
    ))
    db_session.commit()


def test_ordinary_snapshot_path_works_again_after_the_repair(
    client, db_session, accepted_generation
):
    plan = _fixed_plan(db_session, code="REPAIR-THEN-SNAPSHOT")
    survivor, _loser = _poison(db_session, plan, accepted_generation)

    poisoned = client.post(
        f"/api/v1/plan/period-plans/{plan.id}/mrp-snapshot", json={"started_by": "erp"},
    )
    assert poisoned.status_code == 400
    assert "несколько текущих" in poisoned.json()["detail"]

    repair = client.post(
        f"/api/v1/plan/period-plans/{plan.id}/repair-duplicate-snapshots", json={},
    )
    assert repair.status_code == 200, repair.text

    healed = client.post(
        f"/api/v1/plan/period-plans/{plan.id}/mrp-snapshot", json={"started_by": "erp"},
    )
    assert healed.status_code == 200, healed.text
    assert healed.json()["run_id"] == int(survivor.run_id)
    assert healed.json()["immutable"] is True


def test_repair_is_a_no_op_for_a_healthy_plan(client, db_session, accepted_generation):
    plan = _fixed_plan(db_session, code="REPAIR-HEALTHY")
    run = _fixed_run(db_session, plan, accepted_generation)
    db_session.commit()

    first = client.post(
        f"/api/v1/plan/period-plans/{plan.id}/repair-duplicate-snapshots", json={},
    )
    second = client.post(
        f"/api/v1/plan/period-plans/{plan.id}/repair-duplicate-snapshots", json={},
    )

    assert first.status_code == 200, first.text
    assert first.json()["repaired"] is False
    assert first.json()["survivor_run_id"] == int(run.run_id)
    assert second.json() == first.json()
    db_session.expire_all()
    assert str(db_session.get(PlanningRun, run.run_id).status) == "FIXED_SNAPSHOT"


def test_repair_is_idempotent_on_an_already_repaired_plan(
    client, db_session, accepted_generation
):
    plan = _fixed_plan(db_session, code="REPAIR-TWICE")
    survivor, loser = _poison(db_session, plan, accepted_generation)

    first = client.post(
        f"/api/v1/plan/period-plans/{plan.id}/repair-duplicate-snapshots", json={},
    )
    second = client.post(
        f"/api/v1/plan/period-plans/{plan.id}/repair-duplicate-snapshots", json={},
    )

    assert first.json()["repaired"] is True
    assert second.json()["repaired"] is False
    assert second.json()["survivor_run_id"] == int(survivor.run_id)
    db_session.expire_all()
    assert str(db_session.get(PlanningRun, loser.run_id).status) == "SUPERSEDED"


def test_repair_rejects_an_unknown_plan(client, db_session, accepted_generation):
    # Pin the in-memory SQLite connection to this session: the TestClient serves
    # the request from another thread, and a session with no open transaction
    # would otherwise be handed that thread's own (empty) ``:memory:`` database.
    db_session.execute(text("SELECT 1"))

    response = client.post(
        "/api/v1/plan/period-plans/999999/repair-duplicate-snapshots", json={},
    )

    assert response.status_code == 400
    assert "не найден" in response.json()["detail"]
