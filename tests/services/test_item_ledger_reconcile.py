"""Ledger-1 Balance-reconcile (the shrunk drift) + shadow diagnostics — §3б.

Exercises the inc3 after-step against a mocked Balance snapshot vs bin state:

* match (|delta|≤EPS) → last_reconciled_at set, pending cleared, no SLE;
* first delta → reconcile_pending_qty stored, no SLE;
* second consecutive same delta, no in-flight pull → adjustment-SLE written and
  the bin folded to the balance;
* delta with an in-flight (pending) pull → held, no adjustment;
* an out-of-band write-off (bin 10, balance 7 twice) → −3 adjustment-SLE, bin→7;
* a sign-flip / zeroing resets the one-sweep debounce;
* build_balance_snapshot aligns converted rows on the full key (org included);
* ledger_on_hand_by_item honours the selected/ignored warehouse contour;
* the shadow diagnostic report shape.
"""

import datetime

from app import models
from app.services.item_ledger import (
    LedgerKey,
    build_balance_snapshot,
    ledger_on_hand_by_item,
    reconcile_balance_snapshot,
    seed_from_balance,
    stock_shadow_report,
)
from app.services.item_ledger.reconcile import RECONCILE_SOURCE


def _f(x):
    return float(x)


def _item(db, code, ref, name=None, stock_qty=0.0):
    it = models.Item(item_code=code, item_name=name or code, item_ref1c=ref, stock_qty=stock_qty)
    db.add(it)
    db.flush()
    return it


def _seed_bin(db, item_id, wh, qty, org="", period=datetime.date(2026, 7, 1)):
    """Create a bin with on_hand backed by a seed SLE (so a later reconcile
    adjustment folds correctly: rebuild sums Σ SLE)."""
    seed_from_balance(db, {LedgerKey(item_id, "", org, wh): qty}, anchor_period=period)
    db.flush()


def _adj_sles(db, item_id=None):
    q = db.query(models.StockLedgerEntry).filter_by(ingest_source=RECONCILE_SOURCE)
    if item_id is not None:
        q = q.filter_by(item_id=item_id)
    return q.all()


def _bin(db, item_id, wh, org=""):
    return (
        db.query(models.StockBin)
        .filter_by(item_id=item_id, warehouse_ref1c=wh, organization_ref=org)
        .one()
    )


# ---------------------------------------------------------------------------
# match / pending / apply (§3б steps 2–3)
# ---------------------------------------------------------------------------


def test_reconcile_match_sets_last_reconciled_no_sle(db_session):
    it = _item(db_session, "P1", "ref-1")
    _seed_bin(db_session, it.item_id, "wh-1", 10)
    key = LedgerKey(it.item_id, "", "", "wh-1")

    res = reconcile_balance_snapshot(db_session, {key: 10})
    db_session.commit()

    assert res.matched == 1 and res.pending == 0 and res.adjusted == 0
    b = _bin(db_session, it.item_id, "wh-1")
    assert b.last_reconciled_at is not None
    assert _f(b.reconcile_pending_qty) == 0
    assert _adj_sles(db_session) == []


def test_reconcile_first_delta_is_pending_no_sle(db_session):
    it = _item(db_session, "P1", "ref-1")
    _seed_bin(db_session, it.item_id, "wh-1", 10)
    key = LedgerKey(it.item_id, "", "", "wh-1")

    res = reconcile_balance_snapshot(db_session, {key: 7})
    db_session.commit()

    assert res.pending == 1 and res.adjusted == 0 and res.matched == 0
    b = _bin(db_session, it.item_id, "wh-1")
    assert _f(b.reconcile_pending_qty) == -3
    assert b.last_reconciled_at is None
    assert _adj_sles(db_session) == []


def test_reconcile_second_same_delta_applies_adjustment_and_folds_bin(db_session):
    it = _item(db_session, "P1", "ref-1")
    _seed_bin(db_session, it.item_id, "wh-1", 10)
    key = LedgerKey(it.item_id, "", "", "wh-1")

    # sweep 1 — store pending
    reconcile_balance_snapshot(db_session, {key: 7})
    db_session.commit()
    # sweep 2 — same delta, no in-flight pull → apply
    res = reconcile_balance_snapshot(db_session, {key: 7})
    db_session.commit()

    assert res.adjusted == 1 and res.pending == 0 and res.held == 0
    sles = _adj_sles(db_session)
    assert len(sles) == 1
    adj = sles[0]
    assert _f(adj.qty) == -3
    assert adj.movement_kind == "reconcile_adjustment"
    assert adj.record_type == "Expense"
    assert adj.recorder_type == "reconcile"
    b = _bin(db_session, it.item_id, "wh-1")
    assert _f(b.on_hand) == 7  # 10 seed + (-3) adjustment, folded
    assert _f(b.reconcile_pending_qty) == 0
    assert b.last_reconciled_at is not None

    # sweep 3 — now on_hand == balance → matched, no second adjustment
    res3 = reconcile_balance_snapshot(db_session, {key: 7})
    db_session.commit()
    assert res3.matched == 1 and res3.adjusted == 0
    assert len(_adj_sles(db_session)) == 1


