"""Tests for produce_line + one_c_manufacture_export."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.models import (
    Item,
    ProductionManufacture,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SyncLink,
)
from app.services import one_c_manufacture_export as exporter
from app.services.production_control_production_flow import produce_line


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_item(db, *, code: str, ref1c: str | None = None) -> Item:
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


def _mk_product(db, item: Item, *, qty: float = 10.0) -> ProductionProduct:
    order = ProductionOrder(
        order_number=f"O-{item.item_id}",
        order_date=datetime(2026, 5, 20),
        is_posted=True,
        deletion_mark=False,
        order_ref1c=f"order-ref-{item.item_id}",
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
    return product


class _FakeClient:
    def __init__(self, *, ref_key: str = "manuf-ref-key", fail: bool = False) -> None:
        self.ref_key = ref_key
        self.fail = fail
        self.posts: list = []

    def post(self, entity, payload, **_):
        self.posts.append((entity, payload))
        if self.fail:
            raise RuntimeError("simulated 1C failure")
        return {"Ref_Key": self.ref_key}


def _stub_config(monkeypatch, *, base_url: str) -> None:
    monkeypatch.setattr(
        exporter,
        "_load_odata_config",
        lambda: {"base_url": base_url, "username": "u", "password": "p"},
    )


# ---------------------------------------------------------------------------
# produce_line
# ---------------------------------------------------------------------------


def test_produce_full_marks_line_produced(db_session):
    db = db_session
    item = _mk_item(db, code="PRD-FULL", ref1c="ref-prd-full")
    product = _mk_product(db, item, qty=5.0)

    result = produce_line(db, product.product_id, qty=5, executor="иван")
    assert result["status"] == "ok"
    assert result["qty"] == 5.0
    assert result["produced_qty_total"] == 5.0
    assert result["remaining_qty"] == 0.0
    assert result["line_status"] == "produced"

    db.refresh(product)
    assert float(product.produced_qty) == 5.0
    assert float(product.remaining_qty) == 0.0

    state = (
        db.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one()
    )
    assert state.status == "produced"

    manufacture = (
        db.query(ProductionManufacture)
        .filter_by(product_id=product.product_id)
        .one()
    )
    assert float(manufacture.qty) == 5.0
    assert manufacture.executor == "иван"
    assert manufacture.status == "draft"


def test_partial_then_remaining_finishes_line(db_session):
    """Two produce_line calls combine into a final 'produced' state."""
    db = db_session
    item = _mk_item(db, code="PRD-PART", ref1c="ref-prd-part")
    product = _mk_product(db, item, qty=7.0)

    r1 = produce_line(db, product.product_id, qty=3, executor="op1")
    assert r1["line_status"] == "produced_partial"
    assert r1["remaining_qty"] == 4.0

    r2 = produce_line(db, product.product_id, qty=4, executor="op2")
    assert r2["line_status"] == "produced"
    assert r2["remaining_qty"] == 0.0

    # Two manufactures, total 7.
    mans = (
        db.query(ProductionManufacture)
        .filter_by(product_id=product.product_id)
        .order_by(ProductionManufacture.manufacture_id.asc())
        .all()
    )
    assert [float(m.qty) for m in mans] == [3.0, 4.0]


def test_produce_more_than_remaining_raises(db_session):
    db = db_session
    item = _mk_item(db, code="PRD-OVER", ref1c="ref-prd-over")
    product = _mk_product(db, item, qty=2.0)

    with pytest.raises(ValueError, match="больше остатка"):
        produce_line(db, product.product_id, qty=3)
    # No partial side-effects: product untouched.
    db.refresh(product)
    assert float(product.produced_qty) == 0.0
    assert float(product.remaining_qty) == 2.0
    assert (
        db.query(ProductionManufacture).filter_by(product_id=product.product_id).count()
        == 0
    )


def test_produce_zero_or_negative_raises(db_session):
    db = db_session
    item = _mk_item(db, code="PRD-ZERO", ref1c="ref-prd-zero")
    product = _mk_product(db, item, qty=4.0)
    for bad in [0, -1, -0.5]:
        with pytest.raises(ValueError):
            produce_line(db, product.product_id, qty=bad)


def test_produce_unknown_product_raises(db_session):
    with pytest.raises(ValueError, match="не найдена"):
        produce_line(db_session, 999_999, qty=1)


# ---------------------------------------------------------------------------
# one_c_manufacture_export
# ---------------------------------------------------------------------------


def test_dry_run_returns_payload_with_order_ref(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="EXP-ITEM", ref1c="item-ref-exp")
    product = _mk_product(db, item, qty=4)
    r = produce_line(db, product.product_id, qty=4)
    mid = r["manufacture_id"]

    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: pytest.fail("Network must not be touched in dry-run"),
    )

    result = exporter.export_manufactures_to_1c(db, [mid], dry_run=True)
    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert result["manufactures_eligible"] == 1
    [pl] = result["payloads"]
    payload = pl["payload"]
    assert payload["Posted"] is False
    assert payload["Number"].startswith("PM")
    # Manufacture is created on the basis of the parent production order in 1C.
    assert payload["ЗаказНаПроизводство_Key"] == "order-ref-{}".format(item.item_id)
    assert payload["ДокументОснование"] == "order-ref-{}".format(item.item_id)
    assert payload["ДокументОснование_Type"] == "StandardODATA.Document_ЗаказНаПроизводство"
    [prod_row] = payload["Продукция"]
    assert prod_row["Номенклатура_Key"] == "item-ref-exp"
    assert float(prod_row["Количество"]) == 4.0
    assert (
        db.query(SyncLink).filter_by(source_doctype="manufacture").count() == 0
    )


def test_demo_guard_refuses_non_demo_without_override(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="EXP-GUARD", ref1c="item-ref-guard")
    product = _mk_product(db, item, qty=2)
    mid = produce_line(db, product.product_id, qty=2)["manufacture_id"]

    _stub_config(monkeypatch, base_url="http://erp-prod/odata/unf")
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    with pytest.raises(PermissionError):
        exporter.export_manufactures_to_1c(db, [mid], dry_run=False)
    assert fake.posts == []

    result = exporter.export_manufactures_to_1c(
        db, [mid], dry_run=False, allow_production=True
    )
    assert result["manufactures_created"] == 1


def test_successful_export_stamps_link_and_manufacture(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="EXP-OK", ref1c="item-ref-ok")
    product = _mk_product(db, item, qty=3)
    mid = produce_line(db, product.product_id, qty=3, executor="petrov")["manufacture_id"]

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    monkeypatch.setattr(
        exporter, "OData1CClient", lambda **_: _FakeClient(ref_key="be5ab6fe-manu-ok")
    )

    result = exporter.export_manufactures_to_1c(db, [mid], dry_run=False)
    assert result["status"] == "ok"
    assert result["manufactures_created"] == 1

    m = db.query(ProductionManufacture).filter_by(manufacture_id=mid).one()
    assert m.status == "exported"
    assert m.exported_ref1c == "be5ab6fe-manu-ok"
    assert m.exported_at is not None

    link = (
        db.query(SyncLink)
        .filter_by(
            source_doctype="manufacture",
            source_id=mid,
            target_entity=exporter.MANUFACTURE_ENTITY,
        )
        .one()
    )
    assert link.status == "success"
    assert link.target_ref_key == "be5ab6fe-manu-ok"


def test_second_export_is_noop(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="EXP-DUP", ref1c="item-ref-dup")
    product = _mk_product(db, item, qty=2)
    mid = produce_line(db, product.product_id, qty=2)["manufacture_id"]

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient(ref_key="reuse-ref")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    exporter.export_manufactures_to_1c(db, [mid], dry_run=False)
    assert len(fake.posts) == 1

    result = exporter.export_manufactures_to_1c(db, [mid], dry_run=False)
    assert result["manufactures_created"] == 0
    assert result["manufactures_already_linked"] == 1
    assert len(fake.posts) == 1


def test_skips_manufacture_without_parent_order_ref1c(db_session):
    """Per contract: Document_СборкаЗапасов requires ДокументОснование →
    Document_ЗаказНаПроизводство. Without parent's order_ref1c the manufacture
    cannot be exported."""
    item = _mk_item(db_session, code="MF-NOBASE", ref1c="item-ref-nobase")
    product = _mk_product(db_session, item, qty=3)
    mid = produce_line(db_session, product.product_id, qty=3)["manufacture_id"]
    # Clear the parent's order_ref1c.
    m = db_session.query(ProductionManufacture).filter_by(manufacture_id=mid).one()
    m.order.order_ref1c = None
    db_session.commit()

    result = exporter.export_manufactures_to_1c(db_session, [mid], dry_run=True)

    assert result["manufactures_eligible"] == 0
    assert len(result["skipped_rows"]) == 1
    assert "order_ref1c" in result["skipped_rows"][0]["reason"]


def test_skipped_for_invalid_inputs(db_session, monkeypatch):
    db = db_session
    # 1) item with empty ref1c -> skipped
    item_no_ref = _mk_item(db, code="EXP-NOREF", ref1c=None)
    product_no_ref = _mk_product(db, item_no_ref, qty=2)
    mid_no_ref = produce_line(db, product_no_ref.product_id, qty=2)["manufacture_id"]
    # 2) cancelled manufacture -> skipped
    item_can = _mk_item(db, code="EXP-CAN", ref1c="ref-can")
    product_can = _mk_product(db, item_can, qty=1)
    mid_can = produce_line(db, product_can.product_id, qty=1)["manufacture_id"]
    m_can = db.query(ProductionManufacture).filter_by(manufacture_id=mid_can).one()
    m_can.status = "cancelled"
    db.commit()

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_manufactures_to_1c(
        db, [mid_no_ref, mid_can, 999_999], dry_run=False
    )
    reasons = [r["reason"] for r in result["skipped_rows"]]
    assert any("item_ref1c" in r for r in reasons)
    assert any("cancelled" in r for r in reasons)
    assert any("не найден" in r for r in reasons)
    assert fake.posts == []
