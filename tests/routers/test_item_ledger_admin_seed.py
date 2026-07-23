"""POST /api/v1/item-ledger/admin/seed — сид якоря T0 леджера-1 (Прил. A §4).

* dry-run (default) считает сводку и НЕ пишет в БД;
* боевой сид создаёт seed-SLE + stock_bin + stock_ledger_anchor на каждый
  ненулевой ключ Balance;
* повторный сид при существующих якорях без force → 409 (идемпотентность);
* force не удаляет общую физическую историю и при повторном сиде возвращает
  безопасный конфликт;
* пустой Balance от 1С → 502 (не сеем нулевой леджер);
* без OData-конфига → 400.
"""

import pytest
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
import app.routers.item_ledger_admin as admin_mod


@pytest.fixture()
def db_session():
    # StaticPool: one shared in-memory connection, so the TestClient worker
    # thread sees the same SQLite DB (same pattern as test_item_ledger_router).
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _client(db):
    app = FastAPI()
    app.include_router(admin_mod.router, prefix="/api")

    def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _setup_items(db):
    i1 = models.Item(item_code="P1", item_name="Product 1", item_ref1c="ref-item-1")
    i2 = models.Item(item_code="C1", item_name="Component 1", item_ref1c="ref-item-2")
    db.add_all([i1, i2])
    db.add(models.StockWarehouse(warehouse_ref1c="wh-1", warehouse_name="WH1"))
    db.flush()
    db.commit()
    return i1, i2


def _rows(qty1=10, qty2=4):
    # converted get_stock_from_1c_odata shape (code/ref/organization_ref/warehouse_ref/qty)
    return [
        {"code": "P1", "ref": "ref-item-1", "organization_ref": "ORG1", "warehouse_ref": "wh-1", "qty": qty1},
        {"code": "C1", "ref": "ref-item-2", "organization_ref": "ORG1", "warehouse_ref": "wh-1", "qty": qty2},
        # zero-qty key must be skipped (A §4.2: нулевые ключи не сеются)
        {"code": "C1", "ref": "ref-item-2", "organization_ref": "ORG2", "warehouse_ref": "wh-1", "qty": 0},
    ]


@pytest.fixture()
def seeded_odata(monkeypatch):
    monkeypatch.setattr(admin_mod, "load_odata_config", lambda: {"base_url": "http://1c/odata"})
    monkeypatch.setattr(admin_mod, "_fetch_balance_rows", lambda config: _rows())


def _counts(db):
    return (
        db.query(models.StockLedgerEntry).count(),
        db.query(models.StockBin).count(),
        db.query(models.StockLedgerAnchor).count(),
    )


def test_seed_dry_run_default_writes_nothing(db_session, seeded_odata):
    _setup_items(db_session)
    client = _client(db_session)

    resp = client.post("/api/v1/item-ledger/admin/seed")
    assert resp.status_code == 200
    body = resp.json()

    assert body["dry_run"] is True and body["force"] is False
    assert body["balance_rows"] == 3
    assert body["keys_total"] == 3
    assert body["keys_nonzero"] == 2 and body["keys_skipped_zero"] == 1
    assert body["total_qty"] == 14.0
    assert body["anchors_existing"] == 0
    assert body["anchors_created"] == 0 and body["entries_created"] == 0
    assert body["reseed"] is None
    assert body["physical_import_batch_id"] is None
    assert body["ledger_generation_id"] is None
    assert _counts(db_session) == (0, 0, 0)  # dry-run: БД не тронута
    assert db_session.query(models.PhysicalImportBatch).count() == 0
    assert db_session.query(models.LedgerGeneration).count() == 0


