"""Ledger-1 physical ingest — pull-by-document (design §2.1, §2.3, §3а, §6).

Exercises pull_recorder_movements against a mocked OData client returning the
Inc0-confirmed recorder-row shape ({Recorder, Recorder_Type, RecordSet}):

* signed SLE rows (Receipt → +, Expense → −), across warehouses, with
  qty_after / stock_bin folded (INV-fold);
* the §6 dirt filter with counters (non-warehouse СтруктурнаяЕдиница, qty == 0,
  unknown item) and a diagnostic instead of a crash;
* replace-by-recorder idempotency — a re-pull yields an identical row set
  (INV-idem) and a changed re-pull replaces in place;
* the anchor guard (skip lines at/under the active anchor T0);
* pull-status transitions (done / empty / error) with attempts;
* the queue: enqueue_recorder_pull + process_pending_pulls (happy, error,
  attempt cap);
* the export hook enqueues on success and never raises when the enqueue errors.
"""

import datetime
import re

import pytest

from app import models
from app.services.item_ledger import (
    LedgerKey,
    enqueue_recorder_pull,
    process_pending_pulls,
    pull_recorder_movements as _pull_recorder_movements,
    seed_from_balance,
)
from app.services.item_ledger.ingest import EMPTY_GUID

ASSEMBLY = "Document_СборкаЗапасов"
TRANSFER = "Document_ПеремещениеЗапасов"


def _f(x):
    return float(x)


# ---------------------------------------------------------------------------
# fakes / builders
# ---------------------------------------------------------------------------


class FakeODataClient:
    """Returns the Inc0-confirmed recorder-row shape for a cast-Recorder filter."""

    def __init__(self, records_by_recorder):
        self.records_by_recorder = records_by_recorder
        self.calls = []

    def get_all(self, entity_name, filter_query=None, order_by=None, **kwargs):
        self.calls.append((entity_name, filter_query, order_by))
        m = re.search(r"guid'([^']+)'", filter_query or "")
        ref = m.group(1) if m else None
        lines = self.records_by_recorder.get(ref, [])
        if not lines:
            return []
        return [{"Recorder": ref, "Recorder_Type": "AccumulationRecordType", "RecordSet": list(lines)}]


class BoomClient:
    def get_all(self, *args, **kwargs):
        raise RuntimeError("1C 500 boom")


def _line(
    line_no,
    record_type,
    item_ref,
    wh_ref,
    qty,
    *,
    char="",
    org="ORG1",
    period="2026-07-10T10:00:00",
    active=True,
):
    return {
        "Period": period,
        "LineNumber": line_no,
        "Active": active,
        "RecordType": record_type,
        "Организация_Key": org or EMPTY_GUID,
        "Номенклатура_Key": item_ref,
        "Характеристика_Key": char or EMPTY_GUID,
        "Партия_Key": EMPTY_GUID,
        "СтруктурнаяЕдиница_Key": wh_ref,
        "Ячейка_Key": EMPTY_GUID,
        "Количество": qty,
        "КоличествоИнт": 0,
        "ХозяйственнаяОперация_Key": EMPTY_GUID,
    }


def _setup(db):
    item1 = models.Item(item_code="P1", item_name="Product 1", item_ref1c="ref-item-1")
    item2 = models.Item(item_code="C1", item_name="Component 1", item_ref1c="ref-item-2")
    db.add_all([item1, item2])
    db.add_all([
        models.StockWarehouse(warehouse_ref1c="wh-1", warehouse_name="WH1"),
        models.StockWarehouse(warehouse_ref1c="wh-2", warehouse_name="WH2"),
    ])
    db.flush()
    batch = models.PhysicalImportBatch(
        batch_key=f"ingest-test-{item1.item_id}",
        status="completed",
        cutoff=datetime.datetime(2026, 7, 31),
        source_watermarks={},
        completed_at=datetime.datetime(2026, 7, 31),
    )
    generation = models.LedgerGeneration(
        generation_key=f"ingest-test-{item1.item_id}",
        status="building",
        cutoff=datetime.datetime(2026, 7, 31),
        source_watermarks={},
        capabilities={},
        physical_import_batch=batch,
        algorithm_version="test/ingest",
    )
    db.add(generation)
    db.flush()
    db.info["item_ledger_generation_id"] = generation.id
    return item1, item2


