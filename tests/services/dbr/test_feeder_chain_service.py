"""Child «Цепочка» signals — chain explosion (Фаза 3.2 port)."""

from decimal import Decimal

from app.models import (
    DbrFeederSignal,
    DbrSupermarketPosition,
    DefaultSpecification,
    Item,
    ItemWarehouseStock,
    SpecComponent,
    Specification,
)
from app.services.dbr import feeder_chain_service, feeder_material_service, settings_service
from app.services.dbr.core.drum.kit import KitLine


def _settings(db, *, enabled=True):
    settings = settings_service.get_or_create_settings(db)
    settings.w2_warehouse_ref1c = "W2"
    settings.w3_warehouse_ref1c = "W3"
    settings.w4_warehouse_ref1c = "W4"
    settings.feeder_chain_enabled = enabled
    db.flush()
    return settings


def _item(db, code, *, purchase=False, spec=False):
    item = Item(
        item_code=code, item_name=code,
        replenishment_method="Закупка" if purchase else "Производство",
    )
    db.add(item)
    db.flush()
    if spec:
        specification = Specification(spec_ref1c=f"spec-{code}", spec_name=code)
        db.add(specification)
        db.flush()
        db.add(DefaultSpecification(item_id=item.item_id, spec_id=specification.spec_id))
        db.flush()
    return item


def _shelf(db, item):
    db.add(DbrSupermarketPosition(
        item_id=item.item_id, warehouse_ref1c="W3", supply_type="manufacture",
        mode="shelf", is_active=True, is_stale=False, adu=1, commonality=1,
        rt_days=1, rt_source="class", batch_days=1, q_batch=1,
        k_var=Decimal("0.5"), supply_risk_pct=0, red_qty=1, yellow_qty=1,
        green_qty=1, target_qty=3, data_quality=[], calculation_snapshot={},
    ))
    db.flush()


def _stock(db, item, qty):
    db.add(ItemWarehouseStock(item_id=item.item_id, warehouse_ref1c="STK", qty=qty))
    db.flush()


def _parent(db, item, qty=3, *, priority="1.5"):
    signal = DbrFeederSignal(
        dedup_key=f"R:{item.item_code}", signal_type="Пополнение",
        item_id=item.item_id, warehouse_ref1c="W2", status="Open",
        suggested_qty=Decimal(str(qty)), priority=Decimal(priority),
    )
    db.add(signal)
    db.flush()
    return signal


def _base(db, monkeypatch, *, enabled=True):
    """PROD -> {MAKE1 (make, no shelf, no stock), SHELF1 (shelf), BUY1 (purchase)}."""
    _settings(db, enabled=enabled)
    prod = _item(db, "PROD")
    make = _item(db, "MAKE1", spec=True)
    shelf_item = _item(db, "SHELF1", spec=True)
    buy = _item(db, "BUY1", purchase=True)
    _shelf(db, shelf_item)
    for it in (make, shelf_item, buy):
        _stock(db, it, 0)

    def fake_kit(code, *_):
        if code == "PROD":
            return [
                KitLine("MAKE1", 1, "W4", True),
                KitLine("SHELF1", 1, "W3", False),
                KitLine("BUY1", 1, "W4", False),
            ]
        return []

    monkeypatch.setattr(feeder_material_service, "build_kit", fake_kit)
    return prod, make


def test_spawn_only_for_make_without_shelf(db_session, monkeypatch):
    db = db_session
    prod, make = _base(db, monkeypatch)
    parent = _parent(db, prod, qty=3, priority="1.5")

    result = feeder_chain_service.refresh_chain_signals(db)
    assert result["created"] == 1
    children = db.query(DbrFeederSignal).filter(DbrFeederSignal.signal_type == "Цепочка").all()
    assert len(children) == 1
    child = children[0]
    assert child.item_id == make.item_id
    assert child.parent_signal_id == parent.id
    assert child.chain_depth == 1
    assert float(child.suggested_qty) == 3
    assert child.warehouse_ref1c == "W2"
    # priority inherited from the parent
    assert float(child.priority) == 1.5
    # shelf and purchased components never spawn a chain child
    assert {c.item_id for c in children} == {make.item_id}


def test_switch_off_creates_nothing(db_session, monkeypatch):
    db = db_session
    prod, _make = _base(db, monkeypatch, enabled=False)
    _parent(db, prod)
    result = feeder_chain_service.refresh_chain_signals(db)
    assert result["disabled"] is True
    assert db.query(DbrFeederSignal).filter(DbrFeederSignal.signal_type == "Цепочка").count() == 0


def test_refresh_is_idempotent(db_session, monkeypatch):
    db = db_session
    prod, _make = _base(db, monkeypatch)
    _parent(db, prod)
    first = feeder_chain_service.refresh_chain_signals(db)
    second = feeder_chain_service.refresh_chain_signals(db)
    assert first["created"] == 1
    assert second["created"] == 0 and second["updated"] == 0 and second["reopened"] == 0
    assert db.query(DbrFeederSignal).filter(DbrFeederSignal.signal_type == "Цепочка").count() == 1


