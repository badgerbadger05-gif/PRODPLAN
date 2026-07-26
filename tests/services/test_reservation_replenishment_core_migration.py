import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from importlib.util import module_from_spec, spec_from_file_location

from sqlalchemy import create_engine, text


PG_URL = os.getenv(
    "PRODPLAN_TEST_PG_URL",
    "postgresql://prodplan:prodplan@localhost:55440/prodplan_test",
)


def _pg_available() -> bool:
    try:
        engine = create_engine(PG_URL)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


requires_pg = pytest.mark.skipif(not _pg_available(), reason="PostgreSQL is required for migration SQL semantics")


def _load_migration_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "alembic"
        / "versions"
        / "20260726_01_reservation_replenishment_core.py"
    )
    spec = spec_from_file_location("reservation_replenishment_migration", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema_sql(schema: str) -> str:
    return (
        f"""
        CREATE SCHEMA IF NOT EXISTS {schema};

        CREATE TABLE {schema}.items (
            item_id INTEGER PRIMARY KEY,
            replenishment_method TEXT
        );

        CREATE TABLE {schema}.mrp_requirement (
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL,
            total_required_qty NUMERIC(15,3) NOT NULL DEFAULT 0,
            net_required_qty NUMERIC(15,3) NOT NULL DEFAULT 0
        );

        CREATE TABLE {schema}.reservation_entry (
            id BIGINT PRIMARY KEY,
            item_id INTEGER NOT NULL,
            characteristic_ref TEXT NOT NULL DEFAULT '',
            organization_ref TEXT NOT NULL DEFAULT '',
            planning_stock_pool TEXT NOT NULL DEFAULT 'default',
            run_id INTEGER,
            freeze_version INTEGER NOT NULL DEFAULT 0,
            requirement_id INTEGER NOT NULL,
            priority_period_from DATE NOT NULL,
            priority_period_to DATE NOT NULL,
            realization_mode TEXT NOT NULL DEFAULT 'consume',
            reserved_qty NUMERIC(15,3) NOT NULL DEFAULT 0,
            realized_qty NUMERIC(15,3) NOT NULL DEFAULT 0,
            covered_on_hand_qty NUMERIC(15,3) NOT NULL DEFAULT 0,
            covered_incoming_supplier_qty NUMERIC(15,3) NOT NULL DEFAULT 0,
            covered_incoming_wip_qty NUMERIC(15,3) NOT NULL DEFAULT 0,
            uncovered_qty NUMERIC(15,3) NOT NULL DEFAULT 0,
            lifecycle_status TEXT NOT NULL DEFAULT 'active',
            coverage_state TEXT NOT NULL DEFAULT 'open',
            opened_at TIMESTAMP,
            closed_at TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
            ledger_generation_id BIGINT NOT NULL,
            CONSTRAINT ux_reservation_entry_req_mode
                UNIQUE (ledger_generation_id, requirement_id, realization_mode)
        );
        """
    )


def _drop_schema(conn, schema: str) -> None:
    conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))


