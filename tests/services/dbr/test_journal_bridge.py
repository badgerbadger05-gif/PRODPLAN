from datetime import date
from decimal import Decimal

from app.models import (
    DbrFeederSignal,
    DbrSupermarketPosition,
    DefaultSpecification,
    Item,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    Specification,
)
from app.services.dbr.journal_bridge import sync_journal_rows
from app.services.production_control_journal import list_journal


def _scenario(db, *, supply_type: str = "manufacture"):
    item = Item(
        item_code="DBR-PART",
        item_name="Деталь DBR",
        item_article="DBR-001",
        item_ref1c="item-dbr-ref",
        unit="шт",
        replenishment_method="Производство",
    )
    db.add(item)
    db.flush()
    spec = Specification(spec_name="Спецификация DBR", spec_ref1c="spec-dbr-ref")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    position = DbrSupermarketPosition(
        item_id=item.item_id,
        warehouse_ref1c="W2",
        supply_type=supply_type,
        mode="shelf",
        adu=Decimal("1"),
        commonality=1,
        rt_days=Decimal("7"),
        rt_source="class",
        batch_days=Decimal("5"),
        q_batch=Decimal("1"),
        k_var=Decimal("0.5"),
        supply_risk_pct=Decimal("0"),
        red_qty=Decimal("2"),
        yellow_qty=Decimal("3"),
        green_qty=Decimal("4"),
        target_qty=Decimal("9"),
    )
    db.add(position)
    db.flush()
    signal = DbrFeederSignal(
        dedup_key=f"R:{supply_type}:DBR-PART",
        signal_type="Пополнение",
        supermarket_position_id=position.id,
        item_id=item.item_id,
        warehouse_ref1c="W2",
        status="Open",
        suggested_qty=Decimal("6"),
        priority=Decimal("1.25"),
        zone="red",
        need_date=date(2026, 7, 24),
        required_date=date(2026, 7, 28),
        reason_json={"generator": "bulk_live_nfp"},
    )
    db.add(signal)
    db.commit()
    return signal


def test_bridge_creates_one_idempotent_journal_row_and_projection(db_session):
    signal = _scenario(db_session)

    first = sync_journal_rows(db_session)
    second = sync_journal_rows(db_session)

    assert first["created"] == 1
    assert second["created"] == 0
    assert db_session.query(ProductionOrder).count() == 1
    product = db_session.query(ProductionProduct).one()
    assert product.source_dbr_signal_id == signal.id
    assert product.order.source == "dbr"
    assert float(product.quantity) == 6

    journal = list_journal(db_session, planning_contour="dbr_feeder")
    assert journal["total"] == 1
    row = journal["rows"][0]
    assert row["source_dbr_signal_id"] == signal.id
    assert row["planning"] == {
        "contour": "dbr_feeder",
        "source_id": signal.id,
        "schedule_id": None,
        "slot_id": None,
        "signal_type": "Пополнение",
        "priority": 1.25,
        "zone": "red",
        "need_date": "2026-07-24",
        "required_date": "2026-07-28",
        "queue_state": "ready",
        "chain_depth": 0,
        "parent_signal_id": None,
        "reason": "bulk_live_nfp",
    }


def test_bridge_preserves_manual_state_and_cancels_only_unexported_proposal(db_session):
    signal = _scenario(db_session)
    sync_journal_rows(db_session)
    product = db_session.query(ProductionProduct).one()
    state = db_session.query(ProductionOrderLineState).filter_by(product_id=product.product_id).one()
    state.status = "ready"
    signal.suggested_qty = Decimal("8")
    signal.required_date = date(2026, 7, 30)
    db_session.flush()

    sync_journal_rows(db_session)

    assert state.status == "ready"
    assert float(product.quantity) == 8
    assert state.planned_finish_date == date(2026, 7, 30)

    signal.status = "Cancelled"
    signal.suggested_qty = 0
    db_session.flush()
    sync_journal_rows(db_session)
    assert state.status == "cancelled"
    assert list_journal(db_session, planning_contour="dbr_feeder")["total"] == 0


def test_bridge_skips_non_manufacturing_signal(db_session):
    _scenario(db_session, supply_type="purchase")

    result = sync_journal_rows(db_session)

    assert result["skipped"] == 1
    assert db_session.query(ProductionProduct).count() == 0


def test_bridge_skips_purchase_and_processing_chain_children(db_session):
    for method in ("Закупка", "Переработка"):
        item = Item(
            item_code=f"CHAIN-{method}",
            item_name=method,
            replenishment_method=method,
        )
        db_session.add(item)
        db_session.flush()
        spec = Specification(spec_name=method)
        db_session.add(spec)
        db_session.flush()
        db_session.add(
            DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id)
        )
        db_session.add(
            DbrFeederSignal(
                dedup_key=f"C:{method}",
                signal_type="Цепочка",
                item_id=item.item_id,
                warehouse_ref1c="W2",
                status="Open",
                suggested_qty=Decimal("1"),
            )
        )
    db_session.commit()

    result = sync_journal_rows(db_session)

    assert result["skipped"] == 2
    assert db_session.query(ProductionProduct).count() == 0


def test_dbr_journal_uses_business_priority_order(db_session):
    parent = _scenario(db_session)
    child = DbrFeederSignal(
        dedup_key="C:DBR-PART",
        signal_type="Цепочка",
        item_id=parent.item_id,
        warehouse_ref1c="W2",
        status="Open",
        suggested_qty=Decimal("2"),
        priority=Decimal("9.5"),
        zone="red",
        parent_signal_id=parent.id,
        chain_depth=1,
    )
    db_session.add(child)
    db_session.commit()
    sync_journal_rows(db_session)

    rows = list_journal(
        db_session,
        planning_contour="dbr_feeder",
        sort_by="dbr_priority",
        sort_dir="desc",
    )["rows"]

    assert [row["source_dbr_signal_id"] for row in rows] == [child.id, parent.id]
