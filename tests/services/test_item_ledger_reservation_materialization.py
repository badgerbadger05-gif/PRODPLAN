"""Inc4 — reservation materialization (PURE SHADOW) tests.

Covers §2.2 mode assignment, §2.6 frozen-pin dual-write, §6.1/§6.3 SLE→reservation
matching (pegged consume/make realize + unplanned_consumption), §5 redistribute
ORM adapter (golden example), idempotency, and a regression assertion that
run_ledger_cycle's existing executed/drift/closure result is unchanged with the
reservation block present.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest

pytestmark = pytest.mark.usefixtures("building_ledger_generation")

from app import models
from app.models import (
    Item,
    MrpFreezeAllocation,
    MrpRequirement,
    PlanningRun,
    ProductionManufacture,
    ProductionMaterialIssue,
    ProductionOrder,
    ProductionPlanHeader,
    ProductionProduct,
    ReservationCoverage,
    ReservationEntry,
    ReservationEvent,
    StockBin,
    StockLedgerEntry,
    StockRecorderPull,
    StockWarehouse,
    SyncLink,
)
from app.services.item_ledger.reservation import CONSUME, MAKE
from app.services.item_ledger.reservation_ledger import (
    materialize_reservations,
    materialize_reservations_for_freeze,
    mirror_frozen_pins,
    mode_targets,
    realize_from_sle,
    redistribute_pool,
    reservation_shadow_report,
    run_reservation_shadow,
)
from app.services.mrp_execution_ledger import (
    _ledger_scope,
    run_ledger_cycle as _public_run_ledger_cycle,
)


def run_ledger_cycle(db):
    generation = db.get(models.PlanningTruthState, 1).current_generation
    for run in db.query(PlanningRun).all():
        if run.ledger_generation_id is None:
            run.ledger_generation_id = generation.id
    old_status, old_cutoff, old_accepted_at = (
        generation.status,
        generation.cutoff,
        generation.accepted_at,
    )
    generation.status = "accepted"
    generation.cutoff = generation.cutoff or datetime(2026, 12, 31)
    generation.accepted_at = generation.accepted_at or datetime(2026, 12, 31)
    db.flush()
    try:
        return _public_run_ledger_cycle(db, diagnostic_legacy=True)
    finally:
        generation.status = old_status
        generation.cutoff = old_cutoff
        generation.accepted_at = old_accepted_at
        db.flush()


def diagnostic_ledger_scope(db):
    """Build the retired core's scope without weakening active fail-closed reads.

    Reservation tests intentionally mutate a BUILDING generation.  The retired
    execution core, however, now scopes only through an accepted pointer.  Give
    that private scope builder a short-lived accepted view, then restore the
    mutable test generation before any reservation writer runs.
    """
    generation = db.get(models.PlanningTruthState, 1).current_generation
    for run in db.query(PlanningRun).all():
        if run.ledger_generation_id is None:
            run.ledger_generation_id = generation.id
    old_status, old_cutoff, old_accepted_at = (
        generation.status,
        generation.cutoff,
        generation.accepted_at,
    )
    generation.status = "accepted"
    generation.cutoff = generation.cutoff or datetime(2026, 12, 31)
    generation.accepted_at = generation.accepted_at or datetime(2026, 12, 31)
    db.flush()
    try:
        return _ledger_scope(db)
    finally:
        generation.status = old_status
        generation.cutoff = old_cutoff
        generation.accepted_at = old_accepted_at
        db.flush()


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
def _item(db, code, *, produced=True):
    it = Item(
        item_code=code,
        item_name=code,
        unit="шт",
        replenishment_method="Производство" if produced else "Покупка",
        status="active",
    )
    db.add(it)
    db.flush()
    return it


def _plan(db, pf, pt):
    p = ProductionPlanHeader(name=f"plan-{pf}", period_from=pf, period_to=pt, status="fixed")
    db.add(p)
    db.flush()
    return p


def _run(db, pf, pt, *, version=1):
    plan = _plan(db, pf, pt)
    r = PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        pinned=True,
        source_plan_id=plan.id,
        ledger_generation_id=db.get(models.PlanningTruthState, 1).current_generation_id,
        period_from=pf,
        period_to=pt,
        active_freeze_version=version,
    )
    db.add(r)
    db.flush()
    return r


def _req(db, run, item, *, gross, net, bom_level, version=1):
    rq = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=gross,
        net_required_qty=net,
        covered_qty=gross - net,
        remaining_qty=net,
        period_from=run.period_from,
        period_to=run.period_to,
        bom_level=bom_level,
        status="open",
        freeze_version=version,
    )
    db.add(rq)
    db.flush()
    return rq


def _order(db, ref, *, run=None):
    o = ProductionOrder(
        order_number=ref,
        order_date=datetime(2026, 7, 1),
        order_ref1c=ref,
        source="mrp",
        source_run_id=run.run_id if run else None,
        deletion_mark=False,
    )
    db.add(o)
    db.flush()
    return o


def _prod_line(db, order, item, *, qty, produced=0.0, src_req=None):
    pp = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=qty,
        produced_qty=produced,
        remaining_qty=qty,
        source_mrp_requirement_id=src_req.id if src_req else None,
    )
    db.add(pp)
    db.flush()
    return pp


def _sle(db, item, *, qty, kind, recorder, line_no="1",
         recorder_type="Document_СборкаЗапасов", warehouse=""):
    generation = db.get(models.PlanningTruthState, 1).current_generation
    e = StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash=(
            f"test:{recorder}:{line_no}:{item.item_id}:{qty}:{kind}"
        )[:64],
        item_id=item.item_id,
        qty=Decimal(str(qty)),
        qty_after=Decimal("0"),
        posting_at=datetime(2026, 7, 5),
        record_type="Receipt" if qty > 0 else "Expense",
        movement_kind=kind,
        recorder_type=recorder_type,
        recorder_ref=recorder,
        line_no=line_no,
        warehouse_ref1c=warehouse,
        ingest_source="document_pull",
        active=True,
    )
    db.add(e)
    db.flush()
    return e


# --- real-topology link fixtures: the SLE recorder is the *document* GUID
# (СборкаЗапасов / ПеремещениеЗапасов), NEVER the order GUID. The producing
# order is reachable only via sync_link (our export) or via the pull-captured
# header order_ref (document created in 1С directly). -----------------------
def _pull(db, recorder, *, order_ref=None, recorder_type="Document_СборкаЗапасов"):
    row = StockRecorderPull(
        recorder_type=recorder_type,
        recorder_ref=recorder,
        status="done",
        order_ref=order_ref,
    )
    db.add(row)
    db.flush()
    return row


def _manufacture(db, order, product, *, qty):
    m = ProductionManufacture(
        product_id=product.product_id,
        order_id=order.order_id,
        qty=Decimal(str(qty)),
        status="exported",
    )
    db.add(m)
    db.flush()
    return m


def _material_issue(db, order, product):
    mi = ProductionMaterialIssue(
        document_number=f"MI-{order.order_id}-{product.product_id}",
        product_id=product.product_id,
        order_id=order.order_id,
        status="exported",
    )
    db.add(mi)
    db.flush()
    return mi


def _link(db, doctype, source_id, entity, ref):
    l = SyncLink(
        source_doctype=doctype,
        source_id=int(source_id),
        target_entity=entity,
        target_ref_key=ref,
        status="success",
    )
    db.add(l)
    db.flush()
    return l


def _fg_warehouse(db, ref):
    w = StockWarehouse(
        warehouse_ref1c=ref,
        warehouse_name=f"ГП {ref}",
        is_selected=True,
        is_finished_goods=True,
    )
    db.add(w)
    db.flush()
    return w


def _entry(db, req, mode):
    return (
        db.query(ReservationEntry)
        .filter(ReservationEntry.requirement_id == req.id, ReservationEntry.realization_mode == mode)
        .one_or_none()
    )


# ---------------------------------------------------------------------------
# §2.2 — mode assignment
# ---------------------------------------------------------------------------
def test_mode_assignment_finished_good_make_only(db_session):
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    it = _item(db, "FG", produced=True)
    req = _req(db, run, it, gross=5, net=5, bom_level=0)
    targets = mode_targets(req, it)
    assert targets == [(MAKE, Decimal("5"))]

    materialize_reservations(db, [req], {run.run_id: run}, "cyc")
    assert _entry(db, req, MAKE) is not None
    assert _entry(db, req, CONSUME) is None
    assert Decimal(str(_entry(db, req, MAKE).reserved_qty)) == Decimal("5")


def test_mode_assignment_produced_intermediate_both(db_session):
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    it = _item(db, "INT", produced=True)
    req = _req(db, run, it, gross=10, net=7, bom_level=1)
    targets = dict(mode_targets(req, it))
    assert targets[MAKE] == Decimal("7")  # net
    assert targets[CONSUME] == Decimal("10")  # gross

    materialize_reservations(db, [req], {run.run_id: run}, "cyc")
    assert Decimal(str(_entry(db, req, CONSUME).reserved_qty)) == Decimal("10")
    assert Decimal(str(_entry(db, req, MAKE).reserved_qty)) == Decimal("7")


def test_mode_assignment_purchased_consume_only(db_session):
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    it = _item(db, "BUY", produced=False)
    req = _req(db, run, it, gross=8, net=6, bom_level=1)
    assert mode_targets(req, it) == [(CONSUME, Decimal("8"))]  # gross

    materialize_reservations(db, [req], {run.run_id: run}, "cyc")
    assert _entry(db, req, MAKE) is None
    assert Decimal(str(_entry(db, req, CONSUME).reserved_qty)) == Decimal("8")


# ---------------------------------------------------------------------------
# §2.6 — frozen-pin dual-write mirrors MrpFreezeAllocation
# ---------------------------------------------------------------------------
def test_frozen_pin_dual_write_mirrors_allocation(db_session):
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    it = _item(db, "BUY", produced=False)  # consume-only
    req = _req(db, run, it, gross=10, net=6, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")

    db.add(MrpFreezeAllocation(
        run_id=run.run_id, freeze_version=1, requirement_id=req.id, item_id=it.item_id,
        characteristic_ref="", organization_ref="", planning_stock_pool="default",
        source_type="stock", source_ref="default", source_line_ref="",
        alloc_qty=4.0, fact_at_freeze=4.0,
    ))
    db.add(MrpFreezeAllocation(
        run_id=run.run_id, freeze_version=1, requirement_id=req.id, item_id=it.item_id,
        characteristic_ref="", organization_ref="", planning_stock_pool="default",
        source_type="supplier_order", source_ref="SO-1", source_line_ref="77",
        alloc_qty=3.0, fact_at_freeze=3.0,
    ))
    db.flush()
    allocs = db.query(MrpFreezeAllocation).all()
    n = mirror_frozen_pins(db, [req], allocs)
    assert n == 2

    entry = _entry(db, req, CONSUME)
    pins = db.query(ReservationCoverage).filter(
        ReservationCoverage.reservation_id == entry.id,
        ReservationCoverage.pin_kind == "frozen",
    ).all()
    by_kind = {p.source_kind: p for p in pins}
    assert set(by_kind) == {"on_hand", "supplier_order"}  # stock→on_hand
    assert Decimal(str(by_kind["on_hand"].alloc_qty)) == Decimal("4")
    assert Decimal(str(by_kind["on_hand"].fact_at_freeze)) == Decimal("4")
    assert by_kind["supplier_order"].source_ref == "SO-1"
    assert by_kind["supplier_order"].source_line_ref == "77"
    assert Decimal(str(by_kind["supplier_order"].alloc_qty)) == Decimal("3")

    # idempotent: re-mirror does not duplicate
    assert mirror_frozen_pins(db, [req], db.query(MrpFreezeAllocation).all()) == 2
    assert db.query(ReservationCoverage).filter(
        ReservationCoverage.pin_kind == "frozen"
    ).count() == 2


def test_frozen_pin_supplier_attaches_to_make_for_produced(db_session):
    """Produced intermediate: supplier pin → make; stock/wip pin → consume."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    it = _item(db, "INT", produced=True)
    req = _req(db, run, it, gross=10, net=7, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")
    db.add(MrpFreezeAllocation(
        run_id=run.run_id, freeze_version=1, requirement_id=req.id, item_id=it.item_id,
        source_type="stock", source_ref="default", source_line_ref="", alloc_qty=3.0, fact_at_freeze=3.0,
    ))
    db.add(MrpFreezeAllocation(
        run_id=run.run_id, freeze_version=1, requirement_id=req.id, item_id=it.item_id,
        source_type="supplier_order", source_ref="SO-9", source_line_ref="5", alloc_qty=2.0, fact_at_freeze=2.0,
    ))
    db.flush()
    mirror_frozen_pins(db, [req], db.query(MrpFreezeAllocation).all())

    consume = _entry(db, req, CONSUME)
    make = _entry(db, req, MAKE)
    consume_pins = {p.source_kind for p in db.query(ReservationCoverage).filter(
        ReservationCoverage.reservation_id == consume.id, ReservationCoverage.pin_kind == "frozen").all()}
    make_pins = {p.source_kind for p in db.query(ReservationCoverage).filter(
        ReservationCoverage.reservation_id == make.id, ReservationCoverage.pin_kind == "frozen").all()}
    assert consume_pins == {"on_hand"}
    assert make_pins == {"supplier_order"}


# ---------------------------------------------------------------------------
# §6.1/§6.3 — SLE matching → realize
# ---------------------------------------------------------------------------
def test_pegged_issue_consume_realize_capped_residual_unplanned(db_session):
    """1С-created document: recorder GUID ≠ order GUID; the producing order is
    reachable only via the pull-captured header order_ref (chain source 2)."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    req = _req(db, run, comp, gross=4, net=4, bom_level=1)  # consume reserved 4
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")

    _order(db, "ORD-GUID-1", run=run)
    # СборкаЗапасов document GUID (recorder) ≠ order GUID; header carried the order
    _pull(db, "DOC-GUID-1", order_ref="ORD-GUID-1")
    # physical issue of 5: capped at outstanding 4, residual 1 unplanned
    _sle(db, comp, qty=-5, kind="assembly_out", recorder="DOC-GUID-1")

    scope = diagnostic_ledger_scope(db)
    summary = realize_from_sle(db, scope, "cyc")

    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert Decimal(str(entry.realized_qty)) == Decimal("4")
    assert summary["realized_consume"] == 1
    assert summary["unplanned_consumption"] == 1
    assert abs(summary["unplanned_qty"] - 1.0) < 1e-6
    ev = db.query(ReservationEvent).filter(
        ReservationEvent.reservation_id == entry.id,
        ReservationEvent.event_kind == "realize",
    ).one()
    assert Decimal(str(ev.realized_delta)) == Decimal("4")
    assert ev.match_rule == "pegged"
    # closed (realized 4 >= reserved 4)
    assert entry.lifecycle_status == "closed"


def test_sync_link_material_issue_consume_realize(db_session):
    """Our export: ПеремещениеЗапасов created by one_c_stock_transfer_export —
    recorder resolves via sync_link (source_doctype='material_issue') to the
    local issue → order → run (chain source 1). No pull order_ref needed."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    fg = _item(db, "FG", produced=True)
    comp = _item(db, "COMP", produced=False)
    make_req = _req(db, run, fg, gross=5, net=5, bom_level=0)
    req = _req(db, run, comp, gross=3, net=3, bom_level=1)
    materialize_reservations(db, [make_req, req], {run.run_id: run}, "cyc")

    order = _order(db, "ORD-GUID-2", run=run)
    line = _prod_line(db, order, fg, qty=5, src_req=make_req)
    issue = _material_issue(db, order, line)
    _link(db, "material_issue", issue.issue_id,
          "Document_ПеремещениеЗапасов", "TRANSFER-GUID-2")
    _sle(db, comp, qty=-3, kind="transfer_out", recorder="TRANSFER-GUID-2",
         recorder_type="Document_ПеремещениеЗапасов")

    scope = diagnostic_ledger_scope(db)
    summary = realize_from_sle(db, scope, "cyc")
    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert summary["realized_consume"] == 1
    assert summary["unplanned_consumption"] == 0
    assert Decimal(str(entry.realized_qty)) == Decimal("3")
    ev = db.query(ReservationEvent).filter(
        ReservationEvent.reservation_id == entry.id,
        ReservationEvent.event_kind == "realize",
    ).one()
    assert ev.match_rule == "pegged"


def test_pegged_production_receipt_make_realize(db_session):
    """Our export: СборкаЗапасов created by one_c_manufacture_export — recorder
    resolves via sync_link (source_doctype='manufacture') to the local
    manufacture → exact production line → make requirement (chain source 1)."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    fg = _item(db, "FG", produced=True)
    req = _req(db, run, fg, gross=5, net=5, bom_level=0)  # make reserved 5
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")

    order = _order(db, "ORD-GUID-3", run=run)
    line = _prod_line(db, order, fg, qty=5, produced=5, src_req=req)
    man = _manufacture(db, order, line, qty=5)
    _link(db, "manufacture", man.manufacture_id,
          "Document_СборкаЗапасов", "ASSEMBLY-GUID-3")
    _sle(db, fg, qty=5, kind="assembly_in", recorder="ASSEMBLY-GUID-3")

    scope = diagnostic_ledger_scope(db)
    summary = realize_from_sle(db, scope, "cyc")

    entry = _entry(db, req, MAKE)
    db.refresh(entry)
    assert summary["realized_make"] == 1
    assert Decimal(str(entry.realized_qty)) == Decimal("5")
    assert entry.lifecycle_status == "closed"  # produced 5 >= reserved 5
    ev = db.query(ReservationEvent).filter(
        ReservationEvent.reservation_id == entry.id,
        ReservationEvent.event_kind == "realize",
    ).one()
    assert ev.match_rule == "pegged"


def test_make_receipt_via_pull_order_ref(db_session):
    """1С-created СборкаЗапасов: make receipt resolves via the pull-captured
    ЗаказНаПроизводство_Key (chain source 2) → produced line → make."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    fg = _item(db, "FG", produced=True)
    req = _req(db, run, fg, gross=5, net=5, bom_level=0)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")

    order = _order(db, "ORD-GUID-4", run=run)
    _prod_line(db, order, fg, qty=5, produced=5, src_req=req)
    _pull(db, "ASSEMBLY-GUID-4", order_ref="ORD-GUID-4")
    _sle(db, fg, qty=5, kind="assembly_in", recorder="ASSEMBLY-GUID-4")

    scope = diagnostic_ledger_scope(db)
    summary = realize_from_sle(db, scope, "cyc")
    entry = _entry(db, req, MAKE)
    db.refresh(entry)
    assert summary["realized_make"] == 1
    assert Decimal(str(entry.realized_qty)) == Decimal("5")


def test_transfer_to_finished_goods_warehouse_closes_make(db_session):
    """Перемещение ГП на склад ГП (basis = ЗаказНаПроизводство): transfer_in
    landing on an is_finished_goods warehouse realizes the make reservation via
    the same chain. A transfer_in onto an ordinary warehouse does NOT."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    fg = _item(db, "FG", produced=True)
    req = _req(db, run, fg, gross=5, net=5, bom_level=0)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")

    order = _order(db, "ORD-GUID-5", run=run)
    _prod_line(db, order, fg, qty=5, src_req=req)
    _fg_warehouse(db, "FG-WH")
    # transfer document created in 1С with ДокументОснование = the order
    _pull(db, "TRANSFER-GUID-5", order_ref="ORD-GUID-5",
          recorder_type="Document_ПеремещениеЗапасов")
    # receipt onto an ORDINARY warehouse: not a make receipt → ignored
    _sle(db, fg, qty=5, kind="transfer_in", recorder="TRANSFER-GUID-5",
         recorder_type="Document_ПеремещениеЗапасов", warehouse="PLAIN-WH",
         line_no="1")
    scope = diagnostic_ledger_scope(db)
    summary = realize_from_sle(db, scope, "cyc")
    entry = _entry(db, req, MAKE)
    db.refresh(entry)
    assert summary["realized_make"] == 0
    assert Decimal(str(entry.realized_qty)) == Decimal("0")

    # receipt onto the ГП склад: realizes make
    _sle(db, fg, qty=5, kind="transfer_in", recorder="TRANSFER-GUID-5",
         recorder_type="Document_ПеремещениеЗапасов", warehouse="FG-WH",
         line_no="2")
    summary = realize_from_sle(db, diagnostic_ledger_scope(db), "cyc")
    db.refresh(entry)
    assert summary["realized_make"] == 1
    assert Decimal(str(entry.realized_qty)) == Decimal("5")
    assert entry.lifecycle_status == "closed"


def test_unmatched_issue_is_unplanned_no_realize(db_session):
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    req = _req(db, run, comp, gross=4, net=4, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")

    # recorder has NO sync_link and NO pull order_ref → unplanned, never a
    # silent FIFO (an order for the run exists, but nothing ties the doc to it)
    _order(db, "ORD-GUID-6", run=run)
    _pull(db, "UNKNOWN-DOC")  # pulled, but the header carried no order basis
    _sle(db, comp, qty=-3, kind="assembly_out", recorder="UNKNOWN-DOC")

    scope = diagnostic_ledger_scope(db)
    summary = realize_from_sle(db, scope, "cyc")
    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert Decimal(str(entry.realized_qty)) == Decimal("0")
    assert summary["realized_consume"] == 0
    assert summary["unplanned_consumption"] == 1
    assert abs(summary["unplanned_qty"] - 3.0) < 1e-6
    assert db.query(ReservationEvent).filter(ReservationEvent.event_kind == "realize").count() == 0


def test_pull_order_ref_to_unknown_order_is_unplanned(db_session):
    """Header carried an order GUID, but no local ProductionOrder mirrors it
    → the chain stops honestly at unplanned (no global FIFO)."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    req = _req(db, run, comp, gross=4, net=4, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")

    _pull(db, "DOC-GUID-7", order_ref="ORD-GUID-NOT-SYNCED")
    _sle(db, comp, qty=-2, kind="assembly_out", recorder="DOC-GUID-7")

    summary = realize_from_sle(db, diagnostic_ledger_scope(db), "cyc")
    assert summary["realized_consume"] == 0
    assert summary["unplanned_consumption"] == 1


