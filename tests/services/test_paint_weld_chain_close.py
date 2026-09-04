"""Этап 4 связки «окраска↔сварка»: комбинированный сдельный на оба выпуска.

Один Document_СдельныйНаряд на цепочку: операции сварки и окраски в одном
документе, у каждой строки свой ЗаказНаПроизводство_Key/участок/номенклатура,
основание — окрасочная СборкаЗапасов. Состояние самих заказов экспорт не пишет
(его ставит оператор в 1С), sync_link пишется на оба выпуска — повтор no-op.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app import models
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
    LedgerGeneration,
    PhysicalImportBatch,
    PlanningTruthState,
)
from app.services import one_c_piecework_export as exporter
from app.services.paint_weld_chain import close_paint_chain


@pytest.fixture(autouse=True)
def _accepted_journal_truth(db_session):
    cutoff = datetime(2026, 7, 18)
    physical = PhysicalImportBatch(
        batch_key="paint-weld-close-journal", status="completed", cutoff=cutoff,
        completed_at=cutoff, source_watermarks={},
    )
    generation = LedgerGeneration(
        generation_key="paint-weld-close-journal", status="accepted", cutoff=cutoff,
        accepted_at=cutoff, source_watermarks={}, capabilities={},
        physical_import_batch=physical, algorithm_version="tests/1",
    )
    db_session.add(generation)
    db_session.flush()
    db_session.add(PlanningTruthState(id=1, current_generation_id=generation.id))
    db_session.flush()


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


def _stub_manufactures_export(monkeypatch, *, failing_ids: set):
    """Подменить экспорт СборокЗапасов: часть строк проваливается, часть — нет.

    Возвращает штатную форму сводки (``entries`` + ``manufactures_error``),
    чтобы close_paint_chain разбирал результат построчно.
    """
    from app.services import one_c_manufacture_export

    def _fake(db, ids, **_kwargs):
        entries = []
        errors = 0
        for manufacture_id in ids:
            row = db.get(ProductionManufacture, int(manufacture_id))
            if int(manufacture_id) in failing_ids:
                errors += 1
                entries.append(
                    {
                        "manufacture_id": int(manufacture_id),
                        "status": "error",
                        "error": "1С отказала в проведении СборкаЗапасов",
                    }
                )
                continue
            if not str(row.exported_ref1c or ""):
                row.exported_ref1c = f"sborka-live-{manufacture_id}"
                row.status = "exported"
                db.flush()
            entries.append(
                {
                    "manufacture_id": int(manufacture_id),
                    "status": "created",
                    "target_ref_key": row.exported_ref1c,
                }
            )
        return {
            "status": "ok" if not errors else "partial_error",
            "entries": entries,
            "manufactures_error": errors,
        }

    monkeypatch.setattr(one_c_manufacture_export, "export_manufactures_to_1c", _fake)


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
    # Исполнитель обязателен: 1С не проводит сдельный наряд с пустой строкой
    # регистра «Сдельные наряды».
    if db.query(models.Employee).filter(models.Employee.employee_name == "Иванов").one_or_none() is None:
        db.add(models.Employee(
            employee_ref1c="employee-chain-ref",
            employee_name="Иванов",
            employee_type="employee",
            deletion_mark=False,
        ))
        db.flush()
    manufacture = ProductionManufacture(
        product_id=product.product_id,
        order_id=order.order_id,
        qty=qty,
        executor="Иванов",
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


def test_combined_live_links_both_manufactures_without_touching_orders(db_session, monkeypatch):
    ctx = _setup_chain(db_session)
    fake = _FakeClient(ref_key="pw-chain-ref")
    _stub_live(monkeypatch, fake)

    result = exporter.export_chain_piecework_to_1c(
        db_session,
        weld_manufacture_id=ctx["weld"]["m"].manufacture_id,
        paint_manufacture_id=ctx["paint"]["m"].manufacture_id,
        dry_run=False,
    )

    assert result["status"] == "ok"
    assert result["created"] == 1
    assert len(fake.posts) == 1  # ОДИН документ на оба заказа

    # состояние заказов не пишется ни в 1С, ни локально: закрывает оператор в 1С
    assert all("ЗаказНаПроизводство" not in path for path, _ in fake.patches)
    assert all(
        "СостояниеЗаказа_Key" not in body and "ВариантЗавершения" not in body
        for _path, body in fake.patches
    )
    db_session.refresh(ctx["weld"]["order"])
    db_session.refresh(ctx["paint"]["order"])
    assert ctx["weld"]["order"].order_state_name is None
    assert ctx["paint"]["order"].order_state_name is None

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
    )
    assert first["status"] == "ok"
    posts_after_first = len(fake.posts)

    second = exporter.export_chain_piecework_to_1c(
        db_session,
        weld_manufacture_id=ctx["weld"]["m"].manufacture_id,
        paint_manufacture_id=ctx["paint"]["m"].manufacture_id,
        dry_run=False,
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


def test_close_chain_ignores_corrupted_remaining_cache(db_session):
    ctx = _setup_chain(db_session)
    # Physical accepted output is complete. A non-factual writer corrupting the
    # compatibility remainder must not create another production command.
    ctx["weld"]["product"].remaining_qty = ctx["weld"]["product"].quantity
    ctx["paint"]["product"].remaining_qty = ctx["paint"]["product"].quantity
    db_session.commit()

    result = close_paint_chain(
        db_session, product_id=ctx["paint"]["product"].product_id, dry_run=True
    )

    assert result["weld"]["remaining_qty"] == 0.0
    assert result["paint"]["remaining_qty"] == 0.0
    assert result["weld"]["qty_to_produce"] == 0.0
    assert result["paint"]["qty_to_produce"] == 0.0


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


def test_close_chain_live_exports_combined_without_closing_orders(db_session, monkeypatch):
    ctx = _setup_chain(db_session)
    fake = _FakeClient(ref_key="pw-close-ref")
    _stub_live(monkeypatch, fake)

    # выпуски уже выгружены (exported_ref1c стоит) — экспорт manufactures
    # ничего не должен пересоздавать; стаббим его, чтобы не ходить в конфиг.
    from app.services import one_c_manufacture_export

    export_calls = []

    def _export_in_order(db, ids, **kw):
        export_calls.append(list(ids))
        return {"status": "ok", "manufacture_ids": list(ids)}

    monkeypatch.setattr(
        one_c_manufacture_export,
        "export_manufactures_to_1c",
        _export_in_order,
    )

    result = close_paint_chain(
        db_session,
        product_id=ctx["paint"]["product"].product_id,
        dry_run=False,
    )

    assert result["status"] == "ok"
    assert result["manufactures_export"]["status"] == "ok"
    assert export_calls == [
        [int(ctx["weld"]["m"].manufacture_id)],
        [int(ctx["paint"]["m"].manufacture_id)],
    ]
    assert result["piecework_export"]["status"] == "ok"
    assert len(fake.posts) == 1

    # заказы остаются открытыми: их закрывает оператор в 1С
    assert all("ЗаказНаПроизводство" not in path for path, _ in fake.patches)
    db_session.refresh(ctx["weld"]["order"])
    db_session.refresh(ctx["paint"]["order"])
    assert ctx["weld"]["order"].order_state_name is None
    assert ctx["paint"]["order"].order_state_name is None


def test_close_chain_partial_export_keeps_posted_side_and_resumes(db_session, monkeypatch):
    """Частичный успех: проведённая сборка остаётся, повтор докатывает цепочку.

    Раньше окрасочная сторона падала, а результат уходил исключением — при этом
    сварочная СборкаЗапасов оставалась в 1С без комбинированного наряда и без
    внятного состояния для оператора.
    """
    ctx = _setup_chain(db_session)
    paint_manufacture = ctx["paint"]["m"]
    paint_manufacture.exported_ref1c = None
    paint_manufacture.status = "draft"
    db_session.commit()

    fake = _FakeClient(ref_key="pw-resume-ref")
    _stub_live(monkeypatch, fake)
    failing = {int(paint_manufacture.manufacture_id)}
    _stub_manufactures_export(monkeypatch, failing_ids=failing)

    first = close_paint_chain(
        db_session,
        product_id=ctx["paint"]["product"].product_id,
        dry_run=False,
    )

    assert first["status"] == "partial"
    assert first["chain_state"] == "partially_posted"
    assert first["resume_required"] is True
    assert first["posted_sides"] == ["weld"]
    assert first["pending_sides"] == ["paint"]
    assert "докат" in first["message"]
    assert "1С отказала" in first["error"]
    # комбинированный наряд не создавался
    assert fake.posts == []
    # проведённая сварочная сборка НЕ откачена
    db_session.refresh(ctx["weld"]["m"])
    assert ctx["weld"]["m"].exported_ref1c == "sborka-weld-ref"
    assert db_session.query(ProductionManufacture).count() == 2
    # заказы остаются открытыми
    db_session.refresh(ctx["weld"]["order"])
    db_session.refresh(ctx["paint"]["order"])
    assert ctx["weld"]["order"].order_state_name is None
    assert ctx["paint"]["order"].order_state_name is None

    # ----- докат повторным вызовом -----
    failing.clear()
    second = close_paint_chain(
        db_session,
        product_id=ctx["paint"]["product"].product_id,
        dry_run=False,
    )

    assert second["status"] == "ok"
    assert second["chain_state"] == "closed"
    assert second["resume_required"] is False
    assert second["pending_sides"] == []
    # ровно один комбинированный сдельный и ни одного дубля выпуска
    assert len(fake.posts) == 1
    assert db_session.query(ProductionManufacture).count() == 2

    # докат создаёт наряд, но состояние заказов по-прежнему не пишет
    db_session.refresh(ctx["weld"]["order"])
    db_session.refresh(ctx["paint"]["order"])
    assert ctx["weld"]["order"].order_state_name is None
    assert ctx["paint"]["order"].order_state_name is None

    links = (
        db_session.query(SyncLink)
        .filter(SyncLink.source_doctype == "piecework", SyncLink.status == "success")
        .all()
    )
    assert {int(link.source_id) for link in links} == {
        int(ctx["weld"]["m"].manufacture_id),
        int(ctx["paint"]["m"].manufacture_id),
    }
    assert {link.target_ref_key for link in links} == {"pw-resume-ref"}


def test_close_chain_does_not_export_paint_when_weld_was_not_posted(
    db_session, monkeypatch
):
    """Баланс-гард окраски не запускается до успешного выпуска сварки."""
    ctx = _setup_chain(db_session)
    for side in ("weld", "paint"):
        db_session.delete(ctx[side]["m"])
        ctx[side]["product"].produced_qty = 0
        ctx[side]["product"].remaining_qty = ctx[side]["product"].quantity
    db_session.commit()

    from app.services import one_c_manufacture_export

    export_calls = []

    def _fail_first_export(db, ids, **_kwargs):
        export_calls.append(list(ids))
        manufacture_id = int(ids[0])
        return {
            "status": "partial_error",
            "entries": [
                {
                    "manufacture_id": manufacture_id,
                    "status": "error",
                    "error": "сварочная сборка не проведена",
                }
            ],
            "manufactures_error": 1,
        }

    monkeypatch.setattr(
        one_c_manufacture_export,
        "export_manufactures_to_1c",
        _fail_first_export,
    )

    with pytest.raises(ValueError, match="сварочная сборка не проведена"):
        close_paint_chain(
            db_session,
            product_id=ctx["paint"]["product"].product_id,
            dry_run=False,
        )

    assert len(export_calls) == 1
    assert db_session.query(ProductionManufacture).count() == 0


def test_close_chain_rolls_back_first_command_when_second_side_fails_before_export(
    db_session, monkeypatch
):
    ctx = _setup_chain(db_session)
    for side in ("weld", "paint"):
        db_session.delete(ctx[side]["m"])
    for side in ("weld", "paint"):
        ctx[side]["product"].produced_qty = 0
        ctx[side]["product"].remaining_qty = ctx[side]["product"].quantity
    db_session.commit()

    from app.services import production_control_production_flow as flow

    calls = []

    def _produce(db, product_id, **kwargs):
        calls.append(int(product_id))
        if len(calls) == 2:
            raise ValueError("paint preparation failed")
        product = db.get(ProductionProduct, int(product_id))
        manufacture = ProductionManufacture(
            product_id=int(product.product_id),
            order_id=int(product.order_id),
            qty=float(kwargs["qty"]),
            status="draft",
        )
        db.add(manufacture)
        db.commit()
        return {
            "manufacture_id": int(manufacture.manufacture_id),
            "resumed": False,
        }

    monkeypatch.setattr(flow, "produce_line", _produce)

    with pytest.raises(ValueError, match="paint preparation failed"):
        close_paint_chain(
            db_session,
            product_id=ctx["paint"]["product"].product_id,
            dry_run=False,
        )

    assert calls == [
        int(ctx["weld"]["product"].product_id),
        int(ctx["paint"]["product"].product_id),
    ]
    assert db_session.query(ProductionManufacture).count() == 0


def test_close_chain_dry_run_reports_partially_posted_state(db_session):
    ctx = _setup_chain(db_session)
    ctx["paint"]["m"].exported_ref1c = None
    ctx["paint"]["m"].status = "draft"
    db_session.commit()

    result = close_paint_chain(
        db_session, product_id=ctx["weld"]["product"].product_id, dry_run=True
    )

    assert result["chain_state"] == "partially_posted"
    assert result["resume_required"] is True
    assert result["posted_sides"] == ["weld"]
    assert result["pending_sides"] == ["paint"]


def test_close_chain_never_writes_order_completion(db_session, monkeypatch):
    """Цепочечный путь не пишет состояние заказа ни при каком выпуске."""
    ctx = _setup_chain(db_session)
    # Сварка покрыта лишь частично, окраска — полностью. Ни та, ни другая
    # сторона не даёт PRODPLAN права закрыть заказ в 1С.
    ctx["weld"]["m"].qty = 4
    ctx["weld"]["product"].produced_qty = 0
    db_session.commit()

    fake = _FakeClient(ref_key="pw-partial-output-ref")
    _stub_live(monkeypatch, fake)
    _stub_manufactures_export(monkeypatch, failing_ids=set())

    result = close_paint_chain(
        db_session,
        product_id=ctx["paint"]["product"].product_id,
        dry_run=False,
    )

    assert result["status"] == "ok"
    assert all("ЗаказНаПроизводство" not in path for path, _ in fake.patches)
    db_session.refresh(ctx["weld"]["order"])
    db_session.refresh(ctx["paint"]["order"])
    assert ctx["weld"]["order"].order_state_name is None
    assert ctx["paint"]["order"].order_state_name is None


def test_journal_rows_carry_chain_info(db_session):
    ctx = _setup_chain(db_session)
    # вернуть строки в журнал: цепочка ещё не произведена/не закрыта
    for side in ("weld", "paint"):
        product = ctx[side]["product"]
        product.remaining_qty = product.quantity
        product.produced_qty = 0
        state = (
            db_session.query(ProductionOrderLineState)
            .filter(ProductionOrderLineState.product_id == product.product_id)
            .one()
        )
        state.status = "shortage"
    db_session.commit()

    from app.services.production_control_journal import list_journal

    journal = list_journal(db_session)
    rows = journal["rows"]
    by_pid = {row["product_id"]: row for row in rows}
    paint_row = by_pid[int(ctx["paint"]["product"].product_id)]
    weld_product_id = int(ctx["weld"]["product"].product_id)
    assert weld_product_id in by_pid
    assert journal["total"] == len(rows)
    weld_row = by_pid[weld_product_id]

    assert weld_row["paint_weld_chain"]["role"] == "welded"
    assert weld_row["paint_weld_chain"]["counterpart_product_id"] == paint_row["product_id"]
    assert paint_row["paint_weld_chain"]["role"] == "painted"
    assert paint_row["paint_weld_chain"]["counterpart_product_id"] == weld_row["product_id"]
    assert paint_row["paint_weld_chain"]["counterpart_item_name"] == ctx["weld"]["item"].item_name
    assert paint_row["paint_weld_chain"]["counterpart_quantity"] == 6.0
    # строка вне цепочки — None
    assert all(
        row["paint_weld_chain"] is None
        for row in rows
        if row["product_id"] not in (weld_row["product_id"], paint_row["product_id"])
    )


def test_close_chain_routes_each_side_executors_to_its_own_produce(db_session, monkeypatch):
    """Исполнители цепочки приходят построчно и раздаются по своей стороне.

    Комбинированный наряд несёт строки сварки И окраски, поэтому окно журнала
    выбирает исполнителей обеим сторонам, а закрытие раздаёт их каждой стороне
    отдельно; исполнитель шапки остаётся общим запасным.
    """
    ctx = _setup_chain(db_session)
    for side in ("weld", "paint"):
        ctx[side]["product"].produced_qty = 0
        ctx[side]["product"].remaining_qty = ctx[side]["product"].quantity
    db_session.commit()

    def _side_executor_rows(side: str):
        spec = (
            db_session.query(Specification)
            .filter(Specification.spec_name == f"Спека {'WELD' if side == 'weld' else 'PAINT'}")
            .one()
        )
        spec_operation = (
            db_session.query(SpecOperation)
            .filter(SpecOperation.spec_id == spec.spec_id)
            .order_by(SpecOperation.spec_operation_id.asc())
            .first()
        )
        return [
            {
                "spec_operation_id": int(spec_operation.spec_operation_id),
                "operation_id": int(spec_operation.operation_id),
                "line_number": 1,
                "employee_ref1c": "employee-chain-ref",
            }
        ]

    weld_rows = _side_executor_rows("weld")
    paint_rows = _side_executor_rows("paint")

    captured: dict = {}
    from app.services import production_control_production_flow as flow

    def _fake_produce(db, product_id, **kwargs):
        captured[int(product_id)] = kwargs
        manufacture = (
            db.query(ProductionManufacture)
            .filter(ProductionManufacture.product_id == int(product_id))
            .one()
        )
        return {"manufacture_id": int(manufacture.manufacture_id), "resumed": True}

    monkeypatch.setattr(flow, "produce_line", _fake_produce)
    fake = _FakeClient(ref_key="pw-executors-ref")
    _stub_live(monkeypatch, fake)
    _stub_manufactures_export(monkeypatch, failing_ids=set())

    result = close_paint_chain(
        db_session,
        product_id=ctx["paint"]["product"].product_id,
        executor="Иванов",
        weld_operation_executors=weld_rows,
        paint_operation_executors=paint_rows,
        dry_run=False,
    )

    assert result["status"] == "ok"
    weld_product_id = int(ctx["weld"]["product"].product_id)
    paint_product_id = int(ctx["paint"]["product"].product_id)
    assert captured[weld_product_id]["operation_executors"] == weld_rows
    assert captured[paint_product_id]["operation_executors"] == paint_rows
    # исполнитель шапки — общий запасной для строк без своего исполнителя
    assert captured[weld_product_id]["executor"] == "Иванов"
    assert captured[paint_product_id]["executor"] == "Иванов"
    # сварная деталь выпускается только внутри цепочки
    assert captured[weld_product_id]["allow_paint_weld_chain"] is True
    assert not captured[paint_product_id].get("allow_paint_weld_chain")
    assert captured[paint_product_id]["anticipated_material_receipts"] == {
        int(ctx["weld"]["item"].item_id): float(ctx["weld"]["m"].qty)
    }


def test_combined_piecework_refuses_row_without_executor_before_writing_to_1c(
    db_session, monkeypatch
):
    """Пустой исполнитель в строке — отказ ДО записи, а не 500 из 1С.

    1С не проводит `Document_СдельныйНаряд`, если хоть в одной строке регистра
    «Сдельные наряды» пуст Исполнитель. Раньше документ к этому моменту был уже
    создан. Правило одно для обоих путей: без исполнителя в 1С не уходит ничего.
    """
    ctx = _setup_chain(db_session)
    paint_spec = (
        db_session.query(Specification).filter(Specification.spec_name == "Спека PAINT").one()
    )
    second_operation = Operation(
        operation_ref1c="op-paint-2", operation_name="Сушка", time_norm=0.2, operation_price=5
    )
    db_session.add(second_operation)
    db_session.flush()
    db_session.add(
        SpecOperation(
            spec_id=paint_spec.spec_id, operation_id=second_operation.operation_id, time_norm=0.2
        )
    )
    first_spec_operation = (
        db_session.query(SpecOperation)
        .filter(SpecOperation.spec_id == paint_spec.spec_id)
        .order_by(SpecOperation.spec_operation_id.asc())
        .first()
    )
    # Исполнитель назначен только на первую операцию окраски, шапка пуста.
    paint_manufacture = ctx["paint"]["m"]
    paint_manufacture.executor = None
    db_session.add(
        models.ProductionManufactureOperation(
            manufacture_id=paint_manufacture.manufacture_id,
            spec_operation_id=int(first_spec_operation.spec_operation_id),
            operation_id=int(first_spec_operation.operation_id),
            line_number=1,
            employee_ref1c="employee-chain-ref",
            employee_name="Иванов",
            employee_type="employee",
        )
    )
    db_session.commit()

    fake = _FakeClient(ref_key="pw-no-executor-ref")
    _stub_live(monkeypatch, fake)

    result = exporter.export_chain_piecework_to_1c(
        db_session,
        weld_manufacture_id=ctx["weld"]["m"].manufacture_id,
        paint_manufacture_id=ctx["paint"]["m"].manufacture_id,
        dry_run=False,
    )

    assert result["status"] == "error"
    assert "не указан исполнитель по операциям 2" in result["error"]
    # в 1С не ушло ничего: ни документа, ни проведения
    assert fake.posts == []
    assert fake.operations == []
    assert (
        db_session.query(SyncLink)
        .filter(SyncLink.source_doctype == "piecework", SyncLink.status == "success")
        .count()
        == 0
    )


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
