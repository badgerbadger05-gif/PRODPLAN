"""R7 current future-supply publication on two real PostgreSQL sessions."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app import models
from app.services.item_ledger.future_supply_capture import publish_current_future_supply


def _dsn() -> str:
    value = os.getenv("PRODPLAN_R2_TEST_DSN")
    if not value:
        pytest.skip("PRODPLAN_R2_TEST_DSN is not configured")
    from app.r2_local_contract import validate_r2_dsn

    validate_r2_dsn(value)
    return value


@pytest.mark.integration
def test_current_future_supply_publication_is_atomic_and_noop_idempotent():
    dsn = _dsn()
    pytest.importorskip("psycopg2")
    engine = sa.create_engine(dsn, poolclass=sa.pool.NullPool)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    key = "r7-pg-current-atomic"
    stamp = datetime(2026, 9, 10, tzinfo=timezone.utc)
    seed: dict[str, int] = {}
    try:
        with engine.begin() as conn:
            # Remove only this test's exact prior seed after an interrupted run.
            old_pointer = conn.execute(sa.text("SELECT current_generation_id FROM planning_truth_state WHERE id=1")).scalar_one_or_none()
            old_pointer_key = conn.execute(sa.text("SELECT generation_key FROM ledger_generation WHERE id=:id"), {"id": old_pointer}).scalar_one_or_none() if old_pointer is not None else None
            if old_pointer is not None and (old_pointer_key is None or str(old_pointer_key).startswith(key + "-")):
                # A previously interrupted run may have removed its own test
                # generation after moving the pointer.  Never restore a
                # dangling FK; treat that contour as the known-empty state.
                old_pointer = None
                conn.execute(sa.text("UPDATE planning_truth_state SET current_generation_id=NULL WHERE id=1"))
                conn.execute(sa.text("INSERT INTO planning_truth_state(id,current_generation_id) VALUES (1,NULL) ON CONFLICT (id) DO NOTHING"))
            conn.execute(sa.text("DELETE FROM ledger_future_supply_current_change WHERE current_identity=:key"), {"key": key})
            conn.execute(sa.text("DELETE FROM ledger_future_supply_current WHERE current_identity=:key"), {"key": key})
            conn.execute(sa.text("DELETE FROM ledger_future_supply WHERE current_identity=:key"), {"key": key})
            conn.execute(sa.text("DELETE FROM ledger_build_batch WHERE batch_key LIKE :prefix"), {"prefix": key + "-%"})
            conn.execute(sa.text("DELETE FROM ledger_generation WHERE generation_key LIKE :prefix"), {"prefix": key + "-%"})
            conn.execute(sa.text("DELETE FROM physical_import_batch WHERE batch_key LIKE :prefix"), {"prefix": key + "-%"})
            conn.execute(sa.text("DELETE FROM items WHERE item_code=:key"), {"key": key})
            seed["old_pointer"] = int(old_pointer) if old_pointer is not None else 0
            seed["item"] = int(conn.execute(sa.text("INSERT INTO items(item_code,item_name,status) VALUES (:code,'R7 PG current','active') RETURNING item_id"), {"code": key}).scalar_one())
            seed["physical1"] = int(conn.execute(sa.text("INSERT INTO physical_import_batch(batch_key,status,cutoff,source_watermarks,completed_at) VALUES (:key,'completed',:stamp,'{}'::jsonb,:stamp) RETURNING id"), {"key": key + "-physical-1", "stamp": stamp}).scalar_one())
            seed["gen1"] = int(conn.execute(sa.text("INSERT INTO ledger_generation(generation_key,status,cutoff,source_watermarks,capabilities,physical_import_batch_id,algorithm_version,accepted_at) VALUES (:key,'accepted',:stamp,'{}'::jsonb,'{}'::jsonb,:physical,'r7-test',:stamp) RETURNING id"), {"key": key + "-gen-1", "stamp": stamp, "physical": seed["physical1"]}).scalar_one())
            seed["batch1"] = int(conn.execute(sa.text("INSERT INTO ledger_build_batch(ledger_generation_id,stage,batch_key,status,algorithm_version,metrics,completed_at) VALUES (:gen,'future_supply_capture',:key,'completed','r7-test','{}'::jsonb,:stamp) RETURNING id"), {"gen": seed["gen1"], "key": key + "-batch-1", "stamp": stamp}).scalar_one())
            conn.execute(sa.text("UPDATE planning_truth_state SET current_generation_id=:gen WHERE id=1"), {"gen": seed["gen1"]})
            params = {"gen": seed["gen1"], "batch": seed["batch1"], "item": seed["item"], "stamp": stamp, "hash": "1" * 64, "key": key}
            conn.execute(sa.text("INSERT INTO ledger_future_supply(ledger_generation_id,supply_kind,item_id,planning_stock_pool,source_ref,source_line_ref,ordered_qty_at_cutoff,realized_qty_at_cutoff,open_qty_at_cutoff,source_state_key,capture_cutoff,source_content_hash,capture_batch_id,current_identity,is_current,evidence_status) VALUES (:gen,'supplier_order',:item,'default','R7-PG','1',5,1,4,'open',:stamp,:hash,:batch,:key,false,'exact')"), params)
            conn.execute(sa.text("INSERT INTO ledger_future_supply_current(current_identity,source_generation_id,source_capture_batch_id,supply_kind,item_id,planning_stock_pool,source_ref,source_line_ref,ordered_qty_at_cutoff,realized_qty_at_cutoff,open_qty_at_cutoff,source_state_key,capture_cutoff,source_content_hash,evidence_status) VALUES (:key,:gen,:batch,'supplier_order',:item,'default','R7-PG','1',5,1,4,'open',:stamp,:hash,'exact')"), params)

        reader = Session()
        writer = Session()
        try:
            before = reader.query(models.LedgerFutureSupplyCurrent).filter_by(current_identity=key).one()
            assert before.open_qty_at_cutoff == 4
            assert int(reader.get(models.PlanningTruthState, 1).current_generation_id) == seed["gen1"]

            physical2 = writer.execute(sa.text("INSERT INTO physical_import_batch(batch_key,status,cutoff,source_watermarks,completed_at) VALUES (:key,'completed',:stamp,'{}'::jsonb,:stamp) RETURNING id"), {"key": key + "-physical-2", "stamp": stamp}).scalar_one()
            gen2 = writer.execute(sa.text("INSERT INTO ledger_generation(generation_key,status,cutoff,source_watermarks,capabilities,physical_import_batch_id,algorithm_version,accepted_at) VALUES (:key,'accepted',:stamp,'{}'::jsonb,'{}'::jsonb,:physical,'r7-test',:stamp) RETURNING id"), {"key": key + "-gen-2", "stamp": stamp, "physical": physical2}).scalar_one()
            batch2 = writer.execute(sa.text("INSERT INTO ledger_build_batch(ledger_generation_id,stage,batch_key,status,algorithm_version,metrics,completed_at) VALUES (:gen,'future_supply_capture',:key,'completed','r7-test','{}'::jsonb,:stamp) RETURNING id"), {"gen": gen2, "key": key + "-batch-2", "stamp": stamp}).scalar_one()
            writer.execute(sa.text("UPDATE planning_truth_state SET current_generation_id=:gen WHERE id=1"), {"gen": gen2})
            params2 = {"gen": gen2, "batch": batch2, "item": seed["item"], "stamp": stamp, "hash": "2" * 64, "key": key}
            writer.execute(sa.text("INSERT INTO ledger_future_supply(ledger_generation_id,supply_kind,item_id,planning_stock_pool,source_ref,source_line_ref,ordered_qty_at_cutoff,realized_qty_at_cutoff,open_qty_at_cutoff,source_state_key,capture_cutoff,source_content_hash,capture_batch_id,current_identity,is_current,evidence_status) VALUES (:gen,'supplier_order',:item,'default','R7-PG','1',8,1,7,'open',:stamp,:hash,:batch,:key,false,'exact')"), params2)
            writer.flush()
            publish_current_future_supply(writer, int(gen2))

            # PostgreSQL READ COMMITTED reader sees old pointer and quantity
            # until the writer's atomic publication commits.
            assert reader.query(models.LedgerFutureSupplyCurrent).filter_by(current_identity=key).one().open_qty_at_cutoff == 4
            assert int(reader.get(models.PlanningTruthState, 1).current_generation_id) == seed["gen1"]
            writer.commit()
            reader.rollback()
            reader.expire_all()
            assert reader.query(models.LedgerFutureSupplyCurrent).filter_by(current_identity=key).one().open_qty_at_cutoff == 7
            assert int(reader.get(models.PlanningTruthState, 1).current_generation_id) == int(gen2)
            change_count = reader.query(models.LedgerFutureSupplyCurrentChange).filter_by(current_identity=key).count()
        finally:
            reader.close()
            writer.close()

        # Two independent exact retries are no-ops and do not add audit rows.
        retry_a, retry_b = Session(), Session()
        try:
            assert publish_current_future_supply(retry_a, int(gen2))["generation_id"] == int(gen2)
            retry_a.commit()
            assert publish_current_future_supply(retry_b, int(gen2))["generation_id"] == int(gen2)
            retry_b.commit()
            with engine.connect() as conn:
                assert conn.execute(sa.text("SELECT count(*) FROM ledger_future_supply_current_change WHERE current_identity=:key"), {"key": key}).scalar_one() == change_count
        finally:
            retry_a.close()
            retry_b.close()
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM ledger_future_supply_current_change WHERE current_identity=:key"), {"key": key})
            conn.execute(sa.text("DELETE FROM ledger_future_supply_current WHERE current_identity=:key"), {"key": key})
            conn.execute(sa.text("DELETE FROM ledger_future_supply WHERE current_identity=:key"), {"key": key})
            conn.execute(sa.text("DELETE FROM ledger_build_batch WHERE batch_key LIKE :prefix"), {"prefix": key + "-%"})
            conn.execute(sa.text("UPDATE planning_truth_state SET current_generation_id=:gen WHERE id=1"), {"gen": seed.get("old_pointer") or None})
            conn.execute(sa.text("DELETE FROM ledger_generation WHERE generation_key LIKE :prefix"), {"prefix": key + "-%"})
            conn.execute(sa.text("DELETE FROM physical_import_batch WHERE batch_key LIKE :prefix"), {"prefix": key + "-%"})
            conn.execute(sa.text("DELETE FROM items WHERE item_code=:key"), {"key": key})
        engine.dispose()
