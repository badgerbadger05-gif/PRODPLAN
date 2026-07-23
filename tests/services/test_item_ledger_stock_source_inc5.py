"""Inc5 — switch stock readers to the ledger behind the STOCK_SOURCE flag.

Design §11 Инк5 + §2.5 (projection) + §3 (formulas). This increment is the
first reader flip, gated so the DEFAULT (STOCK_SOURCE=legacy) is byte-identical
to today. Covered here:

* the STOCK_SOURCE feature flag (default legacy, bin case-insensitive, garbage
  → legacy);
* flag=legacy → effective_stock_by_item_all reads the LEGACY source
  (ItemWarehouseStock / Item.stock_qty), stock_bin ignored;
* flag=bin → effective_stock_by_item_all reads stock_bin, and equals the legacy
  result item-for-item when the bin is seeded from the same snapshot (the
  equivalence property) — finished_goods + ignored warehouses excluded
  identically;
* flag=bin excludes an is_finished_goods warehouse even when it is selected;
* freeze S0 (build_shared_pools.stock_initial → MrpFreezeBaseline.stock_qty)
  follows the bin source under the flag and equals legacy S0 when seeded;
* the §2.5 pool projection (item_ledger_position) returns correct available /
  projected / uncovered on a small fixture (make contributes exactly 0);
* the reader exposure (material_availability_positions) is flag-gated + additive.
"""

import datetime

import pytest

pytestmark = pytest.mark.usefixtures("building_ledger_generation")

from app import models
from app.services.item_ledger import (
    LedgerKey,
    item_ledger_position,
    seed_from_balance,
    stock_source,
    use_bin_stock,
)
from app.services.item_ledger.config import STOCK_SOURCE_BIN, STOCK_SOURCE_LEGACY
from app.services.mrp_stock_helpers import effective_stock_by_item_all
from app.services.mrp_freeze import build_shared_pools
from app.services.period_plan_service import material_availability_positions

EPS = 1e-9


def _generation_id(db):
    return db.get(models.PlanningTruthState, 1).current_generation_id


# ---------------------------------------------------------------------------
# fixture helpers
# ---------------------------------------------------------------------------
def _item(db, code, stock_qty=0.0):
    it = models.Item(
        item_code=code, item_name=code, item_ref1c=f"ref-{code}", stock_qty=stock_qty
    )
    db.add(it)
    db.flush()
    return it


def _wh(db, ref, *, selected=True, ignored=False, finished_goods=False):
    db.add(
        models.StockWarehouse(
            warehouse_ref1c=ref,
            warehouse_name=ref,
            is_selected=selected,
            is_finished_goods=finished_goods,
        )
    )
    if ignored:
        db.add(models.IgnoredWarehouse(warehouse_ref1c=ref, warehouse_name=ref))
    db.flush()


def _iws(db, item_id, wh, qty):
    db.add(models.ItemWarehouseStock(item_id=item_id, warehouse_ref1c=wh, qty=qty))
    db.flush()


def _seed_bin(db, item_id, wh, qty, period=datetime.date(2026, 7, 1)):
    generation = db.get(models.PlanningTruthState, 1).current_generation
    seed_from_balance(
        db,
        {LedgerKey(item_id, "", "", wh): qty},
        anchor_period=period,
        import_batch=generation.physical_import_batch,
        ledger_generation_id=generation.id,
    )
    db.flush()


def _res(db, item_id, req_id, reserved, *, mode="consume", realized=0.0, status="active"):
    generation_id = db.get(models.PlanningTruthState, 1).current_generation_id
    db.add(
        models.ReservationEntry(
            ledger_generation_id=generation_id,
            item_id=item_id,
            requirement_id=req_id,
            run_id=None,
            freeze_version=0,
            priority_period_from=datetime.date(2026, 7, 1),
            priority_period_to=datetime.date(2026, 7, 15),
            realization_mode=mode,
            reserved_qty=reserved,
            realized_qty=realized,
            lifecycle_status=status,
        )
    )
    db.flush()


