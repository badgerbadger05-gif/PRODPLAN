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
    DefaultSpecification,
    Item,
    Operation,
    PlannedOrder,
    PlanningRun,
    ProductionOrderLineState,
    ProductionOrder,
    ProductionMaterialIssue,
    ProductionProduct,
    ProductionResource,
    ProductionStage,
    ResourceStage,
    SpecComponent,
    Specification,
    SpecOperation,
    WorkshopWarehouseBinding,
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
        unit=f"unit-ref-{code}",
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

    def __init__(
        self,
        *,
        ref_key: str = "fake-1c-ref-key",
        fail: bool = False,
        existing_docs: list | None = None,
    ) -> None:
        self.ref_key = ref_key
        self.fail = fail
        self.posts: list = []
        self.patches: list = []
        self.operations: list = []
        self.existing_docs = existing_docs or []
        self.get_calls: list = []

    def get_all(self, entity, **kwargs):
        self.get_calls.append((entity, kwargs))
        return list(self.existing_docs)

    def post(self, entity, payload, **_kwargs):
        self.posts.append((entity, payload))
        if self.fail:
            raise RuntimeError("simulated 1C failure")
        return {"Ref_Key": self.ref_key}

    def patch(self, entity_ref, payload, **_kwargs):
        self.patches.append((entity_ref, payload))
        if self.fail:
            raise RuntimeError("simulated 1C failure")
        return {}

    def post_operation(self, operation_path):
        self.operations.append(operation_path)


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
    assert prod_row["ЕдиницаИзмерения"] == item.unit
    assert prod_row["ЕдиницаИзмерения_Type"] == "StandardODATA.Catalog_КлассификаторЕдиницИзмерения"
    assert float(prod_row["Количество"]) == 7.0
    assert "PRODPLAN source=production_order/" in pl["payload"]["Комментарий"]

    # No sync_link writes on dry-run.
    assert db.query(SyncLink).count() == 0


def test_dry_run_payload_includes_materials_operations_and_reserve_warehouse(db_session, monkeypatch):
    db = db_session
    parent = _mk_item(db, code="P-BOM", ref1c="parent-ref")
    component = _mk_item(db, code="C-BOM", ref1c="component-ref")
    spec = Specification(spec_name="Spec BOM", spec_ref1c="spec-ref")
    op = Operation(operation_ref1c="operation-ref", operation_name="Cut", time_norm=0.25)
    stage = ProductionStage(stage_name="Stage BOM", stage_ref1c="stage-ref")
    resource = ProductionResource(resource_name="Workshop BOM")
    db.add_all([spec, op, stage, resource])
    db.flush()
    db.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=2, stage_id=stage.stage_id))
    db.add(SpecOperation(spec_id=spec.spec_id, operation_id=op.operation_id, stage_id=stage.stage_id, time_norm=0.5))
    db.add(ResourceStage(resource_id=resource.resource_id, stage_id=stage.stage_id))
    db.add(
        WorkshopWarehouseBinding(
            workshop_id=resource.resource_id,
            warehouse_ref1c="workshop-warehouse-ref",
            production_warehouse_ref1c="production-warehouse-ref",
        )
    )
    db.flush()
    run = _mk_run(db)
    order = _mk_mrp_order(db, parent, run_id=run.run_id, qty=3)
    product = db.query(ProductionProduct).filter_by(order_id=order.order_id).one()
    product.spec_id = spec.spec_id
    db.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="ready",
            issue_status="not_requested",
            workshop_id=resource.resource_id,
            planned_start_date=_dt.date(2026, 6, 12),
            planned_finish_date=_dt.date(2026, 6, 13),
        )
    )
    db.add(
        ProductionMaterialIssue(
            document_number="MI-BOM",
            product_id=product.product_id,
            order_id=order.order_id,
            status="draft",
            warehouse_ref1c="workshop-warehouse-ref",
            source_warehouse_ref1c="source-warehouse-ref",
        )
    )
    db.commit()

    _stub_odata_config(monkeypatch, base_url="http://1c-demo.local/odata/unf_demo")
    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: pytest.fail("Network client must not be instantiated in dry-run"),
    )
    monkeypatch.setattr(exporter, "_current_1c_datetime", lambda: "2026-05-27T09:58:40")
    monkeypatch.setattr(exporter, "_current_moscow_datetime", lambda: "2026-05-27T10:58:40")

    result = exporter.export_production_orders_to_1c(db, [order.order_id], dry_run=True)
    payload = result["payloads"][0]["payload"]

    assert payload["Date"] == "2026-05-27T09:58:40"
    assert payload["Старт"] == "2026-06-12T10:58:40"
    assert payload["Финиш"] == "2026-06-13T10:58:40"
    assert payload["СтруктурнаяЕдиницаРезерв_Key"] == "workshop-warehouse-ref"
    assert payload["СтруктурнаяЕдиницаПродукции_Key"] == "production-warehouse-ref"
    [prod_row] = payload["Продукция"]
    assert prod_row["СтруктурнаяЕдиница_Key"] == "production-warehouse-ref"
    assert prod_row["Спецификация_Key"] == "spec-ref"
    assert prod_row["КлючСвязи"] == 1
    [stock_row] = payload["Запасы"]
    assert stock_row["Номенклатура_Key"] == component.item_ref1c
    assert stock_row["Количество"] == 6.0
    assert stock_row["ЕдиницаИзмерения"] == component.unit
    assert stock_row["Спецификация_Key"] == "spec-ref"
    assert stock_row["СтруктурнаяЕдиница_Key"] == "workshop-warehouse-ref"
    [operation_row] = payload["Операции"]
    assert operation_row["Операция_Key"] == "operation-ref"
    assert operation_row["КоличествоПлан"] == 3.0
    assert operation_row["НормаВремени"] == 0.5
    assert operation_row["Нормочасы"] == 1.5
    assert operation_row["СтруктурнаяЕдиница_Key"] == "production-warehouse-ref"
    assert operation_row["КлючСвязиПродукция"] == 1
    assert payload["ЗапланированыОперации"] is True


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


