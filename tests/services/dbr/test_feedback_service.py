"""Фаза 3 — feedback from the 1С production-order sync into DBR slots/signals."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from app.models import (
    DbrDrumSchedule,
    DbrDrumSlot,
    DbrFeederSignal,
    Item,
    ProductionOrder,
    ProductionProduct,
    ProductionResource,
    SyncLink,
)
from app.services.dbr import feedback_service
from app.services.dbr.core.feeder import signal_identity


def _dbr_link(db, *, doctype, source_id, ref):
    db.add(SyncLink(
        source_system="dbr", source_doctype=doctype, source_id=source_id,
        target_system="1C", target_entity="Document_ЗаказНаПроизводство",
        target_ref_key=ref, target_number="DBR", status="success",
    ))


def _order_with_output(db, item, *, ref, qty, produced):
    order = ProductionOrder(
        order_number="1C-DBR", order_date=datetime(2026, 8, 1),
        order_ref1c=ref, is_posted=True, deletion_mark=False, source="1c",
    )
    db.add(order)
    db.flush()
    db.add(ProductionProduct(
        order_id=order.order_id, item_id=item.item_id, line_number=1,
        quantity=qty, produced_qty=produced, remaining_qty=qty - produced,
    ))
    db.flush()
    return order


def _slot(db, item, *, qty, ref, release_status="released"):
    resource = ProductionResource(resource_name="Сборка", capacity=1)
    db.add(resource)
    db.flush()
    schedule = DbrDrumSchedule(
        period_from=date(2026, 8, 1), period_to=date(2026, 8, 31), status="active"
    )
    db.add(schedule)
    db.flush()
    slot = DbrDrumSlot(
        schedule_id=schedule.id, slot_date=date(2026, 8, 10),
        planned_date=date(2026, 8, 10), resource_id=resource.resource_id,
        item_id=item.item_id, qty=Decimal(str(qty)), release_status=release_status,
        one_c_order_ref=ref,
    )
    db.add(slot)
    db.flush()
    return slot


def test_feedback_moves_slot_produced_and_completes(db_session):
    db = db_session
    item = Item(item_code="SLED", item_name="Снегоход")
    db.add(item)
    db.flush()
    slot = _slot(db, item, qty=3, ref="ref-a")
    _order_with_output(db, item, ref="ref-a", qty=3, produced=3)
    _dbr_link(db, doctype="drum_slot", source_id=slot.id, ref="ref-a")
    db.commit()

    stats = feedback_service.apply_order_feedback(db)

    assert stats["slots_updated"] == 1
    db.refresh(slot)
    assert float(slot.produced_qty) == 3.0
    assert slot.release_status == "completed"


def test_feedback_partial_output_keeps_slot_released(db_session):
    db = db_session
    item = Item(item_code="SLED", item_name="Снегоход")
    db.add(item)
    db.flush()
    slot = _slot(db, item, qty=5, ref="ref-b")
    _order_with_output(db, item, ref="ref-b", qty=5, produced=2)
    _dbr_link(db, doctype="drum_slot", source_id=slot.id, ref="ref-b")
    db.commit()

    feedback_service.apply_order_feedback(db)
    db.refresh(slot)
    assert float(slot.produced_qty) == 2.0
    assert slot.release_status == "released"


def test_feedback_signal_in_work_then_done(db_session):
    db = db_session
    item = Item(item_code="PROD", item_name="Узел")
    db.add(item)
    db.flush()
    signal = DbrFeederSignal(
        dedup_key="R:PROD", signal_type="Пополнение", item_id=item.item_id,
        warehouse_ref1c="W2", status=signal_identity.ORDER_CREATED,
        suggested_qty=Decimal("4"), one_c_order_ref="ref-c",
    )
    db.add(signal)
    db.flush()
    order = _order_with_output(db, item, ref="ref-c", qty=4, produced=2)
    _dbr_link(db, doctype="feeder_signal", source_id=signal.id, ref="ref-c")
    db.commit()

    feedback_service.apply_order_feedback(db)
    db.refresh(signal)
    assert signal.status == signal_identity.IN_WORK

    # Full output -> Done
    product = db.query(ProductionProduct).filter_by(order_id=order.order_id).one()
    product.produced_qty = 4
    db.commit()
    feedback_service.apply_order_feedback(db)
    db.refresh(signal)
    assert signal.status == signal_identity.DONE


def test_feedback_ignores_orders_not_yet_synced(db_session):
    db = db_session
    item = Item(item_code="SLED", item_name="Снегоход")
    db.add(item)
    db.flush()
    slot = _slot(db, item, qty=3, ref="ref-missing")
    # link exists but no ProductionOrder with that ref yet
    _dbr_link(db, doctype="drum_slot", source_id=slot.id, ref="ref-missing")
    db.commit()

    stats = feedback_service.apply_order_feedback(db)
    assert stats["slots_updated"] == 0
    db.refresh(slot)
    assert float(slot.produced_qty) == 0.0
    assert slot.release_status == "released"


def test_sync_wraps_feedback_in_guarded_hook():
    """The production-order sync must call the feedback hook best-effort so a
    feedback failure cannot fail the load-bearing order sync."""
    src = " ".join(
        Path("backend/app/services/production_order_sync.py").read_text().split()
    )
    assert "from .dbr.feedback_service import apply_order_feedback" in src
    assert "apply_order_feedback(db)" in src
    assert "[DBR FEEDBACK WARNING]" in src
