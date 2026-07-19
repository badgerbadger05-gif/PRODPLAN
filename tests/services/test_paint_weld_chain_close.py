"""Этап 4 связки «окраска↔сварка»: комбинированный сдельный + закрытие обоих заказов.

Один Document_СдельныйНаряд на цепочку: операции сварки и окраски в одном
документе, у каждой строки свой ЗаказНаПроизводство_Key/участок/номенклатура,
основание — окрасочная СборкаЗапасов. Успешный экспорт закрывает оба заказа
(«Успешно», Завершен) и пишет sync_link на оба выпуска — повтор no-op.
"""
from __future__ import annotations

from datetime import datetime

from app.models import (
    DefaultSpecification,
    Item,
    Operation,
    PaintWeldChainLink,
    PaintWeldPair,
    ProductionManufacture,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    Specification,
    SpecOperation,
    SyncLink,
)
from app.services import one_c_piecework_export as exporter
from app.services.paint_weld_chain import close_paint_chain


class _FakeClient:
    def __init__(self, *, ref_key: str = "pw-chain-ref") -> None:
        self.ref_key = ref_key
        self.posts: list = []
        self.patches: list = []
        self.operations: list = []

    def post(self, entity, payload, **_):
        self.posts.append((entity, payload))
        return {"Ref_Key": self.ref_key}

    def patch(self, entity_ref, payload, **_):
        self.patches.append((entity_ref, payload))
        return {}

    def post_operation(self, operation_path):
        self.operations.append(operation_path)

    def _make_request(self, endpoint, params=None, **_):
        if "InformationRegister_ЦеныНоменклатуры" in str(endpoint):
            return {"value": []}
        return {}


def _stub_live(monkeypatch, fake) -> None:
    monkeypatch.setattr(
        exporter,
        "_load_odata_config",
        lambda: {"base_url": "http://mtzw7/unf_demo/odata/standard.odata", "username": "u", "password": "p"},
    )
    monkeypatch.setattr(exporter, "_create_odata_client", lambda *a, **kw: fake)


def _mk_side(db, *, code: str, op_ref: str, op_name: str, qty: float, sborka_ref: str):
    """Одна сторона цепочки: item + спека с операцией + заказ/строка/выпуск."""
    item = Item(
        item_code=code,
        item_name=f"Деталь {code}",
        item_article=f"ART-{code}",
        item_ref1c=f"item-ref-{code}",
        unit="шт",
        status="active",
    )
    db.add(item)
    db.flush()
    spec = Specification(spec_name=f"Спека {code}", spec_ref1c=f"spec-ref-{code}")
    operation = Operation(operation_ref1c=op_ref, operation_name=op_name, time_norm=0.5, operation_price=20)
    db.add_all([spec, operation])
    db.flush()
    db.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    db.add(SpecOperation(spec_id=spec.spec_id, operation_id=operation.operation_id, time_norm=0.5))
    order = ProductionOrder(
        order_number=f"PO-{code}",
        order_date=datetime(2026, 7, 18),
        is_posted=True,
        deletion_mark=False,
        order_ref1c=f"order-ref-{code}",
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=qty,
        produced_qty=qty,
        remaining_qty=0,
    )
    db.add(product)
    db.flush()
    db.add(
        ProductionOrderLineState(
            product_id=product.product_id, status="produced", issue_status="not_requested"
        )
    )
    manufacture = ProductionManufacture(
        product_id=product.product_id,
        order_id=order.order_id,
        qty=qty,
        status="exported",
        exported_ref1c=sborka_ref,
    )
    db.add(manufacture)
    db.flush()
    return item, order, product, manufacture


def _setup_chain(db):
    weld_item, weld_order, weld_product, weld_m = _mk_side(
        db, code="WELD", op_ref="op-weld", op_name="Сварка каркаса", qty=6.0, sborka_ref="sborka-weld-ref"
    )
    paint_item, paint_order, paint_product, paint_m = _mk_side(
        db, code="PAINT", op_ref="op-paint", op_name="Покраска порошковая", qty=10.0, sborka_ref="sborka-paint-ref"
    )
    pair = PaintWeldPair(
        painted_item_id=paint_item.item_id, welded_item_id=weld_item.item_id, source="auto"
    )
    db.add(pair)
    db.flush()
    db.add(
        PaintWeldChainLink(
            painted_order_id=paint_order.order_id,
            welded_order_id=weld_order.order_id,
            pair_id=pair.id,
        )
    )
    db.commit()
    return {
        "weld": {"item": weld_item, "order": weld_order, "product": weld_product, "m": weld_m},
        "paint": {"item": paint_item, "order": paint_order, "product": paint_product, "m": paint_m},
    }