def pull_recorder_movements(db, *args, **kwargs):
    kwargs.setdefault("ledger_generation_id", db.info.get("item_ledger_generation_id"))
    return _pull_recorder_movements(db, *args, **kwargs)


def _snapshot(db, recorder_ref):
    rows = (
        db.query(models.StockLedgerEntry)
        .filter_by(recorder_ref=recorder_ref)
        .all()
    )
    return sorted(
        (r.item_id, r.warehouse_ref1c, r.line_no, _f(r.qty), _f(r.qty_after))
        for r in rows
    )


# ---------------------------------------------------------------------------
# pull — signed rows + bin fold (§2.1, §3а)
# ---------------------------------------------------------------------------


def test_pull_assembly_signed_rows_and_bin(db_session):
    item1, item2 = _setup(db_session)
    client = FakeODataClient({
        "asm-1": [
            _line("1", "Receipt", "ref-item-1", "wh-1", 5),   # product +5
            _line("2", "Expense", "ref-item-2", "wh-2", 3),   # component -3
        ]
    })
    res = pull_recorder_movements(db_session, ASSEMBLY, "asm-1", client=client)
    db_session.commit()

    assert res.status == "done" and res.inserted == 2
    # cast filter was used with order_by disabled.
    assert client.calls[0][1] == "Recorder eq cast(guid'asm-1', 'Document_СборкаЗапасов')"
    assert client.calls[0][2] is None

    rows = {r.item_id: r for r in db_session.query(models.StockLedgerEntry).all()}
    assert _f(rows[item1.item_id].qty) == 5
    assert _f(rows[item2.item_id].qty) == -3
    assert rows[item1.item_id].movement_kind == "assembly_in"
    assert rows[item2.item_id].movement_kind == "assembly_out"
    assert rows[item1.item_id].ingest_source == "document_pull"

    bin1 = db_session.query(models.StockBin).filter_by(item_id=item1.item_id, warehouse_ref1c="wh-1").one()
    bin2 = db_session.query(models.StockBin).filter_by(item_id=item2.item_id, warehouse_ref1c="wh-2").one()
    assert _f(bin1.on_hand) == 5 and _f(bin2.on_hand) == -3  # INV-fold

    pull = db_session.query(models.StockRecorderPull).filter_by(recorder_ref="asm-1").one()
    # attempts counts FAILED attempts only — a successful pull does not bump it.
    assert pull.status == "done" and pull.line_count == 2 and pull.attempts == 0


def test_pull_transfer_expense_receipt(db_session):
    _setup(db_session)
    client = FakeODataClient({
        "trn-1": [
            _line("1", "Expense", "ref-item-1", "wh-1", 4),
            _line("2", "Receipt", "ref-item-1", "wh-2", 4),
        ]
    })
    res = pull_recorder_movements(db_session, TRANSFER, "trn-1", client=client)
    db_session.commit()

    assert res.inserted == 2
    out = db_session.query(models.StockLedgerEntry).filter_by(warehouse_ref1c="wh-1").one()
    inn = db_session.query(models.StockLedgerEntry).filter_by(warehouse_ref1c="wh-2").one()
    assert _f(out.qty) == -4 and out.movement_kind == "transfer_out"
    assert _f(inn.qty) == 4 and inn.movement_kind == "transfer_in"
    assert _f(db_session.query(models.StockBin).filter_by(warehouse_ref1c="wh-1").one().on_hand) == -4
    assert _f(db_session.query(models.StockBin).filter_by(warehouse_ref1c="wh-2").one().on_hand) == 4


# ---------------------------------------------------------------------------
# dirt filter (§6)
# ---------------------------------------------------------------------------


