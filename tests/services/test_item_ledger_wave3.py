"""Wave-3 item-ledger tests (design §5 trigger т1, §6.1 transfer semantics).

Covers:
* т1 — event-driven incremental redistribute after a ledger-1 apply:
  - a pull refreshes position / uncovered WITHOUT running the cycle;
  - the realize-before-redistribute ORDER is enforced (a pegged issue lowers
    outstanding first, so coverage is computed against the reduced demand);
  - a reconcile adjustment triggers redistribute (a negative delta surfaces
    uncovered — §7 example 3);
  - the trigger is idempotent (no doubled events, byte-identical caches).
* Task 2 — transfer_out consume-realize = ФАКТ ВЫХОДА из контура:
  - an internal contour→contour move does NOT realize (reserve keeps the pool);
  - a transfer_out leaving the contour (workshop) realizes consume.
* Task 3 — material_issue direction='return' decreases realized (unrealize to
  the same reserve, capped), reopening a reserve the outbound had closed.
"""

from datetime import date
from decimal import Decimal

import pytest

pytestmark = pytest.mark.usefixtures("building_ledger_generation")

from app import models
from app.models import ReservationEvent
from app.services.item_ledger.ingest import pull_recorder_movements
from app.services.item_ledger.physical import LedgerKey, rebuild_running_balance
from app.services.item_ledger.reconcile import (
    ledger_on_hand_by_item,
    reconcile_balance_snapshot,
)
from app.services.item_ledger.reservation import CONSUME
from app.services.item_ledger.reservation_ledger import (
    materialize_reservations,
    realize_from_sle,
    redistribute_after_ledger_apply,
    redistribute_pool,
)
from app.services.mrp_execution_ledger import _ledger_scope
from tests.services.test_item_ledger_reservation_lifecycle import (
    FakeODataClient,
    _register_line,
)
from tests.services.test_item_ledger_reservation_materialization import (
    diagnostic_ledger_scope,
    _entry,
    _item,
    _order,
    _prod_line,
    _pull,
    _req,
    _run,
    _sle,
)

TRANSFER = "Document_ПеремещениеЗапасов"
RECEIPT = "Document_ПоступлениеТоваровУслуг"


def _warehouse(db, ref, *, selected=True, fg=False):
    w = models.StockWarehouse(
        warehouse_ref1c=ref,
        warehouse_name=f"WH {ref}",
        is_selected=selected,
        is_finished_goods=fg,
    )
    db.add(w)
    db.flush()
    return w


def _material_issue(db, order, product, *, direction="issue"):
    mi = models.ProductionMaterialIssue(
        document_number=f"MI-{order.order_id}-{product.product_id}-{direction}",
        product_id=product.product_id,
        order_id=order.order_id,
        status="exported",
        direction=direction,
    )
    db.add(mi)
    db.flush()
    return mi


def _link(db, doctype, source_id, ref):
    lnk = models.SyncLink(
        source_doctype=doctype,
        source_id=int(source_id),
        target_entity=TRANSFER,
        target_ref_key=ref,
        status="success",
    )
    db.add(lnk)
    db.flush()
    return lnk


def _rebuild(db, item, wh):
    generation_id = db.get(models.PlanningTruthState, 1).current_generation_id
    rebuild_running_balance(
        db, LedgerKey(item.item_id, "", "", wh),
        ledger_generation_id=generation_id,
    )


