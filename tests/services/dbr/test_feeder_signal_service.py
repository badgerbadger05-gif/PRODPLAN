from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.models import (
    DbrDrumSchedule, DbrDrumSlot, DbrFeederSignal, DbrSupermarketPosition,
    Item, ItemWarehouseStock, ProductionResource, SupplierOrder, SupplierOrderItem,
)
from app.services.dbr import feeder_signal_service
from app.services.dbr import settings_service
from app.services.dbr.core.drum.kit import KitLine


def _scenario(db, *, nfp=15, zone="Yellow", complete=True):
    item = Item(item_code="PART", item_name="Part")
    schedule = DbrDrumSchedule(
        period_from=date(2026, 8, 1), period_to=date(2026, 8, 31), status="active"
    )
    db.add_all([item, schedule])
    db.flush()
    position = DbrSupermarketPosition(
        item_id=item.item_id, warehouse_ref1c="WH-2", supply_type="manufacture",
        mode="shelf", is_active=True, is_stale=False, adu=1, commonality=1,
        rt_days=1, rt_source="class", batch_days=1, q_batch=10,
        k_var=Decimal("0.5"), supply_risk_pct=0, red_qty=10, yellow_qty=10,
        green_qty=10, target_qty=30, source_schedule_id=schedule.id,
        data_quality=[], calculation_snapshot={},
    )
    db.add(position)
    db.flush()
    live = {
        position.id: {
            "nfp": nfp, "zone": zone, "penetration": 1 - nfp / 30,
            "is_complete": complete,
            "missing_reasons": [] if complete else ["open_supply_destination_missing"],
        }
    }
    return schedule, position, live


def test_refresh_is_idempotent_and_reuses_stable_identity(db_session, monkeypatch):
    db = db_session
    schedule, position, live = _scenario(db)
    monkeypatch.setattr(feeder_signal_service.feeder_nfp_service, "live_nfp_rows", lambda db, positions: live)

    first = feeder_signal_service.refresh_signals(db, schedule.id)
    second = feeder_signal_service.refresh_signals(db, schedule.id)

    assert first["created"] == 1
    assert second["created"] == 0 and second["updated"] == 1
    assert db.query(DbrFeederSignal).count() == 1
    signal = db.query(DbrFeederSignal).one()
    assert signal.status == "Open" and float(signal.suggested_qty) == 20
    assert float(signal.priority) == 0.5
    assert signal.dedup_key.startswith("R:")


def test_green_cancels_and_later_reopens_same_row(db_session, monkeypatch):
    db = db_session
    schedule, position, live = _scenario(db)
    monkeypatch.setattr(feeder_signal_service.feeder_nfp_service, "live_nfp_rows", lambda db, positions: live)
    feeder_signal_service.refresh_signals(db)
    live[position.id].update(nfp=25, zone="Green")
    result = feeder_signal_service.refresh_signals(db)
    signal = db.query(DbrFeederSignal).one()
    assert result["cancelled"] == 1, result
    assert signal.status == "Cancelled" and signal.cancelled_at is not None
    assert float(signal.suggested_qty) == 0

    live[position.id].update(nfp=15, zone="Yellow")
    result = feeder_signal_service.refresh_signals(db)
    assert result["reopened"] == 1
    assert db.query(DbrFeederSignal).count() == 1
    assert signal.status == "Open" and signal.cancelled_at is None


def test_kit_shortage_forces_green_but_incomplete_nfp_fails_closed(db_session, monkeypatch):
    db = db_session
    schedule, position, live = _scenario(db, nfp=25, zone="Green")
    monkeypatch.setattr(feeder_signal_service.feeder_nfp_service, "live_nfp_rows", lambda db, positions: live)
    monkeypatch.setattr(feeder_signal_service, "_kit_shortages", lambda db, schedule: {("PART", "wh-2"): 7})
    result = feeder_signal_service.refresh_signals(db)
    signal = db.query(DbrFeederSignal).one()
    assert result["created"] == 1
    assert signal.kit_force is True and float(signal.suggested_qty) == 10

    live[position.id].update(is_complete=False, missing_reasons=["stale_schedule"])
    result = feeder_signal_service.refresh_signals(db)
    assert result["cancelled"] == 1, result
    assert signal.status == "Cancelled"


