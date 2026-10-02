import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text, types as sqltypes

from tools.current_execution_migration import (
    _DIGEST_BATCH_BYTES, _digest_fetch_plan, _digest_fetch_size, _table_digest, build_manifest,
)
from tools import current_execution_migration as migration_tools


def _engine():
    return create_engine("sqlite:///:memory:")


def _create_inventory_tables(engine):
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE current_execution_scope ("
            "id INTEGER PRIMARY KEY, entity_kind TEXT NOT NULL, "
            "scope_key TEXT NOT NULL, result_ready BOOLEAN NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE current_execution_row ("
            "id INTEGER PRIMARY KEY, entity_kind TEXT NOT NULL, "
            "scope_key TEXT NOT NULL, business_identity TEXT NOT NULL, "
            "payload TEXT NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE planning_read_snapshot ("
            "id INTEGER PRIMARY KEY, ledger_generation_id INTEGER NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE planning_read_row ("
            "id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, row_key TEXT)"
        ))


def test_manifest_is_deterministic_and_classifies_rows_by_transition():
    engine = _engine()
    _create_inventory_tables(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO current_execution_scope "
            "(id, entity_kind, scope_key, result_ready) VALUES (1, 'mrp_result', 'mrp:all', 1)"
        ))
        connection.execute(text(
            "INSERT INTO current_execution_row "
            "(id, entity_kind, scope_key, business_identity, payload) "
            "VALUES (1, 'mrp_result', 'mrp:all', 'mrp-run:1:buy:item:10', '{}')"
        ))
        connection.execute(text(
            "INSERT INTO planning_read_snapshot (id, ledger_generation_id) VALUES (7, 3)"
        ))
        connection.execute(text(
            "INSERT INTO planning_read_row (id, snapshot_id, row_key) VALUES (11, 7, 'legacy:11')"
        ))

    first = build_manifest(engine)
    second = build_manifest(engine)

    assert first == second
    assert first["status"] == "ready"
    assert first["categories"]["preserve"]["current_execution_scope"]["row_count"] == 1
    assert first["categories"]["migrate"]["planning_read_snapshot"]["row_count"] == 1
    assert first["categories"]["migrate"]["planning_read_row"]["row_count"] == 1


def test_closed_plan_snapshot_is_preserved_business_closure_history():
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE closed_plan_snapshot (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        ))
        connection.execute(text("INSERT INTO closed_plan_snapshot(id, payload) VALUES (1, '{}')"))

    manifest = build_manifest(engine)

    assert manifest["status"] == "ready"
    assert manifest["categories"]["preserve"]["closed_plan_snapshot"]["row_count"] == 1
    assert "closed_plan_snapshot" not in manifest["categories"]["delete"]


