"""Ledger-1 Balance-reconcile (the shrunk drift) + shadow diagnostics — .

Exercises the  after-step against a mocked Balance snapshot vs bin state:

* match (|delta|≤EPS) → last_reconciled_at set, pending cleared, no SLE;
* first delta → reconcile_pending_qty stored, no SLE;
* second consecutive same delta, no in-flight pull → adjustment-SLE written and
  the bin folded to the balance;
* delta with an in-flight pull (pending OR retriable error) → held, no
  adjustment; an exhausted error pull does NOT hold (Д8);
* an out-of-band write-off (bin 10, balance 7 twice) → −3 adjustment-SLE, bin→7;
* a sign-flip / zeroing resets the one-sweep debounce;
* build_balance_snapshot aligns converted rows on the full key (org included);
* ledger_on_hand_by_item honours the selected/ignored warehouse contour;
* the shadow diagnostic report shape.
"""

import datetime

import pytest

from app import models
from app.services.item_ledger import (
    LedgerKey,
    build_balance_snapshot,
    ledger_on_hand_by_item,
    process_pending_pulls as _process_pending_pulls,
    reconcile_balance_snapshot as _reconcile_balance_snapshot,
    seed_from_balance as _seed_from_balance,
)
from app.services.item_ledger.reconcile import (
    RECONCILE_DISCOVERY_SOURCE,
    RECONCILE_SOURCE,
)


def _f(x):
    return float(x)


@pytest.fixture(autouse=True)
def _explicit_build_context(db_session, building_ledger_generation):
    db_session.info["ledger_generation_id"] = building_ledger_generation.id
    return building_ledger_generation


def reconcile_balance_snapshot(db, *args, **kwargs):
    kwargs.setdefault("ledger_generation_id", db.info["ledger_generation_id"])
    return _reconcile_balance_snapshot(db, *args, **kwargs)


def seed_from_balance(db, *args, **kwargs):
    generation = db.get(
        models.LedgerGeneration, db.info["ledger_generation_id"]
    )
    kwargs.setdefault("ledger_generation_id", generation.id)
    kwargs.setdefault("import_batch", generation.physical_import_batch)
    return _seed_from_balance(db, *args, **kwargs)


def process_pending_pulls(db, *args, **kwargs):
    kwargs.setdefault("ledger_generation_id", db.info["ledger_generation_id"])
    return _process_pending_pulls(db, *args, **kwargs)


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
        .filter_by(
            ledger_generation_id=db.info["ledger_generation_id"],
            item_id=item_id,
            warehouse_ref1c=wh,
            organization_ref=org,
        )
        .one()
    )


# ---------------------------------------------------------------------------
# match / pending / apply ( steps 2–3)
# ---------------------------------------------------------------------------


def test_reconcile_fails_closed_without_generation(db_session):
    with pytest.raises(ValueError, match="explicit ledger_generation_id"):
        _reconcile_balance_snapshot(db_session, {})

    assert db_session.query(models.StockBin).count() == 0
    assert _adj_sles(db_session) == []


def test_reconcile_rejects_accepted_generation_without_mutation(db_session):
    generation = db_session.get(
        models.LedgerGeneration, db_session.info["ledger_generation_id"]
    )
    generation.status = "accepted"
    generation.accepted_at = datetime.datetime(2026, 7, 23)
    db_session.flush()
    it = _item(db_session, "IMMUTABLE", "immutable-ref")
    key = LedgerKey(it.item_id, "", "", "wh-1")
    batch_count = db_session.query(models.PhysicalImportBatch).count()

    with pytest.raises(ValueError, match="only an explicit building"):
        _reconcile_balance_snapshot(
            db_session,
            {key: 5},
            ledger_generation_id=generation.id,
        )

    assert db_session.query(models.StockBin).count() == 0
    assert _adj_sles(db_session) == []
    assert db_session.query(models.PhysicalImportBatch).count() == batch_count


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
    generation = db_session.get(
        models.LedgerGeneration, db_session.info["ledger_generation_id"]
    )
    initial_boundary = generation.physical_import_batch_id

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
    assert adj.ingest_batch_id != initial_boundary
    assert generation.physical_import_batch_id == adj.ingest_batch_id
    assert len(adj.source_content_hash) == 64
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
    """Bin 10, balance 7 for two sweeps → −3 adjustment-SLE, bin → 7 ()."""
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