def test_inactive_or_non_shelf_position_cancels_live_signal(db_session, monkeypatch):
    db = db_session
    schedule, position, live = _scenario(db)
    monkeypatch.setattr(feeder_signal_service.feeder_nfp_service, "live_nfp_rows", lambda db, positions: live)
    feeder_signal_service.refresh_signals(db)
    position.is_active = False
    db.flush()
    result = feeder_signal_service.refresh_signals(db)
    signal = db.query(DbrFeederSignal).one()
    assert result["cancelled"] == 1, result
    assert signal.status == "Cancelled"
    assert signal.reason_json["missing_reasons"] == ["position_not_active_shelf"]


def test_expected_schedule_guard_does_not_mutate(db_session, monkeypatch):
    db = db_session
    schedule, position, live = _scenario(db)
    monkeypatch.setattr(feeder_signal_service.feeder_nfp_service, "live_nfp_rows", lambda db, positions: live)
    try:
        feeder_signal_service.refresh_signals(db, expected_schedule_id=schedule.id + 1)
    except ValueError as exc:
        assert "активный график изменился" in str(exc)
    else:
        raise AssertionError("expected schedule mismatch must fail")
    assert db.query(DbrFeederSignal).count() == 0


def test_postgres_refresh_locks_before_preview(monkeypatch):
    events = []

    class Dialect:
        name = "postgresql"

    class Bind:
        dialect = Dialect()

    class FakeDb:
        def get_bind(self):
            return Bind()

        def execute(self, statement, params):
            events.append((str(statement), params))

    def preview(db):
        assert events and "pg_advisory_xact_lock" in events[0][0]
        return {"schedule_id": 1, "positions": 0, "actionable": 0, "rows": []}

    monkeypatch.setattr(feeder_signal_service, "preview_signals", preview)
    try:
        feeder_signal_service.refresh_signals(FakeDb(), expected_schedule_id=2)
    except ValueError:
        pass
    else:
        raise AssertionError("guard must stop after taking the lock")
    assert events[0][1]["key"] == feeder_signal_service._REFRESH_LOCK


def test_list_orders_kit_force_then_priority_and_zone_is_case_insensitive(db_session):
    db = db_session
    schedule, position, _live = _scenario(db)

    def add_signal(code, priority, kit_force, zone):
        item = Item(item_code=code, item_name=code)
        db.add(item)
        db.flush()
        shelf = DbrSupermarketPosition(
            item_id=item.item_id, warehouse_ref1c=f"WH-{code}", supply_type="manufacture",
            mode="shelf", is_active=True, is_stale=False, adu=1, commonality=1,
            rt_days=1, rt_source="class", batch_days=1, q_batch=1,
            k_var=Decimal("0.5"), supply_risk_pct=0, red_qty=1, yellow_qty=1,
            green_qty=1, target_qty=3, source_schedule_id=schedule.id,
            data_quality=[], calculation_snapshot={},
        )
        db.add(shelf)
        db.flush()
        signal = DbrFeederSignal(
            dedup_key=f"R:{code}", supermarket_position_id=shelf.id,
            item_id=item.item_id, warehouse_ref1c=shelf.warehouse_ref1c,
            status="Open", suggested_qty=1, priority=priority, kit_force=kit_force,
            zone=zone,
        )
        db.add(signal)
        db.flush()
        return signal

    ordinary = add_signal("ORD", Decimal("0.900000"), False, "Yellow")
    forced = add_signal("KIT", Decimal("0.100000"), True, "Yellow")
    lower = add_signal("LOW", Decimal("0.200000"), False, "Yellow")

    rows = feeder_signal_service.list_signals(db, zone="yElLoW")
    assert [row["id"] for row in rows] == [forced.id, ordinary.id, lower.id]
    assert rows[0]["priority"] == 0.1 and rows[0]["kit_force"] is True
    lower.status = "Diagnostic"
    db.flush()
    diagnostic = feeder_signal_service.list_signals(db, status="Diagnostic")
    assert [row["id"] for row in diagnostic] == [lower.id]


