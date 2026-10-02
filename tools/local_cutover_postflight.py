"""Guarded local §58 rebase/republish and read-only physical/API postflight.

Uses canonical allocators, publishers, physical visibility and fold. Rebase
always republishes dependents, including a zero-changed-pairs result, in the
same transaction as its exact frozen/operator preservation checks.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]
from tools.local_cutover_refresh import validate_target, _receipt
from tools.operational_preservation import TABLES, OPTIONAL_TABLES, TARGET_REVISION
from tools.current_execution_migration import _table_digest


class PostflightBlocked(RuntimeError):
    """A controlled diagnostic code containing no credentials."""


@contextmanager
def deny_network():
    from app.services.odata_client import OData1CClient
    original = (urllib.request.urlopen, urllib.request.OpenerDirector.open,
                OData1CClient._make_request, OData1CClient._send_write)
    def denied(*args, **kwargs):
        raise PostflightBlocked("external_http_odata_forbidden")
    urllib.request.urlopen = urllib.request.OpenerDirector.open = denied
    OData1CClient._make_request = OData1CClient._send_write = denied
    try:
        yield
    finally:
        (urllib.request.urlopen, urllib.request.OpenerDirector.open,
         OData1CClient._make_request, OData1CClient._send_write) = original


def assert_select_only(statement):
    sql = str(statement).strip()
    if (not sql or ";" in sql.rstrip(";") or "--" in sql or "/*" in sql or "*/" in sql
            or sql.split(None, 1)[0].upper() not in {"SELECT", "SHOW"}
            or re.search(r"\b(?:INTO|FOR\s+UPDATE)\b", sql, re.I)
            or re.search(r"\b(?:nextval|setval|set_config|pg_notify|lo_create|dblink_exec|"
                         r"pg_reload_conf|pg_stat_reset|pg_terminate_backend|pg_cancel_backend|"
                         r"pg_(?:try_)?advisory\w*)\s*\(", sql, re.I)):
        raise PostflightBlocked("api_sql_mutation_forbidden")


@contextmanager
def api_read_guards(engine):
    def select_only(connection, cursor, statement, parameters, context, executemany):
        assert_select_only(statement)
    def no_flush(session, flush_context, instances):
        if session.new or session.dirty or session.deleted:
            raise PostflightBlocked("api_orm_flush_forbidden")
    event.listen(engine, "before_cursor_execute", select_only)
    event.listen(Session, "before_flush", no_flush)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", select_only)
        event.remove(Session, "before_flush", no_flush)


def guard_session(session, *, database, generation, quiet=False):
    identity = session.execute(text("SELECT current_database(),current_setting('TimeZone')")).one()
    revisions = list(session.execute(text("SELECT version_num FROM alembic_version")).scalars())
    pointer = session.execute(text("""SELECT g.id,g.status,g.cutoff,g.physical_import_batch_id
        FROM planning_truth_state s JOIN ledger_generation g ON g.id=s.current_generation_id WHERE s.id=1""")).one()
    building = session.execute(text("SELECT count(*) FROM ledger_generation WHERE status='building'")).scalar_one()
    if (tuple(identity) != (database, "Europe/Moscow") or revisions != [TARGET_REVISION]
            or pointer.id != generation or pointer.status != "accepted" or building):
        raise PostflightBlocked("database_head_pointer_building_guard_failed")
    if quiet:
        busy = session.execute(text("""SELECT count(*) FROM pg_stat_activity
            WHERE datname=current_database() AND pid<>pg_backend_pid()
            AND state IN ('active','idle in transaction','idle in transaction (aborted)')""")).scalar_one()
        if busy:
            raise PostflightBlocked("other_active_database_sessions")
    return pointer


def protected_snapshot(session):
    """Canonical exact row digests; projected frozen fields have no live math."""
    connection = session.connection()
    schema = inspect(connection)
    present = set(schema.get_table_names())
    tables = tuple(table for table in TABLES if table not in OPTIONAL_TABLES) + ("production_order_line_states",)
    if set(tables) - present:
        raise PostflightBlocked("protected_operator_table_missing")
    # The same UTC presentation is used before/after, including TIMESTAMPTZ.
    session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    try:
        result = {table: _table_digest(connection, schema, table) for table in tables}
        for table, columns in (
            ("mrp_requirement", ["id", "run_id", "item_id", "total_required_qty", "net_required_qty", "freeze_version"]),
            ("reservation_entry", ["id", "current_identity", "requirement_id", "run_id", "realization_mode",
                                   "reserved_qty", "covered_from_stock_at_freeze_qty", "replenishment_required_qty"]),
        ):
            result[table + ":frozen"] = _table_digest(connection, schema, table, columns=columns)
        return result
    finally:
        # _table_digest streams on this connection. SQLAlchemy 2 applies its
        # execution options in place; subsequent transactional SET/DML must
        # use an ordinary cursor rather than DECLARE CURSOR FOR SET/UPDATE.
        connection.execution_options(stream_results=False, yield_per=None)
        session.execute(text("SET LOCAL TIME ZONE 'Europe/Moscow'"))


def atomic_rebase(session, generation, *, snapshot=protected_snapshot, max_seconds,
                  clock=time.monotonic, progress=lambda phase: None):
    from tools.current_execution_migration import (
        _replenishment_rebase_on_session, _republish_current_execution_after_rebase,
    )
    started = clock()
    progress("protected_before")
    before = snapshot(session)
    progress("canonical_rebase")
    rebase = _replenishment_rebase_on_session(session, generation, republish=False)
    progress("mandatory_republish")
    republish = _republish_current_execution_after_rebase(session, generation)
    progress("protected_after")
    after = snapshot(session)
    if before != after:
        raise PostflightBlocked("rebase_changed_frozen_or_operator_rows")
    if clock() - started > max_seconds:
        raise PostflightBlocked("rebase_duration_budget_exceeded")
    return {"rebase": rebase, "republish": republish, "protected_snapshot": after,
            "protected_equal_before_after": True, "republish_mandatory": True}


def full_stock_fold(session, pointer):
    from app import models
    from app.services.item_ledger.current_physical import fold_current_stock
    from app.services.item_ledger.physical_visibility import visible_sle_query
    selected = session.query(models.StockWarehouse).filter_by(is_selected=True).count()
    if not selected:
        raise PostflightBlocked("selected_warehouses_missing")
    query = visible_sle_query(
        session, physical_import_batch_id=pointer.physical_import_batch_id,
        cutoff=pointer.cutoff.astimezone(ZoneInfo("Europe/Moscow")).replace(tzinfo=None),
    ).join(models.StockWarehouse, models.StockWarehouse.warehouse_ref1c == models.StockLedgerEntry.warehouse_ref1c).filter(
        models.StockWarehouse.is_selected.is_(True),
    ).with_entities(models.StockLedgerEntry.id, models.StockLedgerEntry.item_id,
                    models.StockLedgerEntry.characteristic_ref, models.StockLedgerEntry.organization_ref,
                    models.StockLedgerEntry.warehouse_ref1c, models.StockLedgerEntry.qty).yield_per(10000)
    last_by_key = {}
    count = 0
    def rows():
        nonlocal count
        for row in query:
            key = (int(row.item_id), row.characteristic_ref or "", row.organization_ref or "", row.warehouse_ref1c or "")
            # visible_sle_query owns deterministic posting_at/id ordering.
            last_by_key[key] = int(row.id)
            count += 1
            yield row
    folded = fold_current_stock(rows())
    if not count or not folded:
        raise PostflightBlocked("selected_physical_visibility_empty")
    expected = {key: (cell.on_hand, last_by_key[key]) for key, cell in folded.items()}
    actual = {}
    for row, owner_status in session.query(models.StockBin, models.LedgerGeneration.status).join(
        models.LedgerGeneration, models.LedgerGeneration.id == models.StockBin.ledger_generation_id,
    ).join(
        models.StockWarehouse, models.StockWarehouse.warehouse_ref1c == models.StockBin.warehouse_ref1c,
    ).filter(models.StockWarehouse.is_selected.is_(True), models.StockBin.is_current.is_(True)).yield_per(10000):
        key = (int(row.item_id), row.characteristic_ref or "", row.organization_ref or "", row.warehouse_ref1c or "")
        if key in actual:
            raise PostflightBlocked("stock_bin_duplicate_current_key")
        if owner_status != "accepted":
            raise PostflightBlocked("stock_bin_owner_not_accepted")
        actual[key] = (Decimal(str(row.on_hand)), row.last_entry_id)
    mismatches = [key for key in sorted(expected.keys() | actual.keys()) if expected.get(key) != actual.get(key)]
    if mismatches:
        raise PostflightBlocked("stock_bin_physical_fold_mismatch")
    return {"selected_warehouses": selected, "visible_sle_rows": count,
            "visible_full_keys": len(expected), "current_bin_full_keys": len(actual), "mismatch_count": 0}


async def asgi_get(app, path):
    path_only, _, query = path.partition("?")
    status, chunks, requested = None, [], False
    completed = asyncio.Event()
    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await completed.wait()
        return {"type": "http.disconnect"}
    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = int(message["status"])
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                completed.set()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": path_only, "raw_path": path_only.encode("ascii"),
             "query_string": query.encode("ascii"), "root_path": "", "headers": [(b"host", b"rehearsal.invalid")],
             "client": ("127.0.0.1", 0), "server": ("rehearsal.invalid", 80)}
    await asyncio.wait_for(app(scope, receive, send), timeout=30)
    payload = json.loads(b"".join(chunks))
    if status != 200 or not isinstance(payload, dict):
        raise PostflightBlocked("api_non_200_or_non_object")
    return payload


def api_state(session, *, database, generation):
    guard_session(session, database=database, generation=generation, quiet=True)
    scopes = session.execute(text("SELECT count(*),count(*) FILTER (WHERE result_ready AND source_generation_id=:g) FROM current_execution_scope"), {"g": generation}).one()
    if tuple(scopes) != (11, 11):
        raise PostflightBlocked("current_scopes_not_all_ready")
    row = dict(session.execute(text("SELECT source_revision,content_hash,summary FROM current_execution_scope WHERE entity_kind='production_control_journal' AND scope_key='production:all-live-orders'")).mappings().one())
    if not row["summary"].get("root_product_options"):
        raise PostflightBlocked("production_root_options_missing")
    return row


async def api_checks(app, before, generation):
    summary = before["summary"]
    roots = await asgi_get(app, "/api/v1/production-control/orders/root-products")
    if (not isinstance(roots.get("rows"), list) or roots.get("total") != len(roots["rows"])
            or roots["total"] != len(summary["root_product_options"])):
        raise PostflightBlocked("root_options_differ_from_current_scope")
    root = int(roots["rows"][0]["item_id"])
    orders = await asgi_get(app, "/api/v1/production-control/orders?limit=1")
    filtered = await asgi_get(app, f"/api/v1/production-control/orders?root_item_id={root}&limit=1")
    for page in (orders, filtered):
        meta = page.get("truth_meta") or {}
        if (page.get("latest_run_id") != summary["latest_run_id"]
                or page.get("latest_source_plan_id") != summary["latest_source_plan_id"]
                or meta.get("truth_status") != "accepted" or int(meta.get("ledger_generation") or 0) != generation):
            raise PostflightBlocked("api_truth_or_run_metadata_differ")
    if not 0 < int(filtered.get("total", -1)) <= int(orders.get("total", -1)):
        raise PostflightBlocked("root_filtered_total_invalid")
    endpoints = {}
    for path in (
        "/api/v1/production-control/assembly-queue?limit=1",
        "/api/v1/production-control/assembly-readiness?limit=1",
        "/api/v1/production-control/drum?limit=1",
        "/api/v1/production-control/shelves?limit=1",
        "/api/v1/purchase-control/orders?limit=1",
    ):
        payload = await asgi_get(app, path)
        if path.startswith("/api/v1/purchase-control/"):
            # The existing purchase DTO exposes these fields at the envelope
            # level; the production DTO uses the shared truth_meta object.
            status, source = payload.get("truth_status"), payload.get("ledger_generation_id")
        else:
            meta = payload.get("truth_meta") or {}
            status, source = meta.get("truth_status"), meta.get("ledger_generation")
        if status != "accepted" or int(source or 0) != generation:
            raise PostflightBlocked("api_execution_truth_pointer_differs")
        endpoints[path.partition("?")[0]] = {"http_status": 200, "ledger_generation": generation}
    return {"root_options_total": roots["total"], "filtered_root_item_id": root,
            "production_total": orders["total"], "filtered_total": filtered["total"], "execution_endpoints": endpoints}


def run(args):
    url = validate_target(os.environ.get("DATABASE_URL", ""), database=args.database, port=args.port)
    if (not args.writers_stopped or args.generation <= 0 or not math.isfinite(args.max_seconds)
            or args.max_seconds <= 0 or (os.environ.get("TZ"), os.environ.get("PGTZ")) != ("Europe/Moscow", "Europe/Moscow")):
        raise PostflightBlocked("explicit_local_generation_writers_timezone_budget_required")
    output = Path(args.output)
    report = {"status": "started", "phase": args.phase, "database": url.database,
              "generation": args.generation, "head": TARGET_REVISION,
              "max_seconds_declared": args.max_seconds, "rehearsal_only": True}
    _receipt(output, report, create=True)
    started = time.monotonic()
    engine = create_engine(url, connect_args={"options": f"-c timezone=Europe/Moscow -c statement_timeout={int(args.max_seconds*1000)} -c lock_timeout=10000"})
    saved_pgoptions = os.environ.get("PGOPTIONS")
    os.environ["PGOPTIONS"] = f"-c timezone=Europe/Moscow -c statement_timeout={int(args.max_seconds*1000)} -c lock_timeout=10000"
    try:
        with deny_network():
            def progress(stage):
                report["stage"] = stage
                _receipt(output, report)
            if args.phase == "api":
                from app.database import engine as api_engine, SessionLocal
                validate_target(api_engine.url.render_as_string(hide_password=False), database=args.database, port=args.port)
                with api_read_guards(api_engine):
                    from app.main import app
                    with SessionLocal() as session:
                        before = api_state(session, database=args.database, generation=args.generation)
                        session.rollback()
                    report["api"] = asyncio.run(api_checks(app, before, args.generation))
                    with SessionLocal() as session:
                        after = api_state(session, database=args.database, generation=args.generation)
                        session.rollback()
                    if before != after:
                        raise PostflightBlocked("api_current_scope_changed")
                    report.update(sql_guard="select_show_with_for_share", odata="denied", scope_unchanged=True)
            else:
                with Session(engine, autoflush=False) as session:
                    with session.begin():
                        if args.phase == "fold":
                            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                        pointer = guard_session(session, database=args.database, generation=args.generation, quiet=True)
                        if args.phase == "rebase":
                            report["rebase"] = atomic_rebase(
                                session, args.generation, max_seconds=args.max_seconds, progress=progress,
                            )
                            guard_session(session, database=args.database, generation=args.generation)
                        else:
                            report["fold"] = full_stock_fold(session, pointer)
                        if time.monotonic() - started > args.max_seconds:
                            raise PostflightBlocked("phase_duration_budget_exceeded")
            if time.monotonic() - started > args.max_seconds:
                raise PostflightBlocked("phase_duration_budget_exceeded")
        report.update(status="passed", elapsed_seconds=time.monotonic()-started)
    except Exception as exc:
        report.update(status="failed", error_class=type(exc).__name__,
                      error_code=str(exc) if isinstance(exc, PostflightBlocked) else "dependency_failed",
                      elapsed_seconds=time.monotonic()-started)
        _receipt(output, report)
        raise PostflightBlocked(report["error_code"]) from None
    finally:
        engine.dispose()
        if saved_pgoptions is None:
            os.environ.pop("PGOPTIONS", None)
        else:
            os.environ["PGOPTIONS"] = saved_pgoptions
    _receipt(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("rebase", "fold", "api"))
    parser.add_argument("--database", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--writers-stopped", action="store_true", required=True)
    parser.add_argument("--max-seconds", type=float, required=True)
    parser.add_argument("--output", required=True)
    result = run(parser.parse_args())
    print(json.dumps({"status": result["status"], "phase": result["phase"], "generation": result["generation"]}))


if __name__ == "__main__":
    main()