def test_pull_dirt_filtered_with_counters(db_session):
    _setup(db_session)
    client = FakeODataClient({
        "dirty-1": [
            _line("1", "Receipt", "ref-item-1", "wh-1", 7),               # good
            _line("2", "Receipt", "ref-item-1", "counterparty-guid", 2),  # non-warehouse
            _line("3", "Receipt", "ref-item-1", "wh-1", 0),               # qty 0
            _line("4", "Expense", "ref-unknown", "wh-2", 5),              # unknown item
        ]
    })
    res = pull_recorder_movements(db_session, ASSEMBLY, "dirty-1", client=client)
    db_session.commit()

    assert res.inserted == 1
    assert res.skipped_non_warehouse == 1
    assert res.skipped_zero_qty == 1
    assert res.skipped_unknown_item == 1
    assert res.diagnostics  # unknown item surfaced, not crashed
    assert db_session.query(models.StockLedgerEntry).count() == 1


def test_pull_skips_inactive_lines(db_session):
    _setup(db_session)
    client = FakeODataClient({
        "asm-x": [
            _line("1", "Receipt", "ref-item-1", "wh-1", 5),
            _line("2", "Receipt", "ref-item-1", "wh-1", 9, active=False),
        ]
    })
    res = pull_recorder_movements(db_session, ASSEMBLY, "asm-x", client=client)
    db_session.commit()
    assert res.inserted == 1 and res.skipped_inactive == 1


# ---------------------------------------------------------------------------
# replace-by-recorder (§3а step 4) — idempotency
# ---------------------------------------------------------------------------


def test_pull_replace_by_recorder_idempotent(db_session):
    _setup(db_session)
    lines = [
        _line("1", "Receipt", "ref-item-1", "wh-1", 5),
        _line("2", "Expense", "ref-item-2", "wh-2", 3),
    ]
    client = FakeODataClient({"asm-1": lines})

    pull_recorder_movements(db_session, ASSEMBLY, "asm-1", client=client)
    db_session.commit()
    first = _snapshot(db_session, "asm-1")

    res2 = pull_recorder_movements(db_session, ASSEMBLY, "asm-1", client=client)
    db_session.commit()
    second = _snapshot(db_session, "asm-1")

    assert first == second  # INV-idem — identical row set on re-pull
    assert res2.deleted == 0 and res2.inserted == 0
    assert db_session.query(models.StockLedgerEntry).count() == 2


def test_pull_replace_by_recorder_updates_qty(db_session):
    item1, _ = _setup(db_session)
    c1 = FakeODataClient({"asm-1": [_line("1", "Receipt", "ref-item-1", "wh-1", 5)]})
    pull_recorder_movements(db_session, ASSEMBLY, "asm-1", client=c1)
    db_session.commit()

    c2 = FakeODataClient({"asm-1": [_line("1", "Receipt", "ref-item-1", "wh-1", 8)]})
    res = pull_recorder_movements(db_session, ASSEMBLY, "asm-1", client=c2)
    db_session.commit()

    assert res.deleted == 0
    assert db_session.query(models.StockLedgerEntry).filter_by(recorder_ref="asm-1").count() == 2
    assert db_session.query(models.StockLedgerEntry).filter_by(
        recorder_ref="asm-1", active=True
    ).count() == 1
    assert _f(db_session.query(models.StockBin).filter_by(item_id=item1.item_id).one().on_hand) == 8


# ---------------------------------------------------------------------------
# anchor guard (§3а step 4)
# ---------------------------------------------------------------------------


def test_pull_anchor_guard_skips_pre_t0(db_session):
    item1, _ = _setup(db_session)
    key = LedgerKey(item1.item_id, "", "ORG1", "wh-1")
    seed_from_balance(db_session, {key: 10}, anchor_period=datetime.date(2026, 7, 5))
    db_session.commit()

    client = FakeODataClient({
        "asm-1": [
            _line("1", "Receipt", "ref-item-1", "wh-1", 5, period="2026-07-04T00:00:00"),  # <= T0
            _line("2", "Receipt", "ref-item-1", "wh-1", 2, period="2026-07-10T00:00:00"),  # > T0
        ]
    })
    res = pull_recorder_movements(db_session, ASSEMBLY, "asm-1", client=client)
    db_session.commit()

    assert res.skipped_pre_anchor == 1 and res.inserted == 1
    bin1 = (
        db_session.query(models.StockBin)
        .filter_by(item_id=item1.item_id, warehouse_ref1c="wh-1", organization_ref="ORG1")
        .one()
    )
    assert _f(bin1.on_hand) == 12  # seed 10 + kept 2