def test_seed_real_creates_anchors_bins_entries(db_session, seeded_odata):
    i1, i2 = _setup_items(db_session)
    client = _client(db_session)

    resp = client.post("/api/v1/item-ledger/admin/seed", params={"dry_run": "false"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["anchors_created"] == 2 and body["entries_created"] == 2
    assert body["ledger_generation_status"] == "building"
    assert _counts(db_session) == (2, 2, 2)

    batch = db_session.get(
        models.PhysicalImportBatch, body["physical_import_batch_id"]
    )
    generation = db_session.get(
        models.LedgerGeneration, body["ledger_generation_id"]
    )
    assert batch.status == "completed"
    assert generation.status == "building"
    assert generation.physical_import_batch_id == batch.id
    assert db_session.get(models.PlanningTruthState, 1) is None

    sle = {r.item_id: r for r in db_session.query(models.StockLedgerEntry).all()}
    assert float(sle[i1.item_id].qty) == 10 and float(sle[i1.item_id].qty_after) == 10
    assert sle[i1.item_id].movement_kind == "seed" and sle[i1.item_id].ingest_source == "seed"
    assert {row.ingest_batch_id for row in sle.values()} == {batch.id}
    assert all(len(row.source_content_hash) == 64 for row in sle.values())

    b1 = db_session.query(models.StockBin).filter_by(item_id=i1.item_id).one()
    assert float(b1.on_hand) == 10
    assert b1.ledger_generation_id == generation.id

    anchor = db_session.query(models.StockLedgerAnchor).filter_by(item_id=i2.item_id).one()
    assert float(anchor.balance_qty) == 4 and anchor.source == "balance_seed"
    # T0 == posting_at seed-строк — anchor guard отсечёт пуллы с Period <= T0.
    assert anchor.anchor_at == sle[i2.item_id].posting_at


def test_repeat_seed_without_force_conflicts_409(db_session, seeded_odata):
    _setup_items(db_session)
    client = _client(db_session)
    assert client.post("/api/v1/item-ledger/admin/seed", params={"dry_run": "false"}).status_code == 200

    resp = client.post("/api/v1/item-ledger/admin/seed", params={"dry_run": "false"})
    assert resp.status_code == 409
    assert "force" in resp.json()["detail"]
    assert _counts(db_session) == (2, 2, 2)  # ничего не задвоено

    # dry-run поверх засеянного леджера тоже честно отвечает 409 (то, что
    # случилось бы при боевом вызове), а не рисует сводку.
    assert client.post("/api/v1/item-ledger/admin/seed").status_code == 409


def test_force_reseed_is_non_destructive_conflict(
    db_session, seeded_odata, monkeypatch
):
    i1, i2 = _setup_items(db_session)
    client = _client(db_session)
    assert client.post("/api/v1/item-ledger/admin/seed", params={"dry_run": "false"}).status_code == 200

    generation = db_session.query(models.LedgerGeneration).one()
    generation.status = "accepted"
    generation.accepted_at = generation.cutoff
    db_session.add(models.PlanningTruthState(
        id=1, current_generation_id=generation.id
    ))

    # жизнь после сида: документ-пулл добавил движение и строку очереди
    seed_sle = db_session.query(models.StockLedgerEntry).first()
    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=seed_sle.ingest_batch_id,
        source_content_hash="f" * 64,
        item_id=i1.item_id, characteristic_ref="", organization_ref="ORG1",
        warehouse_ref1c="wh-1", qty=5, qty_after=15,
        record_type="Receipt", movement_kind="receipt",
        recorder_type="Document_СборкаЗапасов", recorder_ref="asm-1",
        line_no="1", ingest_source="document_pull",
    ))
    db_session.add(models.StockRecorderPull(
        recorder_type="Document_СборкаЗапасов", recorder_ref="asm-1", status="done",
    ))
    db_session.commit()

    before = {
        "entries": db_session.query(models.StockLedgerEntry).count(),
        "anchors": db_session.query(models.StockLedgerAnchor).count(),
        "bins": db_session.query(models.StockBin).count(),
        "pulls": db_session.query(models.StockRecorderPull).count(),
        "batches": db_session.query(models.PhysicalImportBatch).count(),
        "generations": db_session.query(models.LedgerGeneration).count(),
    }

    monkeypatch.setattr(admin_mod, "_fetch_balance_rows", lambda config: _rows(qty1=12, qty2=4))

    resp = client.post(
        "/api/v1/item-ledger/admin/seed", params={"dry_run": "false", "force": "true"}
    )
    assert resp.status_code == 409
    assert "не удаляются" in resp.json()["detail"]
    after = {
        "entries": db_session.query(models.StockLedgerEntry).count(),
        "anchors": db_session.query(models.StockLedgerAnchor).count(),
        "bins": db_session.query(models.StockBin).count(),
        "pulls": db_session.query(models.StockRecorderPull).count(),
        "batches": db_session.query(models.PhysicalImportBatch).count(),
        "generations": db_session.query(models.LedgerGeneration).count(),
    }
    assert after == before
    assert db_session.query(models.StockLedgerEntry).filter_by(
        recorder_ref="asm-1"
    ).one().active is True


