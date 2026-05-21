"""Tests for one_c_piece_order_export (Document_СдельныйНаряд)."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.models import (
    DefaultSpecification,
    Item,
    Operation,
    ProductionManufacture,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionStage,
    SpecOperation,
    Specification,
    SyncLink,
)
from app.services import one_c_piece_order_export as exporter
from app.services.production_control import produce_line


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_item(db, *, code: str, ref1c: str | None) -> Item:
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


def _mk_spec(db, *, ref1c: str | None = None, name: str = "S") -> Specification:
    spec = Specification(spec_code=f"SC-{name}", spec_name=name, spec_ref1c=ref1c)
    db.add(spec)
    db.flush()
    return spec


def _mk_stage(db, *, name: str, ref1c: str | None) -> ProductionStage:
    stage = ProductionStage(stage_name=name, stage_ref1c=ref1c, stage_order=1)
    db.add(stage)
    db.flush()
    return stage


def _mk_operation(db, *, name: str, ref1c: str | None, time_norm: float = 0.5) -> Operation:
    op = Operation(operation_name=name, operation_ref1c=ref1c, time_norm=time_norm)
    db.add(op)
    db.flush()
    return op


def _setup_full(db, *, qty: float = 4, with_op_ref: bool = True):
    """Item with a spec linking one operation. Returns the manufacture_id
    after produce_line."""
    item = _mk_item(db, code="PCS", ref1c="item-ref-pcs")
    spec = _mk_spec(db, ref1c="spec-ref-pcs", name="PCS spec")
    db.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))

    stage = _mk_stage(db, name="Stage A", ref1c="stage-ref-a")
    op = _mk_operation(
        db, name="Operation A", ref1c=("op-ref-a" if with_op_ref else None), time_norm=0.5
    )
    db.add(
        SpecOperation(
            spec_id=spec.spec_id,
            operation_id=op.operation_id,
            stage_id=stage.stage_id,
            time_norm=0.25,
        )
    )

    order = ProductionOrder(
        order_number="O-PCS",
        order_date=datetime(2026, 5, 20),
        is_posted=True,
        deletion_mark=False,
        order_ref1c="order-ref-pcs",
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=qty,
        produced_qty=0,
        remaining_qty=qty,
        spec_id=spec.spec_id,
    )
    db.add(product)
    db.flush()
    db.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="ready",
            issue_status="not_requested",
        )
    )
    db.commit()

    r = produce_line(db, product.product_id, qty=qty, executor="ivanov")
    return product, r["manufacture_id"]


class _FakeClient:
    def __init__(self, *, ref_key: str = "piece-ref-key", fail: bool = False) -> None:
        self.ref_key = ref_key
        self.fail = fail
        self.posts: list = []

    def post(self, entity, payload, **_):
        self.posts.append((entity, payload))
        if self.fail:
            raise RuntimeError("simulated 1C failure")
        return {"Ref_Key": self.ref_key}


def _stub_config(monkeypatch, *, base_url: str):
    monkeypatch.setattr(
        exporter,
        "_load_odata_config",
        lambda: {"base_url": base_url, "username": "u", "password": "p"},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dry_run_emits_full_piece_order_payload(db_session, monkeypatch):
    db = db_session
    product, mid = _setup_full(db, qty=4)

    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: pytest.fail("Network must not be touched in dry-run"),
    )

    result = exporter.export_piece_orders_to_1c(db, [mid], dry_run=True)
    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert result["manufactures_eligible"] == 1
    [env] = result["payloads"]
    p = env["payload"]
    assert p["Posted"] is False
    assert p["Закрыт"] is False
    assert p["Number"].startswith("PN")
    assert p["ЗаказНаПроизводство_Key"] == "order-ref-pcs"
    assert p["ПоложениеИсполнителя"] == "ВШапке"

    [line] = p["Операции"]
    assert line["Номенклатура_Key"] == "item-ref-pcs"
    assert line["Операция_Key"] == "op-ref-a"
    assert line["Этап_Key"] == "stage-ref-a"
    assert line["Спецификация_Key"] == "spec-ref-pcs"
    assert float(line["КоличествоПлан"]) == 4.0
    assert float(line["КоличествоФакт"]) == 4.0
    assert float(line["НормаВремени"]) == 0.25  # spec_op.time_norm wins
    assert float(line["Нормочасы"]) == 1.0  # 4 * 0.25
    assert float(line["Расценка"]) == 0.0
    assert float(line["Стоимость"]) == 0.0
    assert line["ЗаказНаПроизводство_Key"] == "order-ref-pcs"

    assert db.query(SyncLink).filter_by(source_doctype="piece_order").count() == 0


def test_operation_without_ref1c_still_emitted_without_operation_key(db_session, monkeypatch):
    """Missing Operation.operation_ref1c -> line is still emitted, just
    without Операция_Key so 1С admin can fill it."""
    db = db_session
    product, mid = _setup_full(db, qty=2, with_op_ref=False)

    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: pytest.fail("no net"))
    result = exporter.export_piece_orders_to_1c(db, [mid], dry_run=True)
    [env] = result["payloads"]
    [line] = env["payload"]["Операции"]
    assert "Операция_Key" not in line
    # Other refs still present.
    assert line["Номенклатура_Key"] == "item-ref-pcs"
    assert line["Этап_Key"] == "stage-ref-a"


def test_no_spec_operations_skipped(db_session, monkeypatch):
    """If the spec has zero SpecOperation rows, the manufacture is skipped
    with an explicit reason — sending a piece order with empty Операции[]
    doesn't make sense."""
    db = db_session
    item = _mk_item(db, code="NOOP", ref1c="item-ref-noop")
    spec = _mk_spec(db, ref1c="spec-noop", name="No ops")
    db.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    order = ProductionOrder(
        order_number="O-NOOP",
        order_date=datetime(2026, 5, 20),
        is_posted=True,
        deletion_mark=False,
        order_ref1c="order-ref-noop",
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=2,
        produced_qty=0,
        remaining_qty=2,
        spec_id=spec.spec_id,
    )
    db.add(product)
    db.flush()
    db.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="ready",
            issue_status="not_requested",
        )
    )
    db.commit()
    mid = produce_line(db, product.product_id, qty=2)["manufacture_id"]

    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: pytest.fail("no net"))
    result = exporter.export_piece_orders_to_1c(db, [mid], dry_run=True)
    assert result["manufactures_eligible"] == 0
    assert result["payloads"] == []
    assert any(
        "SpecOperation" in (s.get("reason") or "") for s in result["skipped_rows"]
    )