# ---------------------------------------------------------------------------
# т1 — incremental redistribute after a ledger-1 apply
# ---------------------------------------------------------------------------
def test_t1_pull_receipt_refreshes_uncovered_without_cycle(db_session):
    """A physical receipt pulled by document refreshes the touched pool's
    uncovered WITHOUT any run_ledger_cycle — «переписываем цифры при поступлении»."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    comp.item_ref1c = "ref-comp"
    _warehouse(db, "wh-1", selected=True)
    req = _req(db, run, comp, gross=6, net=6, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "seed")
    # baseline: empty pool → uncovered = reserved 6
    redistribute_pool(db, comp.item_id, {}, "seed")
    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert Decimal(str(entry.uncovered_qty)) == Decimal("6")

    # pull a receipt of 6 onto the contour warehouse — NO cycle is run
    client = FakeODataClient(
        {"RCPT-1": [_register_line("1", "Receipt", "ref-comp", "wh-1", 6)]}
    )
    generation_id = db.get(models.PlanningTruthState, 1).current_generation_id
    pull_recorder_movements(
        db, RECEIPT, "RCPT-1", client=client,
        ledger_generation_id=generation_id,
    )

    db.refresh(entry)
    assert Decimal(str(entry.uncovered_qty)) == Decimal("0")  # т1 refreshed it
    assert entry.coverage_state == "covered"
    assert Decimal(str(entry.covered_on_hand_qty)) == Decimal("6")


def _pegged_issue_pool(db, *, reserved, on_hand, issue_qty):
    """reserve(reserved) + on_hand seeded via a receipt SLE + a pegged
    assembly_out issue SLE (not yet matched). Returns (comp, req)."""
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    _warehouse(db, "wh-1", selected=True)
    req = _req(db, run, comp, gross=reserved, net=reserved, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "seed")
    _order(db, "ORD-O", run=run)
    _pull(db, "DOC-O", order_ref="ORD-O")
    _sle(db, comp, qty=on_hand, kind="receipt", recorder="SEED",
         recorder_type=RECEIPT, warehouse="wh-1")
    _sle(db, comp, qty=-issue_qty, kind="assembly_out", recorder="DOC-O",
         warehouse="wh-1", line_no="1")
    _rebuild(db, comp, "wh-1")
    return comp, req


def test_t1_realize_precedes_redistribute(db_session):
    """The trigger realizes the pegged issue FIRST (outstanding drops), THEN
    redistributes — so coverage is computed against the reduced demand. A bare
    redistribute (wrong order) would surface a phantom uncovered instead."""
    db = db_session
    comp, req = _pegged_issue_pool(db, reserved=10, on_hand=10, issue_qty=6)
    entry = _entry(db, req, CONSUME)

    # wrong order: redistribute WITHOUT realizing first → outstanding still 10,
    # on_hand already 4 (issue SLE applied to the bin) → phantom uncovered 6.
    on_hand = ledger_on_hand_by_item(db)
    assert on_hand[comp.item_id] == 4.0
    redistribute_pool(db, comp.item_id, on_hand, "pre")
    db.refresh(entry)
    assert Decimal(str(entry.realized_qty)) == Decimal("0")
    assert Decimal(str(entry.uncovered_qty)) == Decimal("6")

    # т1: realize (6) THEN redistribute → outstanding 4, on_hand 4 → fully covered
    summary = redistribute_after_ledger_apply(db, [comp.item_id], "t1")
    db.refresh(entry)
    assert summary["realized_consume"] == 1
    assert Decimal(str(entry.realized_qty)) == Decimal("6")
    assert Decimal(str(entry.uncovered_qty)) == Decimal("0")
    assert entry.coverage_state == "covered"


def test_t1_idempotent(db_session):
    """A repeat trigger call adds no events and leaves the caches byte-identical."""
    db = db_session
    comp, req = _pegged_issue_pool(db, reserved=10, on_hand=10, issue_qty=6)
    redistribute_after_ledger_apply(db, [comp.item_id], "t1")
    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    events_1 = db.query(ReservationEvent).count()
    realized_1 = Decimal(str(entry.realized_qty))
    uncovered_1 = Decimal(str(entry.uncovered_qty))
    floats_1 = db.query(models.ReservationCoverage).filter(
        models.ReservationCoverage.pin_kind == "floating"
    ).count()

    redistribute_after_ledger_apply(db, [comp.item_id], "t1-again")
    db.refresh(entry)
    assert db.query(ReservationEvent).count() == events_1  # no doubled realize
    assert Decimal(str(entry.realized_qty)) == realized_1
    assert Decimal(str(entry.uncovered_qty)) == uncovered_1
    assert db.query(models.ReservationCoverage).filter(
        models.ReservationCoverage.pin_kind == "floating"
    ).count() == floats_1


def test_t1_reconcile_adjustment_triggers_redistribute(db_session):
    """A matured reconcile adjustment (negative delta) refreshes the pool via т1:
    on_hand drops and the reserve's uncovered surfaces (§7 example 3)."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    comp.item_ref1c = "ref-comp"
    _warehouse(db, "wh-1", selected=True)
    req = _req(db, run, comp, gross=10, net=10, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "seed")
    _sle(db, comp, qty=10, kind="receipt", recorder="SEED",
         recorder_type=RECEIPT, warehouse="wh-1")
    _rebuild(db, comp, "wh-1")
    redistribute_pool(db, comp.item_id, ledger_on_hand_by_item(db), "seed")
    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert Decimal(str(entry.uncovered_qty)) == Decimal("0")

    # a manual списание happened in 1С → Balance snapshot shows 7 (delta -3).
    snapshot = {LedgerKey(comp.item_id, "", "", "wh-1"): Decimal("7")}
    # 1st sweep: debounce stores pending, no apply. 2nd sweep: apply + т1.
    generation_id = db.get(models.PlanningTruthState, 1).current_generation_id
    reconcile_balance_snapshot(
        db, snapshot, block_all_items=False, discovery_client=None,
        ledger_generation_id=generation_id,
    )
    db.refresh(entry)
    assert Decimal(str(entry.uncovered_qty)) == Decimal("0")  # not applied yet
    reconcile_balance_snapshot(
        db, snapshot, block_all_items=False, discovery_client=None,
        ledger_generation_id=generation_id,
    )

    db.refresh(entry)
    assert ledger_on_hand_by_item(db)[comp.item_id] == 7.0
    assert Decimal(str(entry.uncovered_qty)) == Decimal("3")  # т1 surfaced it
    assert entry.coverage_state == "partial"


