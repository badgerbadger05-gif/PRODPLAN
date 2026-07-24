"""Router contract for purchase-control materialization endpoint."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import count

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import models
from app.database import Base, get_db
from app.routers.purchase_control import router as purchase_control_router
from app.services import planning_truth
from app.services.purchase_control_snapshot import build_candidate_snapshot


CAPABILITIES = {
    "physical_ledger": True,
    "reservation_replay": True,
    "planning_snapshots": True,
    "purchase_control_journal": True,
}


_fixture_seq = count(1)


def _accepted_generation(db) -> tuple[models.LedgerGeneration, models.Item, models.Supplier]:
    cutoff = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    idx = next(_fixture_seq)
    physical = models.PhysicalImportBatch(
        batch_key=f"pcm-router-physical-{idx}",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key=f"pcm-router-generation-{idx}",
        status="building",
        cutoff=cutoff,
        source_watermarks={},
        capabilities={},
        physical_import_batch=physical,
        algorithm_version="tests/pcm-router",
    )
    db.add(generation)
    db.flush()

    item = models.Item(
        item_code=f"PUR-MAT-{idx}",
        item_name="Материал PCM",
        item_ref1c=f"item-ref-pcm-{idx}",
        supplier_ref1c=f"SUP-PCM-{idx}",
        unit="шт",
    )
    supplier = models.Supplier(supplier_ref1c=f"SUP-PCM-{idx}", supplier_name="Поставщик PCM")
    db.add_all([item, supplier])
    db.flush()

    return generation, item, supplier


def _add_buy_run(
    db,
    *,
    generation,
    item,
    period_from: date,
    period_to: date,
    required_qty: Decimal,
    realized_qty: Decimal,
    covered_incoming: Decimal,
    uncovered: Decimal,
):
    plan = models.ProductionPlanHeader(
        name=f"buy-run-{period_from.isoformat()}",
        period_from=period_from,
        period_to=period_to,
        status="fixed",
    )
    db.add(plan)
    db.flush()

    planning_run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={"plan": "pcm"},
        source_plan_id=plan.id,
        period_from=period_from,
        period_to=period_to,
        ledger_generation_id=generation.id,
    )
    db.add(planning_run)
    db.flush()

    requirement = models.MrpRequirement(
        run_id=planning_run.run_id,
        item_id=item.item_id,
        total_required_qty=required_qty,
        net_required_qty=required_qty,
        covered_qty=Decimal("0"),
        remaining_qty=required_qty,
        period_from=period_from,
        period_to=period_to,
        bom_level=1,
    )
    db.add(requirement)
    db.flush()

    reservation = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="main",
        run_id=planning_run.run_id,
        freeze_version=0,
        requirement_id=requirement.id,
        priority_period_from=period_from,
        priority_period_to=period_to,
        realization_mode="buy",
        reserved_qty=required_qty,
        realized_qty=realized_qty,
        covered_incoming_supplier_qty=covered_incoming,
        covered_incoming_wip_qty=Decimal("0"),
        uncovered_qty=uncovered,
        lifecycle_status="active",
    )
    db.add(reservation)
    db.flush()

    if covered_incoming > Decimal("0"):
        db.add(
            models.ReservationCoverage(
                reservation_id=reservation.id,
                source_kind="supplier_order",
                source_ref="supplier-1",
                source_line_ref="10",
                pin_kind="incoming",
                alloc_qty=covered_incoming,
                covered_qty=covered_incoming,
                realized_qty=covered_incoming,
                evaporated_qty=Decimal("0"),
            )
        )
    db.flush()

    return reservation, planning_run


def _accept_generation_snapshot(
    db, generation: models.LedgerGeneration, snapshot: models.PlanningReadSnapshot
):
    accepted_at = generation.cutoff + timedelta(hours=1)
    generation.status = "accepted"
    generation.accepted_at = accepted_at
    generation.capabilities = dict(CAPABILITIES)
    snapshot.truth_status = "accepted"
    snapshot.reason = None
    snapshot.published_at = accepted_at
    planning_truth.publish_generation(db, generation)
    db.flush()


def _build_multi_run_snapshot(db) -> tuple[models.LedgerGeneration, models.PlanningReadSnapshot]:
    generation, item, _supplier = _accepted_generation(db)
    _add_buy_run(
        db,
        generation=generation,
        item=item,
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        required_qty=Decimal("10"),
        realized_qty=Decimal("3"),
        covered_incoming=Decimal("0"),
        uncovered=Decimal("7"),
    )
    _add_buy_run(
        db,
        generation=generation,
        item=item,
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        required_qty=Decimal("12"),
        realized_qty=Decimal("4"),
        covered_incoming=Decimal("1"),
        uncovered=Decimal("7"),
    )

    snapshot = build_candidate_snapshot(db, generation.id)
    _accept_generation_snapshot(db, generation, snapshot)
    return generation, snapshot


@pytest.fixture()
def client(db_session):
    app = FastAPI()
    app.include_router(purchase_control_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client


def _snapshot_first_row(snapshot: models.PlanningReadSnapshot) -> dict:
    rows = snapshot.payload.get("rows")
    assert isinstance(rows, list) and rows, "snapshot rows are required"
    return dict(rows[0])


def test_materialize_endpoint_dry_run_preview(client, db_session):
    generation, snapshot = _build_multi_run_snapshot(db_session)
    row = _snapshot_first_row(snapshot)

    response = client.post(
        "/api/v1/purchase-control/materialize",
        json={
            "snapshot_id": snapshot.id,
            "row_keys": [row["row_key"]],
            "dry_run": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["snapshot_id"] == snapshot.id
    assert body["rows_total"] == 1
    assert db_session.query(models.PurchaseExportBatch).count() == 0
    assert db_session.query(models.PurchaseExportObligationAllocation).count() == 0


def test_materialize_endpoint_returns_not_configured_when_materializer_missing(client, db_session):
    _generation, snapshot = _build_multi_run_snapshot(db_session)
    row = _snapshot_first_row(snapshot)

    response = client.post(
        "/api/v1/purchase-control/materialize",
        json={
            "snapshot_id": snapshot.id,
            "row_keys": [row["row_key"]],
            "dry_run": False,
        },
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "purchase_control_materializer_not_configured"


def test_materialize_endpoint_rejects_empty_row_keys(client, db_session):
    _generation, snapshot = _build_multi_run_snapshot(db_session)

    response = client.post(
        "/api/v1/purchase-control/materialize",
        json={
            "snapshot_id": snapshot.id,
            "row_keys": [],
            "dry_run": True,
        },
    )

    assert response.status_code == 400
    assert "row_keys must be a non-empty list" in response.text