def test_demo_guard_refuses_non_demo_without_override(db_session, monkeypatch):
    db = db_session
    product, mid = _setup_full(db, qty=1)

    _stub_config(monkeypatch, base_url="http://erp-prod/odata/unf")
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    with pytest.raises(PermissionError):
        exporter.export_piece_orders_to_1c(db, [mid], dry_run=False)
    assert fake.posts == []

    result = exporter.export_piece_orders_to_1c(
        db, [mid], dry_run=False, allow_production=True
    )
    assert result["manufactures_created"] == 1


def test_successful_export_stamps_sync_link(db_session, monkeypatch):
    db = db_session
    product, mid = _setup_full(db, qty=3)

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    monkeypatch.setattr(
        exporter, "OData1CClient", lambda **_: _FakeClient(ref_key="piece-ref-ok")
    )

    result = exporter.export_piece_orders_to_1c(db, [mid], dry_run=False)
    assert result["status"] == "ok"
    assert result["manufactures_created"] == 1
    link = (
        db.query(SyncLink)
        .filter_by(
            source_doctype="piece_order",
            source_id=mid,
            target_entity=exporter.PIECE_ORDER_ENTITY,
        )
        .one()
    )
    assert link.status == "success"
    assert link.target_ref_key == "piece-ref-ok"
    assert link.target_number.startswith("PN")
    assert link.last_synced_at is not None


