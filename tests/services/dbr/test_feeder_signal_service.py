from datetime import date
from decimal import Decimal

from app.models import DbrDrumSchedule, DbrFeederSignal, DbrSupermarketPosition, Item
from app.services.dbr import feeder_signal_service


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