# ---------------------------------------------------------------------------
# §5 — redistribute ORM adapter (golden: §7 example 1 step 0)
# ---------------------------------------------------------------------------
def test_redistribute_persists_floating_coverage_golden(db_session):
    db = db_session
    it = _item(db, "X", produced=False)
    r1 = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    r2 = _run(db, date(2026, 7, 16), date(2026, 7, 31))
    r3 = _run(db, date(2026, 8, 1), date(2026, 8, 15))
    q1 = _req(db, r1, it, gross=6, net=6, bom_level=1)
    q2 = _req(db, r2, it, gross=5, net=5, bom_level=1)
    q3 = _req(db, r3, it, gross=4, net=4, bom_level=1)
    runs = {r.run_id: r for r in (r1, r2, r3)}
    materialize_reservations(db, [q1, q2, q3], runs, "cyc")

    # on_hand = 10 (no warehouse settings → sum all bins)
    generation_id = db.get(models.PlanningTruthState, 1).current_generation_id
    db.add(StockBin(
        ledger_generation_id=generation_id,
        item_id=it.item_id, warehouse_ref1c="WH", on_hand=Decimal("10"),
    ))
    db.flush()

    redistribute_pool(db, it.item_id, {it.item_id: 10.0}, "cyc")

    e1, e2, e3 = _entry(db, q1, CONSUME), _entry(db, q2, CONSUME), _entry(db, q3, CONSUME)
    for e in (e1, e2, e3):
        db.refresh(e)
    # Pass B (§7 ex1 step0): covered 6 / 4 / 0
    assert Decimal(str(e1.covered_on_hand_qty)) == Decimal("6")
    assert Decimal(str(e2.covered_on_hand_qty)) == Decimal("4")
    assert Decimal(str(e3.covered_on_hand_qty)) == Decimal("0")
    assert Decimal(str(e1.uncovered_qty)) == Decimal("0")
    assert Decimal(str(e2.uncovered_qty)) == Decimal("1")
    assert Decimal(str(e3.uncovered_qty)) == Decimal("4")
    assert e1.coverage_state == "covered"
    assert e2.coverage_state == "partial"
    assert e3.coverage_state == "uncovered"

    # floating coverage rows persisted (on_hand for R1=6, R2=4)
    floats = db.query(ReservationCoverage).filter(
        ReservationCoverage.pin_kind == "floating", ReservationCoverage.source_kind == "on_hand"
    ).all()
    covered = {int(f.reservation_id): Decimal(str(f.covered_qty)) for f in floats}
    assert covered.get(e1.id) == Decimal("6")
    assert covered.get(e2.id) == Decimal("4")
    assert e3.id not in covered  # zero coverage → no floating row


