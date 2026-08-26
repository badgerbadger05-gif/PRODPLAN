"""Router contract for purchase-control materialization endpoint."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import count

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import models
from app.database import get_db
from app.routers.purchase_control import router as purchase_control_router
from app.services import planning_truth
from app.services import purchase_control_materialization as pcm
from app.routers import purchase_control as purchase_control_router_module
from app.services.purchase_control_snapshot import build_candidate_snapshot


CAPABILITIES = {
    "physical_ledger": True,
    "reservation_replay": True,
    "planning_snapshots": True,
    "purchase_control_journal": True,
}


_fixture_seq = count(1)
_BASE_URL = "http://mtzdock/unf_demo/odata/standard.odata"
_MATERIALIZATION_DESTINATION = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _materialization_purchase_odata_config(monkeypatch):
    config = {
        "base_url": _BASE_URL,
        "purchase_destination_warehouse_ref1c": _MATERIALIZATION_DESTINATION,
    }
    monkeypatch.setattr(
        pcm,
        "_load_odata_config",
        lambda: config,
    )
    monkeypatch.setattr(
        pcm.purchase_control_snapshot,
        "_load_odata_config",
        lambda: config,
    )


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
        covered_from_stock_at_freeze_qty=Decimal("0"),
        replenishment_required_qty=required_qty,
        replenishment_received_qty=realized_qty,
        lifecycle_status="active",
    )
    db.add(reservation)
    db.flush()
    work_item = models.ReplenishmentWorkItem(
        ledger_generation_id=generation.id,
        reservation_id=reservation.id,
        plan_id=plan.id,
        run_id=planning_run.run_id,
        requirement_id=requirement.id,
        item_id=item.item_id,
        replenishment_method="buy",
        replenishment_required_qty=required_qty,
        replenishment_fulfilled_qty=realized_qty,
        replenishment_remaining_qty=required_qty - realized_qty,
    )
    db.add(work_item)
    db.flush()

    db.flush()

    return reservation, planning_run, work_item


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
    row = dict(rows[0])
    assert isinstance(row.get("slices"), list) and row["slices"], "snapshot rows should include replenishment slices"
    for bucket in row["slices"]:
        assert bucket.get("work_item_id") is not None
    return row


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

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["dry_run"] is True
    assert body["snapshot_id"] == snapshot.id
    assert body["rows_total"] == 1
    assert db_session.query(models.PurchaseExportBatch).count() == 0
    assert db_session.query(models.PurchaseExportObligationAllocation).count() == 0


def test_selection_summary_endpoint_reports_missing_accounting_price(client, db_session):
    _generation, snapshot = _build_multi_run_snapshot(db_session)
    row = _snapshot_first_row(snapshot)

    response = client.post(
        "/api/v1/purchase-control/selection-summary",
        json={
            "snapshot_id": snapshot.id,
            "row_keys": [row["row_key"]],
        },
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {
        "snapshot_id": snapshot.id,
        "selected_rows": 1,
        "priced_rows": 0,
        "unpriced_rows": 1,
        "known_amount": 0.0,
        "total_amount": None,
        "amount_status": "unavailable",
    }


def test_materialize_endpoint_returns_not_configured_when_materializer_missing(
    client,
    db_session,
    monkeypatch,
):
    def _missing_writer(
        _db,
        *_args,
        **_kwargs,
    ):
        raise pcm.PurchaseControlMaterializerNotConfigured(
            "purchase-control materialization writer is not configured"
        )

    monkeypatch.setattr(purchase_control_router_module, "materialize_rows", _missing_writer)

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

    assert response.status_code == 503, response.json()
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