def test_empty_local_link_recovers_document_from_1c_origin_marker(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="PX", ref1c="99999999-9999-9999-9999-999999999999")
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id, qty=7)
    preview = exporter.export_production_orders_to_1c(db, [order.order_id], dry_run=True)
    comment = preview["payloads"][0]["payload"]["Комментарий"]
    assert "prodplan-origin=" in comment

    _stub_odata_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient(
        existing_docs=[
            {
                "Ref_Key": "cross-instance-ref",
                "Number": "OTHER",
                "Комментарий": comment,
                "Posted": True,
            }
        ]
    )
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_production_orders_to_1c(
        db, [order.order_id], dry_run=False
    )

    assert result["orders_created"] == 0
    assert result["orders_recovered"] == 1
    assert fake.posts == []
    db.refresh(order)
    assert order.order_ref1c == "cross-instance-ref"
    assert db.query(SyncLink).filter_by(
        source_doctype="production_order", source_id=order.order_id
    ).one().target_ref_key == "cross-instance-ref"


def test_existing_error_link_with_ref_patches_not_posts_duplicate(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="P4R", ref1c="44444444-4444-4444-4444-44444444444a")
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id)

    db.add(SyncLink(
        source_system="PRODPLAN",
        source_doctype="production_order",
        source_id=order.order_id,
        target_system="1C",
        target_entity=exporter.PRODUCTION_ORDER_ENTITY,
        target_number="PP-RETRY",
        payload_hash="old-hash",
        target_ref_key="existing-order-ref",
        status="error",
        last_error="post failed after create",
    ))
    db.commit()

    _stub_odata_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient(ref_key="new-ref-should-not-be-used")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_production_orders_to_1c(db, [order.order_id], dry_run=False)

    assert result["orders_created"] == 1
    assert fake.posts == []
    assert len(fake.patches) == 1
    assert fake.patches[0][0] == "Document_ЗаказНаПроизводство(guid'existing-order-ref')"
    assert fake.operations == [
        "Document_ЗаказНаПроизводство(guid'existing-order-ref')/Post?PostingModeOperational=true"
    ]
    db.refresh(order)
    assert order.order_ref1c == "existing-order-ref"


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