# ---------------------------------------------------------------------------
# idempotency — re-run cycle → identical reservation state, no double events
# ---------------------------------------------------------------------------
def test_reservation_shadow_idempotent(db_session):
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    req = _req(db, run, comp, gross=4, net=4, bom_level=1)
    _order(db, "ORD-GUID-8", run=run)
    _pull(db, "DOC-GUID-8", order_ref="ORD-GUID-8")
    _sle(db, comp, qty=-2, kind="assembly_out", recorder="DOC-GUID-8")
    generation_id = db.get(models.PlanningTruthState, 1).current_generation_id
    db.add(StockBin(
        ledger_generation_id=generation_id,
        item_id=comp.item_id, warehouse_ref1c="WH", on_hand=Decimal("10"),
    ))
    db.flush()

    scope = diagnostic_ledger_scope(db)
    run_reservation_shadow(db, scope, "cyc-1")
    entries_1 = db.query(ReservationEntry).count()
    events_1 = db.query(ReservationEvent).count()
    entry = _entry(db, req, CONSUME)
    realized_1 = Decimal(str(entry.realized_qty))

    scope2 = diagnostic_ledger_scope(db)
    run_reservation_shadow(db, scope2, "cyc-2")
    assert db.query(ReservationEntry).count() == entries_1
    assert db.query(ReservationEvent).count() == events_1  # no double open / realize
    db.refresh(entry)
    assert Decimal(str(entry.realized_qty)) == realized_1