def test_known_20260603_maintenance_backups_are_preserved_explicitly():
    tables = (
        "ops_backup_mrp13_coverage_before_fix_20260603",
        "ops_backup_obsolete_mrp12_after_run13_20260603_orders",
        "ops_backup_obsolete_mrp12_after_run13_20260603_products",
        "ops_backup_obsolete_mrp12_after_run13_20260603_states",
        "ops_backup_obsolete_mrp12_after_run13_20260603_summary",
        "ops_backup_remove_g_articles_20260603_line_states",
        "ops_backup_remove_g_articles_20260603_plan_line",
        "ops_backup_remove_g_articles_20260603_production_orders",
        "ops_backup_remove_g_articles_20260603_production_products",
    )
    engine = _engine()
    with engine.begin() as connection:
        for table in tables:
            connection.execute(text(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)'))
            connection.execute(text(f'INSERT INTO "{table}" (id) VALUES (1)'))

    manifest = build_manifest(engine)

    assert manifest["status"] == "ready"
    assert set(manifest["categories"]["preserve"]) >= set(tables)
    assert all(manifest["categories"]["preserve"][table]["row_count"] == 1 for table in tables)
    assert not set(tables) & set(manifest["categories"]["unknown"])


def test_historical_bucket_evidence_is_migrated_not_unknown():
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE planning_run_bucket_modes (run_id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE mrp_bucket_type_legacy (record_id INTEGER PRIMARY KEY)"))

    manifest = build_manifest(engine)

    assert manifest["status"] == "ready"
    assert set(manifest["categories"]["migrate"]) == {
        "planning_run_bucket_modes",
        "mrp_bucket_type_legacy",
    }


def test_table_checksum_is_stable_and_changes_with_subject_values():
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE items (item_id INTEGER PRIMARY KEY, item_name TEXT)"))
        connection.execute(text("INSERT INTO items (item_id, item_name) VALUES (1, 'A')"))

    first = build_manifest(engine)
    second = build_manifest(engine)
    assert first["categories"]["preserve"]["items"]["checksum"] == second["categories"]["preserve"]["items"]["checksum"]

    with engine.begin() as connection:
        connection.execute(text("UPDATE items SET item_name = 'B' WHERE item_id = 1"))
    changed = build_manifest(engine)
    assert changed["categories"]["preserve"]["items"]["checksum"] != first["categories"]["preserve"]["items"]["checksum"]


def test_table_digest_streams_one_row_at_a_time_without_changing_checksum():
    class Row:
        def __init__(self, mapping):
            self._mapping = mapping

    class Result:
        def __init__(self, rows):
            self.rows = iter(rows)
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.rows)

        def close(self):
            self.closed = True

    class Connection:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(self, result):
            self.result = result
            self.options = None

        def execution_options(self, **options):
            self.options = options
            return self

        def execute(self, _statement):
            return self.result

    class Inspector:
        def get_columns(self, _table_name):
            return [{"name": "id", "type": sqltypes.Integer()}, {"name": "payload", "type": sqltypes.JSON()}]

        def get_pk_constraint(self, _table_name):
            return {"constrained_columns": ["id"]}

    payloads = ["x" * 250_000, {"nested": ["y" * 100_000, 7]}]
    rows = [Row({"id": index, "payload": payload}) for index, payload in enumerate(payloads)]
    result = Result(rows)
    connection = Connection(result)

    row_count, actual = _table_digest(connection, Inspector(), "large_rows")

    expected = hashlib.sha256()
    for index, payload in enumerate(payloads):
        encoded = json.dumps(
            [index, payload],
            ensure_ascii=False,
            sort_keys=False,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
        expected.update(len(encoded).to_bytes(8, "big"))
        expected.update(encoded)

    assert row_count == 2
    assert actual == expected.hexdigest()
    assert connection.options == {"stream_results": True, "yield_per": 1}
    assert result.closed


class _ScalarProbeConnection:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, maxima=(), isolation="REPEATABLE READ"):
        self.maxima = maxima
        self.isolation = isolation
        self.statements = []

    def get_isolation_level(self):
        return self.isolation

    def execute(self, statement):
        self.statements.append(str(statement))
        return SimpleNamespace(one=lambda: self.maxima)


def _definitions(*datatypes):
    return [{"name": f"c{index}", "type": datatype} for index, datatype in enumerate(datatypes)]


@pytest.mark.parametrize("datatype", [
    sqltypes.JSON(), sqltypes.ARRAY(sqltypes.Integer()), sqltypes.LargeBinary(),
    sqltypes.NullType(), sqltypes.Numeric(), sqltypes.Float(),
])
def test_unbounded_complex_or_unknown_columns_never_batch(datatype):
    connection = _ScalarProbeConnection(maxima=(0,))
    definitions = _definitions(sqltypes.Integer(), sqltypes.Text(), datatype)
    assert _digest_fetch_size(connection, definitions, "wide_rows", ["c0", "c1", "c2"]) == 1
    assert connection.statements == []  # Don't scan TEXT if another field is unbounded.


def test_bounded_scalar_schema_uses_batch_even_under_read_committed():
    connection = _ScalarProbeConnection(isolation="READ COMMITTED")
    definitions = _definitions(sqltypes.Integer(), sqltypes.Boolean(), sqltypes.Numeric(15, 3), sqltypes.String(8), sqltypes.Date())
    assert _digest_fetch_size(connection, definitions, "scalar_rows", [c["name"] for c in definitions]) > 1
    assert connection.statements == []


def test_short_text_bounds_use_one_snapshot_probe_for_all_unbounded_strings():
    connection = _ScalarProbeConnection(maxima=(9, None))
    definitions = _definitions(sqltypes.Integer(), sqltypes.Text(), sqltypes.String())
    batch = _digest_fetch_size(connection, definitions, 'small"text', ["c0", "c1", "c2"])
    assert 1 < batch <= 512
    assert connection.statements == ['SELECT MAX(octet_length("c1")), MAX(octet_length("c2")) FROM "small""text"']


def test_wide_text_falls_back_to_one_row_and_read_committed_never_probes():
    definitions = _definitions(sqltypes.Text())
    wide = _ScalarProbeConnection(maxima=(_DIGEST_BATCH_BYTES,))
    assert _digest_fetch_size(wide, definitions, "wide_text", ["c0"]) == 1
    concurrent = _ScalarProbeConnection(maxima=(1,), isolation="READ COMMITTED")
    assert _digest_fetch_size(concurrent, definitions, "mutable_text", ["c0"]) == 1
    assert concurrent.statements == []


def test_sqlite_declared_widths_are_not_assumed_to_bound_stored_values():
    connection = _ScalarProbeConnection()
    connection.dialect = SimpleNamespace(name="sqlite")
    assert _digest_fetch_size(connection, _definitions(sqltypes.String(1)), "unenforced", ["c0"]) == 1


def test_schema_estimates_reduce_batch_for_wide_bounded_varchar():
    connection = _ScalarProbeConnection()
    small = _digest_fetch_size(connection, _definitions(sqltypes.String(8)), "tiny", ["c0"])
    wide = _digest_fetch_size(connection, _definitions(sqltypes.String(8000)), "wider", ["c0"])
    assert 1 < wide < small <= 512


def test_scalar_batch_and_one_row_stream_have_identical_hash_order_and_projection():
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE scalar_rows(id INTEGER PRIMARY KEY,label VARCHAR(8),quantity NUMERIC(15,3))"))
        connection.execute(text("INSERT INTO scalar_rows VALUES(3,'Я\n',-2.125),(1,NULL,0),(2,'A',1.5)"))

        class FetchConnection:
            dialect = SimpleNamespace(name="postgresql")

            def __init__(self, dialect_name="postgresql"):
                self.dialect = SimpleNamespace(name=dialect_name)
                self.options = None

            def execution_options(self, **options):
                self.options = options
                return self

            def execute(self, statement):
                return connection.execute(statement)

        schema = inspect(connection)
        for fields in (None, ["quantity", "label"], ["label", "id"]):
            one = FetchConnection(dialect_name="sqlite")
            batched = FetchConnection()
            assert _table_digest(one, schema, "scalar_rows", columns=fields) == _table_digest(batched, schema, "scalar_rows", columns=fields)
            assert one.options["yield_per"] == 1
            assert 1 < batched.options["yield_per"] <= 512
            # Preserve the original SHA algorithm, including selected-column
            # ordering and PK ordering even when the PK isn't projected.
            fields_used = fields or ["id", "label", "quantity"]
            expected = hashlib.sha256()
            for row in connection.execute(text("SELECT * FROM scalar_rows ORDER BY id")).mappings():
                encoded = json.dumps([row[c] for c in fields_used], ensure_ascii=False, default=str, separators=(",", ":")).encode()
                expected.update(len(encoded).to_bytes(8, "big"))
                expected.update(encoded)
            assert _table_digest(batched, schema, "scalar_rows", columns=fields)[1] == expected.hexdigest()


def test_projection_can_batch_without_reading_excluded_json_values():
    connection = _ScalarProbeConnection()
    definitions = [{"name": "id", "type": sqltypes.Integer()}, {"name": "payload", "type": sqltypes.JSON()}]
    assert _digest_fetch_size(connection, definitions, "documents", ["id"]) > 1
    assert _digest_fetch_size(connection, definitions, "documents", ["id", "payload"]) == 1


def test_small_json_uses_bounded_raw_text_plan_with_one_combined_probe(monkeypatch):
    monkeypatch.setattr(migration_tools, "_default_psycopg2_json_codec", lambda c: True)
    connection = _ScalarProbeConnection(maxima=(None, 402))
    definitions = _definitions(sqltypes.Integer(), sqltypes.Text(), sqltypes.JSON())
    batch, raw = _digest_fetch_plan(connection, definitions, "decisions", ["c0", "c1", "c2"])
    assert 1 < batch <= 512 and raw == ("c2",)
    assert connection.statements == ['SELECT MAX(octet_length("c1")), MAX(octet_length(CAST("c2" AS TEXT))) FROM "decisions"']


@pytest.mark.parametrize("maximum,isolation", [(_DIGEST_BATCH_BYTES, "REPEATABLE READ"), (402, "READ COMMITTED")])
def test_wide_or_unstable_json_keeps_native_one_row_without_cast(monkeypatch, maximum, isolation):
    monkeypatch.setattr(migration_tools, "_default_psycopg2_json_codec", lambda c: True)
    connection = _ScalarProbeConnection(maxima=(maximum,), isolation=isolation)
    batch, raw = _digest_fetch_plan(connection, _definitions(sqltypes.JSON()), "decisions", ["c0"])
    assert batch == 1 and raw == ()


def test_raw_json_batch_decodes_only_the_current_row_before_hashing(monkeypatch):
    monkeypatch.setattr(migration_tools, "_default_psycopg2_json_codec", lambda c: True)
    events = []
    original_loads = json.loads
    payloads = ['{"x":[1.25,null,"Я\\n"],"duplicate":1,"duplicate":2}', 'null']

    def loads(payload):
        events.append("decode")
        return original_loads(payload)

    monkeypatch.setattr(migration_tools.json, "loads", loads)

    class Result:
        def __iter__(self):
            for index, payload in enumerate(payloads):
                events.append("row")
                yield SimpleNamespace(_mapping={"id": index, "payload": payload})

        def close(self):
            events.append("close")

    class Connection(_ScalarProbeConnection):
        def execution_options(self, **options):
            self.options = options
            return self

        def execute(self, statement):
            self.statements.append(str(statement))
            return SimpleNamespace(one=lambda: (200,)) if "MAX(" in str(statement) else Result()

    connection = Connection()
    schema = SimpleNamespace(
        get_columns=lambda table: [{"name": "id", "type": sqltypes.Integer()}, {"name": "payload", "type": sqltypes.JSON()}],
        get_pk_constraint=lambda table: {"constrained_columns": ["id"]},
    )
    count, checksum = _table_digest(connection, schema, "decisions")
    assert count == 2
    assert events == ["row", "decode", "row", "decode", "close"]
    assert connection.options["yield_per"] > 1
    assert 'CAST("payload" AS TEXT) AS "payload"' in connection.statements[-1]
    expected = hashlib.sha256()
    for index, payload in enumerate(payloads):
        encoded = json.dumps([index, original_loads(payload)], ensure_ascii=False, separators=(",", ":")).encode()
        expected.update(len(encoded).to_bytes(8, "big"))
        expected.update(encoded)
    assert checksum == expected.hexdigest()


@pytest.mark.parametrize("orderable,json_primary_key", [(True, False), (False, False), (True, True)])
def test_json_ordering_fallback_keeps_native_ordering_and_original_errors(monkeypatch, orderable, json_primary_key):
    from sqlalchemy.dialects.postgresql import JSONB
    monkeypatch.setattr(migration_tools, "_default_psycopg2_json_codec", lambda c: True)

    class Result:
        def __iter__(self):
            # Native JSONB numeric order is 1,2,10; lexical text is 1,10,2.
            for value in (1, 2, 10):
                yield SimpleNamespace(_mapping={"payload": value})

        def close(self):
            pass

    class Connection(_ScalarProbeConnection):
        def execution_options(self, **options):
            self.options = options
            return self

        def execute(self, statement):
            self.statements.append(str(statement))
            if "MAX(" in str(statement):
                return SimpleNamespace(one=lambda: (200,))
            if not orderable:
                raise RuntimeError("could not identify an ordering operator for type json")
            return Result()

    connection = Connection()
    schema = SimpleNamespace(
        get_columns=lambda table: [{"name": "payload", "type": JSONB() if orderable else sqltypes.JSON()}],
        get_pk_constraint=lambda table: {"constrained_columns": ["payload"] if json_primary_key else []},
    )
    if not orderable:
        with pytest.raises(RuntimeError, match="ordering operator"):
            _table_digest(connection, schema, "historical_no_pk")
    else:
        count, actual = _table_digest(connection, schema, "historical_no_pk")
        expected = hashlib.sha256()
        for value in (1, 2, 10):
            encoded = json.dumps([value], separators=(",", ":")).encode()
            expected.update(len(encoded).to_bytes(8, "big"))
            expected.update(encoded)
        assert count == 3 and actual == expected.hexdigest()
    assert connection.options["yield_per"] == 1
    assert 'CAST("payload" AS TEXT) AS "payload"' not in connection.statements[-1]
    assert 'ORDER BY "payload"' in connection.statements[-1]


def test_postgresql_raw_json_hash_matches_native_default_codec_without_database_writes(monkeypatch, record_property):
    """Actual PostgreSQL fixtures live entirely in SELECT-only CTE VALUES."""
    dsn = os.getenv("PRODPLAN_DIGEST_TEST_DSN")
    if not dsn:
        pytest.skip("PRODPLAN_DIGEST_TEST_DSN is not configured")
    from sqlalchemy.engine import make_url
    from sqlalchemy.dialects.postgresql import JSONB
    import time
    import tracemalloc

    url = make_url(dsn)
    assert url.host in {"127.0.0.1", "localhost", "::1"}
    assert url.drivername in {"postgresql", "postgresql+psycopg2"}
    engine = create_engine(url)
    payloads = [
        '{"nested":[null,true,1.25,-0.0,"Я","😀","\\u001f"],"duplicate":1,"duplicate":2}',
        '[null,{"a":1e20,"b":0.000001,"c":"quoted \\\" value"}]',
        'null',
    ]
    prefix = (
        'WITH "digest_json_fixture" AS ('
        'SELECT (n*10+v.ordinal) AS id,CAST(v.payload AS JSON) AS payload,'
        'CAST(v.payload AS JSONB) AS payload_b,CAST(NULL AS TEXT) AS reason '
        'FROM (VALUES (1,:p0),(2,:p1),(3,:p2)) AS v(ordinal,payload) '
        'CROSS JOIN generate_series(1,1000) AS n) '
    )
    parameters = {f"p{n}": payload for n, payload in enumerate(payloads)}
    definitions = [
        {"name": "id", "type": sqltypes.Integer()},
        {"name": "payload", "type": sqltypes.JSON()},
        {"name": "payload_b", "type": JSONB()},
        {"name": "reason", "type": sqltypes.Text()},
    ]
    schema = SimpleNamespace(get_columns=lambda t: definitions, get_pk_constraint=lambda t: {"constrained_columns": ["id"]})
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        assert migration_tools._default_psycopg2_json_codec(connection) is True

        class FixtureConnection:
            def __init__(self, delegate):
                self.delegate = delegate
                self.dialect = delegate.dialect

            @property
            def connection(self):
                return self.delegate.connection

            def get_isolation_level(self):
                return self.delegate.get_isolation_level()

            def execution_options(self, **options):
                self.options = options
                self.delegate.execution_options(**options)
                return self

            def execute(self, statement):
                return self.delegate.execute(text(prefix + str(statement)), parameters)

        wrapper = FixtureConnection(connection)
        profiles = {}
        for mode in ("native", "raw_text"):
            tracemalloc.start()
            started = time.perf_counter()
            if mode == "native":
                with monkeypatch.context() as patch:
                    patch.setattr(migration_tools, "_default_psycopg2_json_codec", lambda c: False)
                    native = _table_digest(wrapper, schema, "digest_json_fixture")
                assert wrapper.options["yield_per"] == 1
            else:
                batched = _table_digest(wrapper, schema, "digest_json_fixture")
                assert wrapper.options["yield_per"] > 1
            profiles[mode] = {"seconds": time.perf_counter() - started, "peak_bytes": tracemalloc.get_traced_memory()[1], "yield_per": wrapper.options["yield_per"]}
            tracemalloc.stop()
        assert native == batched and native[0] == 3000
        assert profiles["raw_text"]["peak_bytes"] < profiles["native"]["peak_bytes"] + 4 * _DIGEST_BATCH_BYTES
        # Per-connection custom codec must retain native streaming semantics.
        from psycopg2.extras import register_default_json
        register_default_json(connection.connection.driver_connection, loads=lambda value: {"custom": value})
        assert migration_tools._default_psycopg2_json_codec(connection) is False
        assert _digest_fetch_plan(wrapper, definitions, "digest_json_fixture", [c["name"] for c in definitions]) == (1, ())
        record_property("json_digest_profiles", json.dumps(profiles))
        print("json_digest_profiles=" + json.dumps(profiles))
    engine.dispose()


def test_nested_legacy_references_are_fail_closed():
    engine = _engine()
    _create_inventory_tables(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO current_execution_row "
            "(id, entity_kind, scope_key, business_identity, payload) "
            "VALUES (1, 'production_control_journal', 'production:all', 'order:1', :payload)"
        ), {"payload": json.dumps({"basis": [{"snapshot": {"planning_read_snapshot_id": 7}}]})})

    manifest = build_manifest(engine)

    assert manifest["status"] == "blocked"
    assert any(
        finding["key"] == "planning_read_snapshot_id"
        and finding["identity"] == "order:1"
        for finding in manifest["dependencies"]["unknown"]
    )