def test_reconcile_held_by_retryable_error_pull(db_session):
    """Д8: a failed-but-retriable pull is in-flight — its retry may still insert
    the very movements the delta reflects, so applying an adjustment now would
    double-count them transiently. Must hold, same as pending."""
    it = _item(db_session, "P1", "ref-1")
    _seed_bin(db_session, it.item_id, "wh-1", 10)
    key = LedgerKey(it.item_id, "", "", "wh-1")

    reconcile_balance_snapshot(db_session, {key: 7})  # sweep 1 → pending
    db_session.commit()

    db_session.add(models.StockRecorderPull(
        recorder_type="Document_СборкаЗапасов", recorder_ref="asm-err",
        status="error", attempts=2, last_error="1C timeout",
    ))
    db_session.commit()

    res = reconcile_balance_snapshot(db_session, {key: 7})  # sweep 2 → held
    db_session.commit()

    assert res.held == 1 and res.adjusted == 0
    assert _adj_sles(db_session) == []
    assert _f(_bin(db_session, it.item_id, "wh-1").reconcile_pending_qty) == -3


def test_reconcile_not_held_by_exhausted_error_pull(db_session):
    """Д8: an error pull past the attempt cap will never retry on its own —
    it is terminal, and the reconcile adjustment IS the recovery path for its
    movements. Must NOT hold the sweep."""
    it = _item(db_session, "P1", "ref-1")
    _seed_bin(db_session, it.item_id, "wh-1", 10)
    key = LedgerKey(it.item_id, "", "", "wh-1")

    reconcile_balance_snapshot(db_session, {key: 7})  # sweep 1 → pending
    db_session.commit()

    db_session.add(models.StockRecorderPull(
        recorder_type="Document_СборкаЗапасов", recorder_ref="asm-dead",
        status="error", attempts=5, last_error="1C down",  # == cap → exhausted
    ))
    db_session.commit()

    res = reconcile_balance_snapshot(db_session, {key: 7})  # sweep 2 → apply
    db_session.commit()

    assert res.adjusted == 1 and res.held == 0
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
# Д7 — characteristics: the aggregate comparison axis (variant «б»)
# ---------------------------------------------------------------------------


def test_reconcile_char_bin_matches_aggregate_balance_no_false_drift(db_session):
    """Regression Д7: a bin keyed by a REAL characteristic must reconcile against
    the char-less Balance row as one aggregate — previously the sweep saw two
    mismatched keys (char-bin −10, ''-bin +10) and after the debounce «перелила»
    the stock into the ''-bin with an adjustment pair."""
    it = _item(db_session, "X1", "ref-x1")
    seed_from_balance(
        db_session,
        {LedgerKey(it.item_id, "char-1", "", "wh-1"): 10},
        anchor_period=datetime.date(2026, 7, 1),
    )
    agg = LedgerKey(it.item_id, "", "", "wh-1")

    r1 = reconcile_balance_snapshot(db_session, {agg: 10})
    db_session.commit()
    r2 = reconcile_balance_snapshot(db_session, {agg: 10})
    db_session.commit()

    assert (r1.matched, r1.pending, r1.adjusted) == (1, 0, 0)
    assert (r2.matched, r2.pending, r2.adjusted) == (1, 0, 0)
    assert _adj_sles(db_session) == []
    # the char-bin is untouched and stamped reconciled; no ''-bin was created
    bins = db_session.query(models.StockBin).all()
    assert len(bins) == 1
    assert bins[0].characteristic_ref == "char-1"
    assert _f(bins[0].on_hand) == 10
    assert bins[0].last_reconciled_at is not None