# ---------------------------------------------------------------------------
# Task 2 — transfer_out realizes consume only when it LEAVES the contour
# ---------------------------------------------------------------------------
def test_internal_contour_transfer_does_not_realize(db_session):
    """A ПеремещениеЗапасов whose transfer_in lands on a CONTOUR warehouse is an
    internal pool move — the reserve keeps holding the pool, realize NOTHING."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    _warehouse(db, "wh-A", selected=True)
    _warehouse(db, "wh-B", selected=True)  # both in the contour
    req = _req(db, run, comp, gross=5, net=5, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "seed")

    # same recorder moves comp out of A and into B (both contour)
    _sle(db, comp, qty=-5, kind="transfer_out", recorder="MOVE-1",
         recorder_type=TRANSFER, warehouse="wh-A", line_no="1")
    _sle(db, comp, qty=5, kind="transfer_in", recorder="MOVE-1",
         recorder_type=TRANSFER, warehouse="wh-B", line_no="2")

    summary = realize_from_sle(db, diagnostic_ledger_scope(db), "cyc")
    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert summary["internal_transfer"] == 1
    assert summary["realized_consume"] == 0
    assert Decimal(str(entry.realized_qty)) == Decimal("0")
    assert entry.lifecycle_status == "active"


def test_transfer_out_of_contour_realizes(db_session):
    """A transfer_out whose partner lands on a NON-contour (workshop) warehouse
    leaves the contour → realizes the consume reserve (pegged)."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    _warehouse(db, "wh-A", selected=True)          # contour source
    _warehouse(db, "wh-W", selected=False)         # workshop, out of contour
    req = _req(db, run, comp, gross=5, net=5, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "seed")
    _order(db, "ORD-2", run=run)
    _pull(db, "MOVE-2", order_ref="ORD-2", recorder_type=TRANSFER)

    _sle(db, comp, qty=-5, kind="transfer_out", recorder="MOVE-2",
         recorder_type=TRANSFER, warehouse="wh-A", line_no="1")
    _sle(db, comp, qty=5, kind="transfer_in", recorder="MOVE-2",
         recorder_type=TRANSFER, warehouse="wh-W", line_no="2")

    summary = realize_from_sle(db, diagnostic_ledger_scope(db), "cyc")
    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert summary["internal_transfer"] == 0
    assert summary["realized_consume"] == 1
    assert Decimal(str(entry.realized_qty)) == Decimal("5")
    ev = db.query(ReservationEvent).filter(
        ReservationEvent.reservation_id == entry.id,
        ReservationEvent.event_kind == "realize",
    ).one()
    assert ev.match_rule == "pegged"