def test_same_requirement_materializes_independently_per_generation(db_session):
    db = db_session
    first_generation = db.get(models.PlanningTruthState, 1).current_generation
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    item = _item(db, "GEN-ISO", produced=True)
    req = _req(db, run, item, gross=4, net=4, bom_level=0)
    materialize_reservations(
        db, [req], {run.run_id: run}, "first",
        ledger_generation_id=first_generation.id,
    )
    second_generation = models.LedgerGeneration(
        generation_key="test-building-generation-two",
        status="building",
        source_watermarks={},
        capabilities={},
        physical_import_batch_id=first_generation.physical_import_batch_id,
        algorithm_version="tests/2",
    )
    db.add(second_generation)
    db.flush()
    materialize_reservations(
        db, [req], {run.run_id: run}, "second",
        ledger_generation_id=second_generation.id,
    )
    db.commit()

    entries = (
        db.query(ReservationEntry)
        .filter(
            ReservationEntry.requirement_id == req.id,
            ReservationEntry.realization_mode == MAKE,
        )
        .order_by(ReservationEntry.ledger_generation_id)
        .all()
    )
    assert [e.ledger_generation_id for e in entries] == [
        first_generation.id,
        second_generation.id,
    ]
    open_events = db.query(ReservationEvent).filter(
        ReservationEvent.event_kind == "open",
        ReservationEvent.reservation_id.in_([e.id for e in entries]),
    ).all()
    assert {e.ledger_generation_id for e in open_events} == {
        first_generation.id,
        second_generation.id,
    }
    assert len({e.idempotency_key for e in open_events}) == 1