def test_reconcile_aggregate_discrepancy_adjusts_only_empty_char_bin(db_session):
    """Aggregate (char-bin 10 + ''-bin 2 = 12) vs balance 9 → one −3 adjustment
    into the ''-bin after the debounce; the char-bin is never touched."""
    it = _item(db_session, "X2", "ref-x2")
    seed_from_balance(
        db_session,
        {
            LedgerKey(it.item_id, "char-1", "", "wh-1"): 10,
            LedgerKey(it.item_id, "", "", "wh-1"): 2,
        },
        anchor_period=datetime.date(2026, 7, 1),
    )
    agg = LedgerKey(it.item_id, "", "", "wh-1")

    r1 = reconcile_balance_snapshot(db_session, {agg: 9})
    db_session.commit()
    assert r1.pending == 1 and r1.adjusted == 0
    # pending is stored on the ''-bin, char-bin stays clean
    char_bin = (
        db_session.query(models.StockBin)
        .filter_by(item_id=it.item_id, characteristic_ref="char-1")
        .one()
    )
    empty_bin = (
        db_session.query(models.StockBin)
        .filter_by(item_id=it.item_id, characteristic_ref="")
        .one()
    )
    assert _f(empty_bin.reconcile_pending_qty) == -3
    assert _f(char_bin.reconcile_pending_qty) == 0

    r2 = reconcile_balance_snapshot(db_session, {agg: 9})
    db_session.commit()
    assert r2.adjusted == 1
    sles = _adj_sles(db_session, it.item_id)
    assert len(sles) == 1
    assert _f(sles[0].qty) == -3 and sles[0].characteristic_ref == ""
    assert _f(char_bin.on_hand) == 10  # untouched
    assert _f(empty_bin.on_hand) == -1  # 2 + (−3); aggregate = 9 = balance

    r3 = reconcile_balance_snapshot(db_session, {agg: 9})
    db_session.commit()
    assert r3.matched == 1 and r3.adjusted == 0
    assert len(_adj_sles(db_session, it.item_id)) == 1  # no oscillation


# ---------------------------------------------------------------------------
#  — сверка v2: discovery of the missed source document
# ---------------------------------------------------------------------------

ASSEMBLY = "Document_СборкаЗапасов"


class FakeRegisterClient:
    """Discovery-side fake: returns recorder rows for the dimension-filtered
    register query (Инк0 shape)."""

    def __init__(self, recorder_rows):
        self.recorder_rows = recorder_rows
        self.calls = []

    def get_all(self, entity_name, filter_query=None, order_by=None, **kwargs):
        self.calls.append((entity_name, filter_query))
        return list(self.recorder_rows)


class BoomRegisterClient:
    def get_all(self, *args, **kwargs):
        raise RuntimeError("1C 504 gateway timeout")


class FakePullClient:
    """Ingest-side fake for emulating the queued pull draining: answers the
    cast-Recorder filter with one recorder row of movement lines."""

    def __init__(self, recorder_ref, lines):
        self.recorder_ref = recorder_ref
        self.lines = lines

    def get_all(self, entity_name, filter_query=None, order_by=None, **kwargs):
        if "Recorder eq cast" in (filter_query or ""):
            return [{
                "Recorder": self.recorder_ref,
                "Recorder_Type": "AccumulationRecordType",
                "RecordSet": list(self.lines),
            }]
        return []  # document-header fetch etc. — best-effort, empty is fine


def _recorder_row(ref, rtype=f"StandardODATA.{ASSEMBLY}"):
    return {"Recorder": ref, "Recorder_Type": rtype, "RecordSet": []}