def _wip(db, item_id, remaining, finish=datetime.date(2026, 8, 1)):
    order = models.ProductionOrder(
        order_number=f"WIP-{item_id}-{remaining}",
        order_date=datetime.datetime(2026, 5, 20),
        is_posted=True,
        deletion_mark=False,
    )
    db.add(order)
    db.flush()
    product = models.ProductionProduct(
        order_id=order.order_id,
        item_id=item_id,
        line_number=1,
        quantity=remaining,
        produced_qty=0,
        remaining_qty=remaining,
    )
    db.add(product)
    db.flush()
    db.add(
        models.ProductionOrderLineState(
            product_id=product.product_id,
            status="ready",
            issue_status="not_requested",
            planned_finish_date=finish,
        )
    )
    db.flush()


# ---------------------------------------------------------------------------
# the feature flag
# ---------------------------------------------------------------------------
def test_flag_defaults_to_legacy(monkeypatch):
    monkeypatch.delenv("STOCK_SOURCE", raising=False)
    assert stock_source() == STOCK_SOURCE_LEGACY
    assert use_bin_stock() is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("bin", STOCK_SOURCE_BIN),
        ("BIN", STOCK_SOURCE_BIN),
        ("  Bin ", STOCK_SOURCE_BIN),
        ("legacy", STOCK_SOURCE_LEGACY),
        ("garbage", STOCK_SOURCE_LEGACY),
        ("", STOCK_SOURCE_LEGACY),
    ],
)
def test_flag_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("STOCK_SOURCE", raw)
    assert stock_source() == expected
    assert use_bin_stock() is (expected == STOCK_SOURCE_BIN)


# ---------------------------------------------------------------------------
# legacy source is used by default (explicit)
# ---------------------------------------------------------------------------
def test_default_flag_reads_legacy_source_not_bin(db_session, monkeypatch):
    """Default flag: effective_stock reads ItemWarehouseStock and IGNORES a
    divergent stock_bin — proof the legacy source is the one consulted."""
    monkeypatch.delenv("STOCK_SOURCE", raising=False)
    db = db_session
    _wh(db, "wh-sel", selected=True)
    it = _item(db, "P1")
    _iws(db, it.item_id, "wh-sel", 30.0)
    _seed_bin(db, it.item_id, "wh-sel", 999.0)  # divergent bin — must be ignored

    result = effective_stock_by_item_all(db)
    assert result[it.item_id] == 30.0  # legacy (iws), NOT the bin's 999


def test_bin_flag_reads_bin_source(db_session, monkeypatch):
    """flag=bin: the same fixture now resolves to the bin's value."""
    db = db_session
    _wh(db, "wh-sel", selected=True)
    it = _item(db, "P1")
    _iws(db, it.item_id, "wh-sel", 30.0)
    _seed_bin(db, it.item_id, "wh-sel", 999.0)

    monkeypatch.setenv("STOCK_SOURCE", "bin")
    result = effective_stock_by_item_all(db)
    assert result[it.item_id] == 999.0  # bin, NOT the legacy iws 30