def test_unknown_table_blocks_preflight_instead_of_being_deleted():
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE "ops_backup_X" (id INTEGER PRIMARY KEY)'))

    manifest = build_manifest(engine)

    assert manifest["status"] == "blocked"
    assert manifest["categories"]["unknown"]["ops_backup_X"]["row_count"] == 0
    assert "not classified" in manifest["categories"]["unknown"]["ops_backup_X"]["reason"]


def test_ambiguous_legacy_row_to_current_identity_blocks_preflight():
    engine = _engine()
    _create_inventory_tables(engine)
    payload_a = json.dumps({"legacy_row_id": 11})
    payload_b = json.dumps({"legacy_row_id": 11})
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO current_execution_row "
            "(id, entity_kind, scope_key, business_identity, payload) "
            "VALUES (1, 'purchase_control_journal', 'purchase:all', 'purchase:a', :payload)"
        ), {"payload": payload_a})
        connection.execute(text(
            "INSERT INTO current_execution_row "
            "(id, entity_kind, scope_key, business_identity, payload) "
            "VALUES (2, 'purchase_control_journal', 'purchase:all', 'purchase:b', :payload)"
        ), {"payload": payload_b})

    manifest = build_manifest(engine)

    assert manifest["status"] == "blocked"
    assert any(
        finding["legacy_id"] == "11" and finding["kind"] == "ambiguous"
        for finding in manifest["dependencies"]["unknown"]
    )