def _matured_delta(db, code, ref, wh="wh-1", on_hand=10, balance=7):
    """Seed a bin and mature a (balance − on_hand) delta through sweep 1."""
    it = _item(db, code, ref)
    _seed_bin(db, it.item_id, wh, on_hand)
    key = LedgerKey(it.item_id, "", "", wh)
    reconcile_balance_snapshot(db, {key: balance})
    db.commit()
    return it, key


def test_discovery_not_queried_for_first_sighting(db_session):
    it = _item(db_session, "D0", "ref-d0")
    _seed_bin(db_session, it.item_id, "wh-1", 10)
    client = FakeRegisterClient([])

    res = reconcile_balance_snapshot(
        db_session, {LedgerKey(it.item_id, "", "", "wh-1"): 7},
        discovery_client=client,
    )
    db_session.commit()

    assert res.pending == 1 and res.adjusted == 0
    assert client.calls == []  # point query ONLY for matured discrepancies


def test_discovery_unknown_recorder_enqueued_and_held(db_session):
    it, key = _matured_delta(db_session, "D1", "ref-d1")
    client = FakeRegisterClient([_recorder_row("doc-miss-1")])

    res = reconcile_balance_snapshot(db_session, {key: 7}, discovery_client=client)
    db_session.commit()

    assert res.held == 1 and res.adjusted == 0 and res.anomalies == 0
    assert res.discovered_recorders == 1
    assert _adj_sles(db_session) == []
    pull = (
        db_session.query(models.StockRecorderPull)
        .filter_by(recorder_ref="doc-miss-1")
        .one()
    )
    assert pull.status == "pending"
    assert pull.recorder_type == ASSEMBLY  # StandardODATA. prefix stripped
    assert pull.source == RECONCILE_DISCOVERY_SOURCE
    # delta still debounced, awaiting the drain
    assert _f(_bin(db_session, it.item_id, "wh-1").reconcile_pending_qty) == -3
    # the point query is dimension-filtered with a Period lower bound
    entity, flt = client.calls[0]
    assert entity == "AccumulationRegister_ЗапасыНаСкладах"
    assert "Номенклатура_Key eq guid'ref-d1'" in flt
    assert "СтруктурнаяЕдиница_Key eq guid'wh-1'" in flt
    assert "Period gt datetime'" in flt  # seed anchor provides the bound


def test_discovery_then_drained_pull_converges_without_adjustment(db_session):
    """The  happy path: discrepancy → recorder discovered + enqueued →
    orchestrator drains the pull (emulated) → the ledger replays the movement
    with a real Recorder and the next sweep matches. No anonymous SLE at all."""
    db_session.add(models.StockWarehouse(warehouse_ref1c="wh-1", warehouse_name="WH1"))
    db_session.flush()
    it, key = _matured_delta(db_session, "D2", "ref-d2")
    client = FakeRegisterClient([_recorder_row("doc-miss-2")])

    res = reconcile_balance_snapshot(db_session, {key: 7}, discovery_client=client)
    db_session.commit()
    assert res.discovered_recorders == 1 and res.adjusted == 0

    # emulate the orchestrator drain: the missed Списание −3 arrives by-document
    pull_client = FakePullClient("doc-miss-2", [{
        "Period": "2026-07-15T12:00:00",
        "LineNumber": "1",
        "Active": True,
        "RecordType": "Expense",
        "Организация_Key": "00000000-0000-0000-0000-000000000000",
        "Номенклатура_Key": "ref-d2",
        "Характеристика_Key": "00000000-0000-0000-0000-000000000000",
        "СтруктурнаяЕдиница_Key": "wh-1",
        "Количество": 3,
    }])
    results = process_pending_pulls(db_session, client=pull_client)
    assert [r.status for r in results] == ["done"]
    assert _f(_bin(db_session, it.item_id, "wh-1").on_hand) == 7

    # next sweep: recorder is known, delta is gone → matched, still no adjustment
    res2 = reconcile_balance_snapshot(db_session, {key: 7}, discovery_client=client)
    db_session.commit()
    assert res2.matched == 1 and res2.adjusted == 0 and res2.held == 0
    assert _adj_sles(db_session) == []


