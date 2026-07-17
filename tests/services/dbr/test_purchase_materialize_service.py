"""Фаза 3 — materialization of DBR purchasing decisions into 1С.

Covers launch_purchase_signals / purchase_plan_preview / materialize_purchase_plan
plus the supplier-order feedback loop: dry-run writes nothing, real writes stamp
sync_link (source_system='dbr', target='Document_ЗаказПоставщику') idempotently,
lines group by supplier, supplierless signals land in `unresolved`, received qty
moves signals In Work/Done, and the plan preview nets demand correctly.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from app.models import (
    DbrProductionProgram,
    DbrProductionProgramItem,
    DbrFeederSignal,
    DefaultSpecification,
    Item,
    ItemWarehouseStock,
    SpecComponent,
    Specification,
    StockWarehouse,
    Supplier,
    SupplierOrder,
    SupplierOrderItem,
    SyncLink,
)
from app.services.dbr import (
    feedback_service,
    purchase_materialize_service as pms,
    settings_service,
)
from app.services.dbr.core.feeder import signal_identity


# ---------------------------------------------------------------------------
# Fakes / stubs
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, *, ref_key="dbr-po-ref", fail=False):
        self.ref_key = ref_key
        self.fail = fail
        self.posts = []

    def post(self, entity, payload, **_):
        self.posts.append((entity, payload))
        if self.fail:
            raise RuntimeError("simulated 1C failure")
        # unique ref per POST so multi-supplier tests get distinct refs
        return {"Ref_Key": f"{self.ref_key}-{len(self.posts)}"}


def _stub(monkeypatch, *, client=None, base_url="http://demo/odata/unf_demo"):
    monkeypatch.setattr(
        pms,
        "_load_odata_config",
        lambda: {"base_url": base_url, "username": "u", "password": "p"},
    )
    if client is not None:
        monkeypatch.setattr(pms, "OData1CClient", lambda **_: client)


# ---------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------


def _purchase_item(db, *, code, supplier_ref, rt=7, ref_suffix=None):
    ref_suffix = ref_suffix or code.lower()
    item = Item(
        item_code=code,
        item_name=f"Закуп {code}",
        item_article=code,
        item_ref1c=f"item-ref-{ref_suffix}",
        supplier_ref1c=supplier_ref,
        replenishment_method="Закупка",
        replenishment_time=rt,
        unit="unit-ref",
    )
    db.add(item)
    db.flush()
    return item


def _purchase_signal(db, item, *, qty=5, warehouse="W4"):
    signal = DbrFeederSignal(
        dedup_key=f"R:{item.item_code}",
        signal_type="Пополнение",
        item_id=item.item_id,
        warehouse_ref1c=warehouse,
        status=signal_identity.OPEN,
        suggested_qty=Decimal(str(qty)),
        priority=Decimal("1.0"),
        need_date=date(2026, 8, 5),
    )
    db.add(signal)
    db.flush()
    return signal


# ---------------------------------------------------------------------------
# launch_purchase_signals
# ---------------------------------------------------------------------------


def test_launch_purchase_dry_run_writes_nothing(db_session, monkeypatch):
    db = db_session
    item = _purchase_item(db, code="BOLT", supplier_ref="sup-A", rt=10)
    signal = _purchase_signal(db, item, qty=5)
    db.commit()
    fake = _FakeClient()
    _stub(monkeypatch, client=fake)

    res = pms.launch_purchase_signals(db, dry_run=True)

    assert res["dry_run"] is True
    assert res["orders_planned"] == 1
    order = res["orders"][0]
    assert order["supplier_ref1c"] == "sup-A"
    assert order["lines"][0]["item_ref1c"] == "item-ref-bolt"
    assert order["lines"][0]["qty"] == 5.0
    # receipt date = today + replenishment_time
    assert order["lines"][0]["need_date"] == (date.today() + timedelta(days=10)).isoformat()
    assert fake.posts == []
    db.refresh(signal)
    assert signal.status == signal_identity.OPEN
    assert db.query(SyncLink).count() == 0


def test_launch_purchase_real_write_stamps_link_and_signal(db_session, monkeypatch):
    db = db_session
    item = _purchase_item(db, code="BOLT", supplier_ref="sup-A")
    signal = _purchase_signal(db, item, qty=5)
    db.commit()
    fake = _FakeClient(ref_key="ref-po")
    _stub(monkeypatch, client=fake)

    res = pms.launch_purchase_signals(db, dry_run=False)

    assert res["orders_created"] == 1
    assert len(fake.posts) == 1
    entity, payload = fake.posts[0]
    assert entity == pms.PURCHASE_ORDER_ENTITY
    assert payload["Контрагент_Key"] == "sup-A"
    assert payload["Posted"] is False
    db.refresh(signal)
    assert signal.status == signal_identity.ORDER_CREATED
    assert signal.one_c_order_ref == "ref-po-1"
    link = db.query(SyncLink).filter_by(
        source_system="dbr", source_doctype="feeder_signal", source_id=signal.id
    ).one()
    assert link.status == "success"
    assert link.target_entity == pms.PURCHASE_ORDER_ENTITY
    assert link.target_ref_key == "ref-po-1"


def test_launch_purchase_second_call_is_idempotent(db_session, monkeypatch):
    db = db_session
    item = _purchase_item(db, code="BOLT", supplier_ref="sup-A")
    signal = _purchase_signal(db, item, qty=5)
    db.commit()
    fake = _FakeClient()
    _stub(monkeypatch, client=fake)

    pms.launch_purchase_signals(db, dry_run=False)
    assert len(fake.posts) == 1

    # Normal flow: the signal left Open (→ Order Created), so a second batch over
    # "all open" finds nothing to launch and never re-POSTs.
    res = pms.launch_purchase_signals(db, dry_run=False)
    assert res["orders_planned"] == 0
    assert len(fake.posts) == 1

    # sync_link guard: even a signal forced back to Open with a success link is
    # reported as already_exported and never re-POSTed.
    signal.status = signal_identity.OPEN
    db.commit()
    res = pms.launch_purchase_signals(db, signal_ids=[signal.id], dry_run=False)
    assert res["orders_planned"] == 0
    assert res["already_exported"] and res["already_exported"][0]["signal_id"] == signal.id
    assert len(fake.posts) == 1  # no second POST


def test_launch_purchase_groups_by_supplier(db_session, monkeypatch):
    db = db_session
    a1 = _purchase_item(db, code="A1", supplier_ref="sup-A")
    a2 = _purchase_item(db, code="A2", supplier_ref="sup-A")
    b1 = _purchase_item(db, code="B1", supplier_ref="sup-B")
    for it in (a1, a2, b1):
        _purchase_signal(db, it, qty=2)
    db.commit()
    fake = _FakeClient()
    _stub(monkeypatch, client=fake)

    res = pms.launch_purchase_signals(db, dry_run=True)

    assert res["orders_planned"] == 2  # two suppliers
    by_supplier = {o["supplier_ref1c"]: o for o in res["orders"]}
    assert len(by_supplier["sup-A"]["lines"]) == 2  # A1 + A2 in one order
    assert len(by_supplier["sup-B"]["lines"]) == 1


def test_launch_purchase_unresolved_without_supplier(db_session, monkeypatch):
    db = db_session
    item = _purchase_item(db, code="NOSUP", supplier_ref=None)
    signal = _purchase_signal(db, item, qty=4)
    db.commit()
    _stub(monkeypatch, client=_FakeClient())

    res = pms.launch_purchase_signals(db, dry_run=True)

    assert res["orders_planned"] == 0
    assert res["unresolved"]
    assert res["unresolved"][0]["signal_id"] == signal.id
    assert res["unresolved"][0]["missing_supplier"] is True


def test_launch_purchase_skips_non_purchase_signals(db_session, monkeypatch):
    db = db_session
    made = Item(
        item_code="MADE", item_name="Узел", item_ref1c="item-ref-made",
        replenishment_method="Производство", unit="unit-ref",
    )
    db.add(made)
    db.flush()
    _purchase_signal(db, made, qty=3)
    db.commit()
    _stub(monkeypatch, client=_FakeClient())

    res = pms.launch_purchase_signals(db, dry_run=True)
    assert res["orders_planned"] == 0
    assert res["unresolved"] == []


# ---------------------------------------------------------------------------
# purchase feedback (supplier order received qty → signal status)
# ---------------------------------------------------------------------------


def _materialize_one_signal(db, monkeypatch, *, qty=5, ref="ref-po"):
    item = _purchase_item(db, code="BOLT", supplier_ref="sup-A")
    signal = _purchase_signal(db, item, qty=qty)
    db.commit()
    fake = _FakeClient(ref_key=ref)
    _stub(monkeypatch, client=fake)
    pms.launch_purchase_signals(db, dry_run=False)
    db.refresh(signal)
    return signal, item


def _add_supplier_order(db, ref_key, item, *, qty, received):
    supplier = Supplier(supplier_ref1c="sup-A", supplier_name="Sup A")
    db.add(supplier)
    db.flush()
    order = SupplierOrder(
        order_number="DBRPS00001",
        order_date=datetime(2026, 8, 1),
        order_ref1c=ref_key,
        supplier_id=supplier.supplier_id,
        deletion_mark=False,
    )
    db.add(order)
    db.flush()
    db.add(
        SupplierOrderItem(
            order_id=order.order_id,
            item_id_ref=item.item_id,
            line_number=1,
            quantity=Decimal(str(qty)),
            received_qty=Decimal(str(received)),
            remaining_qty=Decimal(str(max(qty - received, 0))),
        )
    )
    db.commit()


def test_purchase_feedback_partial_moves_in_work(db_session, monkeypatch):
    db = db_session
    signal, item = _materialize_one_signal(db, monkeypatch, qty=5, ref="ref-fb")
    _add_supplier_order(db, "ref-fb-1", item, qty=5, received=2)

    stats = feedback_service.apply_purchase_order_feedback(db)
    assert stats["signals_updated"] == 1
    db.refresh(signal)
    assert signal.status == signal_identity.IN_WORK


def test_purchase_feedback_full_moves_done(db_session, monkeypatch):
    db = db_session
    signal, item = _materialize_one_signal(db, monkeypatch, qty=5, ref="ref-fb")
    _add_supplier_order(db, "ref-fb-1", item, qty=5, received=5)

    stats = feedback_service.apply_purchase_order_feedback(db)
    assert stats["signals_updated"] == 1
    db.refresh(signal)
    assert signal.status == signal_identity.DONE


def test_purchase_feedback_no_order_yet_is_noop(db_session, monkeypatch):
    db = db_session
    signal, _item = _materialize_one_signal(db, monkeypatch, qty=5, ref="ref-fb")
    stats = feedback_service.apply_purchase_order_feedback(db)
    assert stats["signals_updated"] == 0
    db.refresh(signal)
    assert signal.status == signal_identity.ORDER_CREATED


# ---------------------------------------------------------------------------
# purchase_plan_preview / materialize_purchase_plan
# ---------------------------------------------------------------------------


def _plan_scenario(db, *, slot_qty=5, comp_qty=2, stock=3, open_po=2, program_date=None):
    """SKU (made) with one purchased component, a program, stock + one open PO."""
    settings = settings_service.get_or_create_settings(db)
    settings.w2_warehouse_ref1c = "W2"
    settings.w3_warehouse_ref1c = "W3"
    settings.w4_warehouse_ref1c = "W4"
    db.add(StockWarehouse(warehouse_ref1c="STK", warehouse_name="Main", is_selected=True))
    sku = Item(item_code="SKU", item_name="Изделие", item_ref1c="item-ref-sku",
               replenishment_method="Производство", unit="unit-ref")
    comp = _purchase_item(db, code="COMP", supplier_ref="sup-A", rt=10, ref_suffix="comp")
    db.add(sku)
    db.flush()
    spec = Specification(spec_name="Spec SKU", spec_ref1c="spec-ref")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=sku.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=comp_qty))
    if stock:
        db.add(ItemWarehouseStock(item_id=comp.item_id, warehouse_ref1c="STK", qty=stock))
    if open_po:
        supplier = Supplier(supplier_ref1c="sup-A", supplier_name="Sup A")
        db.add(supplier)
        db.flush()
        order = SupplierOrder(order_number="OLD", order_date=datetime(2026, 7, 1),
                              order_ref1c="old-po", supplier_id=supplier.supplier_id,
                              deletion_mark=False)
        db.add(order)
        db.flush()
        db.add(SupplierOrderItem(order_id=order.order_id, item_id_ref=comp.item_id,
                                 line_number=1, quantity=Decimal(str(open_po)),
                                 received_qty=Decimal("0"),
                                 remaining_qty=Decimal(str(open_po))))
    program = DbrProductionProgram(
        from_date=date(2026, 8, 1), to_date=date(2026, 8, 31), status="draft"
    )
    db.add(program)
    db.flush()
    db.add(DbrProductionProgramItem(
        program_id=program.id, item_id=sku.item_id,
        program_date=program_date or date(2026, 8, 20), qty=Decimal(str(slot_qty)),
    ))
    db.commit()
    return program, sku, comp


def test_purchase_plan_preview_net_math(db_session):
    db = db_session
    program, _sku, comp = _plan_scenario(db, slot_qty=5, comp_qty=2, stock=3, open_po=2)

    res = pms.purchase_plan_preview(db, program_id=program.id)

    assert res["source"] == {"kind": "program", "program_id": program.id}
    row = next(r for r in res["rows"] if r["item_id"] == comp.item_id)
    assert row["demand_qty"] == 10.0  # 5 slots * 2 per unit
    assert row["stock_qty"] == 3.0
    assert row["open_order_qty"] == 2.0
    assert row["available_qty"] == 5.0
    assert row["to_order_qty"] == 5.0  # 10 - 5
    # order_before = need_date(2026-08-20) - replenishment_time(10 days)
    assert row["need_date"] == "2026-08-20"
    assert row["order_before"] == "2026-08-10"
    assert row["supplier_ref1c"] == "sup-A"


def test_purchase_plan_preview_threshold_flag(db_session):
    db = db_session
    # need far in the future so order_before is beyond today+threshold
    far = date.today() + timedelta(days=400)
    program, _sku, comp = _plan_scenario(
        db, slot_qty=5, comp_qty=1, stock=0, open_po=0, program_date=far
    )
    res = pms.purchase_plan_preview(db, program_id=program.id, lead_time_threshold_days=30)
    row = next(r for r in res["rows"] if r["item_id"] == comp.item_id)
    assert row["within_lead_time_threshold"] is False


def test_purchase_plan_preview_covered_by_stock(db_session):
    db = db_session
    program, _sku, comp = _plan_scenario(db, slot_qty=1, comp_qty=1, stock=100, open_po=0)
    res = pms.purchase_plan_preview(db, program_id=program.id)
    row = next(r for r in res["rows"] if r["item_id"] == comp.item_id)
    assert row["to_order_qty"] == 0.0
    assert res["rows_to_order"] == 0


def test_materialize_purchase_plan_dry_run_writes_nothing(db_session, monkeypatch):
    db = db_session
    program, _sku, comp = _plan_scenario(db, slot_qty=5, comp_qty=2, stock=0, open_po=0)
    fake = _FakeClient()
    _stub(monkeypatch, client=fake)

    res = pms.materialize_purchase_plan(db, program_id=program.id, dry_run=True)

    assert res["dry_run"] is True
    assert res["orders_planned"] == 1
    assert res["orders"][0]["supplier_ref1c"] == "sup-A"
    assert res["orders"][0]["lines"][0]["item_id"] == comp.item_id
    assert fake.posts == []
    assert db.query(SyncLink).count() == 0


def test_materialize_purchase_plan_real_write_and_idempotent(db_session, monkeypatch):
    db = db_session
    program, _sku, comp = _plan_scenario(db, slot_qty=5, comp_qty=2, stock=0, open_po=0)
    fake = _FakeClient(ref_key="ref-plan")
    _stub(monkeypatch, client=fake)

    res = pms.materialize_purchase_plan(db, program_id=program.id, dry_run=False)
    assert res["orders_created"] == 1
    assert len(fake.posts) == 1
    link = db.query(SyncLink).filter_by(
        source_system="dbr", source_doctype="purchase_plan", source_id=comp.item_id
    ).one()
    assert link.status == "success"

    res2 = pms.materialize_purchase_plan(db, program_id=program.id, dry_run=False)
    assert res2["orders_planned"] == 0
    assert res2["already_exported"][0]["item_id"] == comp.item_id
    assert len(fake.posts) == 1  # no second POST


def test_materialize_purchase_plan_missing_program(db_session, monkeypatch):
    db = db_session
    _stub(monkeypatch, client=_FakeClient())
    import pytest

    with pytest.raises(LookupError):
        pms.materialize_purchase_plan(db, program_id=999999, dry_run=True)
