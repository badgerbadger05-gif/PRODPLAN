"""Inc6 (design §11 Инк6 + §3/§3.1 + §5) — netting/drift onto the ledger
substrate, gated by STOCK_SOURCE=bin.

Three atomic parts flip on the SAME flag as Inc5:
  (а) drift shrink — actual consumption READ from the SLE (Σ issue-kind qty<0
      since the freeze anchor), the frozen-norm model + W=48h window REMOVED;
  (б) effective_net = uncovered(consume) + Σ supplier-pin (alloc − realized) —
      reconstructs today's net_required from the reservation ledger (Finding A);
  (г) the evaporation term is REMOVED from compute_stock_drift, since (б) already
      surfaces a dead supplier pin via uncovered — counted EXACTLY ONCE (Finding D).

DEFAULT (STOCK_SOURCE=legacy / unset) is byte-identical to Inc5 — asserted here
too (norm model + W window still in force). The whole 1144-test baseline stays
green; these tests are additive.
"""

from datetime import date, datetime

import pytest

from app import models
from app.models import (
    MrpDriftEvent,
    MrpFreezeBaseline,
    ReservationEntry,
    StockLedgerEntry,
)
from app.services.item_ledger.reservation_ledger import effective_net_bin
from app.services.mrp_execution_ledger import run_ledger_cycle

