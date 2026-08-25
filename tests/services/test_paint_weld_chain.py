"""Цепочка открытия пары «окраска ↔ сварка» (этап 2).

Документное направление: сварная деталь входит в состав окрашенной, значит
основанием в 1С является ОКРАСОЧНЫЙ заказ, а сварочный вводится на его
основании и как подчинённый закрывается раньше. Производственная
последовательность обратная и не меняется: сварка финиширует к старту окраски.

Проверяем:
- вердикт stock_covers → сварка не создаётся (окраска — штатно);
- частичное покрытие → qty сварки уменьшено на эффективный остаток сварной;
- need_weld → пара заказов в правильном порядке выгрузки (окраска раньше
  сварки), с датами (финиш сварки = старт окраски, старт = финиш − buffer_days
  участка) и «основанием» в полях и комментарии сварочного 1С-документа +
  локальной связью;
- идемпотентность повтора (нет дублей заказа/связи, нет повторного POST);
- dry-run ничего не пишет.

Мок OData — как в tests/services/test_one_c_production_order_export.py.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from app import models
from app.models import (
    DefaultSpecification,
    Item,
    PaintWeldChainLink,
    PaintWeldPair,
    PlannedOrder,
    PlanningRun,
    ProductionKind,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionResource,
    ResourceProductionKind,
    SpecComponent,
    Specification,
    MrpFreezeComponent,
    SyncLink,
    ProductionMaterialIssueLine,
    WorkshopWarehouseBinding,
    StockBin,
)
from app.services import production_material_custody_projection as custody_projection
from app.services import one_c_production_order_export as exporter
from app.services import production_control_material_issues as material_issues
from app.services.paint_weld_chain import open_paint_chain, open_paint_chains_for_products
from app.services.planning_truth import publish_generation

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
                replenishment_method="Производство",
        status="active",
    )
    db.add(it)
    db.flush()
    return it


def _setup_pair(
    db,
    *,
    weld_outstanding: float,
    legacy_welded_stock: float = 0.0,
):
    """Painted item (spec component = welded) + welded item + active pair +
    weld workshop (buffer_days=14 via production kind). Returns (painted, welded)."""
    painted = _item(db, code="PNT", name="Кронштейн после покраски", ref1c="ref-painted")
    welded = _item(
        db,
        code="WLD",
        name="Кронштейн после сварки",
        ref1c="ref-welded",
        stock=legacy_welded_stock,
    )

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
    weld_spec = Specification(
        spec_name="Сварка кронштейна",
        spec_ref1c="spec-weld",
        production_kind_id=kind.id,
        content_hash="spec-weld-frozen-v1",
    )
    db.add(weld_spec)
    db.flush()
    db.add(DefaultSpecification(item_id=welded.item_id, spec_id=weld_spec.spec_id))
    weld_component = _item(
        db,
        code="WLD-COMP",
        name="Компонент сварной детали",
        ref1c="ref-weld-component",
    )
    db.add(
        SpecComponent(
            spec_id=weld_spec.spec_id,
            item_id=weld_component.item_id,
            quantity=1,
            component_type="Сборка",
        )
    )
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
    cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
    generation = models.LedgerGeneration(
        generation_key=f"paint-weld-{id(db)}",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        source_watermarks={},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
        },
        algorithm_version="test/1",
        replay_version="test/1",
        physical_import_batch=models.PhysicalImportBatch(
            batch_key=f"paint-weld-batch-{id(db)}",
            status="completed",
            cutoff=cutoff,
            source_watermarks={},
        ),
    )
    publish_generation(db, generation)
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot=json.dumps({}),
        active_freeze_version=1,
        ledger_generation_id=generation.id,
        ledger_cutoff=cutoff,
    )
    db.add(run)
    db.flush()
    db.add(
        MrpFreezeComponent(
            run_id=run.run_id,
            freeze_version=1,
            parent_item_id=welded.item_id,
            component_item_id=weld_component.item_id,
            spec_ref=str(weld_spec.spec_ref1c or ""),
            spec_version=str(weld_spec.content_hash or ""),
            norm_qty_per_unit=1,
        )
    )
    painted_proposal = PlannedOrder(
        run_id=run.run_id,
        item_id=painted.item_id,
        requested_qty=10,
        planned_qty=10,
        qty=10,
        need_date=date(2026, 8, 20),
        bucket_date=date(2026, 8, 20),
        ledger_generation_id=generation.id,
    )
    db.add(painted_proposal)
    db.flush()
    painted_order = ProductionOrder(
        order_number=f"MRP-{run.run_id}-paint",
        order_date=cutoff,
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=run.run_id,
    )
    db.add(painted_order)
    db.flush()
    painted_product = ProductionProduct(
        order_id=painted_order.order_id,
        item_id=painted.item_id,
        line_number=1,
        quantity=10,
        produced_qty=0,
        remaining_qty=10,
        spec_id=paint_spec.spec_id,
        source_planned_order_id=painted_proposal.order_id,
        ledger_generation_id=generation.id,
    )
    db.add(painted_product)
    db.flush()
    db.add(
        ProductionOrderLineState(
            product_id=painted_product.product_id,
            status="shortage",
            issue_status="not_requested",
        )
    )
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=welded.item_id,
        total_required_qty=10,
        net_required_qty=weld_outstanding,
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        bom_level=1,
        freeze_version=1,
    )
    db.add(requirement)
    db.flush()
    db.add(
        models.ReservationEntry(
            ledger_generation_id=generation.id,
            item_id=welded.item_id,
            characteristic_ref="",
            organization_ref="",
            planning_stock_pool="default",
            run_id=run.run_id,
            freeze_version=1,
            requirement_id=requirement.id,
            priority_period_from=date(2026, 8, 1),
            priority_period_to=date(2026, 8, 31),
            realization_mode="make",
            reserved_qty=weld_outstanding,
            realized_qty=0,
            replenishment_required_qty=weld_outstanding,
            replenishment_received_qty=0,
            lifecycle_status="active",
        )
    )
    db.commit()
    return painted, welded, painted_product


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


class _RefusingClient(_FakeClient):
    """1С приняла запрос, но не вернула Ref_Key — заказ не создан."""

    def post(self, entity, payload, **_kwargs):
        self.posts.append((entity, payload))
        return {}


class _FailingClient(_FakeClient):
    """1С отклонила создание первого (окрасочного) заказа цепочки."""

    def post(self, entity, payload, **_kwargs):
        self.posts.append((entity, payload))
        raise RuntimeError("1С: не заполнена организация")


class _FailSecondClient(_FakeClient):
    """Окраска создана, но 1С отклонила следующий сварочный заказ."""

    def post(self, entity, payload, **_kwargs):
        self._n += 1
        self.posts.append((entity, payload))
        if self._n == 2:
            raise RuntimeError("1С: ошибка сварки")
        return {"Ref_Key": "ref-paint-success"}


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
    painted, welded, painted_product = _setup_pair(
        db, weld_outstanding=0, legacy_welded_stock=999
    )
    _stub_demo(monkeypatch)
    _no_network(monkeypatch)

    res = open_paint_chain(
        db, painted_product_id=painted_product.product_id, dry_run=True
    )

    assert res["verdict"] == "stock_covers"
    assert res["weld_needed"] is False
    assert res["welded"] is None
    # окрасочный payload всё равно построен
    assert res["painted"]["payload"] is not None
    assert res["painted"]["payload"]["Продукция"][0]["Номенклатура_Key"] == "ref-painted"
    # dry-run ничего не пишет
    assert db.query(ProductionOrder).count() == 1
    assert db.query(PaintWeldChainLink).count() == 0


def test_preview_need_weld_builds_pair_with_dates_and_basis(db_session, monkeypatch):
    db = db_session
    painted, welded, painted_product = _setup_pair(db, weld_outstanding=10)
    _stub_demo(monkeypatch)
    _no_network(monkeypatch)

    res = open_paint_chain(
        db,
        painted_product_id=painted_product.product_id,
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
    # Окраска — первичный документ без основания; сварка ссылается на окраску.
    assert "ДокументОснование" not in res["painted"]["payload"]
    assert "ЗаказНаПроизводствоОснование_Key" not in res["painted"]["payload"]
    assert "основание: окрасочный заказ" in welded_out["payload"]["Комментарий"]
    assert welded_out["basis_pending_1c_ref"] is True
    assert "ДокументОснование" not in welded_out["payload"]
    assert welded_out["payload"]["Продукция"][0]["Номенклатура_Key"] == "ref-welded"
    # dry-run ничего не пишет
    assert db.query(ProductionOrder).count() == 1
    assert db.query(PaintWeldChainLink).count() == 0


def test_preview_uses_ledger_outstanding_not_legacy_stock(db_session, monkeypatch):
    db = db_session
    painted, welded, painted_product = _setup_pair(
        db, weld_outstanding=6, legacy_welded_stock=999
    )
    _stub_demo(monkeypatch)
    _no_network(monkeypatch)

    res = open_paint_chain(
        db, painted_product_id=painted_product.product_id, dry_run=True
    )

    assert res["verdict"] == "need_weld"
    assert res["welded"]["qty"] == 6.0
    assert float(res["welded"]["payload"]["Продукция"][0]["Количество"]) == 6.0


# ---------------------------------------------------------------------------
# Real write (dry_run=False)
# ---------------------------------------------------------------------------

def test_open_need_weld_creates_orders_in_order_with_basis(db_session, monkeypatch):
    db = db_session
    painted, welded, painted_product = _setup_pair(db, weld_outstanding=10)
    _stub_demo(monkeypatch)
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    res = open_paint_chain(
        db,
        painted_product_id=painted_product.product_id,
        planned_start="2026-08-10",
        dry_run=False,
    )

    assert res["verdict"] == "need_weld"
    # окраска выгружена первой (родитель), сварка — второй (на её основании)
    assert len(fake.posts) == 2
    assert res["painted"]["order_ref1c"] == "ref-1c-1"
    assert res["welded"]["order_ref1c"] == "ref-1c-2"

    # заказы существуют локально
    paint_order = db.query(ProductionOrder).filter(ProductionOrder.order_id == res["painted"]["order_id"]).one()
    weld_order = db.query(ProductionOrder).filter(ProductionOrder.order_id == res["welded"]["order_id"]).one()
    assert paint_order.order_ref1c == "ref-1c-1"
    assert weld_order.order_ref1c == "ref-1c-2"
    weld_product = (
        db.query(ProductionProduct)
        .filter(ProductionProduct.order_id == weld_order.order_id)
        .one()
    )
    assert weld_order.source_run_id == paint_order.source_run_id
    assert weld_product.ledger_generation_id == res["guard"]["ledger_generation_id"]
    assert weld_product.source_mrp_requirement_id == res["guard"]["requirement_id"]
    assert weld_product.source_mrp_allocation_key == (
        f"paint_weld:{paint_order.order_id}:"
        f"requirement:{res['guard']['requirement_id']}"
    )

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
    assert "ДокументОснование_Type" not in paint_payload

    # sync_link на оба заказа
    assert (
        db.query(SyncLink)
        .filter(SyncLink.source_doctype == "production_order", SyncLink.status == "success")
        .count()
        == 2
    )

    expanded = open_paint_chains_for_products(
        db,
        product_ids=[painted_product.product_id],
        initiated_by="test",
    )
    assert expanded["status"] == "ok"
    assert expanded["entries"][0]["status"] == "ok"
    assert expanded["product_ids"] == sorted(
        [painted_product.product_id, weld_product.product_id]
    )


def test_weld_document_is_entered_on_basis_of_paint_document(db_session, monkeypatch):
    """Направление основания в 1С: сварной заказ — подчинённый окрасочного.

    Сваренная деталь входит в состав окрашенной, поэтому родителем цепочки в 1С
    является окрасочный заказ: он выгружается ПЕРВЫМ и без основания, а
    сварочный вводится ВТОРЫМ на его основании и как подчинённый закрывается
    раньше. Плановые даты остаются производственными: сварка финиширует к
    старту окраски.
    """
    db = db_session
    _painted, _welded, painted_product = _setup_pair(db, weld_outstanding=10)
    _stub_demo(monkeypatch)
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    res = open_paint_chain(
        db,
        painted_product_id=painted_product.product_id,
        planned_start="2026-08-10",
        dry_run=False,
    )

    assert res["status"] == "ok"
    paint_number = res["painted"]["order_number"]
    weld_number = res["welded"]["order_number"]

    # 1. Порядок выгрузки: окраска первой, сварка второй.
    posted_numbers = [payload.get("Number") for _entity, payload in fake.posts]
    paint_ref = res["painted"]["order_ref1c"]
    weld_ref = res["welded"]["order_ref1c"]
    assert len(fake.posts) == 2
    assert posted_numbers[0] != posted_numbers[1]
    paint_payload, weld_payload = fake.posts[0][1], fake.posts[1][1]
    assert paint_payload["Продукция"][0]["Номенклатура_Key"] == "ref-painted"
    assert weld_payload["Продукция"][0]["Номенклатура_Key"] == "ref-welded"

    # 2. У окрасочного (родительского) документа основания нет.
    for field in (
        "ЗаказНаПроизводствоОснование_Key",
        "ДокументОснование",
        "ДокументОснование_Type",
    ):
        assert field not in paint_payload

    # 3. Сварочный документ введён на основании окрасочного.
    assert weld_payload["ЗаказНаПроизводствоОснование_Key"] == paint_ref
    assert weld_payload["ДокументОснование"] == paint_ref
    assert (
        weld_payload["ДокументОснование_Type"]
        == "StandardODATA.Document_ЗаказНаПроизводство"
    )
    assert "основание: окрасочный заказ" in weld_payload["Комментарий"]
    assert paint_ref in weld_payload["Комментарий"]
    assert weld_ref != paint_ref
    assert res["welded"]["basis"] is not None
    assert "basis" not in res["painted"]

    # 4. Производственная последовательность не перевёрнута: сварка финиширует
    #    к старту окраски.
    assert res["welded"]["planned_finish_date"] == "2026-08-10"

    # 5. Локальная связь цепочки не меняет ролей колонок.
    link = db.query(PaintWeldChainLink).one()
    assert link.painted_order_id == res["painted"]["order_id"]
    assert link.welded_order_id == res["welded"]["order_id"]
    assert paint_number and weld_number


def test_chain_opens_on_top_of_an_already_exported_paint_order(db_session, monkeypatch):
    """Окрасочный заказ уже в 1С — цепочка достраивает сварочный на его основании.

    В новом направлении окрасочный документ первичен и не требует основания,
    поэтому его наличие в 1С не блокирует открытие цепочки: повторно он не
    отправляется, а сварочный уходит вторым с его Ref_Key в основании.
    """
    db = db_session
    _painted, _welded, painted_product = _setup_pair(db, weld_outstanding=0)
    _stub_demo(monkeypatch)
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    covered = open_paint_chain(
        db, painted_product_id=painted_product.product_id, dry_run=False
    )
    assert covered["verdict"] == "stock_covers"
    assert covered["welded"] is None
    assert len(fake.posts) == 1
    paint_ref = covered["painted"]["order_ref1c"]
    assert paint_ref

    reservation = db.query(models.ReservationEntry).one()
    reservation.reserved_qty = 10
    reservation.replenishment_required_qty = 10
    db.commit()

    opened = open_paint_chain(
        db, painted_product_id=painted_product.product_id, dry_run=False
    )

    assert opened["status"] == "ok"
    assert opened["verdict"] == "need_weld"
    # окрасочный документ не отправлен повторно; в 1С ушёл только сварочный
    assert len(fake.posts) == 2
    assert opened["painted"]["order_ref1c"] == paint_ref
    weld_payload = fake.posts[1][1]
    assert weld_payload["Продукция"][0]["Номенклатура_Key"] == "ref-welded"
    assert weld_payload["ЗаказНаПроизводствоОснование_Key"] == paint_ref
    assert weld_payload["ДокументОснование"] == paint_ref


def test_weld_order_is_not_exported_when_paint_order_has_no_ref(db_session, monkeypatch):
    """Без подтверждённой окраски сварочный документ не отправляется."""
    db = db_session
    _painted, _welded, painted_product = _setup_pair(db, weld_outstanding=10)
    _stub_demo(monkeypatch)
    fake = _RefusingClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    with pytest.raises(ValueError, match="без подтверждённого окрасочного основания"):
        open_paint_chain(
            db,
            painted_product_id=painted_product.product_id,
            planned_start="2026-08-10",
            dry_run=False,
        )

    # Ровно одна попытка POST — окрасочная; сварки в 1С нет.
    assert len(fake.posts) == 1
    assert db.query(ProductionOrder).count() == 2
    assert db.query(PaintWeldChainLink).count() == 1
    assert all(not (order.order_ref1c or "") for order in db.query(ProductionOrder).all())


def test_weld_order_is_not_exported_when_paint_export_errors(db_session, monkeypatch):
    """Ошибка первого окрасочного этапа не допускает POST сварки."""
    db = db_session
    _painted, _welded, painted_product = _setup_pair(db, weld_outstanding=10)
    _stub_demo(monkeypatch)
    fake = _FailingClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    with pytest.raises(ValueError) as excinfo:
        open_paint_chain(
            db,
            painted_product_id=painted_product.product_id,
            planned_start="2026-08-10",
            dry_run=False,
        )

    message = str(excinfo.value)
    assert "не заполнена организация" in message
    assert "не выгружен" in message
    assert len(fake.posts) == 1
    assert db.query(ProductionOrder).count() == 2
    assert db.query(PaintWeldChainLink).count() == 1


def test_open_fails_closed_when_frozen_weld_bom_is_missing(db_session, monkeypatch):
    db = db_session
    _painted, _welded, painted_product = _setup_pair(db, weld_outstanding=10)
    db.query(MrpFreezeComponent).delete(synchronize_session=False)
    db.commit()
    _stub_demo(monkeypatch)
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    with pytest.raises(ValueError, match="frozen BOM rows are missing"):
        open_paint_chain(
            db,
            painted_product_id=painted_product.product_id,
            planned_start="2026-08-10",
            dry_run=False,
        )

    assert fake.posts == []
    assert db.query(ProductionOrder).count() == 1
    assert db.query(PaintWeldChainLink).count() == 0


def test_weld_failure_keeps_paint_success_and_retry_posts_only_weld(db_session, monkeypatch):
    db = db_session
    _painted, _welded, painted_product = _setup_pair(db, weld_outstanding=10)
    _stub_demo(monkeypatch)
    first_client = _FailSecondClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: first_client)

    first = open_paint_chain(
        db,
        painted_product_id=painted_product.product_id,
        planned_start="2026-08-10",
        dry_run=False,
    )
    assert first["status"] == "partial_error"
    assert first["painted"]["order_ref1c"] == "ref-paint-success"
    assert first["welded"]["order_ref1c"] is None
    assert len(first_client.posts) == 2

    retry_client = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: retry_client)
    second = open_paint_chain(
        db,
        painted_product_id=painted_product.product_id,
        planned_start="2026-08-10",
        dry_run=False,
    )
    assert second["status"] == "ok"
    assert len(retry_client.posts) == 1
    assert retry_client.posts[0][1]["ЗаказНаПроизводствоОснование_Key"] == "ref-paint-success"


def test_open_is_idempotent_on_repeat(db_session, monkeypatch):
    db = db_session
    painted, welded, painted_product = _setup_pair(db, weld_outstanding=10)
    _stub_demo(monkeypatch)
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    first = open_paint_chain(
        db,
        painted_product_id=painted_product.product_id,
        planned_start="2026-08-10",
        dry_run=False,
    )
    assert len(fake.posts) == 2

    second = open_paint_chain(
        db,
        painted_product_id=painted_product.product_id,
        planned_start="2026-08-10",
        dry_run=False,
    )

    # никаких повторных POST — оба заказа уже в 1С (sync_link/order_ref1c)
    assert len(fake.posts) == 2
    assert second["painted"]["order_id"] == first["painted"]["order_id"]
    assert second["welded"]["order_id"] == first["welded"]["order_id"]
    # без дублей заказов/связей
    assert db.query(ProductionOrder).count() == 2
    assert db.query(PaintWeldChainLink).count() == 1


def test_open_dry_run_writes_nothing(db_session, monkeypatch):
    db = db_session
    painted, welded, painted_product = _setup_pair(db, weld_outstanding=10)
    _stub_demo(monkeypatch)
    _no_network(monkeypatch)

    open_paint_chain(
        db,
        painted_product_id=painted_product.product_id,
        planned_start="2026-08-10",
        dry_run=True,
    )

    assert db.query(ProductionOrder).count() == 1
    assert db.query(PaintWeldChainLink).count() == 0
    assert db.query(SyncLink).count() == 0


def test_item_only_chain_is_rejected_as_unpublished_demand(db_session):
    painted, _welded, _product = _setup_pair(
        db_session, weld_outstanding=10
    )

    with pytest.raises(ValueError, match="unpublished demand"):
        open_paint_chain(
            db_session,
            painted_item_id=painted.item_id,
            qty=10,
            dry_run=True,
        )


def test_chain_rejects_stale_painted_materialization(db_session):
    _painted, _welded, product = _setup_pair(
        db_session, weld_outstanding=10
    )
    product.ledger_generation_id = None
    db_session.commit()

    with pytest.raises(ValueError, match="null, mixed or stale Ledger lineage"):
        open_paint_chain(
            db_session, painted_product_id=product.product_id, dry_run=True
        )


def test_existing_weld_allocations_reduce_shared_obligation(
    db_session, monkeypatch
):
    _painted, _welded, painted_product = _setup_pair(
        db_session, weld_outstanding=10, legacy_welded_stock=999
    )
    req = db_session.query(models.MrpRequirement).one()
    generation = db_session.query(models.LedgerGeneration).one()
    run = db_session.query(PlanningRun).one()
    already_order = ProductionOrder(
        order_number="WELD-ALREADY",
        order_date=datetime(2026, 8, 1, tzinfo=timezone.utc),
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=run.run_id,
    )
    db_session.add(already_order)
    db_session.flush()
    already_product = ProductionProduct(
        order_id=already_order.order_id,
        item_id=req.item_id,
        line_number=1,
        quantity=7,
        produced_qty=999,  # legacy cache must not affect allocation
        remaining_qty=999,
        source_mrp_requirement_id=req.id,
        source_mrp_allocation_key="existing-ledger-allocation",
        ledger_generation_id=generation.id,
    )
    db_session.add(already_product)
    db_session.flush()
    reservation = db_session.query(models.ReservationEntry).filter_by(
        requirement_id=req.id, realization_mode="make"
    ).one()
    reservation.realized_qty = 2
    reservation.replenishment_received_qty = 2
    db_session.commit()
    _stub_demo(monkeypatch)
    _no_network(monkeypatch)

    result = open_paint_chain(
        db_session,
        painted_product_id=painted_product.product_id,
        dry_run=True,
    )

    assert result["guard"]["allocated_qty"] == 5
    assert result["guard"]["outstanding_qty"] == 3
    assert result["welded"]["qty"] == 3


def test_welded_material_issues_use_frozen_bom_after_default_spec_change(
    db_session, monkeypatch
):
    db = db_session
    painted, welded, painted_product = _setup_pair(db, weld_outstanding=10)

    run = db.query(PlanningRun).one()
    run_id = int(run.run_id)
    freeze_version = run.active_freeze_version
    assert freeze_version is not None

    old_weld_default = db.query(DefaultSpecification).filter(
        DefaultSpecification.item_id == int(welded.item_id)
    ).one()
    old_weld_spec = db.get(Specification, int(old_weld_default.spec_id))
    assert old_weld_spec is not None
    old_weld_spec.content_hash = "spec_hash_frozen_v1"
    db.query(MrpFreezeComponent).filter(
        MrpFreezeComponent.parent_item_id == int(welded.item_id)
    ).delete(synchronize_session=False)
    db.query(SpecComponent).filter(
        SpecComponent.spec_id == int(old_weld_spec.spec_id)
    ).delete(synchronize_session=False)

    old_component = _item(
        db,
        code="CMP-A",
        name="Комплект для старой сварки",
        ref1c="ref-comp-a",
    )
    new_component = _item(
        db,
        code="CMP-B",
        name="Комплект для новой сварки",
        ref1c="ref-comp-b",
    )

    db.add(
        SpecComponent(
            spec_id=int(old_weld_spec.spec_id),
            item_id=int(old_component.item_id),
            quantity=1,
            component_type="Сборка",
        )
    )
    db.add(
        MrpFreezeComponent(
            run_id=run_id,
            freeze_version=int(freeze_version),
            parent_item_id=int(welded.item_id),
            component_item_id=int(old_component.item_id),
            spec_ref=str(old_weld_spec.spec_ref1c or ""),
            spec_version=str(old_weld_spec.content_hash or ""),
            norm_qty_per_unit=1,
        )
    )

    new_weld_spec = Specification(
        spec_name="Сварка кронштейна v2",
        spec_ref1c="spec-weld-updated",
        content_hash="spec_hash_live_v2",
    )
    db.add(new_weld_spec)
    db.flush()
    db.add(
        SpecComponent(
            spec_id=int(new_weld_spec.spec_id),
            item_id=int(new_component.item_id),
            quantity=1,
            component_type="Сборка",
        )
    )
    old_weld_default.spec_id = int(new_weld_spec.spec_id)

    weld_resource = db.query(ProductionResource).filter(
        ProductionResource.resource_name == "Участок сварочный"
    ).one()
    db.add(
        WorkshopWarehouseBinding(
            workshop_id=int(weld_resource.resource_id),
            warehouse_ref1c="warehouse-weld",
            production_warehouse_ref1c="warehouse-out",
        )
    )
    for comp in [old_component, new_component]:
        db.add(
            StockBin(
                ledger_generation_id=int(run.ledger_generation_id),
                item_id=int(comp.item_id),
                warehouse_ref1c="warehouse-source",
                on_hand=10,
                characteristic_ref="",
                organization_ref="",
            )
        )
    db.flush()

    _stub_demo(monkeypatch)
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)
    monkeypatch.setattr(
        custody_projection,
        "load_current_accepted_material_custody",
        lambda _db, consumer=None: (
            int(run.ledger_generation_id),
            custody_projection.MaterialCustodyState(),
        ),
    )
    monkeypatch.setattr(
        material_issues,
        "load_current_accepted_material_custody",
        lambda _db, consumer=None: (
            int(run.ledger_generation_id),
            custody_projection.MaterialCustodyState(),
        ),
    )

    result = open_paint_chain(
        db,
        painted_product_id=painted_product.product_id,
        planned_start="2026-08-10",
        dry_run=False,
    )

    assert result["verdict"] == "need_weld"
    weld_order_id = int(result["welded"]["order_id"])
    welded_product = (
        db.query(ProductionProduct)
        .filter(ProductionProduct.order_id == weld_order_id)
        .one()
    )
    assert welded_product.spec_id == int(old_weld_spec.spec_id)
    assert str(welded_product.spec_revision_hash or "") == str(old_weld_spec.content_hash)

    issues = material_issues.create_material_issues(
        db,
        product_ids=[int(welded_product.product_id)],
        initiated_by="pytest",
    )
    assert issues["status"] == "ok"
    assert not issues["selection_required"]
    assert issues["errors"] == []

    issue_line = (
        db.query(ProductionMaterialIssueLine)
        .filter(ProductionMaterialIssueLine.issue_id == int(issues["created"][0]["issue_id"]))
        .first()
    )
    assert issue_line is not None
    assert int(issue_line.component_item_id) == int(old_component.item_id)
    assert int(issue_line.source_spec_id or 0) == int(old_weld_spec.spec_id)