def test_orphan_child_revoked_when_parent_deficit_covered(db_session, monkeypatch):
    db = db_session
    prod, make = _base(db, monkeypatch)
    _parent(db, prod)
    feeder_chain_service.refresh_chain_signals(db)
    child = db.query(DbrFeederSignal).filter(DbrFeederSignal.signal_type == "Цепочка").one()
    assert child.status == "Open"

    # The parent's blank is now on stock -> its deficit is covered, no child needed.
    db.query(ItemWarehouseStock).filter(ItemWarehouseStock.item_id == make.item_id).one().qty = 100
    db.flush()
    result = feeder_chain_service.refresh_chain_signals(db)
    assert result["revoked"] == 1
    assert child.status == "Cancelled" and child.cancelled_at is not None
    assert float(child.suggested_qty) == 0


def test_orphan_child_revoked_when_parent_closed(db_session, monkeypatch):
    db = db_session
    prod, _make = _base(db, monkeypatch)
    parent = _parent(db, prod)
    feeder_chain_service.refresh_chain_signals(db)
    child = db.query(DbrFeederSignal).filter(DbrFeederSignal.signal_type == "Цепочка").one()

    parent.status = "Cancelled"
    db.flush()
    result = feeder_chain_service.refresh_chain_signals(db)
    assert result["revoked"] == 1
    assert child.status == "Cancelled"


def test_cancelled_child_reopens_on_same_row(db_session, monkeypatch):
    db = db_session
    prod, make = _base(db, monkeypatch)
    _parent(db, prod)
    feeder_chain_service.refresh_chain_signals(db)
    child = db.query(DbrFeederSignal).filter(DbrFeederSignal.signal_type == "Цепочка").one()
    db.query(ItemWarehouseStock).filter(ItemWarehouseStock.item_id == make.item_id).one().qty = 100
    db.flush()
    feeder_chain_service.refresh_chain_signals(db)
    assert child.status == "Cancelled"

    # Deficit returns -> the same stable row reopens (no dedup_key collision).
    db.query(ItemWarehouseStock).filter(ItemWarehouseStock.item_id == make.item_id).one().qty = 0
    db.flush()
    result = feeder_chain_service.refresh_chain_signals(db)
    assert result["reopened"] == 1
    assert db.query(DbrFeederSignal).filter(DbrFeederSignal.signal_type == "Цепочка").count() == 1
    assert child.status == "Open"


def test_multi_pass_drives_chain_to_second_level(db_session, monkeypatch):
    db = db_session
    _settings(db)
    prod = _item(db, "PROD")
    make1 = _item(db, "MAKE1", spec=True)
    make2 = _item(db, "MAKE2", spec=True)
    for it in (make1, make2):
        _stock(db, it, 0)

    def fake_kit(code, *_):
        if code == "PROD":
            return [KitLine("MAKE1", 1, "W4", True)]
        if code == "MAKE1":
            return [KitLine("MAKE2", 1, "W4", True)]
        return []

    monkeypatch.setattr(feeder_material_service, "build_kit", fake_kit)
    _parent(db, prod)

    result = feeder_chain_service.refresh_chain_signals(db)
    assert result["created"] == 2 and result["passes"] >= 2
    depths = {
        db.get(Item, c.item_id).item_code: c.chain_depth
        for c in db.query(DbrFeederSignal).filter(DbrFeederSignal.signal_type == "Цепочка")
    }
    assert depths == {"MAKE1": 1, "MAKE2": 2}


def test_preview_is_read_only_and_sizes_first_level(db_session, monkeypatch):
    db = db_session
    prod, _make = _base(db, monkeypatch)
    _parent(db, prod)
    preview = feeder_chain_service.preview_chain_signals(db)
    assert preview["enabled"] is True
    assert preview["level1_children"] == 1
    assert preview["distinct_items"] == 1
    assert preview["top_items"][0]["item"] == "MAKE1"
    # dry-run writes nothing
    assert db.query(DbrFeederSignal).filter(DbrFeederSignal.signal_type == "Цепочка").count() == 0