# ---------------------------------------------------------------------------
# pull-status transitions (§2.3)
# ---------------------------------------------------------------------------


def test_pull_empty_status(db_session):
    _setup(db_session)
    client = FakeODataClient({"asm-empty": []})
    res = pull_recorder_movements(db_session, ASSEMBLY, "asm-empty", client=client)
    db_session.commit()
    assert res.status == "empty" and res.inserted == 0
    pull = db_session.query(models.StockRecorderPull).filter_by(recorder_ref="asm-empty").one()
    # empty is a SUCCESS outcome — attempts (failed-attempt counter) stays 0.
    assert pull.status == "empty" and pull.attempts == 0


# ---------------------------------------------------------------------------
# queue + retry (§2.3 / §3а)
# ---------------------------------------------------------------------------


def test_enqueue_recorder_pull_is_pending_no_odata(db_session):
    _setup(db_session)
    row = enqueue_recorder_pull(db_session, ASSEMBLY, "asm-1", source="manufacture_export")
    db_session.commit()
    assert row.status == "pending" and row.source == "manufacture_export"
    # re-enqueue resets to pending.
    row.status = "done"
    db_session.flush()
    again = enqueue_recorder_pull(db_session, ASSEMBLY, "asm-1")
    assert again.id == row.id and again.status == "pending"


def test_reenqueue_resets_failed_pull_retry_bookkeeping(db_session):
    _setup(db_session)
    row = enqueue_recorder_pull(db_session, ASSEMBLY, "asm-retry", source="manufacture_export")
    row.status = "error"
    row.attempts = 5
    row.last_error = "1C timeout"
    db_session.commit()

    again = enqueue_recorder_pull(db_session, ASSEMBLY, "asm-retry")
    assert again.status == "pending"
    assert again.attempts == 0
    assert again.last_error is None


def test_process_pending_pulls_happy(db_session):
    item1, _ = _setup(db_session)
    enqueue_recorder_pull(db_session, ASSEMBLY, "asm-1", source="manufacture_export")
    db_session.commit()

    client = FakeODataClient({"asm-1": [_line("1", "Receipt", "ref-item-1", "wh-1", 5)]})
    results = process_pending_pulls(
        db_session,
        client=client,
        ledger_generation_id=db_session.info["item_ledger_generation_id"],
    )

    assert len(results) == 1 and results[0].status == "done"
    pull = db_session.query(models.StockRecorderPull).filter_by(recorder_ref="asm-1").one()
    assert pull.status == "done" and pull.line_count == 1
    assert _f(db_session.query(models.StockBin).filter_by(item_id=item1.item_id).one().on_hand) == 5


def test_process_pending_pulls_records_error_and_attempt_cap(db_session):
    _setup(db_session)
    enqueue_recorder_pull(db_session, ASSEMBLY, "asm-err", source="x")
    db_session.commit()

    results = process_pending_pulls(db_session, client=BoomClient())
    assert results[0].status == "error"
    pull = db_session.query(models.StockRecorderPull).filter_by(recorder_ref="asm-err").one()
    assert pull.status == "error" and pull.attempts == 1 and "boom" in (pull.last_error or "")

    # attempt cap: with max_attempts=1 the row (attempts==1) is no longer drained.
    results2 = process_pending_pulls(db_session, client=BoomClient(), max_attempts=1)
    assert results2 == []


