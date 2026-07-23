"""Фаза 3 — materialization of DBR decisions into 1С.

Covers release_slot / launch_signal / release_day: dry-run writes nothing,
real write stamps sync_link (source_system='dbr') idempotently, red slots and
deficit signals are refused with 409-worthy conflicts, and release_day isolates
per-slot failures.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models import (
    DbrDrumSchedule,
    DbrDrumSlot,
    DbrFeederSignal,
    DbrSupermarketPosition,
    DefaultSpecification,
    Item,
    ItemWarehouseStock,
    ProductionOrder,
    ProductionProduct,
    ProductionResource,
    SpecComponent,
    Specification,
    SyncLink,
    WorkshopWarehouseBinding,
)
from app.services.dbr import (
    feeder_material_service,
    materialize_service,
    settings_service,
)
from app.services.dbr.core.drum.kit import KitLine
from app.services.dbr.core.feeder import signal_identity


# ---------------------------------------------------------------------------
# Fakes / stubs
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, *, ref_key="dbr-1c-ref", fail=False, existing_docs=None):
        self.ref_key = ref_key
        self.fail = fail
        self.posts = []
        self.patches = []
        self.operations = []
        self.existing_docs = existing_docs or []

    def get_all(self, _entity, **_kwargs):
        return list(self.existing_docs)

    def post(self, entity, payload, **_):
        self.posts.append((entity, payload))
        if self.fail:
            raise RuntimeError("simulated 1C failure")
        return {"Ref_Key": self.ref_key}

    def patch(self, entity_ref, payload, **_):
        self.patches.append((entity_ref, payload))
        return {}

    def post_operation(self, path):
        self.operations.append(path)


def _stub(monkeypatch, *, base_url="http://demo/odata/unf_demo", client=None):
    monkeypatch.setattr(
        materialize_service,
        "_load_odata_config",
        lambda: {"base_url": base_url, "username": "u", "password": "p"},
    )
    if client is not None:
        monkeypatch.setattr(materialize_service, "OData1CClient", lambda **_: client)


# ---------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------


def _slot_scenario(db, *, kit_status="green", qty=2):
    settings_service.get_or_create_settings(db)
    resource = ProductionResource(resource_name="Сборка A", capacity=1)
    item = Item(item_code="SLED", item_name="Снегоход", item_article="SLED",
                item_ref1c="item-ref-sled", unit="unit-ref")
    comp = Item(item_code="COMP", item_name="Деталь", item_ref1c="item-ref-comp", unit="unit-ref")
    db.add_all([resource, item, comp])
    db.flush()
    spec = Specification(spec_name="Spec SLED", spec_ref1c="spec-ref")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=2))
    db.add(
        WorkshopWarehouseBinding(
            workshop_id=resource.resource_id,
            warehouse_ref1c="wip-wh-ref",
            production_warehouse_ref1c="prod-wh-ref",
        )
    )
    schedule = DbrDrumSchedule(
        period_from=date(2026, 8, 1), period_to=date(2026, 8, 31), status="active"
    )
    db.add(schedule)
    db.flush()
    slot = DbrDrumSlot(
        schedule_id=schedule.id,
        slot_date=date(2026, 8, 10),
        planned_date=date(2026, 8, 10),
        resource_id=resource.resource_id,
        item_id=item.item_id,
        qty=Decimal(str(qty)),
        kit_status=kit_status,
    )
    db.add(slot)
    db.commit()
    return slot, schedule, item, comp


def _signal_scenario(db, *, kit_in_stock=True, qty=3):
    settings = settings_service.get_or_create_settings(db)
    settings.w2_warehouse_ref1c = "W2"
    settings.w3_warehouse_ref1c = "W3"
    settings.w4_warehouse_ref1c = "W4"
    item = Item(item_code="PROD", item_name="Узел", item_article="PROD",
                item_ref1c="item-ref-prod", unit="unit-ref",
                replenishment_method="Производство")
    comp = Item(item_code="COMP", item_name="Заготовка", item_ref1c="item-ref-comp",
                unit="unit-ref")
    db.add_all([item, comp])
    db.flush()
    spec = Specification(spec_name="Spec PROD", spec_ref1c="spec-ref")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=1))
    db.add(ItemWarehouseStock(item_id=comp.item_id, warehouse_ref1c="STK",
                              qty=100 if kit_in_stock else 0))
    signal = DbrFeederSignal(
        dedup_key="R:PROD",
        signal_type="Пополнение",
        item_id=item.item_id,
        warehouse_ref1c="W2",
        status=signal_identity.OPEN,
        suggested_qty=Decimal(str(qty)),
        priority=Decimal("1.0"),
        need_date=date(2026, 8, 5),
        required_date=date(2026, 8, 12),
    )
    db.add(signal)
    db.commit()
    return signal, item, comp


# ---------------------------------------------------------------------------
# release_slot
# ---------------------------------------------------------------------------


def test_release_slot_dry_run_writes_nothing(db_session, monkeypatch):
    db = db_session
    slot, _sch, item, _comp = _slot_scenario(db)
    fake = _FakeClient()
    _stub(monkeypatch, client=fake)

    res = materialize_service.release_slot(db, slot.id, dry_run=True)

    assert res["dry_run"] is True
    assert res["created"] is False
    assert res["payload"]["Продукция"][0]["Номенклатура_Key"] == item.item_ref1c
    assert float(res["payload"]["Продукция"][0]["Количество"]) == 2.0
    assert res["payload"]["Продукция"][0]["СтруктурнаяЕдиница_Key"] == "prod-wh-ref"
    assert res["payload"]["СтруктурнаяЕдиницаРезерв_Key"] == "wip-wh-ref"
    assert "dbr" in res["payload"]["Комментарий"]
    # no writes
    assert fake.posts == []
    db.refresh(slot)
    assert slot.release_status == "pending"
    assert slot.one_c_order_ref is None
    assert db.query(SyncLink).count() == 0


def test_release_slot_real_write_stamps_sync_link_and_slot(db_session, monkeypatch):
    db = db_session
    slot, _sch, _item, _comp = _slot_scenario(db)
    fake = _FakeClient(ref_key="ref-slot-1")
    _stub(monkeypatch, client=fake)

    res = materialize_service.release_slot(db, slot.id, dry_run=False)

    assert res["created"] is True
    assert res["release_status"] == "released"
    assert res["one_c_order_ref"] == "ref-slot-1"
    assert len(fake.posts) == 1
    assert fake.operations == [
        "Document_ЗаказНаПроизводство(guid'ref-slot-1')/Post?PostingModeOperational=true"
    ]
    db.refresh(slot)
    assert slot.release_status == "released"
    assert slot.one_c_order_ref == "ref-slot-1"
    link = db.query(SyncLink).filter_by(
        source_system="dbr", source_doctype="drum_slot", source_id=slot.id
    ).one()
    assert link.status == "success"
    assert link.target_ref_key == "ref-slot-1"


def test_release_slot_second_call_is_idempotent(db_session, monkeypatch):
    db = db_session
    slot, _sch, _item, _comp = _slot_scenario(db)
    fake = _FakeClient(ref_key="ref-slot-2")
    _stub(monkeypatch, client=fake)

    materialize_service.release_slot(db, slot.id, dry_run=False)
    assert len(fake.posts) == 1

    res = materialize_service.release_slot(db, slot.id, dry_run=False)
    assert res["created"] is False
    assert res["already_released"] is True
    assert res["one_c_order_ref"] == "ref-slot-2"
    assert len(fake.posts) == 1  # no second POST


def test_release_slot_recovers_cross_instance_document_without_post(db_session, monkeypatch):
    db = db_session
    slot, _sch, _item, _comp = _slot_scenario(db)
    preview = materialize_service.release_slot(db, slot.id, dry_run=True)
    comment = preview["payload"]["Комментарий"]
    assert "prodplan-origin=" in comment
    fake = _FakeClient(
        existing_docs=[
            {
                "Ref_Key": "parallel-slot-ref",
                "Number": preview["number"],
                "Комментарий": comment,
                "Posted": True,
            }
        ]
    )
    _stub(monkeypatch, client=fake)

    result = materialize_service.release_slot(db, slot.id, dry_run=False)

    assert fake.posts == []
    assert result["created"] is False
    assert result["one_c_order_ref"] == "parallel-slot-ref"
    db.refresh(slot)
    assert slot.one_c_order_ref == "parallel-slot-ref"


def test_release_slot_red_is_conflict(db_session, monkeypatch):
    db = db_session
    slot, _sch, _item, _comp = _slot_scenario(db, kit_status="red")
    fake = _FakeClient()
    _stub(monkeypatch, client=fake)

    with pytest.raises(materialize_service.MaterializeConflict) as exc:
        materialize_service.release_slot(db, slot.id, dry_run=True)
    assert "зелён" in exc.value.detail.lower() or "green" in exc.value.detail.lower()
    assert fake.posts == []
    db.refresh(slot)
    assert slot.release_status == "pending"


def test_release_slot_missing_slot_raises_lookup(db_session, monkeypatch):
    db = db_session
    _stub(monkeypatch)
    with pytest.raises(LookupError):
        materialize_service.release_slot(db, 999999, dry_run=True)


# ---------------------------------------------------------------------------
# launch_signal
# ---------------------------------------------------------------------------


def test_launch_signal_dry_run_writes_nothing(db_session, monkeypatch):
    db = db_session
    signal, item, _comp = _signal_scenario(db, kit_in_stock=True)
    monkeypatch.setattr(
        feeder_material_service, "build_kit",
        lambda code, *_: [KitLine("COMP", 1, "W4", False)] if code == "PROD" else [],
    )
    fake = _FakeClient()
    _stub(monkeypatch, client=fake)

    res = materialize_service.launch_signal(db, signal.id, dry_run=True)

    assert res["dry_run"] is True
    assert res["created"] is False
    assert res["payload"]["Продукция"][0]["Номенклатура_Key"] == item.item_ref1c
    # produced part lands on the shelf warehouse
    assert res["payload"]["Продукция"][0]["СтруктурнаяЕдиница_Key"] == "W2"
    assert fake.posts == []
    db.refresh(signal)
    assert signal.status == signal_identity.OPEN
    assert db.query(SyncLink).count() == 0


def test_launch_signal_real_write_moves_to_order_created(db_session, monkeypatch):
    db = db_session
    signal, _item, _comp = _signal_scenario(db, kit_in_stock=True)
    monkeypatch.setattr(
        feeder_material_service, "build_kit",
        lambda code, *_: [KitLine("COMP", 1, "W4", False)] if code == "PROD" else [],
    )
    fake = _FakeClient(ref_key="ref-sig-1")
    _stub(monkeypatch, client=fake)
    local_order = ProductionOrder(
        order_number=f"DBR-S{signal.id}",
        order_date=date(2026, 8, 1),
        source="dbr",
    )
    db.add(local_order)
    db.flush()
    db.add(ProductionProduct(
        order_id=local_order.order_id,
        item_id=signal.item_id,
        line_number=1,
        quantity=signal.suggested_qty,
        remaining_qty=signal.suggested_qty,
        produced_qty=0,
        source_dbr_signal_id=signal.id,
    ))
    db.flush()

    res = materialize_service.launch_signal(db, signal.id, dry_run=False)

    assert res["created"] is True
    assert res["status"] == signal_identity.ORDER_CREATED
    assert res["one_c_order_ref"] == "ref-sig-1"
    db.refresh(signal)
    assert signal.status == signal_identity.ORDER_CREATED
    assert signal.one_c_order_ref == "ref-sig-1"
    db.refresh(local_order)
    assert local_order.order_ref1c == "ref-sig-1"
    assert local_order.order_number
    link = db.query(SyncLink).filter_by(
        source_system="dbr", source_doctype="feeder_signal", source_id=signal.id
    ).one()
    assert link.status == "success"


def test_launch_signal_requires_explicit_production_override(db_session, monkeypatch):
    db = db_session
    signal, _item, _comp = _signal_scenario(db, kit_in_stock=True)
    monkeypatch.setattr(
        feeder_material_service, "build_kit",
        lambda code, *_: [KitLine("COMP", 1, "W4", False)] if code == "PROD" else [],
    )
    fake = _FakeClient()
    _stub(monkeypatch, base_url="http://prod/odata/unf", client=fake)

    with pytest.raises(PermissionError):
        materialize_service.launch_signal(db, signal.id, dry_run=False)
    assert fake.posts == []

    result = materialize_service.launch_signal(
        db, signal.id, dry_run=False, allow_production=True
    )
    assert result["created"] is True


def test_launch_signal_deficit_is_conflict_with_deficit_lines(db_session, monkeypatch):
    db = db_session
    signal, _item, _comp = _signal_scenario(db, kit_in_stock=False)
    monkeypatch.setattr(
        feeder_material_service, "build_kit",
        lambda code, *_: [KitLine("COMP", 1, "W4", False)] if code == "PROD" else [],
    )
    fake = _FakeClient()
    _stub(monkeypatch, client=fake)

    with pytest.raises(materialize_service.MaterializeConflict) as exc:
        materialize_service.launch_signal(db, signal.id, dry_run=False)
    assert "дефицит" in exc.value.detail.lower()
    assert exc.value.payload["deficit_lines"]
    assert fake.posts == []
    db.refresh(signal)
    assert signal.status == signal_identity.OPEN


def test_launch_signal_non_open_is_conflict(db_session, monkeypatch):
    db = db_session
    signal, _item, _comp = _signal_scenario(db, kit_in_stock=True)
    signal.status = signal_identity.CANCELLED
    db.commit()
    _stub(monkeypatch, client=_FakeClient())
    with pytest.raises(materialize_service.MaterializeConflict):
        materialize_service.launch_signal(db, signal.id, dry_run=True)


# ---------------------------------------------------------------------------
# release_day
# ---------------------------------------------------------------------------


def test_release_day_partial_failure_keeps_successes(db_session, monkeypatch):
    db = db_session
    slot_ok, schedule, item, _comp = _slot_scenario(db)
    # a second green+pending slot for the same day
    slot_bad = DbrDrumSlot(
        schedule_id=schedule.id,
        slot_date=date(2026, 8, 10),
        planned_date=date(2026, 8, 10),
        resource_id=slot_ok.resource_id,
        item_id=item.item_id,
        qty=Decimal("1"),
        kit_status="green",
        position=1,
    )
    db.add(slot_bad)
    db.commit()

    calls = {"n": 0}

    class _SometimesFail:
        def post(self, entity, payload, **_):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("boom")
            return {"Ref_Key": "ref-day-ok"}

        def post_operation(self, path):
            pass

    _stub(monkeypatch, client=None)
    monkeypatch.setattr(materialize_service, "OData1CClient", lambda **_: _SometimesFail())

    res = materialize_service.release_day(db, schedule.id, date(2026, 8, 10), dry_run=False)

    assert res["slots_total"] == 2
    assert res["released"] == 1
    assert res["errors"] == 1
    # the first slot committed its release despite the second failing
    db.refresh(slot_ok)
    assert slot_ok.release_status == "released"
    assert slot_ok.one_c_order_ref == "ref-day-ok"


def test_release_day_dry_run_previews_only(db_session, monkeypatch):
    db = db_session
    slot, schedule, _item, _comp = _slot_scenario(db)
    _stub(monkeypatch, client=_FakeClient())

    res = materialize_service.release_day(db, schedule.id, date(2026, 8, 10), dry_run=True)
    assert res["dry_run"] is True
    assert res["previews"] == 1
    assert res["released"] == 0
    db.refresh(slot)
    assert slot.release_status == "pending"
    assert db.query(SyncLink).count() == 0