def test_writer_without_generation_context_fails_before_insert(db_session):
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    item = _item(db, "GEN-MISSING", produced=True)
    req = _req(db, run, item, gross=4, net=4, bom_level=0)
    db.query(models.PlanningTruthState).delete()
    db.flush()

    with pytest.raises(ValueError, match="requires explicit"):
        materialize_reservations(db, [req], {run.run_id: run}, "missing")

    assert db.query(ReservationEntry).filter(
        ReservationEntry.requirement_id == req.id,
    ).count() == 0


def test_accepted_generation_is_immutable_for_reservation_writers(db_session):
    db = db_session
    generation = db.get(models.PlanningTruthState, 1).current_generation
    generation.status = "accepted"
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    item = _item(db, "GEN-ACCEPTED", produced=True)
    req = _req(db, run, item, gross=4, net=4, bom_level=0)
    db.flush()

    with pytest.raises(ValueError, match="writes require building"):
        materialize_reservations(
            db, [req], {run.run_id: run}, "accepted",
            ledger_generation_id=generation.id,
        )

    assert db.query(ReservationEntry).filter(
        ReservationEntry.requirement_id == req.id,
    ).count() == 0


def test_freeze_materialization_rejects_accepted_generation_and_writes_building_target(
    db_session,
):
    """Freeze obligations are Ledger writes: target must be an explicit BUILDING fork."""
    db = db_session
    accepted = db.get(models.PlanningTruthState, 1).current_generation
    accepted.status = "accepted"
    accepted.cutoff = datetime(2026, 7, 23)
    accepted.accepted_at = datetime(2026, 7, 23)
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    item = _item(db, "FREEZE-GEN", produced=True)
    req = _req(db, run, item, gross=4, net=4, bom_level=0)
    db.flush()

    with pytest.raises(ValueError, match="writes require building"):
        materialize_reservations_for_freeze(
            db, [run.run_id], ledger_generation_id=accepted.id,
        )
    assert db.query(ReservationEntry).filter(
        ReservationEntry.requirement_id == req.id,
    ).count() == 0

    building = models.LedgerGeneration(
        generation_key="test-freeze-obligation-target",
        status="building",
        source_watermarks={},
        capabilities={},
        physical_import_batch_id=accepted.physical_import_batch_id,
        algorithm_version="tests/obligation-target",
    )
    db.add(building)
    db.flush()

    result = materialize_reservations_for_freeze(
        db, [run.run_id], ledger_generation_id=building.id,
    )
    assert result == {"reservations": 1, "frozen_pins": 0}
    entry = _entry(db, req, MAKE)
    assert entry is not None
    assert entry.ledger_generation_id == building.id


