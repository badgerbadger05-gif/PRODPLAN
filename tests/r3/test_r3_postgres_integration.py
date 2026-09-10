"""R3 PostgreSQL checks; they only run against the guarded local R2 DSN."""

from __future__ import annotations

import os
import importlib.util
from pathlib import Path
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app import models
from app.services.item_ledger import LedgerKey, seed_from_balance
from app.services.item_ledger.physical_visibility import (
    PhysicalVisibilityError,
    require_import_batch,
)


def _dsn():
    dsn = os.getenv("PRODPLAN_R2_TEST_DSN")
    if not dsn:
        pytest.skip("PRODPLAN_R2_TEST_DSN is not configured")
    from app.r2_local_contract import validate_r2_dsn

    validate_r2_dsn(dsn)
    return dsn


def _r3_migration_module():
    path = Path(__file__).parents[2] / "backend" / "alembic" / "versions" / "20260910_01_r3_identity_acceptance.py"
    spec = importlib.util.spec_from_file_location("r3_identity_acceptance", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.integration
def test_r3_postgres_schema_and_incomplete_visibility_guard():
    engine = create_engine(_dsn(), poolclass=__import__("sqlalchemy").pool.NullPool)
    with engine.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                text("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public'")
            )
        }
        assert {
            "physical_import_page",
            "stock_ledger_business_identity_map",
            "planning_live_pointer",
            "planning_run_successor",
        } <= tables
        connection.rollback()
        tx = connection.begin()
        try:
            batch_id = connection.execute(
                text(
                    "INSERT INTO physical_import_batch "
                    "(batch_key, status, source_watermarks, source_complete, "
                    "received_page_count, expected_page_count) "
                    "VALUES (:key, 'completed', '{}'::jsonb, false, 1, 2) RETURNING id"
                ),
                {"key": "r3-pg-incomplete-guard"},
            ).scalar_one()
            db = Session(bind=connection)
            with pytest.raises(PhysicalVisibilityError, match="incomplete"):
                require_import_batch(db, int(batch_id))
            db.close()
        finally:
            tx.rollback()
    engine.dispose()


@pytest.mark.integration
def test_r3_postgres_seed_repeat_preserves_mapping_and_id():
    engine = create_engine(_dsn(), poolclass=__import__("sqlalchemy").pool.NullPool)
    with engine.connect() as connection:
        tx = connection.begin()
        db = Session(bind=connection)
        item = db.query(models.Item).order_by(models.Item.item_id.asc()).first()
        if item is None:
            tx.rollback()
            pytest.skip("local R2 database has no item catalog")
        key = LedgerKey(int(item.item_id), "", "r3-pg-org", "r3-pg-wh")
        for _ in range(100):
            seed_from_balance(db, {key: Decimal("7")}, anchor_period=date(2026, 9, 1))
        rows = db.query(models.StockLedgerEntry).filter_by(
            recorder_type="seed", organization_ref="r3-pg-org", warehouse_ref1c="r3-pg-wh"
        ).all()
        mappings = db.query(models.StockLedgerBusinessIdentityMap).filter(
            models.StockLedgerBusinessIdentityMap.stock_ledger_entry_id.in_([row.id for row in rows])
        ).all()
        assert len(rows) == len(mappings) == 1
        assert rows[0].business_identity
        db.close()
        tx.rollback()
    engine.dispose()


@pytest.mark.integration
def test_r3_postgres_pointer_backfill_maps_existing_fixed_plan():
    engine = create_engine(_dsn(), poolclass=__import__("sqlalchemy").pool.NullPool)
    migration = _r3_migration_module()
    with engine.connect() as connection:
        tx = connection.begin()
        try:
            plan_id = connection.execute(
                text(
                    "INSERT INTO production_plan_header "
                    "(name, period_from, period_to, status) "
                    "VALUES ('r3 backfill test', DATE '2026-09-01', DATE '2026-09-30', 'fixed') "
                    "RETURNING id"
                )
            ).scalar_one()
            run_id = connection.execute(
                text(
                    "INSERT INTO planning_run "
                    "(status, config_snapshot, source_plan_id, period_from, period_to) "
                    "VALUES ('FIXED_SNAPSHOT', '{}'::jsonb, :plan_id, DATE '2026-09-01', DATE '2026-09-30') "
                    "RETURNING run_id"
                ),
                {"plan_id": int(plan_id)},
            ).scalar_one()
            migration.backfill_live_pointers(connection)
            row = connection.execute(
                text(
                    "SELECT run_id FROM planning_live_pointer "
                    "WHERE plan_id = :plan_id AND status = 'active'"
                ),
                {"plan_id": int(plan_id)},
            ).scalar_one()
            assert int(row) == int(run_id)
        finally:
            tx.rollback()
    engine.dispose()
