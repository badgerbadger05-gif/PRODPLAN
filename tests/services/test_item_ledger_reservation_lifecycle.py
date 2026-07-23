"""Reservation lifecycle — аудит Д2/Д3/Д6 (design §6.1, §6.2, §8, решение №15).

Covers:
* Д2 — unrealize compensation on replace-by-recorder: a re-pull of the same
  recorder must NOT double realized_qty; the compensation is visible as
  ``unrealize`` events; a reserve closed by the compensated realize is
  re-opened (``reopen``) and correctly re-realized by the fresh SLE rows.
* Д3а — release on run closure (apply_run_closure / force_close_run):
  reserves → released, outstanding → 0, reserved_soft falls / available rises;
  force-close is idempotent; reopen_run returns released reserves to active;
  the run_reservation_shadow sweep self-heals pre-existing ghosts.
* Д3б — cancel on refreeze when the requirement's gross demand vanished
  (identity row kept by the (run,item) upsert); resurrection re-opens.
* Д6 — mirror_frozen_pins removes orphan frozen pins (a requirement left
  without allocations, or an empty allocation set).
"""

import re
from datetime import date
from decimal import Decimal

import pytest

from app import models
from app.models import (
    MrpFreezeAllocation,
    ReservationCoverage,
    ReservationEntry,
    ReservationEvent,
)
from app.services.item_ledger.ingest import EMPTY_GUID, pull_recorder_movements
from app.services.item_ledger.reservation import CONSUME, MAKE
from app.services.item_ledger.reservation_ledger import (
    item_ledger_position,
    materialize_reservations,
    mirror_frozen_pins,
    realize_from_sle,
    release_run_reservations,
    reopen_run_reservations,
    run_reservation_shadow,
    unrealize_replaced_sle,
)
from app.services.mrp_execution_ledger import (
    _ledger_scope,
    apply_run_closure,
)
from app.services.mrp_reconciliation import force_close_run, reopen_run
from tests.services.test_item_ledger_reservation_materialization import (
    _entry,
    _item,
    _order,
    _prod_line,
    _pull,
    _req,
    _run,
    _sle,
)

ASSEMBLY = "Document_СборкаЗапасов"


# ---------------------------------------------------------------------------
# fakes / helpers
# ---------------------------------------------------------------------------
class FakeODataClient:
    """Returns the Inc0-confirmed recorder-row shape for a cast-Recorder filter."""

    def __init__(self, records_by_recorder):
        self.records_by_recorder = records_by_recorder

    def get_all(self, entity_name, filter_query=None, order_by=None, **kwargs):
        m = re.search(r"guid'([^']+)'", filter_query or "")
        ref = m.group(1) if m else None
        lines = self.records_by_recorder.get(ref, [])
        if not lines:
            return []
        return [{"Recorder": ref, "Recorder_Type": "X", "RecordSet": list(lines)}]


def _register_line(line_no, record_type, item_ref, wh_ref, qty):
    return {
        "Period": "2026-07-10T10:00:00",
        "LineNumber": line_no,
        "Active": True,
        "RecordType": record_type,
        "Организация_Key": EMPTY_GUID,
        "Номенклатура_Key": item_ref,
        "Характеристика_Key": EMPTY_GUID,
        "СтруктурнаяЕдиница_Key": wh_ref,
        "Количество": qty,
    }


def _events(db, entry):
    return (
        db.query(ReservationEvent)
        .filter(ReservationEvent.reservation_id == entry.id)
        .order_by(ReservationEvent.id.asc())
        .all()
    )


def _kinds(db, entry):
    return [str(e.event_kind) for e in _events(db, entry)]