def test_attempts_counts_failures_only(db_session):
    """Д5: attempts = number of FAILED pull attempts. Success never bumps it,
    every error bumps it by one, and a success after failures leaves the failed
    count untouched (status alone decides drain eligibility)."""
    item1, _ = _setup(db_session)
    enqueue_recorder_pull(db_session, ASSEMBLY, "asm-flap", source="x")
    db_session.commit()

    # two failing drains → attempts 1, then 2.
    process_pending_pulls(db_session, client=BoomClient())
    process_pending_pulls(db_session, client=BoomClient())
    pull = db_session.query(models.StockRecorderPull).filter_by(recorder_ref="asm-flap").one()
    assert pull.status == "error" and pull.attempts == 2

    # third drain succeeds → done, attempts unchanged (no success bump).
    ok_client = FakeODataClient({"asm-flap": [_line("1", "Receipt", "ref-item-1", "wh-1", 5)]})
    results = process_pending_pulls(db_session, client=ok_client)
    assert [r.status for r in results] == ["done"]
    db_session.expire_all()
    pull = db_session.query(models.StockRecorderPull).filter_by(recorder_ref="asm-flap").one()
    assert pull.status == "done" and pull.attempts == 2

    # a repeated direct re-pull (replace-by-recorder) also never bumps attempts.
    pull_recorder_movements(db_session, ASSEMBLY, "asm-flap", client=ok_client)
    db_session.commit()
    db_session.expire_all()
    pull = db_session.query(models.StockRecorderPull).filter_by(recorder_ref="asm-flap").one()
    assert pull.attempts == 2


def test_exhausted_error_not_drained_until_reenqueued(db_session):
    """Д5: an error row at the attempt cap is skipped by the drain, and a fresh
    enqueue (attempts reset) resurrects it into the queue."""
    item1, _ = _setup(db_session)
    row = enqueue_recorder_pull(db_session, ASSEMBLY, "asm-dead", source="x")
    row.status = "error"
    row.attempts = 5  # == DEFAULT_MAX_ATTEMPTS → exhausted
    row.last_error = "1C down"
    db_session.commit()

    ok_client = FakeODataClient({"asm-dead": [_line("1", "Receipt", "ref-item-1", "wh-1", 3)]})
    assert process_pending_pulls(db_session, client=ok_client) == []

    # re-enqueue resets attempts → next drain pulls it successfully.
    enqueue_recorder_pull(db_session, ASSEMBLY, "asm-dead")
    db_session.commit()
    results = process_pending_pulls(db_session, client=ok_client)
    assert [r.status for r in results] == ["done"]
    pull = db_session.query(models.StockRecorderPull).filter_by(recorder_ref="asm-dead").one()
    assert pull.status == "done" and pull.attempts == 0


# ---------------------------------------------------------------------------
# export hooks (§3а step 1, §6.1) — via the transfer on-success writer
# ---------------------------------------------------------------------------


def _mk_issue(db, doc_number, status="requested"):
    # SQLite does not enforce FKs in this test harness, so the product/order
    # parents are not required to exercise the hook wrapper.
    issue = models.ProductionMaterialIssue(
        document_number=doc_number,
        product_id=1,
        order_id=1,
        status=status,
        direction="issue",
    )
    db.add(issue)
    db.flush()
    return issue


def test_transfer_hook_enqueues_on_success(db_session):
    from app.services import one_c_stock_transfer_export as ste

    issue = _mk_issue(db_session, "MI-HOOK-1")
    ste._mark_issue_exported(db_session, issue.issue_id, "REF-OK")
    db_session.flush()

    pull = db_session.query(models.StockRecorderPull).filter_by(recorder_ref="REF-OK").one()
    assert pull.status == "pending"
    assert pull.recorder_type == ste.STOCK_TRANSFER_ENTITY
    assert pull.source == "stock_transfer_export"


def test_transfer_hook_never_raises_when_enqueue_errors(db_session, monkeypatch):
    from app.services import one_c_stock_transfer_export as ste
    import app.services.item_ledger.ingest as ingest_mod

    issue = _mk_issue(db_session, "MI-HOOK-2")

    def _boom(*args, **kwargs):
        raise RuntimeError("enqueue down")

    monkeypatch.setattr(ingest_mod, "enqueue_recorder_pull", _boom)

    # Must NOT raise into the export flow (safety net is inc3 Balance-reconcile).
    ste._mark_issue_exported(db_session, issue.issue_id, "REF-XYZ")
    db_session.flush()  # export path flushes/commits after on_success
    assert issue.status == "exported" and issue.exported_ref1c == "REF-XYZ"
    # and the failed enqueue left no queue row behind.
    assert db_session.query(models.StockRecorderPull).filter_by(recorder_ref="REF-XYZ").count() == 0