# ---------------------------------------------------------------------------
# Task 3 — material_issue direction='return' decreases realized (unrealize)
# ---------------------------------------------------------------------------
def _return_setup(db, *, reserved, outbound_qty):
    """reserve + a pegged outbound assembly_out that realizes `outbound_qty` +
    a return material_issue (direction='return') linked to recorder 'RET-1'.
    Returns (comp, req, order)."""
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    parent = _item(db, "PARENT", produced=True)
    comp = _item(db, "COMP", produced=False)
    _warehouse(db, "wh-A", selected=True)     # contour
    _warehouse(db, "wh-W", selected=False)    # workshop (out of contour)
    req = _req(db, run, comp, gross=reserved, net=reserved, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "seed")
    order = _order(db, "ORD-1", run=run)
    line = _prod_line(db, order, parent, qty=reserved)
    # outbound issue realizes the reserve (assembly_out, pegged via pull order_ref)
    _pull(db, "DOC-O", order_ref="ORD-1")
    _sle(db, comp, qty=-outbound_qty, kind="assembly_out", recorder="DOC-O",
         warehouse="wh-A", line_no="1")
    realize_from_sle(db, diagnostic_ledger_scope(db), "outbound")

    # return document: material_issue direction='return', workshop → contour
    ret = _material_issue(db, order, line, direction="return")
    _link(db, "material_issue", ret.issue_id, "RET-1")
    return comp, req, order


def test_return_decreases_realized_reserve_active(db_session):
    """Partial outbound (reserve stays active): a return unrealizes the returned
    qty back off realized (capped)."""
    db = db_session
    comp, req, _order_ = _return_setup(db, reserved=10, outbound_qty=6)
    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert Decimal(str(entry.realized_qty)) == Decimal("6")
    assert entry.lifecycle_status == "active"

    # return 2 of the leftovers back into the contour
    _sle(db, comp, qty=-2, kind="transfer_out", recorder="RET-1",
         recorder_type=TRANSFER, warehouse="wh-W", line_no="1")
    summary = realize_from_sle(db, diagnostic_ledger_scope(db), "return")

    db.refresh(entry)
    assert summary["returned_unrealize"] == 1
    assert summary["realized_consume"] == 0
    assert Decimal(str(entry.realized_qty)) == Decimal("4")  # 6 − 2
    assert entry.lifecycle_status == "active"
    kinds = [str(e.event_kind) for e in db.query(ReservationEvent).filter(
        ReservationEvent.reservation_id == entry.id
    ).order_by(ReservationEvent.id.asc()).all()]
    assert kinds == ["open", "realize", "unrealize"]


def test_return_reopens_closed_reserve(db_session):
    """Full outbound closes the reserve; a return unrealizes and REOPENS it."""
    db = db_session
    comp, req, _order_ = _return_setup(db, reserved=6, outbound_qty=6)
    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert Decimal(str(entry.realized_qty)) == Decimal("6")
    assert entry.lifecycle_status == "closed"

    _sle(db, comp, qty=-2, kind="transfer_out", recorder="RET-1",
         recorder_type=TRANSFER, warehouse="wh-W", line_no="1")
    summary = realize_from_sle(db, diagnostic_ledger_scope(db), "return")

    db.refresh(entry)
    assert summary["returned_unrealize"] == 1
    assert Decimal(str(entry.realized_qty)) == Decimal("4")
    assert entry.lifecycle_status == "active"  # reopened
    kinds = [str(e.event_kind) for e in db.query(ReservationEvent).filter(
        ReservationEvent.reservation_id == entry.id
    ).order_by(ReservationEvent.id.asc()).all()]
    assert kinds == ["open", "realize", "unrealize", "reopen"]


def test_return_unrealize_capped_and_idempotent(db_session):
    """Unrealize is capped at realized (never negative) and idempotent by SLE id."""
    db = db_session
    comp, req, _order_ = _return_setup(db, reserved=10, outbound_qty=6)
    # return MORE than was realized → capped at 6
    _sle(db, comp, qty=-9, kind="transfer_out", recorder="RET-1",
         recorder_type=TRANSFER, warehouse="wh-W", line_no="1")
    realize_from_sle(db, diagnostic_ledger_scope(db), "return")
    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert Decimal(str(entry.realized_qty)) == Decimal("0")  # 6 − min(9, 6)

    events_1 = db.query(ReservationEvent).count()
    realize_from_sle(db, diagnostic_ledger_scope(db), "return-again")
    db.refresh(entry)
    assert db.query(ReservationEvent).count() == events_1  # no double unrealize
    assert Decimal(str(entry.realized_qty)) == Decimal("0")