def _pegged_consume_setup(db, *, net=4):
    """run + purchased component req (consume reserved=net) + producing order
    reachable via the pull-captured header order_ref (chain source 2), with a
    real warehouse and item_ref1c so pull_recorder_movements can ingest."""
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    comp.item_ref1c = "ref-comp"
    db.add(models.StockWarehouse(warehouse_ref1c="wh-1", warehouse_name="WH1"))
    req = _req(db, run, comp, gross=net, net=net, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")
    _order(db, "ORD-G", run=run)
    _pull(db, "DOC-G", order_ref="ORD-G")
    db.flush()
    return run, comp, req


def _sle_id_spacer(db):
    """Keep stock_ledger_entry ids monotonic across a replace-by-recorder.

    PostgreSQL BigInt identity never reuses ids, so a re-pulled recorder's fresh
    rows always get NEW ids (the realize idempotency key realize:{entry}:{sle}
    relies on that). SQLite reuses the max rowid after a delete — park an
    unrelated row at the top so the test matches the prod id semantics."""
    spacer = _item(db, f"SPACER-{db.query(models.Item).count()}", produced=False)
    _sle(db, spacer, qty=1, kind="receipt", recorder="SPACER-DOC")


# ---------------------------------------------------------------------------
# Д2 — replace-by-recorder → unrealize compensation
# ---------------------------------------------------------------------------
def test_repull_same_recorder_does_not_double_realized(db_session):
    """Перепроведение (identical re-pull): realized stays at the document qty,
    compensation is visible as unrealize, the closed reserve is re-opened and
    re-closed by the fresh rows — never doubled."""
    db = db_session
    run, comp, req = _pegged_consume_setup(db, net=4)
    client = FakeODataClient(
        {"DOC-G": [_register_line("1", "Expense", "ref-comp", "wh-1", 4)]}
    )
    pull_recorder_movements(db, ASSEMBLY, "DOC-G", client=client)
    realize_from_sle(db, _ledger_scope(db), "cyc")

    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert Decimal(str(entry.realized_qty)) == Decimal("4")
    assert entry.lifecycle_status == "closed"

    # replace-by-recorder: same document re-pulled (identical lines, new SLE ids).
    # UPDATED for trigger т1: the pull now compensates (unrealize + reopen) AND
    # eagerly re-matches the fresh rows within the SAME pull (redistribute_after_
    # ledger_apply). Pre-т1 the re-match was deferred to the next explicit
    # realize_from_sle, so this asserted the transient realized=0 / active state;
    # now the observable post-pull state is the correct single-count realized=4
    # (compensated then re-realized — NOT doubled to 8), reserve re-closed.
    _sle_id_spacer(db)
    pull_recorder_movements(db, ASSEMBLY, "DOC-G", client=client)
    db.refresh(entry)
    assert Decimal(str(entry.realized_qty)) == Decimal("4")
    assert Decimal(str(entry.reserved_qty)) == Decimal("4")
    assert entry.lifecycle_status == "closed"
    assert _kinds(db, entry) == ["open", "realize", "unrealize", "reopen", "realize"]

    # the explicit re-match is now a no-op (т1 already applied it in the pull) —
    # still realized 4, never 8.
    realize_from_sle(db, _ledger_scope(db), "cyc2")
    db.refresh(entry)
    assert Decimal(str(entry.realized_qty)) == Decimal("4")
    assert entry.lifecycle_status == "closed"
    assert _kinds(db, entry) == ["open", "realize", "unrealize", "reopen", "realize"]


def test_repull_changed_qty_lands_on_new_document_qty(db_session):
    """Перепроведение с изменённым количеством: realized = новое qty документа."""
    db = db_session
    run, comp, req = _pegged_consume_setup(db, net=4)
    client4 = FakeODataClient(
        {"DOC-G": [_register_line("1", "Expense", "ref-comp", "wh-1", 4)]}
    )
    pull_recorder_movements(db, ASSEMBLY, "DOC-G", client=client4)
    realize_from_sle(db, _ledger_scope(db), "cyc")

    client3 = FakeODataClient(
        {"DOC-G": [_register_line("1", "Expense", "ref-comp", "wh-1", 3)]}
    )
    _sle_id_spacer(db)
    pull_recorder_movements(db, ASSEMBLY, "DOC-G", client=client3)
    realize_from_sle(db, _ledger_scope(db), "cyc2")

    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert Decimal(str(entry.realized_qty)) == Decimal("3")
    # outstanding 1 → the reserve stays active (no premature closure)
    assert entry.lifecycle_status == "active"


def test_unrealize_replaced_sle_idempotent(db_session):
    """Direct unit: a second compensation pass over the same sle_ids is a no-op
    (keyed by the compensated realize event id)."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    req = _req(db, run, comp, gross=4, net=4, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")
    _order(db, "ORD-U", run=run)
    _pull(db, "DOC-U", order_ref="ORD-U")
    sle = _sle(db, comp, qty=-4, kind="assembly_out", recorder="DOC-U")
    realize_from_sle(db, _ledger_scope(db), "cyc")

    assert unrealize_replaced_sle(db, [sle.id], "DOC-U") == 1
    assert unrealize_replaced_sle(db, [sle.id], "DOC-U") == 0
    entry = _entry(db, req, CONSUME)
    assert _kinds(db, entry).count("unrealize") == 1
    db.refresh(entry)
    assert Decimal(str(entry.realized_qty)) == Decimal("0")


def test_unrealize_no_realize_events_is_noop(db_session):
    """A replaced recorder whose SLE were never matched produces nothing."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    _req(db, run, comp, gross=4, net=4, bom_level=1)
    sle = _sle(db, comp, qty=-4, kind="assembly_out", recorder="DOC-X")
    assert unrealize_replaced_sle(db, [sle.id], "DOC-X") == 0
    assert db.query(ReservationEvent).filter(
        ReservationEvent.event_kind == "unrealize"
    ).count() == 0


# ---------------------------------------------------------------------------
# Д3а — release on run closure / reopen
# ---------------------------------------------------------------------------
def test_release_run_reservations_drops_reserved_soft(db_session):
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    req = _req(db, run, comp, gross=6, net=6, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")

    pos_before = item_ledger_position(db, [comp.item_id])[comp.item_id]
    assert pos_before["reserved_soft"] == pytest.approx(6.0)
    assert pos_before["available"] == pytest.approx(-6.0)

    assert release_run_reservations(db, [run.run_id], "cyc") == 1
    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert entry.lifecycle_status == "released"
    # outstanding → 0: release reserved_delta = −outstanding
    assert Decimal(str(entry.reserved_qty)) == Decimal(str(entry.realized_qty))
    ev = [e for e in _events(db, entry) if e.event_kind == "release"]
    assert len(ev) == 1
    assert Decimal(str(ev[0].reserved_delta)) == Decimal("-6")

    pos_after = item_ledger_position(db, [comp.item_id])[comp.item_id]
    assert pos_after["reserved_soft"] == pytest.approx(0.0)
    assert pos_after["available"] == pytest.approx(0.0)  # rose from −6

    # idempotent: repeat call finds no active entries
    assert release_run_reservations(db, [run.run_id], "cyc") == 0
    assert _kinds(db, entry).count("release") == 1


def test_apply_run_closure_releases_reservations(db_session):
    """Auto-closure by execution releases the run's reservations."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    req = _req(db, run, comp, gross=5, net=5, bom_level=1)
    req.executed_qty = 5.0  # deficit executed in full
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")

    scope = _ledger_scope(db)
    closed = apply_run_closure(db, scope, cycle_id="cyc")
    assert closed == [run.run_id]

    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert entry.lifecycle_status == "released"
    assert item_ledger_position(db, [comp.item_id])[comp.item_id][
        "reserved_soft"
    ] == pytest.approx(0.0)


def test_force_close_releases_and_is_idempotent(db_session):
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    fg = _item(db, "FG", produced=True)
    comp = _item(db, "COMP", produced=False)
    make_req = _req(db, run, fg, gross=5, net=5, bom_level=0)
    req = _req(db, run, comp, gross=3, net=3, bom_level=1)
    materialize_reservations(db, [make_req, req], {run.run_id: run}, "cyc")

    res = force_close_run(db, run.run_id)
    assert res["status"] == "closed"
    assert res["reservations_released"] == 2  # make + consume

    for r, mode in ((make_req, MAKE), (req, CONSUME)):
        entry = _entry(db, r, mode)
        db.refresh(entry)
        assert entry.lifecycle_status == "released"
        assert _kinds(db, entry).count("release") == 1

    # idempotent repeat: already_closed, no duplicate release events
    res2 = force_close_run(db, run.run_id)
    assert res2["status"] == "already_closed"
    assert res2["reservations_released"] == 0
    assert db.query(ReservationEvent).filter(
        ReservationEvent.event_kind == "release"
    ).count() == 2


def test_reopen_run_restores_released_reservations(db_session):
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    req = _req(db, run, comp, gross=6, net=6, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")

    force_close_run(db, run.run_id)
    res = reopen_run(db, run.run_id)
    assert res["reservations_reopened"] == 1

    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert entry.lifecycle_status == "active"
    # the reopen event restores the released outstanding
    assert Decimal(str(entry.reserved_qty)) == Decimal("6")
    assert _kinds(db, entry) == ["open", "release", "reopen"]
    assert item_ledger_position(db, [comp.item_id])[comp.item_id][
        "reserved_soft"
    ] == pytest.approx(6.0)

    # second closure generation: release keys stay unique
    force_close_run(db, run.run_id)
    db.refresh(entry)
    assert entry.lifecycle_status == "released"
    assert _kinds(db, entry) == ["open", "release", "reopen", "release"]


def test_reopen_run_reservations_direct_idempotent(db_session):
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    req = _req(db, run, comp, gross=2, net=2, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")
    release_run_reservations(db, [run.run_id], "cyc")
    assert reopen_run_reservations(db, [run.run_id], "cyc") == 1
    assert reopen_run_reservations(db, [run.run_id], "cyc") == 0
    entry = _entry(db, req, CONSUME)
    assert _kinds(db, entry).count("reopen") == 1


def test_shadow_sweep_releases_ghosts_of_closed_run(db_session):
    """Pre-fix ghosts: a run already CLOSED with active reservations is swept
    by run_reservation_shadow (release_closed_run_reservations)."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    req = _req(db, run, comp, gross=4, net=4, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")
    run.status = "CLOSED"  # closed WITHOUT release (pre-fix state)
    db.flush()

    scope = _ledger_scope(db)  # CLOSED run with an open req stays in scope
    summary = run_reservation_shadow(db, scope, "cyc")
    assert summary["reservations_released_swept"] == 1

    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert entry.lifecycle_status == "released"
    assert item_ledger_position(db, [comp.item_id])[comp.item_id][
        "reserved_soft"
    ] == pytest.approx(0.0)


def test_released_entry_not_amended_by_materialize(db_session):
    """materialize must never amend a released reserve back up (the sweep and
    the cycle would otherwise fight each other)."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    req = _req(db, run, comp, gross=6, net=6, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")
    release_run_reservations(db, [run.run_id], "cyc")

    materialize_reservations(db, [req], {run.run_id: run}, "cyc2")
    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert entry.lifecycle_status == "released"
    assert Decimal(str(entry.reserved_qty)) == Decimal("0")
    assert "amend" not in _kinds(db, entry)


def test_late_realization_of_released_reserve_is_unplanned(db_session):
    """Released-маршрутизация (Прил. B §5): a late issue does not resurrect a
    released reserve — it is honest unplanned_consumption."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    req = _req(db, run, comp, gross=4, net=4, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")
    _order(db, "ORD-L", run=run)
    _pull(db, "DOC-L", order_ref="ORD-L")
    release_run_reservations(db, [run.run_id], "cyc")

    _sle(db, comp, qty=-4, kind="assembly_out", recorder="DOC-L")
    summary = realize_from_sle(db, _ledger_scope(db), "cyc2")
    assert summary["realized_consume"] == 0
    assert summary["unplanned_consumption"] == 1
    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert Decimal(str(entry.realized_qty)) == Decimal("0")
    assert entry.lifecycle_status == "released"


