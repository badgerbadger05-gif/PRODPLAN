import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app import models
from tools import current_execution_migration as canonical
from tools import local_cutover_postflight as runner


@pytest.mark.parametrize("sql", [
    "DELETE FROM items", "UPDATE items SET item_name='x'", "WITH x AS (DELETE FROM items) SELECT 1",
    "SELECT nextval('ids')", "SELECT set_config('x','y',true)", "SELECT * INTO copied FROM items",
    "SELECT * FROM items FOR UPDATE", "SELECT 1; SELECT 2", "SELECT 1 -- comment",
    "SELECT pg_try_advisory_xact_lock(1)", "SELECT lo_create(1)",
])
def test_api_guard_rejects_mutations_even_inside_select(sql):
    with pytest.raises(runner.PostflightBlocked, match="api_sql_mutation_forbidden"):
        runner.assert_select_only(sql)


def test_api_guard_allows_current_reader_for_share():
    runner.assert_select_only("SELECT id FROM current_execution_scope WHERE result_ready FOR SHARE;")
    runner.assert_select_only("SHOW timezone")


@pytest.mark.parametrize("failure", [None, "republish", "protected", "budget"])
def test_zero_change_rebase_always_republishes_and_rolls_back_every_failure(db_session, monkeypatch, failure):
    session = db_session
    session.execute(text("CREATE TABLE postflight_test (id INTEGER PRIMARY KEY, value INTEGER)"))
    session.execute(text("INSERT INTO postflight_test VALUES (1,0),(2,42)"))
    session.commit()
    calls = []
    def rebase(db, generation, *, republish):
        assert db is session and generation == 1652 and republish is False
        calls.append("rebase")
        db.execute(text("UPDATE postflight_test SET value=1 WHERE id=1"))
        return {"changed_pairs": 0}
    def republish(db, generation):
        calls.append("republish")
        if failure == "republish":
            raise runner.PostflightBlocked("republish_failed")
        db.execute(text("UPDATE postflight_test SET value=2 WHERE id=1"))
        if failure == "protected":
            db.execute(text("UPDATE postflight_test SET value=99 WHERE id=2"))
        return {"repaired_scopes": ["production"]}
    def snapshot(db):
        return {"protected": db.execute(text("SELECT value FROM postflight_test WHERE id=2")).scalar_one()}
    monkeypatch.setattr(canonical, "_replenishment_rebase_on_session", rebase)
    monkeypatch.setattr(canonical, "_republish_current_execution_after_rebase", republish)
    ticks = iter([0, 20 if failure == "budget" else 1])
    def execute():
        with session.begin():
            return runner.atomic_rebase(session, 1652, snapshot=snapshot, max_seconds=10, clock=lambda: next(ticks))
    if failure:
        with pytest.raises(runner.PostflightBlocked):
            execute()
        assert session.execute(text("SELECT value FROM postflight_test WHERE id=1")).scalar_one() == 0
        assert snapshot(session) == {"protected": 42}
    else:
        result = execute()
        assert result["rebase"]["changed_pairs"] == 0 and result["republish_mandatory"]
        assert session.execute(text("SELECT value FROM postflight_test WHERE id=1")).scalar_one() == 2
    assert calls == ["rebase", "republish"]
    session.rollback()
    session.execute(text("DROP TABLE postflight_test"))
    session.commit()


def test_fold_uses_canonical_signed_stock_and_posting_order_witness(db_session):
    cutoff = datetime(2026, 10, 2, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(batch_key="postflight", status="completed", cutoff=cutoff,
                                       source_complete=True, completed_at=cutoff)
    generation = models.LedgerGeneration(generation_key="postflight", status="accepted", cutoff=cutoff,
                                         physical_import_batch=batch, algorithm_version="test")
    item = models.Item(item_code="postflight", item_name="test")
    warehouse = models.StockWarehouse(warehouse_ref1c="warehouse", warehouse_name="selected", is_selected=True)
    db_session.add_all([batch, generation, item, warehouse])
    db_session.flush()
    for sle_id, posting, quantity in ((20, cutoff-timedelta(days=2), "5"), (10, cutoff-timedelta(days=1), "-2")):
        db_session.add(models.StockLedgerEntry(
            id=sle_id, ingest_batch_id=batch.id, item_id=item.item_id, characteristic_ref="", organization_ref="",
            warehouse_ref1c="warehouse", qty=Decimal(quantity), posting_at=posting,
            record_type="Receipt" if quantity == "5" else "Expense", movement_kind="receipt",
            source_content_hash=str(sle_id).ljust(64, "0"), recorder_type="Document_Test",
            recorder_ref=str(sle_id), line_no="1", ingest_source="test", active=True,
        ))
    stock = models.StockBin(ledger_generation_id=generation.id, item_id=item.item_id,
                           characteristic_ref="", organization_ref="", warehouse_ref1c="warehouse",
                           on_hand=Decimal("3"), last_entry_id=10, is_current=True)
    db_session.add(stock)
    db_session.flush()
    pointer = SimpleNamespace(id=generation.id, cutoff=cutoff, physical_import_batch_id=batch.id)
    result = runner.full_stock_fold(db_session, pointer)
    assert result["mismatch_count"] == 0 and result["visible_sle_rows"] == 2
    stock.last_entry_id = 20
    db_session.flush()
    with pytest.raises(runner.PostflightBlocked, match="physical_fold_mismatch"):
        runner.full_stock_fold(db_session, pointer)


def test_asgi_gate_has_no_lifespan_or_non_get_requests():
    seen = []
    async def app(scope, receive, send):
        seen.append((scope["type"], scope["method"], scope["path"], scope["query_string"]))
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": json.dumps({"rows": []}).encode(), "more_body": False})
    assert asyncio.run(runner.asgi_get(app, "/api/test?limit=1")) == {"rows": []}
    assert seen == [("http", "GET", "/api/test", b"limit=1")]


@pytest.mark.parametrize("stale_endpoint", [None, "drum", "purchase"])
def test_api_checks_the_actual_production_and_purchase_lineage_envelopes(monkeypatch, stale_endpoint):
    before = {"summary": {"root_product_options": [{"item_id": 5}], "latest_run_id": 1, "latest_source_plan_id": 2}}
    seen = []
    async def get(app, path):
        seen.append(path)
        if path.endswith("root-products"):
            return {"rows": [{"item_id": 5}], "total": 1}
        if "purchase-control" in path:
            return {"truth_status": "accepted", "ledger_generation_id": 1 if stale_endpoint == "purchase" else 1652}
        payload = {"truth_meta": {"truth_status": "accepted", "ledger_generation": 1 if stale_endpoint == "drum" and "/drum?" in path else 1652}}
        if "/orders?" in path:
            payload.update(total=1, latest_run_id=1, latest_source_plan_id=2)
        return payload
    monkeypatch.setattr(runner, "asgi_get", get)
    if stale_endpoint:
        with pytest.raises(runner.PostflightBlocked, match="execution_truth_pointer_differs"):
            asyncio.run(runner.api_checks(object(), before, 1652))
    else:
        result = asyncio.run(runner.api_checks(object(), before, 1652))
        assert len(result["execution_endpoints"]) == 5
        assert len(seen) == 8