def test_empty_balance_rejected_502(db_session, monkeypatch):
    _setup_items(db_session)
    monkeypatch.setattr(admin_mod, "load_odata_config", lambda: {"base_url": "http://1c/odata"})
    monkeypatch.setattr(admin_mod, "_fetch_balance_rows", lambda config: [])
    client = _client(db_session)

    resp = client.post("/api/v1/item-ledger/admin/seed", params={"dry_run": "false"})
    assert resp.status_code == 502
    assert _counts(db_session) == (0, 0, 0)


def test_no_odata_config_400(db_session, monkeypatch):
    monkeypatch.setattr(admin_mod, "load_odata_config", lambda: {})
    client = _client(db_session)
    assert client.post("/api/v1/item-ledger/admin/seed").status_code == 400


def test_operator_accept_endpoint_wires_explicit_generation_and_commits(
    db_session, monkeypatch
):
    observed = {}

    def fake_accept(db, generation_id, **kwargs):
        observed.update(generation_id=generation_id, **kwargs)
        return {"status": "accepted", "valid": True}

    fake_client = object()
    monkeypatch.setattr(admin_mod, "accept_generation_build", fake_accept)
    monkeypatch.setattr(admin_mod, "_odata_client_if_configured", lambda: fake_client)
    client = _client(db_session)

    response = client.post(
        "/api/v1/item-ledger/admin/generations/accept",
        json={
            "generation_id": 17,
            "replay_from": "2026-07-01T00:00:00Z",
            "explicit_empty_physical": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["ledger_generation"] == 17
    assert response.json()["truth_status"] == "accepted"
    assert response.json()["ready"] is True
    assert observed == {
        "generation_id": 17,
        "replay_from": datetime(2026, 7, 1, tzinfo=timezone.utc),
        "odata_client": fake_client,
        "explicit_empty_physical": False,
    }


def test_seeded_generation_reaches_first_accepted_truth_through_operator_endpoint(
    db_session, seeded_odata, monkeypatch
):
    _setup_items(db_session)
    monkeypatch.setattr(admin_mod, "_odata_client_if_configured", lambda: None)
    client = _client(db_session)
    seeded = client.post(
        "/api/v1/item-ledger/admin/seed",
        params={"dry_run": "false"},
    )
    assert seeded.status_code == 200
    generation_id = seeded.json()["ledger_generation_id"]

    accepted = client.post(
        "/api/v1/item-ledger/admin/generations/accept",
        json={
            "generation_id": generation_id,
            "replay_from": seeded.json()["posting_at"],
        },
    )

    assert accepted.status_code == 200
    assert accepted.json()["truth_status"] == "accepted"
    assert accepted.json()["ledger_generation"] == generation_id
    db_session.expire_all()
    assert db_session.get(models.LedgerGeneration, generation_id).status == "accepted"
    assert (
        db_session.get(models.PlanningTruthState, 1).current_generation_id
        == generation_id
    )


def test_historical_phase0_e2e_bootstrap_import_verify_and_accept(
    db_session, monkeypatch
):
    _setup_items(db_session)
    balance_rows = _rows()

    class EmptyHistoricalOData:
        def _make_request(self, _entity, _params):
            return {"value": []}

    monkeypatch.setattr(
        admin_mod,
        "load_odata_config",
        lambda: {"base_url": "http://1c/odata"},
    )
    monkeypatch.setattr(
        admin_mod,
        "_fetch_balance_at",
        lambda _config, _at: balance_rows,
    )
    monkeypatch.setattr(
        admin_mod,
        "_odata_client_if_configured",
        lambda: EmptyHistoricalOData(),
    )
    client = _client(db_session)

    bootstrapped = client.post(
        "/api/v1/item-ledger/admin/historical-generations/bootstrap",
        json={
            "generation_key": "historical-phase0-e2e",
            "opening_at": "2026-06-01T00:00:00Z",
            "replay_from": "2026-06-01T00:00:00Z",
            "cutoff": "2026-06-02T00:00:00Z",
        },
    )
    assert bootstrapped.status_code == 200
    generation_id = bootstrapped.json()["ledger_generation_id"]
    assert bootstrapped.json()["opening"]["entries_created"] == 2

    imported = client.post(
        f"/api/v1/item-ledger/admin/historical-generations/{generation_id}/import",
        json={"max_windows": 1, "window_hours": 24},
    )
    assert imported.status_code == 200, imported.json()
    assert imported.json()["complete"] is True
    assert imported.json()["windows_completed"] == 1

    verified = client.post(
        f"/api/v1/item-ledger/admin/historical-generations/"
        f"{generation_id}/verify-balance",
    )
    assert verified.status_code == 200
    assert verified.json()["valid"] is True
    assert verified.json()["mismatched"] == 0

    wrong_lineage = client.post(
        "/api/v1/item-ledger/admin/generations/accept",
        json={
            "generation_id": generation_id,
            "replay_from": "2026-06-01T00:00:01Z",
        },
    )
    assert wrong_lineage.status_code == 409
    assert "sealed bootstrap lineage" in wrong_lineage.json()["detail"]

    accepted = client.post(
        "/api/v1/item-ledger/admin/generations/accept",
        json={
            "generation_id": generation_id,
            "replay_from": "2026-06-01T00:00:00Z",
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["truth_status"] == "accepted"
    assert (
        db_session.get(models.PlanningTruthState, 1).current_generation_id
        == generation_id
    )


def test_operator_accept_endpoint_returns_conflict_and_rolls_back(
    db_session, monkeypatch
):
    def fail_accept(*_args, **_kwargs):
        raise admin_mod.GenerationValidationError("candidate is incomplete")

    monkeypatch.setattr(admin_mod, "accept_generation_build", fail_accept)
    monkeypatch.setattr(admin_mod, "_odata_client_if_configured", lambda: None)
    client = _client(db_session)

    response = client.post(
        "/api/v1/item-ledger/admin/generations/accept",
        json={
            "generation_id": 17,
            "replay_from": "2026-07-01T00:00:00Z",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "candidate is incomplete"


def test_operator_invalidate_endpoint_fail_closes_current_truth(db_session):
    cutoff = datetime(2026, 7, 24, tzinfo=timezone.utc)
    generation = models.LedgerGeneration(
        generation_key="operator-invalidate",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        source_watermarks={},
        capabilities={},
        physical_import_batch=models.PhysicalImportBatch(
            batch_key="operator-invalidate-batch",
            status="completed",
            cutoff=cutoff,
            source_watermarks={},
        ),
        algorithm_version="test",
    )
    db_session.add(generation)
    db_session.flush()
    db_session.add(models.PlanningTruthState(
        id=1, current_generation_id=generation.id,
    ))
    db_session.commit()
    client = _client(db_session)

    response = client.post(
        "/api/v1/item-ledger/admin/generations/invalidate",
        json={
            "expected_generation_id": generation.id,
            "status": "stale",
            "reason": "operator freshness decision",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "ledger_generation": generation.id,
        "truth_status": "stale",
        "ready": False,
        "reason": "operator freshness decision",
    }
    db_session.expire_all()
    assert db_session.get(models.LedgerGeneration, generation.id).status == "stale"
    assert (
        db_session.get(models.PlanningTruthState, 1).current_generation_id
        == generation.id
    )


def test_operator_invalidate_endpoint_rejects_wrong_generation(db_session):
    client = _client(db_session)

    response = client.post(
        "/api/v1/item-ledger/admin/generations/invalidate",
        json={
            "expected_generation_id": 999,
            "status": "rejected",
            "reason": "wrong target",
        },
    )

    assert response.status_code == 409
    assert "no current generation" in response.json()["detail"]