def _under_schedule_scenario(db, monkeypatch):
    settings = settings_service.get_or_create_settings(db)
    settings.w2_warehouse_ref1c = "W2"
    settings.w3_warehouse_ref1c = "W3"
    settings.w4_warehouse_ref1c = "W4"
    root = Item(item_code="ROOT-US", item_name="Root")
    part = Item(item_code="PART-US", item_name="Part")
    resource = ProductionResource(resource_name="US resource", capacity=1)
    schedule = DbrDrumSchedule(
        period_from=date(2026, 8, 1), period_to=date(2026, 12, 31), status="active"
    )
    db.add_all([root, part, resource, schedule])
    db.flush()
    position = DbrSupermarketPosition(
        item_id=part.item_id, warehouse_ref1c="W4", supply_type="manufacture",
        mode="under_schedule", is_active=True, is_stale=False, adu=0,
        commonality=1, rt_days=2, rt_source="class", batch_days=1, q_batch=10,
        k_var=Decimal("0.5"), supply_risk_pct=0, red_qty=0, yellow_qty=0,
        green_qty=0, target_qty=0, source_schedule_id=schedule.id,
        data_quality=[], calculation_snapshot={},
    )
    db.add(position)
    db.flush()
    slots = []
    for index, (day, status) in enumerate((
        (date(2026, 8, 1), "pending"),
        (date(2026, 10, 1), "pending"),  # proves full schedule horizon
        (date(2026, 7, 31), "pending"),
        (date(2026, 8, 2), "released"),
    )):
        slot = DbrDrumSlot(
            schedule_id=schedule.id, slot_date=day, planned_date=day,
            resource_id=resource.resource_id, item_id=root.item_id, qty=3,
            position=index, release_status=status,
        )
        db.add(slot)
        slots.append(slot)
    db.flush()
    monkeypatch.setattr(
        feeder_signal_service.classify_mod, "build_classifier",
        lambda db, settings: (lambda code: ("under_schedule", "W4"), []),
    )
    monkeypatch.setattr(feeder_signal_service.adapters, "build_components_provider", lambda db: lambda code: [])
    # False is intentional: membership comes from the persisted position.
    monkeypatch.setattr(feeder_signal_service, "build_kit", lambda *args: [KitLine("PART-US", 2, "W4", False)])
    monkeypatch.setattr(
        feeder_signal_service, "load_reservation_state",
        lambda db, item_ids: SimpleNamespace(by_warehouse_item={}),
    )
    return schedule, position, part, slots


def test_under_schedule_membership_full_horizon_and_batch_surplus(db_session, monkeypatch):
    db = db_session
    schedule, position, part, slots = _under_schedule_scenario(db, monkeypatch)
    db.add(ItemWarehouseStock(item_id=part.item_id, warehouse_ref1c="w4", qty=5))
    db.flush()

    rows = feeder_signal_service._under_schedule_rows(db, schedule, today=date(2026, 8, 1))

    assert [row["slot_id"] for row in rows] == [slots[0].id, slots[1].id]
    assert rows[0]["raw_demand_qty"] == 6 and rows[0]["raw_shortage_qty"] == 1
    assert rows[0]["suggested_qty"] == 10
    assert rows[1]["available_before"] == 9
    assert rows[1]["raw_shortage_qty"] == 0 and rows[1]["suggested_qty"] == 0
    assert rows[0]["required_date"] == date(2026, 8, 1)
    assert rows[0]["need_date"] == date(2026, 7, 30)
    assert rows[0]["dedup_key"].startswith("S:")


