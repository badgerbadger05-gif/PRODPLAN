"""Tests for one_c_production_order_export.

Covers:
- dry-run preview returns payloads without touching the network
- demo-DB guard refuses non-demo base_url unless allow_production
- successful write stamps sync_link + production_orders.order_ref1c
- second call is a no-op (sync_link idempotency)
- ineligible orders (1C-synced, deletion_mark, missing item ref1c, missing
  order) are reported in skipped_rows or marked existing
"""
from __future__ import annotations

import datetime as _dt
import json
from datetime import datetime
from pathlib import Path

import pytest

from app.models import (
    Item,
    PlannedOrder,
    PlanningRun,
    ProductionOrder,
    ProductionProduct,
    SyncLink,
)
from app.services import one_c_production_order_export as exporter


# -----------------------------
# Helpers
# -----------------------------


def _mk_run(db) -> PlanningRun:
    run = PlanningRun(status="DONE", config_snapshot=json.dumps({}))
    db.add(run)
    db.flush()
    return run


def _mk_item(db, *, code: str, ref1c: str) -> Item:
    it = Item(
        item_code=code,
        item_name=f"Item {code}",
        item_article=code,
        item_ref1c=ref1c,
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db.add(it)
    db.flush()
    return it


def _mk_mrp_order(db, item, *, run_id: int, qty=5, deletion=False) -> ProductionOrder:
    order = ProductionOrder(
        order_number=f"MRP-{run_id}-{item.item_id}",
        order_date=datetime(2026, 5, 20),
        order_ref1c=None,
        is_posted=False,
        deletion_mark=deletion,
        source="mrp",
        source_run_id=run_id,
    )
    db.add(order)
    db.flush()
    db.add(
        ProductionProduct(
            order_id=order.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=qty,
            produced_qty=0,
            remaining_qty=qty,
        )
    )
    db.commit()
    return order


class _FakeClient:
    """Minimal stand-in for OData1CClient.post."""

    def __init__(self, *, ref_key: str = "fake-1c-ref-key", fail: bool = False) -> None:
        self.ref_key = ref_key
        self.fail = fail
        self.posts: list = []

    def post(self, entity, payload, **_kwargs):
        self.posts.append((entity, payload))
        if self.fail:
            raise RuntimeError("simulated 1C failure")
        return {"Ref_Key": self.ref_key}


def _stub_odata_config(monkeypatch, *, base_url: str) -> None:
    monkeypatch.setattr(
        exporter,
        "_load_odata_config",
        lambda: {"base_url": base_url, "username": "u", "password": "p"},
    )


# -----------------------------
# Tests
# -----------------------------


def test_dry_run_returns_payload_without_touching_network(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="P1", ref1c="11111111-1111-1111-1111-111111111111")
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id, qty=7)

    # Even if config exists, dry-run must not instantiate a client. Stub it
    # to a sentinel that would error on use.
    _stub_odata_config(monkeypatch, base_url="http://1c-demo.local/odata/unf_demo")
    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: pytest.fail("Network client must not be instantiated in dry-run"),
    )

    result = exporter.export_production_orders_to_1c(db, [order.order_id], dry_run=True)

    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert result["orders_eligible"] == 1
    assert result["orders_already_linked"] == 0
    assert result["orders_created"] == 0
    assert result["skipped_rows"] == []

    [pl] = result["payloads"]
    assert pl["order_id"] == order.order_id
    assert pl["payload"]["Posted"] is False
    assert pl["payload"]["Number"].startswith("PP")
    [prod_row] = pl["payload"]["Продукция"]
    assert prod_row["Номенклатура_Key"] == item.item_ref1c
    assert float(prod_row["Количество"]) == 7.0
    assert "PRODPLAN source=production_order/" in pl["payload"]["Комментарий"]

    # No sync_link writes on dry-run.
    assert db.query(SyncLink).count() == 0


def test_demo_base_url_guard_blocks_non_demo_without_override(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="P2", ref1c="22222222-2222-2222-2222-222222222222")
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id)

    _stub_odata_config(monkeypatch, base_url="http://erp-prod.example/odata/unf")  # NOT unf_demo
    # Stub the client to detect accidental writes.
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    with pytest.raises(PermissionError):
        exporter.export_production_orders_to_1c(
            db, [order.order_id], dry_run=False, allow_production=False
        )
    assert fake.posts == []

    # With explicit override the same call goes through.
    result = exporter.export_production_orders_to_1c(
        db, [order.order_id], dry_run=False, allow_production=True
    )
    assert result["orders_created"] == 1
    assert len(fake.posts) == 1