# ---------------------------------------------------------------------------
# export_chain_piecework_to_1c
# ---------------------------------------------------------------------------


def test_combined_dry_run_payload_merges_two_blocks(db_session):
    ctx = _setup_chain(db_session)

    result = exporter.export_chain_piecework_to_1c(
        db_session,
        weld_manufacture_id=ctx["weld"]["m"].manufacture_id,
        paint_manufacture_id=ctx["paint"]["m"].manufacture_id,
        dry_run=True,
    )

    assert result["status"] == "ok"
    assert result["combined"] is True
    assert len(result["payloads"]) == 1
    payload = result["payloads"][0]["payload"]

    # основание — окрасочная СборкаЗапасов, шапка — окрасочный заказ
    assert payload["ДокументОснование"] == "sborka-paint-ref"
    assert payload["ДокументОснование_Type"] == "StandardODATA.Document_СборкаЗапасов"
    assert payload["ЗаказНаПроизводство_Key"] == "order-ref-PAINT"

    rows = payload["Операции"]
    assert len(rows) == 2
    # блок сварки первым: свой заказ, своя номенклатура, своё количество
    assert rows[0]["Операция_Key"] == "op-weld"
    assert rows[0]["ЗаказНаПроизводство_Key"] == "order-ref-WELD"
    assert rows[0]["Номенклатура_Key"] == "item-ref-WELD"
    assert rows[0]["КоличествоФакт"] == 6.0
    # блок окраски вторым
    assert rows[1]["Операция_Key"] == "op-paint"
    assert rows[1]["ЗаказНаПроизводство_Key"] == "order-ref-PAINT"
    assert rows[1]["Номенклатура_Key"] == "item-ref-PAINT"
    assert rows[1]["КоличествоФакт"] == 10.0
    assert [row["LineNumber"] for row in rows] == [1, 2]
    # один документ — один момент времени
    assert payload["Закрыт"] is True
    assert payload["ДатаЗакрытия"] == payload["Date"]


def test_combined_live_closes_both_orders_and_links_both_manufactures(db_session, monkeypatch):
    ctx = _setup_chain(db_session)
    fake = _FakeClient(ref_key="pw-chain-ref")
    _stub_live(monkeypatch, fake)

    result = exporter.export_chain_piecework_to_1c(
        db_session,
        weld_manufacture_id=ctx["weld"]["m"].manufacture_id,
        paint_manufacture_id=ctx["paint"]["m"].manufacture_id,
        dry_run=False,
        allow_production=True,
    )

    assert result["status"] == "ok"
    assert result["created"] == 1
    assert len(fake.posts) == 1  # ОДИН документ на оба заказа

    # оба заказа закрыты в 1С
    patched_paths = [path for path, _ in fake.patches]
    assert "Document_ЗаказНаПроизводство(guid'order-ref-WELD')" in patched_paths
    assert "Document_ЗаказНаПроизводство(guid'order-ref-PAINT')" in patched_paths
    for path, body in fake.patches:
        if "ЗаказНаПроизводство" in path:
            assert body["СостояниеЗаказа_Key"] == exporter.DONE_STATE_KEY
            assert body["ВариантЗавершения"] == exporter.ORDER_COMPLETION_SUCCESS

    # и локально
    db_session.refresh(ctx["weld"]["order"])
    db_session.refresh(ctx["paint"]["order"])
    assert ctx["weld"]["order"].order_state_name == "Завершен"
    assert ctx["paint"]["order"].order_state_name == "Завершен"

    # sync_link на оба выпуска с одним target_ref_key
    links = (
        db_session.query(SyncLink)
        .filter(SyncLink.source_doctype == "piecework", SyncLink.status == "success")
        .all()
    )
    assert {int(link.source_id) for link in links} == {
        int(ctx["weld"]["m"].manufacture_id),
        int(ctx["paint"]["m"].manufacture_id),
    }
    assert {link.target_ref_key for link in links} == {"pw-chain-ref"}


def test_combined_repeat_is_noop(db_session, monkeypatch):
    ctx = _setup_chain(db_session)
    fake = _FakeClient()
    _stub_live(monkeypatch, fake)

    first = exporter.export_chain_piecework_to_1c(
        db_session,
        weld_manufacture_id=ctx["weld"]["m"].manufacture_id,
        paint_manufacture_id=ctx["paint"]["m"].manufacture_id,
        dry_run=False,
        allow_production=True,
    )
    assert first["status"] == "ok"
    posts_after_first = len(fake.posts)

    second = exporter.export_chain_piecework_to_1c(
        db_session,
        weld_manufacture_id=ctx["weld"]["m"].manufacture_id,
        paint_manufacture_id=ctx["paint"]["m"].manufacture_id,
        dry_run=False,
        allow_production=True,
    )
    assert second["status"] == "existing"
    assert len(fake.posts) == posts_after_first