# ---------------------------------------------------------------------------
# Д3б — cancel при refreeze (требование реально исчезло)
# ---------------------------------------------------------------------------
def test_refreeze_vanished_requirement_cancels_reserve(db_session):
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    req = _req(db, run, comp, gross=6, net=6, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")

    # refreeze: the item dropped out of the plan — the (run,item) upsert keeps
    # the identity row and zeroes the demand (period_plan_service._freeze_one_run)
    req.total_required_qty = 0.0
    req.net_required_qty = 0.0
    req.freeze_version = 2
    run.active_freeze_version = 2
    db.flush()
    materialize_reservations(db, [req], {run.run_id: run}, "cyc2")

    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert entry.lifecycle_status == "cancelled"
    assert _kinds(db, entry) == ["open", "cancel"]
    cancel = _events(db, entry)[-1]
    assert Decimal(str(cancel.reserved_delta)) == Decimal("-6")  # −outstanding
    assert item_ledger_position(db, [comp.item_id])[comp.item_id][
        "reserved_soft"
    ] == pytest.approx(0.0)

    # idempotent: a re-run at the same version adds nothing
    materialize_reservations(db, [req], {run.run_id: run}, "cyc3")
    assert _kinds(db, entry) == ["open", "cancel"]


def test_refreeze_cancel_keeps_realized_history(db_session):
    """cancel (reserved_delta = −outstanding): the already-realized part stays
    in history — reserved folds down to realized, not to zero (design §8)."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    req = _req(db, run, comp, gross=6, net=6, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")
    _order(db, "ORD-C", run=run)
    _pull(db, "DOC-C", order_ref="ORD-C")
    _sle(db, comp, qty=-2, kind="assembly_out", recorder="DOC-C")
    realize_from_sle(db, _ledger_scope(db), "cyc")

    req.total_required_qty = 0.0
    req.net_required_qty = 0.0
    req.freeze_version = 2
    run.active_freeze_version = 2
    db.flush()
    materialize_reservations(db, [req], {run.run_id: run}, "cyc2")

    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert entry.lifecycle_status == "cancelled"
    assert Decimal(str(entry.realized_qty)) == Decimal("2")
    assert Decimal(str(entry.reserved_qty)) == Decimal("2")  # 6 − outstanding 4


def test_refreeze_net_zero_with_live_gross_is_not_cancelled(db_session):
    """net=0 covered-by-stock (gross > 0) is NOT «требование исчезло»: the make
    reserve amends to 0 but stays active; consume keeps the gross."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    it = _item(db, "INT", produced=True)
    req = _req(db, run, it, gross=6, net=6, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")

    req.net_required_qty = 0.0  # covered by stock at refreeze; gross stays 6
    req.freeze_version = 2
    run.active_freeze_version = 2
    db.flush()
    materialize_reservations(db, [req], {run.run_id: run}, "cyc2")

    make = _entry(db, req, MAKE)
    consume = _entry(db, req, CONSUME)
    db.refresh(make)
    db.refresh(consume)
    assert make.lifecycle_status == "active"
    assert Decimal(str(make.reserved_qty)) == Decimal("0")
    assert consume.lifecycle_status == "active"
    assert Decimal(str(consume.reserved_qty)) == Decimal("6")
    assert db.query(ReservationEvent).filter(
        ReservationEvent.event_kind == "cancel"
    ).count() == 0


def test_cancelled_reserve_resurrects_on_later_refreeze(db_session):
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    comp = _item(db, "COMP", produced=False)
    req = _req(db, run, comp, gross=6, net=6, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")

    req.total_required_qty = 0.0
    req.net_required_qty = 0.0
    req.freeze_version = 2
    run.active_freeze_version = 2
    db.flush()
    materialize_reservations(db, [req], {run.run_id: run}, "cyc2")

    # the demand came back at a later refreeze
    req.total_required_qty = 4.0
    req.net_required_qty = 4.0
    req.freeze_version = 3
    run.active_freeze_version = 3
    db.flush()
    materialize_reservations(db, [req], {run.run_id: run}, "cyc3")

    entry = _entry(db, req, CONSUME)
    db.refresh(entry)
    assert entry.lifecycle_status == "active"
    assert Decimal(str(entry.reserved_qty)) == Decimal("4")
    assert _kinds(db, entry) == ["open", "cancel", "reopen", "amend"]


# ---------------------------------------------------------------------------
# Д6 — mirror_frozen_pins: пины-сироты
# ---------------------------------------------------------------------------
def _alloc(db, run, req, it, *, source_type, source_ref, line_ref="", qty=3.0, version=1):
    a = MrpFreezeAllocation(
        run_id=run.run_id,
        freeze_version=version,
        requirement_id=req.id,
        item_id=it.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="default",
        source_type=source_type,
        source_ref=source_ref,
        source_line_ref=line_ref,
        alloc_qty=qty,
        fact_at_freeze=qty,
    )
    db.add(a)
    db.flush()
    return a


def _frozen_pins(db, entry):
    return (
        db.query(ReservationCoverage)
        .filter(
            ReservationCoverage.reservation_id == entry.id,
            ReservationCoverage.pin_kind == "frozen",
        )
        .all()
    )


def test_mirror_removes_orphan_pins_when_allocs_vanish_entirely(db_session):
    """refreeze с опустевшими allocs: the old pins must not survive and keep
    feeding Pass A / effective_net_bin."""
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    it = _item(db, "BUY", produced=False)
    req = _req(db, run, it, gross=10, net=6, bom_level=1)
    materialize_reservations(db, [req], {run.run_id: run}, "cyc")
    _alloc(db, run, req, it, source_type="supplier_order", source_ref="SO-1", line_ref="7")
    mirror_frozen_pins(db, [req], db.query(MrpFreezeAllocation).all())
    entry = _entry(db, req, CONSUME)
    assert len(_frozen_pins(db, entry)) == 1

    # refreeze left NO allocations at all — the early return must not skip cleanup
    assert mirror_frozen_pins(db, [req], []) == 0
    assert _frozen_pins(db, entry) == []


def test_mirror_removes_orphan_pins_of_requirement_without_fresh_allocs(db_session):
    db = db_session
    run = _run(db, date(2026, 7, 1), date(2026, 7, 15))
    it1 = _item(db, "BUY1", produced=False)
    it2 = _item(db, "BUY2", produced=False)
    req1 = _req(db, run, it1, gross=5, net=5, bom_level=1)
    req2 = _req(db, run, it2, gross=5, net=5, bom_level=1)
    materialize_reservations(db, [req1, req2], {run.run_id: run}, "cyc")
    a1 = _alloc(db, run, req1, it1, source_type="supplier_order", source_ref="SO-1")
    _alloc(db, run, req2, it2, source_type="supplier_order", source_ref="SO-2")
    mirror_frozen_pins(db, [req1, req2], db.query(MrpFreezeAllocation).all())
    e1, e2 = _entry(db, req1, CONSUME), _entry(db, req2, CONSUME)
    assert len(_frozen_pins(db, e1)) == 1 and len(_frozen_pins(db, e2)) == 1

    # refreeze: req2 lost its allocation, req1 keeps its own
    assert mirror_frozen_pins(db, [req1, req2], [a1]) == 1
    assert len(_frozen_pins(db, e1)) == 1
    assert _frozen_pins(db, e2) == []