def test_reconcile_out_of_band_writeoff_scenario(db_session):
    """Bin 10, balance 7 for two sweeps → −3 adjustment-SLE, bin → 7 (§3б)."""
    it = _item(db_session, "C1", "ref-c1")
    _seed_bin(db_session, it.item_id, "wh-2", 10)
    key = LedgerKey(it.item_id, "", "", "wh-2")

    r1 = reconcile_balance_snapshot(db_session, {key: 7})
    db_session.commit()
    r2 = reconcile_balance_snapshot(db_session, {key: 7})
    db_session.commit()

    assert r1.pending == 1 and r2.adjusted == 1
    assert _f(_adj_sles(db_session, it.item_id)[0].qty) == -3
    assert _f(_bin(db_session, it.item_id, "wh-2").on_hand) == 7


def test_reconcile_held_by_inflight_pull(db_session):
    it = _item(db_session, "P1", "ref-1")
    _seed_bin(db_session, it.item_id, "wh-1", 10)
    key = LedgerKey(it.item_id, "", "", "wh-1")

    reconcile_balance_snapshot(db_session, {key: 7})  # sweep 1 → pending
    db_session.commit()

    # a queued document (pull not drained) → the snapshot race the debounce guards
    db_session.add(models.StockRecorderPull(
        recorder_type="Document_СборкаЗапасов", recorder_ref="asm-x", status="pending",
    ))
    db_session.commit()

    res = reconcile_balance_snapshot(db_session, {key: 7})  # sweep 2 → held
    db_session.commit()

    assert res.held == 1 and res.adjusted == 0
    assert _adj_sles(db_session) == []
    b = _bin(db_session, it.item_id, "wh-1")
    assert _f(b.reconcile_pending_qty) == -3  # still pending, not applied

    # drain the pull → next sweep applies
    db_session.query(models.StockRecorderPull).update({"status": "done"})
    db_session.commit()
    res3 = reconcile_balance_snapshot(db_session, {key: 7})
    db_session.commit()
    assert res3.adjusted == 1
    assert _f(_bin(db_session, it.item_id, "wh-1").on_hand) == 7


def test_reconcile_sign_flip_resets_debounce(db_session):
    it = _item(db_session, "P1", "ref-1")
    _seed_bin(db_session, it.item_id, "wh-1", 10)
    key = LedgerKey(it.item_id, "", "", "wh-1")

    reconcile_balance_snapshot(db_session, {key: 7})   # pending -3
    db_session.commit()
    # different delta next sweep (+2) → treated as a fresh first sighting
    res = reconcile_balance_snapshot(db_session, {key: 12})
    db_session.commit()
    assert res.adjusted == 0 and res.pending == 1
    assert _f(_bin(db_session, it.item_id, "wh-1").reconcile_pending_qty) == 2

    # confirm the new delta on the following sweep → apply +2
    res2 = reconcile_balance_snapshot(db_session, {key: 12})
    db_session.commit()
    assert res2.adjusted == 1
    assert _f(_bin(db_session, it.item_id, "wh-1").on_hand) == 12


def test_reconcile_zeroing_delta_resets_and_clears_pending(db_session):
    it = _item(db_session, "P1", "ref-1")
    _seed_bin(db_session, it.item_id, "wh-1", 10)
    key = LedgerKey(it.item_id, "", "", "wh-1")

    reconcile_balance_snapshot(db_session, {key: 7})  # pending -3
    db_session.commit()
    # balance returns to 10 → delta 0 → matched, pending cleared, no SLE
    res = reconcile_balance_snapshot(db_session, {key: 10})
    db_session.commit()
    assert res.matched == 1 and res.adjusted == 0
    b = _bin(db_session, it.item_id, "wh-1")
    assert _f(b.reconcile_pending_qty) == 0 and b.last_reconciled_at is not None
    assert _adj_sles(db_session) == []