# ---------------------------------------------------------------------------
# THE equivalence test (design §11 Инк5, point 5)
# ---------------------------------------------------------------------------
def test_bin_equals_legacy_when_seeded_from_same_snapshot(db_session, monkeypatch):
    """Seed stock_bin from the SAME snapshot that feeds ItemWarehouseStock, then
    assert effective_stock_by_item_all(bin) == effective_stock_by_item_all(legacy)
    item-for-item over the same contour — including that finished_goods / ignored
    / unselected warehouses are excluded identically.
    """
    db = db_session
    # contour: selected planning wh, a deselected wh, an ignored wh, and a
    # (deselected) finished-goods wh — the realistic setup where both worlds
    # exclude the same warehouses.
    _wh(db, "wh-sel", selected=True)
    _wh(db, "wh-unsel", selected=False)
    _wh(db, "wh-ign", selected=True, ignored=True)
    _wh(db, "wh-fg", selected=False, finished_goods=True)

    # item A — stock spread across all four warehouses (only wh-sel counts).
    a = _item(db, "A")
    for wh, qty in [("wh-sel", 30.0), ("wh-unsel", 100.0), ("wh-ign", 70.0), ("wh-fg", 40.0)]:
        _iws(db, a.item_id, wh, qty)
        _seed_bin(db, a.item_id, wh, qty)  # bin seeded from the SAME snapshot

    # item B — stock only in a deselected warehouse → authoritative 0 in both.
    b = _item(db, "B")
    _iws(db, b.item_id, "wh-unsel", 50.0)
    _seed_bin(db, b.item_id, "wh-unsel", 50.0)

    # item C — no breakdown at all → tier-3 fallback to stock_qty in both.
    c = _item(db, "C", stock_qty=42.0)

    monkeypatch.delenv("STOCK_SOURCE", raising=False)
    legacy = effective_stock_by_item_all(db)

    monkeypatch.setenv("STOCK_SOURCE", "bin")
    binned = effective_stock_by_item_all(db)

    assert set(legacy.keys()) == set(binned.keys())
    for iid in legacy:
        assert abs(legacy[iid] - binned[iid]) <= EPS, (
            f"item {iid}: legacy {legacy[iid]} != bin {binned[iid]}"
        )
    # spot-check the contour semantics held identically in both worlds:
    assert legacy[a.item_id] == 30.0 and binned[a.item_id] == 30.0  # fg/ign/unsel excluded
    assert legacy[b.item_id] == 0.0 and binned[b.item_id] == 0.0
    assert legacy[c.item_id] == 42.0 and binned[c.item_id] == 42.0


def test_bin_excludes_finished_goods_even_when_selected(db_session, monkeypatch):
    """A finished_goods warehouse that is ALSO selected+non-ignored is excluded
    by the bin path (design §2.5) — the intentional divergence from legacy that
    proves the fg filter is wired, not merely relying on deselection."""
    db = db_session
    _wh(db, "wh-fgsel", selected=True, finished_goods=True)
    d = _item(db, "D")
    _iws(db, d.item_id, "wh-fgsel", 25.0)
    _seed_bin(db, d.item_id, "wh-fgsel", 25.0)

    monkeypatch.delenv("STOCK_SOURCE", raising=False)
    legacy = effective_stock_by_item_all(db)
    assert legacy[d.item_id] == 25.0  # legacy knows nothing of finished_goods

    monkeypatch.setenv("STOCK_SOURCE", "bin")
    binned = effective_stock_by_item_all(db)
    assert binned[d.item_id] == 0.0  # ГП excluded from the pool


# ---------------------------------------------------------------------------
# freeze S0 from the bin (design §11 Инк5 point 3; stock-doc инк4)
# ---------------------------------------------------------------------------
def test_freeze_s0_follows_bin_and_equals_legacy_when_seeded(db_session, monkeypatch):
    """build_shared_pools.stock_initial is S0 (→ MrpFreezeBaseline.stock_qty).
    Under the flag it is sourced from the bin: equal to legacy S0 when seeded
    identically (item P), and following the bin when the bin diverges (item Q).
    """
    db = db_session
    _wh(db, "wh-sel", selected=True)
    p = _item(db, "P")
    _iws(db, p.item_id, "wh-sel", 30.0)
    _seed_bin(db, p.item_id, "wh-sel", 30.0)  # seeded equal
    q = _item(db, "Q")
    _iws(db, q.item_id, "wh-sel", 30.0)
    _seed_bin(db, q.item_id, "wh-sel", 55.0)  # bin diverges

    monkeypatch.delenv("STOCK_SOURCE", raising=False)
    legacy_s0 = dict(build_shared_pools(db, []).stock_initial)

    monkeypatch.setenv("STOCK_SOURCE", "bin")
    bin_s0 = dict(build_shared_pools(db, []).stock_initial)

    assert bin_s0[p.item_id] == legacy_s0[p.item_id] == 30.0  # equal when seeded
    assert legacy_s0[q.item_id] == 30.0  # legacy reads iws
    assert bin_s0[q.item_id] == 55.0     # S0 followed the bin under the flag


