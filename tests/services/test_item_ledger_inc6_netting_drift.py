"""Inc6 (design §11 Инк6 + §3/§3.1 + §5) — netting/drift onto the ledger
substrate, gated by STOCK_SOURCE=bin.

Three atomic parts flip on the SAME flag as Inc5:
  (а) drift shrink — actual consumption READ from the SLE (Σ issue-kind qty<0
      since the freeze anchor), the frozen-norm model + W=48h window REMOVED;
  (б) effective_net = uncovered(consume) + Σ supplier-pin pin_live
      (alloc − evaporated − realized) — reconstructs today's net_required from the
      reservation ledger (Finding A) while keeping evaporation single-channel;
  (г) supplier_order evaporation is EXCLUDED from compute_stock_drift (it
      resurfaces via own_open_coverage in the sizer); WIP-pin evaporation is KEPT
      (it WAS netted into net_required and is not own_open_coverage). A dead
      supplier pin thus surfaces EXACTLY ONCE — never as both a drift/effective_net
      rise AND a coverage loss (corrected Finding D).

DEFAULT (STOCK_SOURCE=legacy / unset) is byte-identical to Inc5 — asserted here
too (norm model + W window still in force). The whole 1144-test baseline stays
green; these tests are additive.
"""

from datetime import date, datetime

import pytest

pytestmark = pytest.mark.usefixtures("building_ledger_generation")

from app import models
from decimal import Decimal

from app.models import (
    MrpDriftEvent,
    MrpFreezeBaseline,
    ReservationEntry,
    StockBin,
    StockLedgerEntry,
)
from app.services.item_ledger.reservation_ledger import effective_net_bin
from app.services.mrp_execution_ledger import run_ledger_cycle as _public_run_ledger_cycle


def run_ledger_cycle(db):
    return _public_run_ledger_cycle(db, diagnostic_legacy=True)

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


def _generation_id(db):
    return db.get(models.PlanningTruthState, 1).current_generation_id


def _add_sle(db, item, *, qty, movement_kind, posting_at, recorder="R-1", line="1"):
    generation = db.get(models.PlanningTruthState, 1).current_generation
    row = StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash=(
            f"inc6:{recorder}:{line}:{item.item_id}:{qty}:{movement_kind}"
        )[:64],
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
    assert effective_net_bin(
        db_session, req, ledger_generation_id=_generation_id(db_session),
    ) == 10.0
    assert effective_net_bin(
        db_session, req, ledger_generation_id=_generation_id(db_session),
    ) == float(req.net_required_qty)


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
    eff = effective_net_bin(
        db_session, req, ledger_generation_id=_generation_id(db_session),
    )
    assert eff == 10.0                                   # 6 + 4 (alloc)
    # identical to the legacy closure threshold (net_required + drift, drift 0)
    legacy_eff = max(float(req.net_required_qty) + float(req.drift_adjustment_qty), 0.0)
    assert eff == legacy_eff == 10.0


def test_bin_finding_d_evaporation_stays_single_channel_via_own_coverage(db_session, monkeypatch):
    """(г) corrected Finding D: an evaporating supplier pin must resurface through
    EXACTLY ONE channel — own_open_coverage dropping in the sizer — and must NOT
    also inflate effective_net. A supplier pin is own_open_coverage and is NOT
    netted into net_required, so a dead pin lifts uncovered by 4 (6→10) while
    pin_live drops by 4 (alloc − evaporated − realized = 4 − 4 − 0 = 0); the two
    moves cancel → effective_net stays at the true demand 10, NOT 14 (which would
    over-order by the dead pin's alloc of 4 = the double count)."""
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
    eff = effective_net_bin(
        db_session, req, ledger_generation_id=_generation_id(db_session),
    )
    assert eff == 10.0                                   # single-channel: NOT 14
    # (г): the evaporation was NOT ALSO folded into drift_adjustment under bin
    assert float(req.drift_adjustment_qty) == 0.0