def test_reconcile_balance_only_key_creates_bin_then_adjusts(db_session):
    """1С has stock we never mirrored (no bin) → +qty adjustment after debounce."""
    it = _item(db_session, "N1", "ref-n1")
    key = LedgerKey(it.item_id, "", "", "wh-1")

    r1 = reconcile_balance_snapshot(db_session, {key: 5})
    db_session.commit()
    assert r1.pending == 1
    b = _bin(db_session, it.item_id, "wh-1")
    assert _f(b.on_hand) == 0 and _f(b.reconcile_pending_qty) == 5

    r2 = reconcile_balance_snapshot(db_session, {key: 5})
    db_session.commit()
    assert r2.adjusted == 1
    assert _f(_bin(db_session, it.item_id, "wh-1").on_hand) == 5
    assert _f(_adj_sles(db_session, it.item_id)[0].qty) == 5


def test_reconcile_ignores_double_zero_keys(db_session):
    it = _item(db_session, "P1", "ref-1")
    # no bin, balance 0 → nothing to persist
    res = reconcile_balance_snapshot(db_session, {LedgerKey(it.item_id, "", "", "wh-1"): 0})
    db_session.commit()
    assert res.compared == 0 and res.matched == 0 and res.pending == 0
    assert db_session.query(models.StockBin).count() == 0


# ---------------------------------------------------------------------------
# snapshot builder — org alignment (§2.1 / §3б key)
# ---------------------------------------------------------------------------


def test_build_balance_snapshot_aligns_on_full_key_with_org(db_session):
    it = _item(db_session, "P1", "ref-1")
    db_session.add(models.StockWarehouse(warehouse_ref1c="wh-1", warehouse_name="WH1"))
    db_session.flush()

    rows = [
        {"code": "P1", "ref": "ref-1", "organization_ref": "ORG1", "warehouse_ref": "wh-1", "qty": 4.0},
        {"code": "P1", "ref": "ref-1", "organization_ref": "ORG1", "warehouse_ref": "wh-1", "qty": 1.5},
        {"code": "??", "ref": "unknown-ref", "organization_ref": "ORG1", "warehouse_ref": "wh-1", "qty": 9.0},
    ]
    snap = build_balance_snapshot(db_session, rows)

    assert snap == {LedgerKey(it.item_id, "", "ORG1", "wh-1"): 5.5}  # summed; unknown dropped


# ---------------------------------------------------------------------------
# ledger_on_hand_by_item — selected/ignored contour (§2.5)
# ---------------------------------------------------------------------------


def test_ledger_on_hand_by_item_respects_contour(db_session):
    it = _item(db_session, "P1", "ref-1")
    db_session.add_all([
        models.StockWarehouse(warehouse_ref1c="wh-sel", warehouse_name="Sel", is_selected=True),
        models.StockWarehouse(warehouse_ref1c="wh-unsel", warehouse_name="Unsel", is_selected=False),
        models.StockWarehouse(warehouse_ref1c="wh-ign", warehouse_name="Ign", is_selected=True),
    ])
    db_session.add(models.IgnoredWarehouse(warehouse_ref1c="wh-ign"))
    db_session.flush()
    _seed_bin(db_session, it.item_id, "wh-sel", 6)
    _seed_bin(db_session, it.item_id, "wh-unsel", 100)
    _seed_bin(db_session, it.item_id, "wh-ign", 50)

    by_item = ledger_on_hand_by_item(db_session)
    assert by_item.get(it.item_id) == 6  # only the selected, non-ignored warehouse


# ---------------------------------------------------------------------------
# shadow report shape (point 4)
# ---------------------------------------------------------------------------


def test_stock_shadow_report_shape(db_session):
    it = _item(db_session, "P1", "ref-1", stock_qty=10.0)
    db_session.add(models.StockWarehouse(warehouse_ref1c="wh-1", warehouse_name="WH1", is_selected=True))
    db_session.flush()
    _seed_bin(db_session, it.item_id, "wh-1", 7)  # ledger 7 vs legacy stock_qty 10

    rep = stock_shadow_report(db_session, include_all=True)

    assert set(rep.keys()) == {"generated_at", "counts", "totals", "items"}
    assert set(rep["counts"].keys()) == {
        "bins", "matched", "pending", "adjusted_keys", "adjustment_sles", "divergent_items",
    }
    assert set(rep["totals"].keys()) == {"ledger_on_hand", "legacy_stock", "divergence"}
    row = next(r for r in rep["items"] if r["item_id"] == it.item_id)
    assert row["ledger_on_hand"] == 7.0
    assert row["legacy_stock"] == 10.0
    assert row["divergence"] == -3.0
    assert rep["counts"]["divergent_items"] == 1
    assert rep["counts"]["bins"] == 1