# ---------------------------------------------------------------------------
# regression — run_ledger_cycle executed/drift/closure unchanged with the block
# ---------------------------------------------------------------------------
def test_run_ledger_cycle_result_unchanged_with_reservation_block(db_session):
    db = db_session
    run = _run(db, date(2026, 6, 1), date(2026, 6, 30))
    fg = _item(db, "FG", produced=True)
    req = _req(db, run, fg, gross=10, net=10, bom_level=0)
    order = _order(db, "PO-FG", run=run)
    _prod_line(db, order, fg, qty=10, produced=10, src_req=req)
    db.flush()

    result = run_ledger_cycle(db)

    # existing execution result: full-history Δ = executed 10, requirement closed.
    assert result["total_executed"] == pytest.approx(10.0)
    assert result["requirements_closed"] == 1
    # the standard return-dict keys are present and unextended by the shadow block
    assert set(result) == {
        "cycle_id", "runs", "items_touched", "total_executed", "execution_rows",
        "coverage_rows", "realized_total", "evaporated_total", "evaporation_events",
        "drift_events", "drift_pending_pools", "drift_matured_shortfall",
        "drift_matured_surplus", "drift_evap_adjust", "drift_unattributed",
        "requirements_closed", "runs_closed",
    }
    # The quarantined diagnostic reproduces arithmetic only in its isolated
    # BUILDING generation; accepted Ledger truth remains immutable.
    diagnostic = db.query(models.LedgerGeneration).filter_by(
        generation_key="legacy-ledger-diagnostic",
    ).one()
    diagnostic_entry = db.query(ReservationEntry).filter_by(
        requirement_id=req.id,
        realization_mode=MAKE,
        ledger_generation_id=diagnostic.id,
    ).one_or_none()
    assert diagnostic_entry is not None
    accepted_id = db.get(models.PlanningTruthState, 1).current_generation_id
    assert db.query(ReservationEntry).filter_by(
        requirement_id=req.id,
        realization_mode=MAKE,
        ledger_generation_id=accepted_id,
    ).count() == 0

    # shadow report is read-only and consistent
    report = reservation_shadow_report(db)
    assert report["counts"]["reservations_active"] >= 0
