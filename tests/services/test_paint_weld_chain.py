"""Цепочка открытия «окраска → сварка» (этап 2).

Проверяем:
- вердикт stock_covers → сварка не создаётся (окраска — штатно);
- частичное покрытие → qty сварки уменьшено на эффективный остаток сварной;
- need_weld → пара заказов в правильном порядке (окраска раньше сварки), с
  датами (финиш сварки = старт окраски, старт = финиш − buffer_days участка) и
  «основанием» в комментарии сварочного 1С-документа + локальной связью;
- идемпотентность повтора (нет дублей заказа/связи, нет повторного POST);
- dry-run ничего не пишет.

Мок OData — как в tests/services/test_one_c_production_order_export.py.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.models import (
    DefaultSpecification,
    Item,
    PaintWeldChainLink,
    PaintWeldPair,
    ProductionKind,
    ProductionOrder,
    ProductionResource,
    ResourceProductionKind,
    SpecComponent,
    Specification,
    SyncLink,
)
from app.services import one_c_production_order_export as exporter
from app.services.paint_weld_chain import open_paint_chain

WELD_BUFFER_DAYS = 14


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _item(db, *, code: str, name: str, ref1c: str, stock: float = 0.0) -> Item:
    it = Item(
        item_code=code,
        item_name=name,
        item_article=code,
        item_ref1c=ref1c,
        unit=f"unit-{code}",
        stock_qty=stock,
        replenishment_method="Производство",
        status="active",
    )
    db.add(it)
    db.flush()
    return it


def _setup_pair(db, *, welded_stock: float = 0.0):
    """Painted item (spec component = welded) + welded item + active pair +
    weld workshop (buffer_days=14 via production kind). Returns (painted, welded)."""
    painted = _item(db, code="PNT", name="Кронштейн после покраски", ref1c="ref-painted")
    welded = _item(db, code="WLD", name="Кронштейн после сварки", ref1c="ref-welded", stock=welded_stock)

    # painted default spec = 1 component (welded)
    paint_spec = Specification(spec_name="Окраска кронштейна", spec_ref1c="spec-paint")
    db.add(paint_spec)
    db.flush()
    db.add(DefaultSpecification(item_id=painted.item_id, spec_id=paint_spec.spec_id))
    db.add(SpecComponent(spec_id=paint_spec.spec_id, item_id=welded.item_id, quantity=1, component_type="Сборка"))

    # welded default spec + weld workshop bound via production kind (buffer_days)
    kind = ProductionKind(ref_1c="kind-weld", name="Сварка")
    db.add(kind)
    db.flush()
    weld_spec = Specification(spec_name="Сварка кронштейна", spec_ref1c="spec-weld", production_kind_id=kind.id)
    db.add(weld_spec)
    db.flush()
    db.add(DefaultSpecification(item_id=welded.item_id, spec_id=weld_spec.spec_id))
    weld_resource = ProductionResource(resource_name="Участок сварочный", buffer_days=WELD_BUFFER_DAYS)
    db.add(weld_resource)
    db.flush()
    db.add(ResourceProductionKind(resource_id=weld_resource.resource_id, production_kind_id=kind.id))

    db.add(
        PaintWeldPair(
            painted_item_id=painted.item_id,
            welded_item_id=welded.item_id,
            source="auto",
            is_active=True,
        )
    )
    db.commit()
    return painted, welded


class _FakeClient:
    def __init__(self) -> None:
        self.posts: list = []
        self.patches: list = []
        self.operations: list = []
        self._n = 0

    def post(self, entity, payload, **_kwargs):
        self._n += 1
        self.posts.append((entity, payload))
        return {"Ref_Key": f"ref-1c-{self._n}"}

    def patch(self, entity_ref, payload, **_kwargs):
        self.patches.append((entity_ref, payload))
        return {}

    def post_operation(self, operation_path):
        self.operations.append(operation_path)


def _stub_demo(monkeypatch):
    monkeypatch.setattr(
        exporter,
        "_load_odata_config",
        lambda: {"base_url": "http://mtzw7/unf_demo/odata", "username": "u", "password": "p"},
    )


def _no_network(monkeypatch):
    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: pytest.fail("Network client must not be instantiated in dry-run"),
    )


# ---------------------------------------------------------------------------
# Preview (dry-run)
# ---------------------------------------------------------------------------

def test_preview_stock_covers_no_weld(db_session, monkeypatch):
    db = db_session
    painted, welded = _setup_pair(db, welded_stock=20)
    _stub_demo(monkeypatch)
    _no_network(monkeypatch)

    res = open_paint_chain(db, painted_item_id=painted.item_id, qty=10, dry_run=True)

    assert res["verdict"] == "stock_covers"
    assert res["weld_needed"] is False
    assert res["welded"] is None
    # окрасочный payload всё равно построен
    assert res["painted"]["payload"] is not None
    assert res["painted"]["payload"]["Продукция"][0]["Номенклатура_Key"] == "ref-painted"
    # dry-run ничего не пишет
    assert db.query(ProductionOrder).count() == 0
    assert db.query(PaintWeldChainLink).count() == 0


def test_preview_need_weld_builds_pair_with_dates_and_basis(db_session, monkeypatch):
    db = db_session
    painted, welded = _setup_pair(db, welded_stock=0)
    _stub_demo(monkeypatch)
    _no_network(monkeypatch)

    res = open_paint_chain(
        db,
        painted_item_id=painted.item_id,
        qty=10,
        planned_start="2026-08-10",
        planned_finish="2026-08-20",
        dry_run=True,
    )

    assert res["verdict"] == "need_weld"
    assert res["weld_needed"] is True
    welded_out = res["welded"]
    assert welded_out is not None
    assert welded_out["item_id"] == welded.item_id
    assert welded_out["qty"] == 10.0
    # финиш сварки = старт окраски; старт = финиш − buffer_days
    assert welded_out["planned_finish_date"] == "2026-08-10"
    assert welded_out["planned_start_date"] == (date(2026, 8, 10) - __import__("datetime").timedelta(days=WELD_BUFFER_DAYS)).isoformat()
    # «основание» отражено и в предпросмотре сварочного payload
    assert "основание: окрасочный заказ" in welded_out["payload"]["Комментарий"]
    assert welded_out["payload"]["Продукция"][0]["Номенклатура_Key"] == "ref-welded"
    # dry-run ничего не пишет
    assert db.query(ProductionOrder).count() == 0
    assert db.query(PaintWeldChainLink).count() == 0


def test_preview_partial_stock_reduces_weld_qty(db_session, monkeypatch):
    db = db_session
    painted, welded = _setup_pair(db, welded_stock=4)
    _stub_demo(monkeypatch)
    _no_network(monkeypatch)

    res = open_paint_chain(db, painted_item_id=painted.item_id, qty=10, dry_run=True)

    assert res["verdict"] == "need_weld"
    assert res["welded"]["qty"] == 6.0  # 10 − 4 остатка сварной
    assert float(res["welded"]["payload"]["Продукция"][0]["Количество"]) == 6.0


# ---------------------------------------------------------------------------
# Real write (dry_run=False)
# ---------------------------------------------------------------------------

def test_open_need_weld_creates_orders_in_order_with_basis(db_session, monkeypatch):
    db = db_session
    painted, welded = _setup_pair(db, welded_stock=0)
    _stub_demo(monkeypatch)
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    res = open_paint_chain(
        db,
        painted_item_id=painted.item_id,
        qty=10,
        planned_start="2026-08-10",
        dry_run=False,
    )

    assert res["verdict"] == "need_weld"
    # окраска выгружена первой, сварка — второй
    assert len(fake.posts) == 2
    assert res["painted"]["order_ref1c"] == "ref-1c-1"
    assert res["welded"]["order_ref1c"] == "ref-1c-2"

    # заказы существуют локально
    paint_order = db.query(ProductionOrder).filter(ProductionOrder.order_id == res["painted"]["order_id"]).one()
    weld_order = db.query(ProductionOrder).filter(ProductionOrder.order_id == res["welded"]["order_id"]).one()
    assert paint_order.order_ref1c == "ref-1c-1"
    assert weld_order.order_ref1c == "ref-1c-2"

    # локальная связь зафиксирована
    link = db.query(PaintWeldChainLink).one()
    assert link.painted_order_id == paint_order.order_id
    assert link.welded_order_id == weld_order.order_id

    # «основание» проброшено штатными полями 1С + продублировано в комментарии
    weld_payload = fake.posts[1][1]
    assert weld_payload["ЗаказНаПроизводствоОснование_Key"] == paint_order.order_ref1c
    assert weld_payload["ДокументОснование"] == paint_order.order_ref1c
    assert weld_payload["ДокументОснование_Type"] == "StandardODATA.Document_ЗаказНаПроизводство"
    assert "основание: окрасочный заказ" in weld_payload["Комментарий"]
    assert paint_order.order_ref1c in weld_payload["Комментарий"]

    # у окрасочного (первичного) заказа полей основания нет
    paint_payload = fake.posts[0][1]
    assert "ЗаказНаПроизводствоОснование_Key" not in paint_payload
    assert "ДокументОснование" not in paint_payload

    # sync_link на оба заказа
    assert (
        db.query(SyncLink)
        .filter(SyncLink.source_doctype == "production_order", SyncLink.status == "success")
        .count()
        == 2
    )


def test_open_is_idempotent_on_repeat(db_session, monkeypatch):
    db = db_session
    painted, welded = _setup_pair(db, welded_stock=0)
    _stub_demo(monkeypatch)
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    first = open_paint_chain(db, painted_item_id=painted.item_id, qty=10, planned_start="2026-08-10", dry_run=False)
    assert len(fake.posts) == 2

    second = open_paint_chain(db, painted_item_id=painted.item_id, qty=10, planned_start="2026-08-10", dry_run=False)

    # никаких повторных POST — оба заказа уже в 1С (sync_link/order_ref1c)
    assert len(fake.posts) == 2
    assert second["painted"]["order_id"] == first["painted"]["order_id"]
    assert second["welded"]["order_id"] == first["welded"]["order_id"]
    # без дублей заказов/связей
    assert db.query(ProductionOrder).count() == 2
    assert db.query(PaintWeldChainLink).count() == 1


def test_open_dry_run_writes_nothing(db_session, monkeypatch):
    db = db_session
    painted, welded = _setup_pair(db, welded_stock=0)
    _stub_demo(monkeypatch)
    _no_network(monkeypatch)

    open_paint_chain(db, painted_item_id=painted.item_id, qty=10, planned_start="2026-08-10", dry_run=True)

    assert db.query(ProductionOrder).count() == 0
    assert db.query(PaintWeldChainLink).count() == 0
    assert db.query(SyncLink).count() == 0