@requires_pg
def test_reservation_replenishment_core_upgrade_classifies_and_caps_reserve_rows():
    migration = _load_migration_module()
    schema = f"mig_{uuid4().hex}"
    items = f"{schema}.items"
    requirements = f"{schema}.mrp_requirement"
    reservations = f"{schema}.reservation_entry"
    ledger_generation_id = 771

    engine = create_engine(PG_URL)
    try:
        with engine.connect() as conn:
            conn.execute(text(_schema_sql(schema)))
            conn.execute(text(f"SET search_path TO {schema}"))

            conn.execute(
                text(f"INSERT INTO {items}(item_id, replenishment_method) VALUES (:item_id, :method)"),
                [
                    {"item_id": 1, "method": "Закупка"},
                    {"item_id": 2, "method": "Производство"},
                    {"item_id": 3, "method": "переработка"},
                    {"item_id": 4, "method": None},
                    {"item_id": 5, "method": "Purchase"},
                ],
            )

            conn.execute(
                text(f"""
                    INSERT INTO {requirements}(id, item_id, total_required_qty, net_required_qty)
                    VALUES
                      (:id_a, :item_a, :total_a, :net_a),
                      (:id_b, :item_b, :total_b, :net_b),
                      (:id_c, :item_c, :total_c, :net_c),
                      (:id_d, :item_d, :total_d, :net_d),
                      (:id_e, :item_e, :total_e, :net_e)
                """),
                {
                    "id_a": 101,
                    "item_a": 1,
                    "total_a": "100.000",
                    "net_a": "40.000",
                    "id_b": 102,
                    "item_b": 2,
                    "total_b": "80.000",
                    "net_b": "30.000",
                    "id_c": 103,
                    "item_c": 3,
                    "total_c": "90.000",
                    "net_c": "50.000",
                    "id_d": 104,
                    "item_d": 4,
                    "total_d": "120.000",
                    "net_d": "20.000",
                    "id_e": 105,
                    "item_e": 3,
                    "total_e": "70.000",
                    "net_e": "25.000",
                },
            )

            conn.execute(
                text(f"""
                    INSERT INTO {reservations}(
                        id, item_id, requirement_id, ledger_generation_id,
                        priority_period_from, priority_period_to, realization_mode, realized_qty
                    )
                    VALUES
                        (:id1, :item1, :req1, :gen_id, '2026-01-01', '2026-01-31', :mode1, :realized1),
                        (:id2, :item2, :req2, :gen_id, '2026-01-01', '2026-01-31', :mode2, :realized2),
                        (:id3, :item3, :req3, :gen_id, '2026-01-01', '2026-01-31', :mode3, :realized3),
                        (:id4, :item4, :req3, :gen_id, '2026-01-01', '2026-01-31', :mode4, :realized4),
                        (:id5, :item5, :req4, :gen_id, '2026-01-01', '2026-01-31', :mode5, :realized5),
                        (:id6, :item6, :req5, :gen_id, '2026-01-01', '2026-01-31', :mode6, :realized6)
                """),
                {
                    "id1": 1,
                    "item1": 1,
                    "req1": 101,
                    "id2": 2,
                    "item2": 2,
                    "req2": 102,
                    "id3": 3,
                    "item3": 3,
                    "req3": 103,
                    "id4": 4,
                    "item4": 3,
                    "mode4": "make",
                    "id5": 5,
                    "id6": 6,
                    "mode1": "consume",
                    "mode2": "consume",
                    "mode3": "consume",
                    "realized1": "10",
                    "realized2": "4",
                    "realized3": "7",
                    "realized4": "2",
                    "realized5": "5",
                    "realized6": "12",
                    "gen_id": ledger_generation_id,
                    "item5": 5,
                    "req4": 104,
                    "mode5": "consume",
                    "id6": 6,
                    "item6": 3,
                    "req5": 105,
                    "mode6": "consume",
                },
            )

            ctx = MigrationContext.configure(conn)
            migration.op = Operations(ctx)
            migration.upgrade()

            rows = conn.execute(
                text(f"""
                    SELECT
                        requirement_id,
                        realization_mode,
                        reserved_qty,
                        covered_from_stock_at_freeze_qty,
                        replenishment_required_qty,
                        replenishment_received_qty,
                        realized_qty
                    FROM {reservations}
                    ORDER BY requirement_id
                """)
            ).fetchall()
            row_by_req = {int(r[0]): r[1:] for r in rows}

            assert row_by_req[101][0] == "buy"
            assert float(row_by_req[101][1]) == 100.0
            assert float(row_by_req[101][2]) == 60.0
            assert float(row_by_req[101][3]) == 40.0
            assert float(row_by_req[101][4]) == 10.0
            assert float(row_by_req[101][5]) == 10.0

            assert row_by_req[102][0] == "make"
            assert float(row_by_req[102][1]) == 80.0
            assert float(row_by_req[102][2]) == 50.0
            assert float(row_by_req[102][3]) == 30.0
            assert float(row_by_req[102][4]) == 4.0
            assert float(row_by_req[102][5]) == 4.0

            # Consumption row for requirement 103 has make sibling and must be deleted.
            req103_rows = conn.execute(
                text(f"""
                    SELECT COUNT(*), MIN(realization_mode)
                    FROM {reservations}
                    WHERE requirement_id = 103
                    GROUP BY realization_mode
                """)
            ).fetchall()
            assert len(req103_rows) == 1
            assert req103_rows[0][1] == "make"
            assert float(row_by_req[103][1]) == 90.0
            assert float(row_by_req[103][2]) == 40.0
            assert float(row_by_req[103][3]) == 50.0
            assert float(row_by_req[103][4]) == 2.0

            assert row_by_req[104][0] == "buy"
            assert float(row_by_req[104][1]) == 120.0
            assert float(row_by_req[104][2]) == 100.0
            assert float(row_by_req[104][3]) == 20.0
            assert float(row_by_req[104][4]) == 5.0
            assert float(row_by_req[104][5]) == 5.0

            assert row_by_req[105][0] == "make"
            assert float(row_by_req[105][1]) == 70.0
            assert float(row_by_req[105][2]) == 45.0
            assert float(row_by_req[105][3]) == 25.0
            assert float(row_by_req[105][4]) == 12.0
            assert float(row_by_req[105][5]) == 12.0

            consume_count = conn.execute(
                text(f"SELECT COUNT(*) FROM {reservations} WHERE realization_mode='consume'")
            ).scalar_one()
            assert consume_count == 0

            # Constraints after migration: unique by (generation, requirement) + replenishment flow.
            assert (
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM pg_constraint
                        WHERE conname = :name
                          AND conrelid = to_regclass(:table)
                        """
                    ),
                    {"name": "ck_reservation_entry_replenishment_flow", "table": reservations},
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM pg_constraint
                        WHERE conname = :name
                          AND conrelid = to_regclass(:table)
                        """
                    ),
                    {"name": "ux_reservation_entry_requirement", "table": reservations},
                ).scalar_one()
                == 1
            )
            assert conn.execute(
                text(
                    f"""
                    SELECT realization_mode
                    FROM {reservations}
                    WHERE requirement_id = 103
                    """
                )
            ).fetchall() == [("make",)]
    finally:
        with engine.connect() as conn:
            _drop_schema(conn, schema)
        engine.dispose()
