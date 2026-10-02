"""R6 compaction validates history in SQL and selects only the truth pointer."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


def _migration():
    path = Path(__file__).parents[2] / "backend/alembic/versions/20260910_08_r6_compact_stock_bin.py"
    spec = spec_from_file_location("r6_stock_bin_bounded_migration", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema(connection, *, pointer=10, generations=None, rows=(), unique=False):
    connection.exec_driver_sql("CREATE TABLE planning_truth_state(id INTEGER PRIMARY KEY,current_generation_id INTEGER)")
    connection.exec_driver_sql("CREATE TABLE ledger_generation(id INTEGER PRIMARY KEY,status TEXT)")
    constraint = (
        ", CONSTRAINT ux_stock_bin_ledger_key UNIQUE "
        "(ledger_generation_id,item_id,characteristic_ref,organization_ref,warehouse_ref1c)"
        if unique else ""
    )
    connection.exec_driver_sql(
        "CREATE TABLE stock_bin(id INTEGER PRIMARY KEY,item_id INTEGER NOT NULL,"
        "characteristic_ref TEXT,organization_ref TEXT,warehouse_ref1c TEXT,"
        "ledger_generation_id INTEGER,on_hand NUMERIC(15,3) NOT NULL" + constraint + ")"
    )
    connection.execute(sa.text("INSERT INTO planning_truth_state VALUES(1,:pointer)"), {"pointer": pointer})
    for generation, status in generations or [(9, "accepted"), (10, "accepted"), (11, "building"), (99, "failed")]:
        connection.execute(sa.text("INSERT INTO ledger_generation VALUES(:id,:status)"), {"id": generation, "status": status})
    for row in rows:
        connection.exec_driver_sql("INSERT INTO stock_bin VALUES(?,?,?,?,?,?,?)", row)


@pytest.mark.parametrize("pointer,generations,rows,message", [
    (10, None, [(1, 1, "", "ORG", "WH", None, 1)], "provenance missing"),
    (10, None, [(1, 1, "", "ORG", "WH", 500, 1)], "generation missing"),
    (10, [(10, None)], [(1, 1, "", "ORG", "WH", 10, 1)], "generation missing"),
    (10, None, [(1, 1, None, "ORG", "WH", 10, 1), (2, 1, None, "ORG", "WH", 10, 2)], "ambiguous duplicate generation"),
    (None, None, [(1, 1, "", "ORG", "WH", 10, 1), (2, 1, "", "ORG", "WH", 11, 2)], "no accepted generation"),
    (None, None, [(1, 1, "", "ORG", "WH", 9, 1)], "non-building history without accepted pointer"),
])
def test_bounded_validator_preserves_fail_closed_errors(pointer, generations, rows, message):
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _schema(connection, pointer=pointer, generations=generations, rows=rows)
        with pytest.raises(RuntimeError, match=message):
            _migration()._deduplicate(connection)
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM stock_bin").scalar_one() == len(rows)


@pytest.mark.parametrize("rows", [
    [],
    [(1, 1, "", "ORG", "WH", 11, 1)],
    [(1, 1, "", "ORG", "WH", 11, 1), (2, 1, "", "ORG", "WH-OTHER", 11, 2)],
])
def test_without_pointer_only_single_building_copy_per_full_key_is_valid(rows):
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _schema(connection, pointer=None, rows=rows)
        _migration()._deduplicate(connection)
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM stock_bin").scalar_one() == len(rows)


def test_upgrade_selects_exact_pointer_preserves_building_and_never_synthesizes_history():
    engine = sa.create_engine("sqlite:///:memory:")
    statements = []
    with engine.begin() as connection:
        _schema(connection, unique=True, rows=[
            (1, 1, "", "ORG", "WH", 9, 100),
            (2, 1, "", "ORG", "WH", 10, -2.5),
            (3, 1, "", "ORG", "WH", 11, 7),
            (4, 1, "", "ORG", "WH", 99, 300),
            (5, 2, "", "ORG", "WH", 9, 400),
            (6, 2, "", "ORG", "WH", 11, 8),
            (7, 1, "", "ORG-OTHER", "WH", 10, 12),
            (8, 1, "", "ORG", "WH-OTHER", 10, 13),
        ])
        connection.exec_driver_sql("CREATE TABLE stock_ledger_entry(id INTEGER PRIMARY KEY, qty INTEGER)")
        connection.exec_driver_sql("INSERT INTO stock_ledger_entry VALUES(1,42),(2,-10)")
        sa.event.listen(connection, "before_cursor_execute", lambda conn, cursor, statement, parameters, context, executemany: statements.append(statement))
        migration = _migration()
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        rows = connection.exec_driver_sql("SELECT id,ledger_generation_id,on_hand,is_current FROM stock_bin ORDER BY id").all()
        assert rows == [(2, 10, -2.5, 1), (3, 11, 7, 0), (6, 11, 8, 0), (7, 10, 12, 1), (8, 10, 13, 1)]
        assert connection.exec_driver_sql("SELECT * FROM stock_ledger_entry ORDER BY id").all() == [(1, 42), (2, -10)]
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM ledger_generation").scalar_one() == 4
        field = next(c for c in sa.inspect(connection).get_columns("stock_bin") if c["name"] == "is_current")
        assert field["nullable"] is True and field["default"] is None
        assert {c["name"] for c in sa.inspect(connection).get_unique_constraints("stock_bin")} == {"ux_stock_bin_generation_key"}
        # The bootstrap uses ADD DEFAULT false, never an unbounded rewrite of
        # every historic row before deletion. The only UPDATE selects pointer.
        updates = [" ".join(s.lower().split()) for s in statements if s.lstrip().lower().startswith("update stock_bin")]
        assert len(updates) == 1 and "where ledger_generation_id" in updates[0]
        assert any("add column is_current boolean default" in " ".join(s.lower().split()) for s in statements)


def test_history_validation_never_fetches_the_whole_table_into_python():
    engine = sa.create_engine("sqlite:///:memory:")
    statements = []
    with engine.begin() as connection:
        _schema(connection, rows=[(n, n, "", "ORG", "WH", 10, n) for n in range(1, 201)])

        class BoundedResult:
            def __init__(self, result):
                self.result = result

            def scalar(self):
                return self.result.scalar()

            def first(self):
                return self.result.first()

            def mappings(self):
                raise AssertionError("whole-history mappings are forbidden")

            def all(self):
                raise AssertionError("whole-history rows are forbidden")

        class BoundedConnection:
            def execute(self, statement):
                statements.append(str(statement))
                return BoundedResult(connection.execute(statement))

        _migration()._deduplicate(BoundedConnection())
        assert all("LIMIT 1" in s for s in statements if "FROM stock_bin" in s)
        assert len(statements) == 4


def test_sql_grouping_keeps_organization_warehouse_and_characteristic_boundaries():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _schema(connection, rows=[
            (1, 1, "", "ORG", "WH", 10, 1),
            (2, 1, "C2", "ORG", "WH", 10, 1),
            (3, 1, "", "ORG2", "WH", 10, 1),
            (4, 1, "", "ORG", "WH2", 10, 1),
        ])
        _migration()._deduplicate(connection)