# reuse the battle-tested fixtures from the execution-ledger test module
from tests.services.test_mrp_execution_ledger import (
    _drift_component,
    _make_baseline,
    _make_freeze_alloc,
    _make_purchased_item,
    _make_receipt,
    _make_req,
    _make_run,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _bin(flag):
    return {"STOCK_SOURCE": flag}


def _add_sle(db, item, *, qty, movement_kind, posting_at, recorder="R-1", line="1"):
    row = StockLedgerEntry(
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        warehouse_ref1c="WH-1",
        qty=qty,
        qty_after=0.0,
        posting_at=posting_at,
        record_type="Expense" if qty < 0 else "Receipt",
        movement_kind=movement_kind,
        recorder_type="Document_СборкаЗапасов",
        recorder_ref=recorder,
        line_no=line,
        ingest_source="pull",
        active=True,
    )
    db.add(row)
    db.flush()
    return row


def _drift_adj(db, req):
    db.refresh(req)
    return float(req.drift_adjustment_qty)


def _consume_entry(db, req):
    return (
        db.query(ReservationEntry)
        .filter(
            ReservationEntry.requirement_id == req.id,
            ReservationEntry.realization_mode == "consume",
        )
        .one_or_none()
    )


# ---------------------------------------------------------------------------
# (а) drift — SLE consumption vs norm model + W window
# ---------------------------------------------------------------------------
def test_legacy_drift_uses_norm_model_and_ignores_sle(db_session, monkeypatch):
    """DEFAULT (legacy): compute_stock_drift reads the FROZEN NORM model, NOT the
    SLE — an assembly_out SLE that would imply a different consumption is ignored;
    and the W window is in force (a first-cycle shortfall is pending, not matured).
    """
    monkeypatch.delenv("STOCK_SOURCE", raising=False)
    # norm path: parent produces 10 @ norm 2 → expected consumption 20 →
    # expected stock 80; current stock 80 → drift 0.
    _run, _p, comp, req = _drift_component(
        db_session, comp_stock=80.0, parent_produced=10, norm=2.0, comp_s0=100.0, net=100, initial=100
    )
    # An SLE that, if read, would imply consumption 50 (→ surplus +30). Legacy
    # must ignore it entirely.
    _add_sle(db_session, comp, qty=-50.0, movement_kind="assembly_out", posting_at=datetime(2026, 6, 15))
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    assert _drift_adj(db_session, req) == 0.0
    assert db_session.query(MrpDriftEvent).filter(
        MrpDriftEvent.item_id == comp.item_id, MrpDriftEvent.kind == "surplus"
    ).count() == 0


def test_legacy_shortfall_needs_W_window(db_session, monkeypatch):
    """DEFAULT (legacy): a first-cycle off-plan shortfall is PENDING (W window),
    materialising nothing yet — the contrast to the immediate bin path below."""
    monkeypatch.delenv("STOCK_SOURCE", raising=False)
    _run, _p, comp, req = _drift_component(
        db_session, comp_stock=90.0, parent_produced=0, norm=1.0, comp_s0=100.0, net=100, initial=100
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    assert _drift_adj(db_session, req) == 0.0  # pending — W not elapsed
    ev = db_session.query(MrpDriftEvent).filter(
        MrpDriftEvent.item_id == comp.item_id, MrpDriftEvent.kind == "shortfall"
    ).all()
    assert len(ev) == 1 and bool(ev[0].matured) is False


def test_bin_consumption_read_from_sle_equals_norm_on_clean_case(db_session, monkeypatch):
    """(а) bin: consumption READ from the SLE equals the norm-model expectation on
    a clean case → drift 0, no event. Parent produced 10 @ norm 2 = 20; the
    assembly_out SLE is exactly −20; stock 80 = S0 100 − 20."""
    monkeypatch.setenv("STOCK_SOURCE", "bin")
    _run, _p, comp, req = _drift_component(
        db_session, comp_stock=80.0, parent_produced=10, norm=2.0, comp_s0=100.0, net=100, initial=100
    )
    _add_sle(db_session, comp, qty=-20.0, movement_kind="assembly_out", posting_at=datetime(2026, 6, 15))
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    assert _drift_adj(db_session, req) == 0.0
    assert db_session.query(MrpDriftEvent).filter(
        MrpDriftEvent.item_id == comp.item_id,
        MrpDriftEvent.kind.in_(("shortfall", "surplus")),
    ).count() == 0


def test_bin_sle_drives_drift_not_norm(db_session, monkeypatch):
    """(а) bin: the SLE (not the norm) drives drift — with only −10 consumed by
    an issue SLE but the norm model would have said 20, expected = 100 − 10 = 90
    while actual stock is 80 → shortfall 10, materialised IMMEDIATELY (no W)."""
    monkeypatch.setenv("STOCK_SOURCE", "bin")
    _run, _p, comp, req = _drift_component(
        db_session, comp_stock=80.0, parent_produced=10, norm=2.0, comp_s0=100.0, net=100, initial=100
    )
    _add_sle(db_session, comp, qty=-10.0, movement_kind="assembly_out", posting_at=datetime(2026, 6, 15))
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    assert _drift_adj(db_session, req) == 10.0  # matured on cycle 1


def test_bin_fresh_writeoff_matures_immediately_no_W(db_session, monkeypatch):
    """(а) bin: the W=48h window is GONE. An out-of-band stock drop (no explaining
    issue SLE — the adjustment/write-off residual already in on_hand) materialises
    on the FIRST cycle, whereas legacy waited ≥2 cycles + 48h (asserted above)."""
    monkeypatch.setenv("STOCK_SOURCE", "bin")
    _run, _p, comp, req = _drift_component(
        db_session, comp_stock=90.0, parent_produced=0, norm=1.0, comp_s0=100.0, net=100, initial=100
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    assert _drift_adj(db_session, req) == 10.0  # immediate, no wait
    ev = db_session.query(MrpDriftEvent).filter(
        MrpDriftEvent.item_id == comp.item_id, MrpDriftEvent.kind == "shortfall"
    ).all()
    assert len(ev) == 1 and bool(ev[0].matured) is True


# ---------------------------------------------------------------------------
# (б)/(г) effective_net from the reservation ledger
# ---------------------------------------------------------------------------
def _purchased_req_with_pool(db, code, *, net):
    """A purchased (consume-only) requirement in a scoped FIXED_SNAPSHOT run with
    a freeze baseline anchor. on_hand = 0 (no bins) → uncovered = net at freeze."""
    item = _make_purchased_item(db, code)
    run = _make_run(db, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    req = _make_req(db, run, item, net=net, bom_level=1)
    _make_baseline(db, run, item, version=1)
    return run, item, req


def test_bin_effective_net_equals_net_required_without_supplier_pins(db_session, monkeypatch):
    """(б) bin: effective_net = uncovered + Σ supplier pins; with NO supplier pin
    it equals legacy net_required (closure preserved)."""
    monkeypatch.setenv("STOCK_SOURCE", "bin")
    _run, _item, req = _purchased_req_with_pool(db_session, "P-NOPIN", net=10)
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    entry = _consume_entry(db_session, req)
    assert entry is not None
    assert float(entry.uncovered_qty) == 10.0  # uncovered == net (no coverage)
    assert effective_net_bin(db_session, req) == 10.0
    assert effective_net_bin(db_session, req) == float(req.net_required_qty)


def test_bin_finding_a_supplier_order_keeps_same_closure_threshold(db_session, monkeypatch):
    """(б) Finding A regression: a purchased requirement WITH an existing supplier
    order keeps the SAME closure threshold under bin as legacy. net_required
    (=10) INCLUDES the 4 covered by the order; uncovered EXCLUDES it (=6); the
    supplier term (alloc − realized = 4) adds it back → effective_net = 10."""
    monkeypatch.setenv("STOCK_SOURCE", "bin")
    _run, item, req = _purchased_req_with_pool(db_session, "P-PINA", net=10)
    # a live (in-transit, NON-terminal) supplier order covering 4, frozen as a pin
    line = _make_receipt(db_session, item, received=0, quantity=4, state_name="В пути", order_ref1c="SUP-A")
    _make_freeze_alloc(
        db_session, _run, req, item,
        source_type="supplier_order", source_ref="SUP-A",
        source_line_ref=line.item_id_ref, alloc_qty=4, fact_at_freeze=4,
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    entry = _consume_entry(db_session, req)
    assert float(entry.uncovered_qty) == 6.0            # supplier pin covers 4
    eff = effective_net_bin(db_session, req)
    assert eff == 10.0                                   # 6 + 4 (alloc)
    # identical to the legacy closure threshold (net_required + drift, drift 0)
    legacy_eff = max(float(req.net_required_qty) + float(req.drift_adjustment_qty), 0.0)
    assert eff == legacy_eff == 10.0


def test_bin_finding_d_evaporation_raises_effective_net_exactly_once(db_session, monkeypatch):
    """(г) Finding D regression: an evaporating supplier pin raises effective_net
    by the pin qty EXACTLY ONCE. A dead pin lifts uncovered by 4 (6→10) while the
    supplier term stays 4 (alloc − realized, NOT alloc − evaporated) → 14, i.e.
    net_required(10) + evaporated(4). NOT 18 (double count)."""
    monkeypatch.setenv("STOCK_SOURCE", "bin")
    _run, item, req = _purchased_req_with_pool(db_session, "P-PIND", net=10)
    # a CANCELLED (terminal) supplier order that never delivered → evaporates 4
    line = _make_receipt(db_session, item, received=0, quantity=4, state_name="Отменён", order_ref1c="SUP-D")
    alloc = _make_freeze_alloc(
        db_session, _run, req, item,
        source_type="supplier_order", source_ref="SUP-D",
        source_line_ref=line.item_id_ref, alloc_qty=4, fact_at_freeze=4,
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(alloc)
    assert float(alloc.evaporated_qty) == 4.0            # pin died
    entry = _consume_entry(db_session, req)
    assert float(entry.uncovered_qty) == 10.0            # uncovered rose by 4
    eff = effective_net_bin(db_session, req)
    assert eff == 14.0                                   # ONCE: 10 + 4, not 18
    # (г): the evaporation was NOT ALSO folded into drift_adjustment under bin
    assert float(req.drift_adjustment_qty) == 0.0


def test_bin_evaporation_removed_from_drift_under_bin(db_session, monkeypatch):
    """(г) bin: compute_stock_drift no longer folds evaporation into
    drift_adjustment (that channel now lives on uncovered). Contrast the legacy
    test_i3_evaporation_terminal_order where drift_adjustment == evaporated."""
    monkeypatch.setenv("STOCK_SOURCE", "bin")
    item = _make_purchased_item(db_session, "E-BIN")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    req = _make_req(db_session, run, item, net=100, bom_level=1)
    _make_baseline(db_session, run, item, received_total=0.0, version=1)
    line = _make_receipt(db_session, item, received=10, quantity=50, state_name="Отменён", order_ref1c="SUP-EB")
    alloc = _make_freeze_alloc(
        db_session, run, req, item,
        source_type="supplier_order", source_ref="SUP-EB",
        source_line_ref=line.item_id_ref, alloc_qty=50, fact_at_freeze=50,
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    db_session.refresh(alloc)
    assert float(alloc.evaporated_qty) == 40.0
    # legacy would set drift_adjustment_qty == 40; under bin it is 0 (Finding D)
    assert float(req.drift_adjustment_qty) == 0.0