def test_processing_preview_reuses_chain_rules_and_writes_nothing(db_session, monkeypatch):
    db = db_session
    prod, make = _base(db, monkeypatch)
    spec = Specification(spec_ref1c="spec-PROD", spec_name="PROD")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=prod.item_id, spec_id=spec.spec_id))
    db.add(
        SpecComponent(
            spec_id=spec.spec_id,
            item_id=make.item_id,
            quantity=1,
            component_type="Сборка",
        )
    )
    position = DbrSupermarketPosition(
        item_id=prod.item_id, warehouse_ref1c="W4", supply_type="processing",
        mode="shelf", is_active=True, is_stale=False, adu=1, commonality=1,
        rt_days=1, rt_source="chain", batch_days=1, q_batch=1,
        k_var=Decimal("0.5"), supply_risk_pct=0, red_qty=1, yellow_qty=1,
        green_qty=1, target_qty=3, data_quality=[], calculation_snapshot={},
    )
    db.add(position)
    db.flush()
    parent = _parent(db, prod)
    parent.supermarket_position_id = position.id
    db.flush()

    preview = feeder_chain_service.preview_processing_chain_signals(db)

    assert preview["read_only"] is True
    assert preview["processing_open_signals"] == 1
    assert preview["desired_children"] == 1
    assert preview["children"][0]["component_item"] == "MAKE1"
    assert preview["unresolved_count"] == 0
    assert db.query(DbrFeederSignal).filter(DbrFeederSignal.signal_type == "Цепочка").count() == 0


def test_processing_preview_only_proposes_single_assembly_component(db_session, monkeypatch):
    db = db_session
    _settings(db)
    covered = _item(db, "COVERED")
    bare = _item(db, "BARE", spec=True)
    other = _item(db, "OTHER", spec=True)
    spec = Specification(spec_ref1c="spec-covered", spec_name="covered")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=covered.item_id, spec_id=spec.spec_id))
    db.add_all(
        [
            SpecComponent(
                spec_id=spec.spec_id,
                item_id=bare.item_id,
                quantity=1,
                component_type="Сборка",
            ),
            SpecComponent(
                spec_id=spec.spec_id,
                item_id=other.item_id,
                quantity=1,
                component_type="Материал",
            ),
        ]
    )
    for item in (bare, other):
        _stock(db, item, 0)
    position = DbrSupermarketPosition(
        item_id=covered.item_id, warehouse_ref1c="W4", supply_type="processing",
        mode="shelf", is_active=True, is_stale=False, adu=1, commonality=1,
        rt_days=1, rt_source="chain", batch_days=1, q_batch=1,
        k_var=Decimal("0.5"), supply_risk_pct=0, red_qty=1, yellow_qty=1,
        green_qty=1, target_qty=3, data_quality=[], calculation_snapshot={},
    )
    db.add(position)
    db.flush()
    parent = _parent(db, covered)
    parent.supermarket_position_id = position.id
    db.flush()
    monkeypatch.setattr(
        feeder_material_service,
        "build_kit",
        lambda code, *_: (
            [
                KitLine("BARE", 1, "W4", True),
                KitLine("OTHER", 1, "W4", True),
            ]
            if code == "COVERED"
            else []
        ),
    )

    preview = feeder_chain_service.preview_processing_chain_signals(db)

    assert [row["component_item"] for row in preview["children"]] == ["BARE"]


def test_processing_refresh_never_creates_non_assembly_shortage(db_session, monkeypatch):
    db = db_session
    _settings(db)
    covered = _item(db, "COVERED-REFRESH")
    bare = _item(db, "BARE-REFRESH", spec=True)
    other = _item(db, "OTHER-REFRESH", spec=True)
    spec = Specification(spec_name="covered-refresh")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=covered.item_id, spec_id=spec.spec_id))
    db.add_all(
        [
            SpecComponent(spec_id=spec.spec_id, item_id=bare.item_id, quantity=1, component_type="Сборка"),
            SpecComponent(spec_id=spec.spec_id, item_id=other.item_id, quantity=1, component_type="Материал"),
        ]
    )
    for item in (bare, other):
        _stock(db, item, 0)
    position = DbrSupermarketPosition(
        item_id=covered.item_id, warehouse_ref1c="W4", supply_type="processing",
        mode="shelf", is_active=True, is_stale=False, adu=1, commonality=1,
        rt_days=1, rt_source="chain", batch_days=1, q_batch=1,
        k_var=Decimal("0.5"), supply_risk_pct=0, red_qty=1, yellow_qty=1,
        green_qty=1, target_qty=3, data_quality=[], calculation_snapshot={},
    )
    db.add(position)
    db.flush()
    parent = _parent(db, covered)
    parent.supermarket_position_id = position.id
    db.flush()
    monkeypatch.setattr(
        feeder_material_service,
        "build_kit",
        lambda code, *_: (
            [
                KitLine("BARE-REFRESH", 1, "W4", True),
                KitLine("OTHER-REFRESH", 1, "W4", True),
            ]
            if code == "COVERED-REFRESH"
            else []
        ),
    )

    feeder_chain_service.refresh_chain_signals(db, max_passes=1)

    children = db.query(DbrFeederSignal).filter(
        DbrFeederSignal.signal_type == "Цепочка"
    ).all()
    assert [db.get(Item, row.item_id).item_code for row in children] == ["BARE-REFRESH"]
