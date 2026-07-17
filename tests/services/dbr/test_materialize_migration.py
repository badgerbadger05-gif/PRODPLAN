"""Guard checks for the 20260717_08 materialization migration + ORM schema."""

from pathlib import Path

from sqlalchemy import create_engine, inspect

from app.database import Base


def test_orm_schema_carries_materialization_columns_and_status_check():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    insp = inspect(engine)

    slot_cols = {c["name"] for c in insp.get_columns("dbr_drum_slot")}
    assert {"one_c_order_ref", "one_c_order_number"} <= slot_cols
    sig_cols = {c["name"] for c in insp.get_columns("dbr_feeder_signal")}
    assert {"one_c_order_ref", "one_c_order_number"} <= sig_cols

    status_checks = " ".join(
        str(row.get("sqltext", ""))
        for row in insp.get_check_constraints("dbr_feeder_signal")
        if row.get("name") == "ck_dbr_feeder_signal_status"
    )
    for status in ("Order Created", "In Work", "Done"):
        assert status in status_checks


def test_migration_is_guarded_and_widens_status_check():
    migration = Path(
        "backend/alembic/versions/20260717_08_dbr_materialization.py"
    ).read_text()
    assert 'down_revision = "20260717_07"' in migration
    assert 'revision = "20260717_08"' in migration
    # SQLite-safe check swap via batch_alter_table, Postgres via drop/create.
    assert "batch_alter_table" in migration
    assert "Order Created" in migration
    # Guarded: columns are added only when absent, check swapped only when stale.
    assert "not in columns" in migration
    assert "Order Created' not in status_sql" in migration or '"Order Created" not in status_sql' in migration