# ---------------------------------------------------------------------------
# §2.5 projection — item_ledger_position (design §3 formulas)
# ---------------------------------------------------------------------------
def test_item_ledger_position_projection(db_session):
    db = db_session
    _wh(db, "wh-sel", selected=True)
    x = _item(db, "X")
    _seed_bin(db, x.item_id, "wh-sel", 10.0)  # on_hand = 10
    # two active consume reserves (6 + 5 = reserved_soft 11) + a make reserve
    # (reserved 100) that must contribute EXACTLY 0 (§3.1, INV-RES-make-zero).
    _res(db, x.item_id, 9001, 6.0, mode="consume")
    _res(db, x.item_id, 9002, 5.0, mode="consume")
    _res(db, x.item_id, 9003, 100.0, mode="make")

    pos = item_ledger_position(
        db, ledger_generation_id=_generation_id(db),
    )[x.item_id]
    assert pos["on_hand"] == 10.0
    assert pos["reserved_soft"] == 11.0            # make excluded
    assert pos["incoming"] == 0.0
    assert pos["available"] == pytest.approx(-1.0)  # 10 − 11, surfaced (not clamped)
    assert pos["projected"] == pytest.approx(-1.0)  # 10 + 0 − 11
    assert pos["uncovered"] == pytest.approx(1.0)   # max(11 − 10 − 0, 0)

    # A live legacy WIP mirror is not Ledger truth and cannot change the
    # generation-bound position.
    _wip(db, x.item_id, 4.0)
    pos2 = item_ledger_position(
        db, ledger_generation_id=_generation_id(db),
    )[x.item_id]
    assert pos2["incoming_wip"] == 0.0
    assert pos2["projected"] == pytest.approx(-1.0)
    assert pos2["uncovered"] == pytest.approx(1.0)

    # The persisted coverage projection in this Ledger generation is the only
    # source that lifts projected supply.
    entry = db.query(models.ReservationEntry).filter(
        models.ReservationEntry.ledger_generation_id == _generation_id(db),
        models.ReservationEntry.requirement_id == 9002,
        models.ReservationEntry.realization_mode == "consume",
    ).one()
    entry.covered_incoming_wip_qty = 4
    db.flush()
    pos3 = item_ledger_position(
        db, ledger_generation_id=_generation_id(db),
    )[x.item_id]
    assert pos3["incoming_wip"] == 4.0
    assert pos3["incoming"] == 4.0
    assert pos3["available"] == pytest.approx(-1.0)  # available ignores incoming
    assert pos3["projected"] == pytest.approx(3.0)   # 10 + 4 − 11
    assert pos3["uncovered"] == pytest.approx(0.0)   # max(11 − 10 − 4, 0)


def test_projection_ignores_realized_and_inactive_consume(db_session):
    """outstanding = max(reserved − realized, 0); released/closed reserves and
    fully realized ones drop out of reserved_soft."""
    db = db_session
    _wh(db, "wh-sel", selected=True)
    y = _item(db, "Y")
    _seed_bin(db, y.item_id, "wh-sel", 8.0)
    _res(db, y.item_id, 8001, 5.0, mode="consume", realized=2.0)   # outstanding 3
    _res(db, y.item_id, 8002, 4.0, mode="consume", status="released")  # excluded
    _res(db, y.item_id, 8003, 9.0, mode="consume", realized=9.0)   # outstanding 0

    pos = item_ledger_position(
        db, ledger_generation_id=_generation_id(db),
    )[y.item_id]
    assert pos["reserved_soft"] == pytest.approx(3.0)
    assert pos["available"] == pytest.approx(5.0)  # 8 − 3


# ---------------------------------------------------------------------------
# reader exposure is flag-gated + additive (period_plan_service)
# ---------------------------------------------------------------------------
def test_material_availability_positions_is_flag_gated(db_session, monkeypatch):
    db = db_session
    _wh(db, "wh-sel", selected=True)
    z = _item(db, "Z")
    _seed_bin(db, z.item_id, "wh-sel", 7.0)
    _res(db, z.item_id, 7001, 3.0, mode="consume")

    monkeypatch.delenv("STOCK_SOURCE", raising=False)
    assert material_availability_positions(db) == {}  # inert under legacy

    monkeypatch.setenv("STOCK_SOURCE", "bin")
    positions = material_availability_positions(db)
    assert positions[z.item_id]["available"] == pytest.approx(4.0)  # 7 − 3
