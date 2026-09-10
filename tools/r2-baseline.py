"""Measure the fixed R2 synthetic baseline against an explicitly local DB."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.r2_local_contract import validate_r2_dsn


FIXTURE = ROOT / "tests" / "r2" / "fixtures" / "r2_synthetic_seed.json"


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
        identity = connection.execute(
            sa.text("SELECT current_database(), current_user, COALESCE(inet_server_addr()::text, 'local')")
        ).one()
        connection.execute(sa.text("CREATE TEMP TABLE r2_baseline_probe (movement_id text PRIMARY KEY, quantity numeric(18,3) NOT NULL)"))
        rows = [(movement["id"], movement["quantity"]) for movement in fixture["movements"]]
        connection.execute(sa.text("INSERT INTO r2_baseline_probe (movement_id, quantity) VALUES (:movement_id, :quantity)"), [
            {"movement_id": movement_id, "quantity": quantity} for movement_id, quantity in rows
        ])
        count = connection.execute(sa.text("SELECT count(*) FROM r2_baseline_probe")).scalar_one()
        table_size = connection.execute(sa.text("SELECT pg_total_relation_size('pg_temp.r2_baseline_probe')")).scalar_one()
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    print(json.dumps({
        "seed": fixture["seed"],
        "volumes": {"plans": len(fixture["plans"]), "movements": len(rows)},
        "database": identity[0],
        "user": identity[1],
        "server_address": identity[2],
        "rows_written": count,
        "temp_table_bytes": table_size,
        "sql_write_count": len(rows) + 1,
        "elapsed_ms": elapsed_ms,
        "api_latency_ms": None,
        "api_latency_note": "R2 contour intentionally starts PostgreSQL only; backend API is measured in a later API wave",
    }, sort_keys=True))
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
