"""Run one ordinary physical refresh on an explicitly identified local clone.

No recorder enqueue, historical lookback, discard, migration or 1C write is
performed. Each launch records its operator-declared budget before mutation;
a no-delta tick is reported as a no-op and cannot prove a changing cycle.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]


def validate_target(dsn: str, *, database: str, port: int):
    try:
        url = make_url(dsn)
    except Exception as exc:
        raise ValueError("invalid local refresh DSN") from exc
    if (
        not re.fullmatch(r"prodplan_(?:weekend|cutover_rehearsal)_\d{8}", database)
        or not 1024 <= port <= 65535 or port == 5432
        or url.drivername not in {"postgresql", "postgresql+psycopg2"}
        or url.host not in {"127.0.0.1", "::1"}
        or url.database != database or url.port != port
        or not url.username or url.username in {"postgres", "prodplan"}
        or not url.password or url.query
    ):
        raise ValueError("local refresh requires exact loopback/port/rehearsal database identity")
    return url


def validate_limits(seconds: float, rows: int) -> dict:
    if not math.isfinite(seconds) or seconds <= 0 or rows <= 0:
        raise ValueError("explicit positive finite duration and replay budgets required")
    return {"max_duration_seconds": seconds, "max_replayed_rows": rows,
            "no_op_replayed_rows_max": 0}


def parse_cutoff(value: str, *, now: datetime | None = None) -> datetime:
    cutoff = datetime.fromisoformat(value)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff requires an explicit timezone")
    cutoff = cutoff.astimezone(timezone.utc)
    if cutoff > (now or datetime.now(timezone.utc)):
        raise ValueError("future cutoff is forbidden")
    return cutoff


@contextmanager
def get_only_odata():
    """Reject every urllib write, including paths outside the OData client."""
    from app.services.odata_client import OData1CClient

    original_open = urllib.request.OpenerDirector.open
    original_urlopen = urllib.request.urlopen
    original_write = OData1CClient._send_write

    def check(request, data):
        method = request.get_method() if hasattr(request, "get_method") else "GET"
        if method not in {"GET", "HEAD"} or data is not None or getattr(request, "data", None) is not None:
            raise RuntimeError("local refresh accepts GET/HEAD only; 1C writes forbidden")

    def guarded_open(self, request, data=None, *args, **kwargs):
        check(request, data)
        return original_open(self, request, data, *args, **kwargs)

    def guarded_urlopen(request, data=None, *args, **kwargs):
        check(request, data)
        return original_urlopen(request, data, *args, **kwargs)

    def deny_write(*args, **kwargs):
        raise RuntimeError("local refresh forbids 1C writes")

    urllib.request.OpenerDirector.open = guarded_open
    urllib.request.urlopen = guarded_urlopen
    OData1CClient._send_write = deny_write
    try:
        yield
    finally:
        urllib.request.OpenerDirector.open = original_open
        urllib.request.urlopen = original_urlopen
        OData1CClient._send_write = original_write


def evaluate_cycle(metrics: dict, *, limits: dict, elapsed: float,
                   accepted_pointer: bool, scopes_ready: bool) -> dict:
    from tools.physical_refresh_acceptance import evaluate_physical_refresh_evidence

    baseline = evaluate_physical_refresh_evidence(metrics)
    no_op = int(metrics["input_delta_rows"]) == 0
    checks = {
        "production_scale": baseline["checks"].get("production_scale", False),
        "duration_seconds": elapsed <= limits["max_duration_seconds"],
        "replayed_rows": int(metrics["replayed_rows"]) <= (
            0 if no_op else limits["max_replayed_rows"]),
        "accepted_pointer": accepted_pointer,
        "all_current_scopes_ready": scopes_ready,
    }
    return {"passed": all(checks.values()), "kind": "no_op" if no_op else "delta",
            "counts_as_changing_cycle": not no_op and all(checks.values()),
            "checks": checks, "tracked_r11_budget": baseline}


def _receipt(path: Path, report: dict, *, create: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if create:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        return
    temporary = path.with_name(path.name + ".partial")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def run(args) -> dict:
    if not args.writers_stopped:
        raise ValueError("explicit writers-stopped acknowledgement required for isolated clone")
    url = validate_target(os.environ.get("DATABASE_URL", ""), database=args.database, port=args.port)
    limits = validate_limits(args.max_duration_seconds, args.max_replayed_rows)
    cutoff = parse_cutoff(args.cutoff)
    if (os.environ.get("TZ"), os.environ.get("PGTZ")) != ("Europe/Moscow", "Europe/Moscow"):
        raise ValueError("TZ and PGTZ must both be Europe/Moscow")
    if args.expected_parent <= 0 or not args.generation_key.startswith("local-rehearsal-"):
        raise ValueError("explicit accepted parent and local-rehearsal generation key required")
    config_bytes = Path(args.odata_config).read_bytes()
    config_hash = hashlib.sha256(config_bytes).hexdigest()
    if config_hash != args.config_sha256:
        raise ValueError("OData config hash differs from explicit reviewed configuration")
    config = json.loads(config_bytes)
    if urllib.parse.urlsplit(str(config.get("base_url", ""))).username is not None:
        raise ValueError("OData URL must not contain credentials")

    from app import models
    from app.services.item_ledger.ingest import _build_client
    from app.services.item_ledger.physical_refresh_orchestrator import (
        _bounded_custody_tail_sle_ids, run_physical_refresh,
    )
    from app.services.item_ledger.reconcile import build_balance_snapshot
    from app.services.odata_client import get_stock_from_1c_odata

    engine = create_engine(url, connect_args={"options": (
        f"-c timezone=Europe/Moscow -c statement_timeout={int(limits['max_duration_seconds'] * 1000)} "
        "-c lock_timeout=10000"
    )})
    output = Path(args.output)
    report = {"status": "budget_declared", "database": url.database, "port": url.port,
              "expected_parent": args.expected_parent, "generation_key": args.generation_key,
              "cutoff": cutoff.isoformat(), "budget_declared_at": datetime.now(timezone.utc).isoformat(),
              "limits": limits, "config_sha256": config_hash,
              "ordinary_refresh": {"discovery_lookback_seconds": 0, "audit_all_known_recorders": False}}
    _receipt(output, report, create=True)
    started = time.monotonic()
    try:
        report["stage"] = "preflight"
        with Session(engine, autoflush=False) as db, get_only_odata():
            db.execute(text("SET TRANSACTION READ ONLY"))
            if db.execute(text("SHOW timezone")).scalar_one() != "Europe/Moscow":
                raise ValueError("database timezone guard failed")
            pointer = db.get(models.PlanningTruthState, 1)
            parent = db.get(models.LedgerGeneration, args.expected_parent)
            building = db.query(models.LedgerGeneration.id).filter_by(status="building").count()
            busy = db.execute(text("""SELECT count(*) FROM pg_stat_activity
                WHERE datname=current_database() AND pid<>pg_backend_pid()
                AND state IN ('active','idle in transaction','idle in transaction (aborted)')""")).scalar_one()
            if (pointer is None or pointer.current_generation_id != args.expected_parent
                    or parent is None or parent.status != "accepted" or building or busy
                    or parent.cutoff >= cutoff or cutoff - parent.cutoff > timedelta(days=1)):
                raise ValueError("accepted parent/cutoff/building/quiet guard failed")
            terminal = db.execute(text("SELECT max(id) FROM physical_import_batch")).scalar_one()
            if terminal != parent.physical_import_batch_id:
                raise ValueError("physical terminal differs from accepted parent; explicit recovery required")
            if db.query(models.LedgerGeneration.id).filter_by(generation_key=args.generation_key).first():
                raise ValueError("generation key already exists; no automatic retry")
            manifest = db.get(models.ProductionMaterialCustodyProjectionManifest, parent.id)
            if manifest is None:
                raise ValueError("accepted parent custody manifest missing")
            ids = _bounded_custody_tail_sle_ids(
                db, after_event_id=manifest.source_event_high_watermark_id,
                parent_generation_id=parent.id, target_cutoff=cutoff,
            )
            report["preflight"] = {"building": building, "busy": busy,
                                    "custody_tail_source_sle_ids": list(ids), "parent_batch": terminal}
            database_rows = db.query(models.StockLedgerEntry.id).count()
            db.rollback()
            _receipt(output, report)
            client = _build_client(config)

            def balance_at(instant):
                local = instant.astimezone(ZoneInfo("Europe/Moscow")).replace(tzinfo=None, microsecond=0)
                diagnostics = {}
                rows = get_stock_from_1c_odata(
                    base_url=client.base_url, entity_name="AccumulationRegister_ЗапасыНаСкладах/Balance",
                    username=client.username, password=client.password, token=client.token,
                    filter_query=f"Period le datetime'{local.isoformat()}'", diagnostics=diagnostics,
                )
                if (diagnostics.get("truncated") or diagnostics.get("nomenclature_resolve_error")
                        or diagnostics.get("warehouse_resolve_errors")):
                    raise ValueError("OData balance diagnostics indicate incomplete source")
                return build_balance_snapshot(db, rows, strict=True)

            report["stage"] = "balance_fetch"
            _receipt(output, report)
            balance = balance_at(cutoff)
            report["stage"] = "physical_refresh"
            _receipt(output, report)
            result = run_physical_refresh(
                db, generation_key=args.generation_key, target_cutoff=cutoff, client=client,
                balance_snapshot=balance, opening_balance_loader=balance_at,
                discovery_lookback=timedelta(0), audit_all_known_recorders=False,
                window_size=timedelta(days=1), max_windows=1,
                database_ledger_rows=database_rows, started_by="local-cutover-rehearsal",
            )
            db.rollback()
            report["stage"] = "postflight"
            _receipt(output, report)
            db.execute(text("SET TRANSACTION READ ONLY"))
            db.expire_all()
            current = db.get(models.PlanningTruthState, 1)
            accepted = db.get(models.LedgerGeneration, current.current_generation_id)
            scopes = db.query(models.CurrentExecutionScope).all()
            current_custody = db.get(models.ProductionMaterialCustodyProjectionManifest, accepted.id)
            custody_max = db.execute(text("SELECT coalesce(max(id),0) FROM production_material_custody_event")).scalar_one()
            custody_closed = current_custody is not None and current_custody.source_event_high_watermark_id == custody_max
            metrics = {key: getattr(result, key) for key in (
                "duration_ms", "input_delta_rows", "replayed_rows", "database_ledger_rows")}
            metrics["affected_scopes"] = list(result.affected_scopes)
            elapsed = time.monotonic() - started
            verdict = evaluate_cycle(
                metrics, limits=limits, elapsed=elapsed,
                accepted_pointer=(accepted.status == "accepted" and accepted.id == result.published_generation_id
                    and result.balance_convergence.valid and custody_closed
                    and (accepted.cutoff == cutoff if result.published else result.verified_cutoff == cutoff)),
                scopes_ready=len(scopes) == 11 and all(
                    scope.result_ready and scope.source_generation_id == accepted.id for scope in scopes),
            )
            report.update({"status": "passed" if verdict["passed"] else "failed",
                           "metrics": metrics, "elapsed_seconds": elapsed, "verdict": verdict,
                           "accepted_generation": accepted.id, "published": result.published,
                           "custody_tail_closed": custody_closed,
                           "balance_convergence": {key: getattr(result.balance_convergence, key)
                                                   for key in ("valid", "compared", "matched", "mismatched")},
                           "phase_timings": dict(result.phase_timings),
                           "verified_cutoff": result.verified_cutoff.isoformat() if result.verified_cutoff else None})
            db.rollback()
    except Exception as exc:
        # Persist the exception class only: HTTP/SQL exceptions may carry secrets.
        report.update({"status": "failed", "error_class": type(exc).__name__,
                       "elapsed_seconds": time.monotonic() - started})
        _receipt(output, report)
        raise RuntimeError(f"local refresh failed ({type(exc).__name__}); inspect receipt") from None
    finally:
        engine.dispose()
    _receipt(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--writers-stopped", action="store_true", required=True)
    parser.add_argument("--expected-parent", type=int, required=True)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--generation-key", required=True)
    parser.add_argument("--odata-config", required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-duration-seconds", type=float, required=True)
    parser.add_argument("--max-replayed-rows", type=int, required=True)
    report = run(parser.parse_args())
    print(json.dumps({"status": report["status"], "verdict": report["verdict"],
                      "accepted_generation": report["accepted_generation"]}))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