def test_successful_export_stamps_sync_link_and_order_ref1c(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="P3", ref1c="33333333-3333-3333-3333-333333333333")
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id, qty=4)

    _stub_odata_config(monkeypatch, base_url="http://mtzw7/unf_demo/odata")
    fake = _FakeClient(ref_key="1e1f5690-5345-11f1-9dae-9ee51454587f")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_production_orders_to_1c(db, [order.order_id], dry_run=False)

    assert result["status"] == "ok"
    assert result["orders_created"] == 1
    assert result["orders_error"] == 0

    db.refresh(order)
    assert order.order_ref1c == "1e1f5690-5345-11f1-9dae-9ee51454587f"

    link = (
        db.query(SyncLink)
        .filter_by(
            source_system="PRODPLAN",
            source_doctype="production_order",
            source_id=order.order_id,
            target_entity=exporter.PRODUCTION_ORDER_ENTITY,
        )
        .one()
    )
    assert link.status == "success"
    assert link.target_ref_key == "1e1f5690-5345-11f1-9dae-9ee51454587f"
    assert link.target_number is not None
    assert link.payload_hash is not None
    assert link.last_synced_at is not None


def test_second_export_is_noop_due_to_existing_link(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="P4", ref1c="44444444-4444-4444-4444-444444444444")
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id)

    _stub_odata_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient(ref_key="aaaa-existing-ref-key")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    exporter.export_production_orders_to_1c(db, [order.order_id], dry_run=False)
    assert len(fake.posts) == 1

    # Re-call. Same order, success link already there -> entries[].status='existing',
    # no new POST and orders_created=0.
    result = exporter.export_production_orders_to_1c(db, [order.order_id], dry_run=False)
    assert result["orders_created"] == 0
    assert result["orders_already_linked"] == 1
    assert len(fake.posts) == 1  # no additional POST


def test_skipped_rows_for_invalid_orders(db_session, monkeypatch):
    db = db_session
    run = _mk_run(db)

    # (1) 1C-synced order — wrong source, must be skipped
    item_a = _mk_item(db, code="PA", ref1c="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    order_1c = ProductionOrder(
        order_number="1C-ORIGIN",
        order_date=datetime(2026, 5, 20),
        order_ref1c="some-existing-1c-ref",
        deletion_mark=False,
        source="1c",
    )
    db.add(order_1c)
    db.flush()
    db.add(
        ProductionProduct(
            order_id=order_1c.order_id,
            item_id=item_a.item_id,
            line_number=1,
            quantity=1,
            produced_qty=0,
            remaining_qty=1,
        )
    )
    # (2) deletion_mark=True — must be skipped
    item_b = _mk_item(db, code="PB", ref1c="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    order_del = _mk_mrp_order(db, item_b, run_id=run.run_id, deletion=True)
    # (3) Item with empty ref1c — must be skipped
    item_noref = Item(
        item_code="P-NOREF",
        item_name="No ref",
        item_article="P-NOREF",
        item_ref1c=None,
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db.add(item_noref)
    db.flush()
    order_noref = _mk_mrp_order(db, item_noref, run_id=run.run_id)

    db.commit()

    _stub_odata_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_production_orders_to_1c(
        db,
        [order_1c.order_id, order_del.order_id, order_noref.order_id, 999_999],
        dry_run=False,
    )
    assert result["orders_eligible"] == 0
    assert result["orders_created"] == 0
    skip_reasons = [r["reason"] for r in result["skipped_rows"]]
    assert any("source='1c'" in s for s in skip_reasons)
    assert any("deletion_mark" in s for s in skip_reasons)
    assert any("item_ref1c" in s for s in skip_reasons)
    assert any("не найден" in s for s in skip_reasons)
    assert fake.posts == []


def test_partial_failure_keeps_other_orders_committed(db_session, monkeypatch):
    db = db_session
    item_ok = _mk_item(db, code="POK", ref1c="okokokok-okok-okok-okok-okokokokokok")
    item_bad = _mk_item(db, code="PBAD", ref1c="bdbdbdbd-bdbd-bdbd-bdbd-bdbdbdbdbdbd")
    run = _mk_run(db)
    order_ok = _mk_mrp_order(db, item_ok, run_id=run.run_id)
    order_bad = _mk_mrp_order(db, item_bad, run_id=run.run_id)

    _stub_odata_config(monkeypatch, base_url="http://demo/odata/unf_demo")

    call_count = {"n": 0}

    class _SometimesFail:
        def post(self, entity, payload, **_):
            call_count["n"] += 1
            # Second POST fails — order_bad gets recorded as error.
            if call_count["n"] >= 2:
                raise RuntimeError("simulated failure")
            return {"Ref_Key": "ok-ref-key"}

    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: _SometimesFail())

    result = exporter.export_production_orders_to_1c(
        db, [order_ok.order_id, order_bad.order_id], dry_run=False
    )

    assert result["status"] == "partial_error"
    assert result["orders_created"] == 1
    assert result["orders_error"] == 1

    db.refresh(order_ok)
    db.refresh(order_bad)
    assert order_ok.order_ref1c == "ok-ref-key"
    assert order_bad.order_ref1c is None  # failed -> stays unstamped

    link_ok = (
        db.query(SyncLink)
        .filter_by(source_id=order_ok.order_id, target_entity=exporter.PRODUCTION_ORDER_ENTITY)
        .one()
    )
    assert link_ok.status == "success"
    link_bad = (
        db.query(SyncLink)
        .filter_by(source_id=order_bad.order_id, target_entity=exporter.PRODUCTION_ORDER_ENTITY)
        .one()
    )
    assert link_bad.status == "error"
    assert "simulated failure" in (link_bad.last_error or "")