def test_bin_finding_d_partial_delivery_then_cancel(db_session, monkeypatch):
    """(г) partial supplier pin: alloc=4, 1 delivered (realized, now in stock),
    3 cancelled (evaporated). The 1 delivered counts (on_hand covers 1 → uncovered
    9); nothing else is incoming (pin_live = max(4 − 3 − 1, 0) = 0). effective_net
    = 9 + 0 = 9 → proposal 9, NOT 12 (pre-fix: uncovered 9 + (alloc − realized 3) =
    12, double-counting the dead 3)."""
    monkeypatch.setenv("STOCK_SOURCE", "bin")
    _run, item, req = _purchased_req_with_pool(db_session, "P-PINP", net=10)
    # the 1 delivered unit is now physically on hand
    generation_id = db_session.get(
        models.PlanningTruthState, 1,
    ).current_generation_id
    db_session.add(StockBin(
        ledger_generation_id=generation_id,
        item_id=item.item_id, warehouse_ref1c="WH", on_hand=Decimal("1"),
    ))
    # partially received (1), remainder cancelled (order terminal) → evaporates 3
    line = _make_receipt(db_session, item, received=1, quantity=4, state_name="Отменён", order_ref1c="SUP-P")
    alloc = _make_freeze_alloc(
        db_session, _run, req, item,
        source_type="supplier_order", source_ref="SUP-P",
        source_line_ref=line.item_id_ref, alloc_qty=4, fact_at_freeze=4,
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(alloc)
    assert float(alloc.realized_qty) == 1.0
    assert float(alloc.evaporated_qty) == 3.0
    entry = _consume_entry(db_session, req)
    assert float(entry.uncovered_qty) == 9.0             # on_hand 1 covers 1 of 10
    # pin_live = max(4 − 3 − 1, 0) = 0 → effective_net = uncovered 9 + 0 = 9 (not 12)
    assert effective_net_bin(
        db_session, req, ledger_generation_id=_generation_id(db_session),
    ) == 9.0
    assert float(req.drift_adjustment_qty) == 0.0


def _legacy_effective_net(db, req):
    """Legacy closure target = net_required + drift_adjustment (drift folds in the
    KEPT evaporation channel — WIP pins only, post-fix)."""
    db.refresh(req)
    return max(float(req.net_required_qty) + float(req.drift_adjustment_qty), 0.0)


def test_legacy_supplier_evaporation_excluded_from_drift_matches_bin(db_session, monkeypatch):
    """Legacy path (default): a CANCELLED supplier pin no longer folds into
    drift_adjustment (single-channel — it resurfaces via own_open_coverage in the
    sizer). So legacy effective_net stays at the true demand 10, IDENTICAL to bin
    post-cancellation — both were 14 before the fix. This is the legacy-vs-bin
    equality the fix restores."""
    monkeypatch.setenv("STOCK_SOURCE", "legacy")
    _run, item, req = _purchased_req_with_pool(db_session, "P-LEGD", net=10)
    line = _make_receipt(db_session, item, received=0, quantity=4, state_name="Отменён", order_ref1c="SUP-LD")
    alloc = _make_freeze_alloc(
        db_session, _run, req, item,
        source_type="supplier_order", source_ref="SUP-LD",
        source_line_ref=line.item_id, alloc_qty=4, fact_at_freeze=4,
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(alloc)
    assert float(alloc.evaporated_qty) == 4.0            # pin died
    # supplier evaporation excluded from drift → effective_net holds at 10, not 14
    assert float(req.drift_adjustment_qty) == 0.0
    assert _legacy_effective_net(db_session, req) == 10.0
    # legacy == bin post-cancellation (both correct at 10, was 14 in both)
    monkeypatch.setenv("STOCK_SOURCE", "bin")
    run_ledger_cycle(db_session)
    db_session.commit()
    assert effective_net_bin(
        db_session, req, ledger_generation_id=_generation_id(db_session),
    ) == 10.0


def test_legacy_wip_evaporation_still_raises_effective_net(db_session, monkeypatch):
    """WIP-path guard: a WIP pin WAS netted into net_required at freeze and is NOT
    own_open_coverage, so its evaporation MUST still resurface via drift/
    effective_net. The fix filters ONLY supplier_order pins out of the drift
    evaporation term — WIP evaporation is KEPT: drift_adjustment == evaporated,
    effective_net = net_required + evaporated (10 + 4 = 14)."""
    monkeypatch.setenv("STOCK_SOURCE", "legacy")
    _run, item, req = _purchased_req_with_pool(db_session, "P-LWIP", net=10)
    # a WIP pin covering 4 whose product line is gone → terminal → evaporates 4
    alloc = _make_freeze_alloc(
        db_session, _run, req, item,
        source_type="wip_order", source_ref="WIP-L",
        source_line_ref="987654", alloc_qty=4, fact_at_freeze=4,
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(alloc)
    assert float(alloc.evaporated_qty) == 4.0            # WIP pin died
    # WIP evaporation is KEPT in drift → effective_net rises by the evaporated qty
    assert float(req.drift_adjustment_qty) == 4.0
    assert _legacy_effective_net(db_session, req) == 14.0


def test_legacy_partial_supplier_evaporation_excluded_from_drift(db_session, monkeypatch):
    """Legacy partial: alloc=4, realized 1, evaporated 3. Post-fix the supplier
    evaporation (3) is EXCLUDED from drift → drift_adjustment 0, so legacy
    effective_net = net_required (10) with NO evaporation inflation. (Pre-fix it
    folded the 3 → drift 3 → effective_net 13, the double count.)

    Parity note: legacy ``net_required`` is frozen and does not see the 1 now in
    stock, so this isolated test reads 10; in a real cycle net_required is
    recomputed to 9 (stock nets the delivery), matching bin's live uncovered=9 →
    both paths propose 9. The FIX's contribution — removing the evaporation
    double-count — is what is asserted here (13 → 10)."""
    monkeypatch.setenv("STOCK_SOURCE", "legacy")
    _run, item, req = _purchased_req_with_pool(db_session, "P-LEGP", net=10)
    line = _make_receipt(db_session, item, received=1, quantity=4, state_name="Отменён", order_ref1c="SUP-LP")
    alloc = _make_freeze_alloc(
        db_session, _run, req, item,
        source_type="supplier_order", source_ref="SUP-LP",
        source_line_ref=line.item_id, alloc_qty=4, fact_at_freeze=4,
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(alloc)
    assert float(alloc.realized_qty) == 1.0
    assert float(alloc.evaporated_qty) == 3.0
    # supplier evaporation excluded → NO drift inflation (pre-fix would be 3)
    assert float(req.drift_adjustment_qty) == 0.0
    assert _legacy_effective_net(db_session, req) == 10.0


def test_bin_evaporation_removed_from_drift_under_bin(db_session, monkeypatch):
    """(г) bin: compute_stock_drift no longer folds evaporation into
    drift_adjustment (that channel now lives on uncovered/pin_live). Under legacy
    too, supplier_order evaporation is excluded from drift (single-channel via
    own_open_coverage) — see test_i3_evaporation_terminal_order."""
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
