""" — per-item item-ledger read API (the "nomenclature card").

Read-only endpoints over the accepted Item Ledger. These assert the
response shapes + key numbers on a small seeded fixture: the  position math,
 movement sorting/pagination/filtering,  reservation coverage,  the event
provenance thread (+ cross-item 404), and 404 on unknown item.
Nothing here exercises the compute core — pure SELECT endpoints.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.routers.item_ledger import (
    ItemLedgerMovementsResponse,
    ItemLedgerPositionResponse,
    ItemLedgerReservationEventsResponse,
    ItemLedgerReservationsResponse,
    router,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    imported = models.PhysicalImportBatch(
        batch_key="router-physical",
        status="completed",
        source_watermarks={"fixture": "item-ledger-router"},
        completed_at=dt.datetime(2026, 7, 23),
    )
    generation = models.LedgerGeneration(
        generation_key="router-generation",
        status="accepted",
        cutoff=dt.datetime(2026, 7, 23, 23, 59),
        source_watermarks={},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
            "future_supply": True,
        },
        physical_import_batch=imported,
        algorithm_version="tests/1",
        accepted_at=dt.datetime(2026, 7, 23, 23, 59),
    )
    session.add(generation)
    session.flush()
    session.add(models.PlanningTruthState(
        id=1, current_generation_id=generation.id,
    ))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client(db_session):
    app = FastAPI()
    app.include_router(router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# seed helpers
# ---------------------------------------------------------------------------
def _item(db, code, method="Производство"):
    it = models.Item(item_code=code, item_name=f"Item {code}", item_ref1c=f"ref-{code}",
                     replenishment_method=method)
    db.add(it)
    db.flush()
    return it


def test_openapi_exposes_strict_response_models(client):
    schema = client.app.openapi()
    expected = {
        "/api/v1/item-ledger/{item_id}/position": "ItemLedgerPositionResponse",
        "/api/v1/item-ledger/{item_id}/movements": "ItemLedgerMovementsResponse",
        "/api/v1/item-ledger/{item_id}/reservations": "ItemLedgerReservationsResponse",
        "/api/v1/item-ledger/{item_id}/reservations/{reservation_id}/events": "ItemLedgerReservationEventsResponse",
    }
    for path, model in expected.items():
        response_schema = schema["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert response_schema == {"$ref": f"#/components/schemas/{model}"}


def _wh(db, ref, name, *, selected=True, finished_goods=False, ignored=False):
    db.add(models.StockWarehouse(warehouse_ref1c=ref, warehouse_name=name,
                                 is_selected=selected, is_finished_goods=finished_goods))
    if ignored:
        db.add(models.IgnoredWarehouse(warehouse_ref1c=ref, warehouse_name=name))
    db.flush()


def _bin(db, item_id, wh, qty, pending=0.0):
    generation_id = db.get(models.PlanningTruthState, 1).current_generation_id
    db.add(models.StockBin(ledger_generation_id=generation_id,
                           item_id=item_id, warehouse_ref1c=wh, on_hand=qty,
                           reconcile_pending_qty=pending))
    db.flush()


def _sle(db, item_id, wh, qty, qty_after, posting_at, kind, rref, line, src="document_pull"):
    generation = db.get(models.PlanningTruthState, 1).current_generation
    e = models.StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash=f"router:{rref}:{line}:{item_id}:{qty}"[:64],
        item_id=item_id, warehouse_ref1c=wh, qty=qty, qty_after=qty_after,
        posting_at=posting_at, record_type="Receipt" if qty > 0 else "Expense",
        movement_kind=kind, recorder_type="Document_Test", recorder_ref=rref,
        line_no=str(line), ingest_source=src, active=True,
    )
    db.add(e)
    db.flush()
    return e


def _res(db, item_id, req_id, reserved, *, mode="buy", realized=0.0, status="active",
         run_id=None, cov_oh=0.0, cov_sup=0.0, cov_wip=0.0, uncovered=0.0, cov_state="uncovered"):
    generation_id = db.get(models.PlanningTruthState, 1).current_generation_id
    e = models.ReservationEntry(
        ledger_generation_id=generation_id,
        item_id=item_id, requirement_id=req_id, run_id=run_id, freeze_version=0,
        priority_period_from=dt.date(2026, 8, 1), priority_period_to=dt.date(2026, 8, 31),
        realization_mode=mode, reserved_qty=reserved, realized_qty=realized,
        covered_from_stock_at_freeze_qty=cov_oh,
        replenishment_required_qty=max(Decimal(str(reserved)) - Decimal(str(cov_oh)), Decimal("0")),
        replenishment_received_qty=realized,
        lifecycle_status=status,
    )
    db.add(e)
    db.flush()
    return e


def _event(db, res, kind, *, reserved_delta=0.0, realized_delta=0.0, sle_id=None,
           fact_ref="", match_rule="", cycle_id="cyc-1", key=""):
    db.add(models.ReservationEvent(
        ledger_generation_id=res.ledger_generation_id,
        reservation_id=res.id, item_id=res.item_id, event_kind=kind,
        reserved_delta=reserved_delta, realized_delta=realized_delta, sle_id=sle_id,
        fact_ref=fact_ref, match_rule=match_rule, cycle_id=cycle_id,
        idempotency_key=key or f"{kind}:{res.id}:{sle_id}",
    ))
    db.flush()


@pytest.fixture()
def seeded(db_session):
    db = db_session
    _wh(db, "W1", "Цех 1")
    _wh(db, "W2", "Цех 2")
    _wh(db, "WGP", "Склад ГП", finished_goods=True)  # excluded from pool

    a = _item(db, "00000063")
    b = _item(db, "00000099")

    # on_hand: 300 + 35.144 over the contour; the ГП bin (1000) is excluded.
    _bin(db, a.item_id, "W1", 300.0)
    _bin(db, a.item_id, "W2", 35.144)
    _bin(db, a.item_id, "WGP", 1000.0)

    # movements — 3 active SLE across warehouses, out of posting order on insert.
    _sle(db, a.item_id, "W1", 40.0, 340.0, dt.datetime(2026, 7, 21, 9, 40, 3), "receipt", "rec-2", 1)
    _sle(db, a.item_id, "W1", -40.0, 300.0, dt.datetime(2026, 7, 20, 8, 0, 0), "assembly_out", "rec-1", 2)
    _sle(db, a.item_id, "W2", 35.144, 35.144, dt.datetime(2026, 7, 22, 10, 0, 0), "receipt", "rec-3", 1,
         src="balance_reconcile")
    # a movement for item B (must never leak into A's tape)
    _sle(db, b.item_id, "W1", 5.0, 5.0, dt.datetime(2026, 7, 20, 0, 0, 0), "receipt", "recB", 1)

    # reservations on A: two buy replenishments plus one make replenishment.
    r1 = _res(db, a.item_id, 55831, 270.64, realized=79.57, run_id=17,
              cov_oh=120.0, cov_sup=71.07, uncovered=0.0, cov_state="covered")
    _res(db, a.item_id, 55832, 335.13, realized=0.0, run_id=17,
         cov_oh=0.0, uncovered=335.13, cov_state="uncovered")
    _res(db, a.item_id, 55833, 100.0, mode="make", run_id=17, cov_state="covered")
    # a reservation on B (for cross-item event 404)
    rb = _res(db, b.item_id, 70001, 10.0)

    # events on r1: open + realize (realize links to a physical SLE).
    _event(db, r1, "open", reserved_delta=270.64, key="open:55831")
    _event(db, r1, "realize", realized_delta=79.57, sle_id=1, fact_ref="rec-1",
           match_rule="pegged", key="realize:55831:1")

    db.commit()
    return {"a": a.item_id, "b": b.item_id, "r1": r1.id, "rb": rb.id}


# ---------------------------------------------------------------------------
#  position
# ---------------------------------------------------------------------------
def test_position_math_and_shape(client, seeded):
    r = client.get(f"/api/v1/item-ledger/{seeded['a']}/position")
    assert r.status_code == 200
    d = r.json()
    ItemLedgerPositionResponse.model_validate(d)
    assert d["truth_meta"]["truth_status"] == "accepted"
    assert d["truth_meta"]["ledger_generation"] == 1
    assert d["item_id"] == seeded["a"]
    assert d["item_code"] == "00000063"
    assert d["pool_key"] == f"{seeded['a']}::default"
    assert d["on_hand"] == pytest.approx(335.144)
    # ГП warehouse excluded; two contour warehouses summing to on_hand.
    whs = {w["warehouse_ref1c"]: w["qty"] for w in d["on_hand_by_warehouse"]}
    assert whs == pytest.approx({"W1": 300.0, "W2": 35.144})
    assert d["reserved_soft"] == pytest.approx(705.77)
    assert d["available"] == pytest.approx(335.144 - 705.77)
    # Orders and historical coverage caches are not physical incoming facts.
    assert d["incoming_supplier"] == pytest.approx(0.0)
    assert d["incoming_wip"] == pytest.approx(0.0)
    assert d["projected"] == pytest.approx(335.144 - 705.77)
    assert d["uncovered"] == pytest.approx(705.77 - 335.144)
    assert d["flags"]["on_hand_negative"] is False
    assert d["flags"]["has_uncovered"] is True
    assert d["flags"]["reconcile_pending"] is False


def test_position_unknown_item_404(client, seeded):
    assert client.get("/api/v1/item-ledger/999999/position").status_code == 404


# ---------------------------------------------------------------------------
#  movements
# ---------------------------------------------------------------------------
def test_movements_sorted_and_scoped(client, seeded):
    d = client.get(f"/api/v1/item-ledger/{seeded['a']}/movements").json()
    ItemLedgerMovementsResponse.model_validate(d)
    assert d["truth_meta"]["truth_status"] == "accepted"
    assert d["truth_meta"]["ledger_generation"] == 1
    assert d["total"] == 3  # item B's row excluded
    ats = [row["posting_at"] for row in d["rows"]]
    assert ats == sorted(ats)  # (posting_at, id) ascending
    first = d["rows"][0]
    assert first["movement_kind"] == "assembly_out"
    assert first["qty"] == pytest.approx(-40.0)
    assert first["qty_after"] == pytest.approx(300.0)
    assert first["warehouse_name"] == "Цех 1"
    assert first["ingest_source"] == "document_pull"


def test_movements_pagination(client, seeded):
    d = client.get(f"/api/v1/item-ledger/{seeded['a']}/movements?limit=2&offset=0").json()
    ItemLedgerMovementsResponse.model_validate(d)
    assert d["total"] == 3 and len(d["rows"]) == 2
    d2 = client.get(f"/api/v1/item-ledger/{seeded['a']}/movements?limit=2&offset=2").json()
    ItemLedgerMovementsResponse.model_validate(d2)
    assert d2["total"] == 3 and len(d2["rows"]) == 1


def test_movements_warehouse_filter(client, seeded):
    d = client.get(f"/api/v1/item-ledger/{seeded['a']}/movements?warehouse_ref1c=W2").json()
    ItemLedgerMovementsResponse.model_validate(d)
    assert d["total"] == 1
    assert d["rows"][0]["warehouse_ref1c"] == "W2"
    assert d["rows"][0]["ingest_source"] == "balance_reconcile"


def test_movements_date_filter(client, seeded):
    d = client.get(
        f"/api/v1/item-ledger/{seeded['a']}/movements?date_from=2026-07-21&date_to=2026-07-21"
    ).json()
    ItemLedgerMovementsResponse.model_validate(d)
    assert d["total"] == 1
    assert d["rows"][0]["recorder_ref"] == "rec-2"


def test_movements_unknown_item_404(client, seeded):
    assert client.get("/api/v1/item-ledger/999999/movements").status_code == 404


# ---------------------------------------------------------------------------
#  reservations
# ---------------------------------------------------------------------------
def test_reservations_shape_and_coverage(client, seeded):
    d = client.get(f"/api/v1/item-ledger/{seeded['a']}/reservations").json()
    ItemLedgerReservationsResponse.model_validate(d)
    assert d["truth_meta"]["truth_status"] == "accepted"
    assert d["truth_meta"]["ledger_generation"] == 1
    assert len(d["rows"]) == 3  # 2 buy + 1 make
    by_req = {row["requirement_id"]: row for row in d["rows"]}
    r1 = by_req[55831]
    assert r1["reservation_id"] == seeded["r1"]
    assert r1["run_id"] == 17
    assert r1["realization_mode"] == "buy"
    assert r1["covered_from_stock_at_freeze_qty"] == pytest.approx(120.0)
    assert r1["replenishment_required_qty"] == pytest.approx(150.64)
    assert r1["replenishment_received_qty"] == pytest.approx(79.57)
    assert r1["replenishment_remaining_qty"] == pytest.approx(71.07)
    assert r1["priority"] == {"period_from": "2026-08-01", "period_to": "2026-08-31"}
    # the make row is present and flagged as make.
    assert by_req[55833]["realization_mode"] == "make"


def test_reservations_status_and_run_filters(client, seeded):
    d = client.get(f"/api/v1/item-ledger/{seeded['a']}/reservations?status=active").json()
    ItemLedgerReservationsResponse.model_validate(d)
    assert len(d["rows"]) == 3
    d0 = client.get(f"/api/v1/item-ledger/{seeded['a']}/reservations?status=closed").json()
    ItemLedgerReservationsResponse.model_validate(d0)
    assert d0["rows"] == []
    dr = client.get(f"/api/v1/item-ledger/{seeded['a']}/reservations?run_id=999").json()
    ItemLedgerReservationsResponse.model_validate(dr)
    assert dr["rows"] == []


def test_reservations_unknown_item_404(client, seeded):
    assert client.get("/api/v1/item-ledger/999999/reservations").status_code == 404


# ---------------------------------------------------------------------------
#  events
# ---------------------------------------------------------------------------
def test_events_thread(client, seeded):
    d = client.get(
        f"/api/v1/item-ledger/{seeded['a']}/reservations/{seeded['r1']}/events"
    ).json()
    ItemLedgerReservationEventsResponse.model_validate(d)
    assert d["truth_meta"]["truth_status"] == "accepted"
    assert d["truth_meta"]["ledger_generation"] == 1
    kinds = [e["event_kind"] for e in d["rows"]]
    assert kinds == ["open", "realize"]
    realize = d["rows"][1]
    assert realize["realized_delta"] == pytest.approx(79.57)
    assert realize["sle_id"] == 1           # links the event to the physical movement
    assert realize["match_rule"] == "pegged"
    assert realize["fact_ref"] == "rec-1"


def test_events_cross_item_404(client, seeded):
    # reservation rb belongs to item B → 404 when queried under item A.
    r = client.get(
        f"/api/v1/item-ledger/{seeded['a']}/reservations/{seeded['rb']}/events"
    )
    assert r.status_code == 404


def test_events_unknown_reservation_404(client, seeded):
    r = client.get(f"/api/v1/item-ledger/{seeded['a']}/reservations/999999/events")
    assert r.status_code == 404


def test_position_is_unavailable_when_the_generation_has_no_future_supply(
    client, db_session, seeded
):
    """CANON rule 7: an unanswerable "incoming" is unavailable, never zero."""
    generation = db_session.query(models.LedgerGeneration).one()
    generation.capabilities = {
        name: enabled
        for name, enabled in dict(generation.capabilities or {}).items()
        if name != "future_supply"
    }
    db_session.commit()

    r = client.get(f"/api/v1/item-ledger/{seeded['a']}/position")

    assert r.status_code == 409
    assert "future_supply" in r.json()["detail"]["reason"]