def test_unmapped_snapshot_reference_is_unknown_and_fail_closed():
    engine = _engine()
    _create_inventory_tables(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO current_execution_row "
            "(id, entity_kind, scope_key, business_identity, payload) "
            "VALUES (1, 'production_control_journal', 'production:all', 'order:1', :payload)"
        ), {"payload": json.dumps({"planning_read_snapshot_id": 7})})

    manifest = build_manifest(engine)

    assert manifest["status"] == "blocked"
    assert any(
        finding["key"] == "planning_read_snapshot_id"
        for finding in manifest["dependencies"]["unknown"]
    )


def test_repo_root_cli_loads_application_model_inventory(tmp_path):
    database = tmp_path / "model-inventory.db"
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE items (item_id INTEGER PRIMARY KEY)"))

    repo = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            "tools/current_execution_migration.py",
            "--database-url",
            f"sqlite:///{database}",
        ],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["categories"]["preserve"]["items"]["row_count"] == 0
    assert "items" not in manifest["categories"]["unknown"]


def test_file_cli_resolves_apply_publishers_without_pythonpath():
    repo = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import runpy; "
                "module = runpy.run_path('tools/current_execution_migration.py'); "
                "assert callable(module['publish_current_obligation_views_from_snapshots']); "
                "assert callable(module['publish_current_execution_from_generation'])"
            ),
        ],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert probe.returncode == 0, probe.stdout + probe.stderr
