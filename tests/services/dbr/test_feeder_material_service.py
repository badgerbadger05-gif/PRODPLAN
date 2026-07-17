"""Material readiness of the mechshop queue (Фаза 3.1 port)."""

from decimal import Decimal

import pytest

from app.models import (
    DbrFeederSignal,
    DbrSupermarketPosition,
    DefaultSpecification,
    Item,
    ItemWarehouseStock,
    Specification,
    SpecComponent,
)
from app.services.dbr import feeder_material_service, settings_service
from app.services.dbr.core.drum.kit import KitLine


def _settings(db):
    settings = settings_service.get_or_create_settings(db)
    settings.w2_warehouse_ref1c = "W2"
    settings.w3_warehouse_ref1c = "W3"
    settings.w4_warehouse_ref1c = "W4"
    db.flush()
    return settings


def _item(db, code, *, purchase=False, spec=False, name=None):
    item = Item(
        item_code=code,
        item_name=name or code,
        replenishment_method="Закупка" if purchase else "Производство",
    )
    db.add(item)
    db.flush()
    if spec:
        specification = Specification(spec_ref1c=f"spec-{code}", spec_name=f"spec-{code}")
        db.add(specification)
        db.flush()
        db.add(DefaultSpecification(item_id=item.item_id, spec_id=specification.spec_id))
        db.flush()
    return item


def _signal(db, item, qty, *, priority="1.0", status="Open", need_date=None):
    signal = DbrFeederSignal(
        dedup_key=f"R:{item.item_code}",
        signal_type="Пополнение",
        item_id=item.item_id,
        warehouse_ref1c="W2",
        status=status,
        suggested_qty=Decimal(str(qty)),
        priority=Decimal(priority),
        need_date=need_date,
    )
    db.add(signal)
    db.flush()
    return signal


def _shelf(db, item):
    pos = DbrSupermarketPosition(
        item_id=item.item_id, warehouse_ref1c="W3", supply_type="manufacture",
        mode="shelf", is_active=True, is_stale=False, adu=1, commonality=1,
        rt_days=1, rt_source="class", batch_days=1, q_batch=1,
        k_var=Decimal("0.5"), supply_risk_pct=0, red_qty=1, yellow_qty=1,
        green_qty=1, target_qty=3, data_quality=[], calculation_snapshot={},
    )
    db.add(pos)
    db.flush()
    return pos


def _stock(db, item, qty, warehouse="STK"):
    db.add(ItemWarehouseStock(item_id=item.item_id, warehouse_ref1c=warehouse, qty=qty))
    db.flush()


def test_partial_readiness_and_deficit_lines(db_session, monkeypatch):
    db = db_session
    _settings(db)
    prod = _item(db, "PROD")
    make = _item(db, "MAKE1", spec=True)      # produced, no shelf
    shelf_item = _item(db, "SHELF1", spec=True)
    buy = _item(db, "BUY1", purchase=True)
    _shelf(db, shelf_item)
    _stock(db, make, 0)
    _stock(db, shelf_item, 0)
    _stock(db, buy, 0)

    def fake_kit(code, *_):
        if code == "PROD":
            return [
                KitLine("MAKE1", 1, "W4", True),
                KitLine("SHELF1", 1, "W3", False),
                KitLine("BUY1", 1, "W4", False),
            ]
        return []

    monkeypatch.setattr(feeder_material_service, "build_kit", fake_kit)
    signal = _signal(db, prod, 3)

    result = feeder_material_service.annotate_queue(db, [signal])
    note = result["annotations"][signal.id]
    assert note["material_status"] == "Дефицит"
    assert {ln["item"] for ln in note["deficit_lines"]} == {"MAKE1", "SHELF1", "BUY1"}
    # kind is derived from produced/purchase, independent of the shelf.
    kinds = {ln["item"]: ln["kind"] for ln in note["kit_lines"]}
    assert kinds["MAKE1"] == "make" and kinds["BUY1"] == "buy"
    assert dict((ln["item"], ln["buffered"]) for ln in note["kit_lines"])["SHELF1"] is True


def test_cumulative_netting_second_signal_is_scheduled_above(db_session, monkeypatch):
    db = db_session
    _settings(db)
    prod = _item(db, "PROD")
    make = _item(db, "MAKE1", spec=True)
    _stock(db, make, 3)  # only enough for one signal

    monkeypatch.setattr(
        feeder_material_service, "build_kit",
        lambda code, *_: [KitLine("MAKE1", 1, "W4", True)] if code == "PROD" else [],
    )
    high = _signal(db, prod, 3, priority="2.0")
    # a second Пополнение for the same product, lower priority
    prod2 = _item(db, "PROD2")
    monkeypatch.setattr(
        feeder_material_service, "build_kit",
        lambda code, *_: [KitLine("MAKE1", 1, "W4", True)] if code in ("PROD", "PROD2") else [],
    )
    low = _signal(db, prod2, 3, priority="1.0")
    low.dedup_key = "R:PROD2"
    db.flush()

    queue = feeder_material_service.live_queue(db)
    result = feeder_material_service.annotate_queue(db, queue)
    assert result["annotations"][high.id]["material_status"] == "Готов"
    assert result["annotations"][low.id]["material_status"] == "Расписан выше"


def test_deficit_aggregate_sums_need_and_blocked_signals(db_session, monkeypatch):
    db = db_session
    _settings(db)
    a = _item(db, "PROD-A")
    b = _item(db, "PROD-B")
    make = _item(db, "MAKE1", spec=True)
    _stock(db, make, 1)  # gross 1, total need 4 -> short 3

    monkeypatch.setattr(
        feeder_material_service, "build_kit",
        lambda code, *_: [KitLine("MAKE1", 1, "W4", True)] if code in ("PROD-A", "PROD-B") else [],
    )
    sa = _signal(db, a, 2, priority="2.0")
    sb = _signal(db, b, 2, priority="1.0")
    sb.dedup_key = "R:PROD-B"
    db.flush()

    out = feeder_material_service.get_deficits(db)
    assert out["kpis"]["deficit_materials"] == 1
    deficit = out["deficits"][0]
    assert deficit["item"] == "MAKE1" and deficit["source"] == "make"
    assert deficit["short_qty"] == 3 and deficit["blocks_signals"] == 2


def test_roots_annotation_reports_end_products(db_session, monkeypatch):
    db = db_session
    _settings(db)
    prod = _item(db, "PROD", spec=True)
    make = _item(db, "MAKE1", spec=True)
    # PROD's default spec contains MAKE1 -> PROD is a root of MAKE1.
    prod_spec = db.query(DefaultSpecification).filter_by(item_id=prod.item_id).one().spec_id
    db.add(SpecComponent(spec_id=prod_spec, item_id=make.item_id, quantity=1))
    db.flush()

    monkeypatch.setattr(
        feeder_material_service, "build_kit",
        lambda code, *_: [KitLine("MAKE1", 1, "W4", True)] if code == "MAKE1" else [],
    )
    # A signal directly on the blank MAKE1.
    signal = _signal(db, make, 1)
    result = feeder_material_service.annotate_queue(db, [signal], with_roots=True)
    roots = {r["item"] for r in result["annotations"][signal.id]["root_items"]}
    assert roots == {"PROD"}


def test_unconfigured_settings_raise(db_session, monkeypatch):
    db = db_session
    settings_service.get_or_create_settings(db)  # roles left unset
    prod = _item(db, "PROD")
    signal = _signal(db, prod, 1)
    with pytest.raises(ValueError):
        feeder_material_service.annotate_queue(db, [signal])