def test_discovery_clean_register_writes_anonymous_adjustment(db_session):
    """Register returns no documents at all → the delta is a true anomaly and
    the honest anonymous adjustment happens exactly as before v2."""
    it, key = _matured_delta(db_session, "D3", "ref-d3")
    client = FakeRegisterClient([])

    res = reconcile_balance_snapshot(db_session, {key: 7}, discovery_client=client)
    db_session.commit()

    assert res.adjusted == 1 and res.anomalies == 1
    assert res.held == 0 and res.discovered_recorders == 0
    assert _f(_adj_sles(db_session, it.item_id)[0].qty) == -3
    assert _f(_bin(db_session, it.item_id, "wh-1").on_hand) == 7


def test_discovery_known_recorder_not_reenqueued_treated_as_anomaly(db_session):
    """A recorder already pulled (done) is not the missed document — discovery
    must not re-enqueue it in a loop; with nothing new the adjustment applies."""
    it, key = _matured_delta(db_session, "D4", "ref-d4")
    db_session.add(models.StockRecorderPull(
        recorder_type=ASSEMBLY, recorder_ref="doc-known", status="done",
    ))
    db_session.commit()
    client = FakeRegisterClient([_recorder_row("doc-known")])

    res = reconcile_balance_snapshot(db_session, {key: 7}, discovery_client=client)
    db_session.commit()

    assert res.adjusted == 1 and res.anomalies == 1
    assert res.discovered_recorders == 0
    pull = (
        db_session.query(models.StockRecorderPull)
        .filter_by(recorder_ref="doc-known")
        .one()
    )
    assert pull.status == "done"  # NOT reset to pending


def test_discovery_limit_caps_point_queries_per_sweep(db_session):
    it1 = _item(db_session, "D5", "ref-d5")
    it2 = _item(db_session, "D6", "ref-d6")
    _seed_bin(db_session, it1.item_id, "wh-1", 10)
    _seed_bin(db_session, it2.item_id, "wh-2", 10)
    key1 = LedgerKey(it1.item_id, "", "", "wh-1")
    key2 = LedgerKey(it2.item_id, "", "", "wh-2")
    client = FakeRegisterClient([])

    reconcile_balance_snapshot(db_session, {key1: 7, key2: 7})  # sweep 1: pending
    db_session.commit()
    res = reconcile_balance_snapshot(
        db_session, {key1: 7, key2: 7},
        discovery_client=client, discovery_limit=1,
    )
    db_session.commit()

    assert len(client.calls) == 1  # exactly one point query this sweep
    assert res.adjusted == 1 and res.anomalies == 1  # the budgeted key resolved
    assert res.discovery_skipped == 1 and res.held == 1  # the other held
    assert len(_adj_sles(db_session)) == 1
    # the held key keeps its debounce and resolves on a later sweep
    res2 = reconcile_balance_snapshot(
        db_session, {key1: 7, key2: 7},
        discovery_client=client, discovery_limit=1,
    )
    db_session.commit()
    assert res2.adjusted == 1 and res2.matched == 1


def test_discovery_odata_error_holds_key_without_crash(db_session):
    it, key = _matured_delta(db_session, "D7", "ref-d7")

    res = reconcile_balance_snapshot(
        db_session, {key: 7}, discovery_client=BoomRegisterClient(),
    )
    db_session.commit()

    assert res.held == 1 and res.adjusted == 0
    assert res.discovery_skipped == 1
    assert _adj_sles(db_session) == []
    assert _f(_bin(db_session, it.item_id, "wh-1").reconcile_pending_qty) == -3


# ---------------------------------------------------------------------------
# snapshot builder — org alignment ( /  key)
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
# ledger_on_hand_by_item — selected/ignored contour ()
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