def test_under_schedule_eta_reservations_and_incomplete_inbound(db_session, monkeypatch):
    db = db_session
    schedule, _position, part, _slots = _under_schedule_scenario(db, monkeypatch)
    db.add(ItemWarehouseStock(item_id=part.item_id, warehouse_ref1c="W4", qty=5))
    order = SupplierOrder(
        order_number="SUP-US", order_date=datetime(2026, 7, 1),
        order_ref1c="SUP-US", deletion_mark=False,
    )
    db.add(order)
    db.flush()
    db.add_all([
        SupplierOrderItem(
            order_id=order.order_id, item_id_ref=part.item_id, quantity=4,
            remaining_qty=4, received_qty=0, destination_warehouse_ref1c="W4",
            delivery_date=datetime(2026, 8, 1),
        ),
        SupplierOrderItem(
            order_id=order.order_id, item_id_ref=part.item_id, quantity=2,
            remaining_qty=2, received_qty=0, destination_warehouse_ref1c="W4",
            delivery_date=None,
        ),
        SupplierOrderItem(
            order_id=order.order_id, item_id_ref=part.item_id, quantity=2,
            remaining_qty=2, received_qty=0, destination_warehouse_ref1c=None,
            delivery_date=datetime(2026, 8, 1),
        ),
    ])
    db.flush()
    monkeypatch.setattr(
        feeder_signal_service, "load_reservation_state",
        lambda db, item_ids: SimpleNamespace(by_warehouse_item={("w4", part.item_id): 4}),
    )

    rows = feeder_signal_service._under_schedule_rows(db, schedule, today=date(2026, 8, 1))

    assert rows[0]["available_before"] == 5  # stock 5 - reservation 4 + dated inbound 4
    assert rows[0]["raw_shortage_qty"] == 1
    assert rows[0]["suggested_qty"] == 0  # incomplete data is never actionable
    assert rows[0]["calculated_batch_qty"] == 10
    assert rows[0]["is_complete"] is False
    assert set(rows[0]["data_quality"]) >= {
        "supplier_inbound_eta_missing", "supplier_inbound_destination_missing"
    }

    first = feeder_signal_service.refresh_signals(db, expected_schedule_id=schedule.id)
    earliest = min(
        (row for row in first["rows"] if row["signal_type"] == "Под график"),
        key=lambda row: (row["required_date"], row["slot_id"]),
    )["slot_id"]
    signal = db.query(DbrFeederSignal).filter(
        DbrFeederSignal.drum_slot_id == earliest,
        DbrFeederSignal.signal_type == "Под график",
    ).one()
    assert signal.status == "Diagnostic"
    assert float(signal.suggested_qty) == 0
    assert float(signal.calculated_batch_qty) == 10
    assert float(signal.raw_shortage_qty) > 0
    assert first["diagnostic"] >= 1 and first["actionable"] == 0

    # Once exact destination and ETA become complete, the same stable row
    # reopens as an advisory Open signal.
    for line in db.query(SupplierOrderItem).filter(SupplierOrderItem.order_id == order.order_id):
        line.destination_warehouse_ref1c = "W4"
        line.delivery_date = datetime(2026, 8, 1)
    db.flush()
    completed = feeder_signal_service.refresh_signals(db, expected_schedule_id=schedule.id)
    assert completed["reopened"] >= 1
    assert signal.status == "Open" and float(signal.suggested_qty) == 10

    db.query(ItemWarehouseStock).filter(ItemWarehouseStock.item_id == part.item_id).one().qty = 100
    db.flush()
    cancelled = feeder_signal_service.refresh_signals(db, expected_schedule_id=schedule.id)
    assert cancelled["cancelled"] >= 1
    assert signal.status == "Cancelled" and float(signal.suggested_qty) == 0
