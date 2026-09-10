"""Measure the fixed R2 synthetic baseline against an explicitly local DB."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
import os
import sys
import time
from pathlib import Path

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.r2_local_contract import validate_r2_dsn


FIXTURE = ROOT / "tests" / "r2" / "fixtures" / "r2_synthetic_seed.json"
API_PATH = "/api/v1/items/?skip=0&limit=100"
API_SAMPLE_COUNT = 9
R2_SEED_TIMESTAMP = datetime(2026, 1, 1, 0, 0, 0)


def _percentile(values: list[float], percentile: float) -> float:
    """Return a deterministic nearest-rank percentile in milliseconds."""

    ordered = sorted(values)
    rank = max(1, math.ceil((percentile / 100) * len(ordered)))
    return round(ordered[rank - 1], 3)


def _measure_api_latency(dsn: str, *, sample_count: int = API_SAMPLE_COUNT) -> dict[str, object]:
    """Probe the real DB-backed FastAPI items reader in-process.

    TestClient exercises the production FastAPI application and router without
    opening a public socket or starting any worker. The dependency override
    only binds that app to the already-validated local R2 engine.
    """

    os.environ["DATABASE_URL"] = dsn
    from fastapi.testclient import TestClient

    from app.database import SessionLocal, get_db
    from app.main import app

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            warmup = client.get(API_PATH)
            if warmup.status_code != 200:
                raise RuntimeError(
                    f"R2 API warm-up failed at {API_PATH}: HTTP {warmup.status_code} {warmup.text}"
                )
            samples: list[float] = []
            for _ in range(sample_count):
                started = time.perf_counter()
                response = client.get(API_PATH)
                elapsed_ms = (time.perf_counter() - started) * 1000
                if response.status_code != 200:
                    raise RuntimeError(
                        f"R2 API latency probe failed at {API_PATH}: HTTP {response.status_code} {response.text}"
                    )
                samples.append(elapsed_ms)
    finally:
        app.dependency_overrides.pop(get_db, None)

    return {
        "api_endpoint": API_PATH,
        "api_sample_count": len(samples),
        "api_latency_ms": {
            "min": round(min(samples), 3),
            "p50": _percentile(samples, 50),
            "p95": _percentile(samples, 95),
            "max": round(max(samples), 3),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.getenv("PRODPLAN_R2_TEST_DSN"))
    args = parser.parse_args()
    if not args.dsn:
        raise SystemExit("PRODPLAN_R2_TEST_DSN or --dsn is required; no default database is allowed")
    dsn = validate_r2_dsn(args.dsn)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    engine = sa.create_engine(dsn, poolclass=sa.pool.NullPool)
    started = time.perf_counter()
    with engine.begin() as connection:
        if connection.execute(sa.text("SELECT to_regclass('public.items')")).scalar_one() is None:
            raise RuntimeError("R2 database must be migrated before API baseline")
        identity = connection.execute(
            sa.text("SELECT current_database(), current_user, COALESCE(inet_server_addr()::text, 'local')")
        ).one()
        item_rows = [
            {
                "item_code": f"r2-{item['id']}",
                "item_name": f"R2 synthetic {item['id']}",
                "unit": item["unit"],
                "created_at": R2_SEED_TIMESTAMP,
                "updated_at": R2_SEED_TIMESTAMP,
            }
            for item in fixture["items"]
        ]
        connection.execute(
            sa.text(
                "INSERT INTO items (item_code, item_name, unit, status, created_at, updated_at) "
                "VALUES (:item_code, :item_name, :unit, 'active', :created_at, :updated_at) "
                "ON CONFLICT (item_code) DO UPDATE SET item_name = EXCLUDED.item_name, unit = EXCLUDED.unit, "
                "status = 'active', created_at = EXCLUDED.created_at, updated_at = EXCLUDED.updated_at"
            ),
            item_rows,
        )
        connection.execute(sa.text("CREATE TEMP TABLE r2_baseline_probe (movement_id text PRIMARY KEY, quantity numeric(18,3) NOT NULL)"))
        rows = [(movement["id"], movement["quantity"]) for movement in fixture["movements"]]
        connection.execute(sa.text("INSERT INTO r2_baseline_probe (movement_id, quantity) VALUES (:movement_id, :quantity)"), [
            {"movement_id": movement_id, "quantity": quantity} for movement_id, quantity in rows
        ])
        count = connection.execute(sa.text("SELECT count(*) FROM r2_baseline_probe")).scalar_one()
        table_size = connection.execute(sa.text("SELECT pg_total_relation_size('pg_temp.r2_baseline_probe')")).scalar_one()
    api_metrics = _measure_api_latency(args.dsn)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    print(json.dumps({
        "seed": fixture["seed"],
        "volumes": {"plans": len(fixture["plans"]), "movements": len(rows)},
        "database": identity[0],
        "user": identity[1],
        "server_address": identity[2],
        "rows_written": count,
        "temp_table_bytes": table_size,
        "sql_write_count": len(rows) + len(item_rows) + 2,
        "elapsed_ms": elapsed_ms,
        **api_metrics,
    }, sort_keys=True))
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