def test_combined_refuses_weld_closed_by_separate_piecework(db_session, monkeypatch):
    ctx = _setup_chain(db_session)
    db_session.add(
        SyncLink(
            source_system="PRODPLAN",
            source_doctype="piecework",
            source_id=ctx["weld"]["m"].manufacture_id,
            target_entity="Document_СдельныйНаряд",
            target_ref_key="separate-pw-ref",
            target_number="PW-SEP",
            status="success",
        )
    )
    db_session.commit()

    result = exporter.export_chain_piecework_to_1c(
        db_session,
        weld_manufacture_id=ctx["weld"]["m"].manufacture_id,
        paint_manufacture_id=ctx["paint"]["m"].manufacture_id,
        dry_run=True,
    )

    assert result["status"] == "error"
    assert "отдельным сдельным" in result["error"]


# ---------------------------------------------------------------------------
# close_paint_chain (сервис закрытия из окна журнала)
# ---------------------------------------------------------------------------


def test_close_chain_dry_run_previews_both_sides(db_session):
    ctx = _setup_chain(db_session)

    result = close_paint_chain(
        db_session,
        product_id=ctx["paint"]["product"].product_id,
        dry_run=True,
    )

    assert result["status"] == "ok"
    assert result["weld"]["existing_manufacture_id"] == ctx["weld"]["m"].manufacture_id
    assert result["paint"]["existing_manufacture_id"] == ctx["paint"]["m"].manufacture_id
    # обе стороны произведены полностью — производить нечего
    assert result["weld"]["qty_to_produce"] == 0.0
    assert result["paint"]["qty_to_produce"] == 0.0
    # предпросмотр комбинированного сдельного построен
    assert result["piecework_preview"]["status"] == "ok"
    assert len(result["piecework_preview"]["payloads"]) == 1


def test_close_chain_resolves_from_either_side(db_session):
    ctx = _setup_chain(db_session)

    from_weld = close_paint_chain(
        db_session, product_id=ctx["weld"]["product"].product_id, dry_run=True
    )
    from_paint = close_paint_chain(
        db_session, product_id=ctx["paint"]["product"].product_id, dry_run=True
    )

    assert from_weld["weld"]["product_id"] == from_paint["weld"]["product_id"]
    assert from_weld["paint"]["product_id"] == from_paint["paint"]["product_id"]


def test_close_chain_live_exports_combined_and_closes_orders(db_session, monkeypatch):
    ctx = _setup_chain(db_session)
    fake = _FakeClient(ref_key="pw-close-ref")
    _stub_live(monkeypatch, fake)

    # выпуски уже выгружены (exported_ref1c стоит) — экспорт manufactures
    # ничего не должен пересоздавать; стаббим его, чтобы не ходить в конфиг.
    from app.services import one_c_manufacture_export

    monkeypatch.setattr(
        one_c_manufacture_export,
        "export_manufactures_to_1c",
        lambda db, ids, **kw: {"status": "ok", "manufacture_ids": list(ids)},
    )

    result = close_paint_chain(
        db_session,
        product_id=ctx["paint"]["product"].product_id,
        dry_run=False,
        allow_production=True,
    )

    assert result["status"] == "ok"
    assert result["manufactures_export"]["status"] == "ok"
    assert result["piecework_export"]["status"] == "ok"
    assert len(fake.posts) == 1

    db_session.refresh(ctx["weld"]["order"])
    db_session.refresh(ctx["paint"]["order"])
    assert ctx["weld"]["order"].order_state_name == "Завершен"
    assert ctx["paint"]["order"].order_state_name == "Завершен"


def test_close_chain_without_link_raises(db_session):
    ctx = _setup_chain(db_session)
    solo_item = Item(
        item_code="SOLO-CLOSE",
        item_name="Одиночная деталь",
        item_article="ART-SOLO",
        unit="шт",
        status="active",
    )
    db_session.add(solo_item)
    db_session.flush()
    solo_order = ProductionOrder(
        order_number="PO-SOLO",
        order_date=datetime(2026, 7, 18),
        deletion_mark=False,
    )
    db_session.add(solo_order)
    db_session.flush()
    solo_product = ProductionProduct(
        order_id=solo_order.order_id,
        item_id=solo_item.item_id,
        line_number=1,
        quantity=1,
        produced_qty=0,
        remaining_qty=1,
    )
    db_session.add(solo_product)
    db_session.commit()

    try:
        close_paint_chain(db_session, product_id=solo_product.product_id, dry_run=True)
        assert False, "ожидали ValueError"
    except ValueError as exc:
        assert "цепочка" in str(exc)