def test_second_export_is_noop(db_session, monkeypatch):
    db = db_session
    product, mid = _setup_full(db, qty=2)
    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient(ref_key="piece-ref-reuse")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    exporter.export_piece_orders_to_1c(db, [mid], dry_run=False)
    assert len(fake.posts) == 1
    second = exporter.export_piece_orders_to_1c(db, [mid], dry_run=False)
    assert second["manufactures_created"] == 0
    assert second["manufactures_already_linked"] == 1
    assert len(fake.posts) == 1


def test_skipped_for_invalid_inputs(db_session, monkeypatch):
    db = db_session
    # 1) Item with no ref1c -> skipped on item check
    item_noref = _mk_item(db, code="NR", ref1c=None)
    spec_nr = _mk_spec(db, ref1c="spec-nr", name="NR spec")
    db.add(DefaultSpecification(item_id=item_noref.item_id, spec_id=spec_nr.spec_id))
    op = _mk_operation(db, name="op", ref1c="op-nr")
    db.add(
        SpecOperation(
            spec_id=spec_nr.spec_id, operation_id=op.operation_id, time_norm=0.1
        )
    )
    order = ProductionOrder(
        order_number="O-NR",
        order_date=datetime(2026, 5, 20),
        is_posted=True,
        deletion_mark=False,
        order_ref1c="ord-nr",
    )
    db.add(order)
    db.flush()
    p = ProductionProduct(
        order_id=order.order_id,
        item_id=item_noref.item_id,
        line_number=1,
        quantity=1,
        produced_qty=0,
        remaining_qty=1,
        spec_id=spec_nr.spec_id,
    )
    db.add(p)
    db.flush()
    db.add(ProductionOrderLineState(product_id=p.product_id, status="ready", issue_status="not_requested"))
    db.commit()
    mid_nr = produce_line(db, p.product_id, qty=1)["manufacture_id"]

    # 2) Cancelled manufacture -> skipped
    product, mid_can = _setup_full(db, qty=2)
    m_can = db.query(ProductionManufacture).filter_by(manufacture_id=mid_can).one()
    m_can.status = "cancelled"
    db.commit()

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_piece_orders_to_1c(
        db, [mid_nr, mid_can, 999_999], dry_run=False
    )
    reasons = [r["reason"] for r in result["skipped_rows"]]
    assert any("item_ref1c" in r for r in reasons)
    assert any("cancelled" in r for r in reasons)
    assert any("не найден" in r for r in reasons)
    assert fake.posts == []


def test_fallback_to_default_specification_when_product_spec_missing(db_session, monkeypatch):
    """If ProductionProduct.spec_id is NULL (e.g. legacy 1С-synced lines),
    the exporter should fall back to DefaultSpecification of the item."""
    db = db_session
    item = _mk_item(db, code="FB", ref1c="item-fb")
    spec = _mk_spec(db, ref1c="spec-fb", name="FB spec")
    db.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    op = _mk_operation(db, name="opfb", ref1c="op-fb")
    db.add(
        SpecOperation(spec_id=spec.spec_id, operation_id=op.operation_id, time_norm=0.2)
    )

    order = ProductionOrder(
        order_number="O-FB",
        order_date=datetime(2026, 5, 20),
        is_posted=True,
        deletion_mark=False,
        order_ref1c="ord-fb",
    )
    db.add(order)
    db.flush()
    p = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=2,
        produced_qty=0,
        remaining_qty=2,
        spec_id=None,  # NULL on purpose
    )
    db.add(p)
    db.flush()
    db.add(ProductionOrderLineState(product_id=p.product_id, status="ready", issue_status="not_requested"))
    db.commit()
    mid = produce_line(db, p.product_id, qty=2)["manufacture_id"]

    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: pytest.fail("no net"))
    result = exporter.export_piece_orders_to_1c(db, [mid], dry_run=True)
    assert result["manufactures_eligible"] == 1
    [env] = result["payloads"]
    [line] = env["payload"]["Операции"]
    assert line["Операция_Key"] == "op-fb"
    assert line["Спецификация_Key"] == "spec-fb"
