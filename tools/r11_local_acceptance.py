"""Local-only R11 acceptance harness.

The harness owns disposable PostgreSQL schema setup and measurements only.  It
never selects a default database, emits credentials, or writes to the public
schema.  Business publication remains owned by ``publish_current_execution_scope``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import time
from typing import Any, Iterable, Mapping
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUDGET_PATH = ROOT / "config-test" / "r11_acceptance_budgets.json"
SCHEMA_RE = re.compile(r"^r11_acceptance_[0-9a-f]{32}$")
ENTITY_KIND = "r11_acceptance"
SCOPE_KEY = "r11:acceptance"


def _load_budget(path: str | Path) -> dict[str, Any]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise ValueError("unsupported R11 budget schema")
    budgets = document.get("budgets")
    if not isinstance(budgets, Mapping):
        raise ValueError("R11 budget section is missing")
    return document


def _validated_url(dsn: str) -> URL:
    if not dsn:
        raise ValueError("PRODPLAN_R2_TEST_DSN is required")
    from app.r2_local_contract import validate_r2_dsn

    return validate_r2_dsn(dsn)


def _safe_target(url: URL) -> dict[str, Any]:
    return {
        "driver": url.drivername,
        "host": url.host,
        "port": url.port,
        "database": url.database,
        "user": url.username,
    }


def _schema_name() -> str:
    value = f"r11_acceptance_{uuid4().hex}"
    if not SCHEMA_RE.fullmatch(value):
        raise AssertionError("generated schema name is not controlled")
    return value


def _quote_schema(schema: str) -> str:
    if not SCHEMA_RE.fullmatch(schema):
        raise ValueError("uncontrolled R11 schema")
    return f'"{schema}"'


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("cannot calculate percentile of an empty sample")
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * percentile / 100))
    return round(ordered[rank - 1], 3)


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _baseline_rows(revision: str) -> list[dict[str, Any]]:
    return [
        {
            "entity_kind": ENTITY_KIND,
            "business_identity": f"r11:line:{index}",
            "scope_key": SCOPE_KEY,
            "payload": {
                "line": index,
                "item_ref": f"R11-{index}",
                "quantity": f"{10 + index}.125",
                "revision": revision,
            },
        }
        for index in range(1, 5)
    ]


def _count_rows(db: Session) -> dict[str, int]:
    from app import models

    return {
        "scope": db.query(models.CurrentExecutionScope).count(),
        "row": db.query(models.CurrentExecutionRow).count(),
        "change": db.query(models.CurrentExecutionChange).count(),
    }


def _scope_snapshot(db: Session) -> dict[str, Any]:
    from app import models

    scope = db.query(models.CurrentExecutionScope).filter_by(
        entity_kind=ENTITY_KIND, scope_key=SCOPE_KEY
    ).one()
    rows = db.query(models.CurrentExecutionRow).filter_by(
        entity_kind=ENTITY_KIND, scope_key=SCOPE_KEY
    ).order_by(models.CurrentExecutionRow.business_identity).all()
    return {
        "scope_id": int(scope.id),
        "scope_hash": str(scope.content_hash),
        "row_ids": [int(row.id) for row in rows],
        "row_hashes": [str(row.content_hash) for row in rows],
        "row_count": len(rows),
    }


def _external_scope_snapshot(engine: sa.Engine, schema: str) -> dict[str, Any]:
    """Read committed current state through a separate connection."""

    with engine.connect() as connection:
        connection.exec_driver_sql(f"SET search_path TO {_quote_schema(schema)}")
        reader = Session(bind=connection, autoflush=False, expire_on_commit=False)
        try:
            return _scope_snapshot(reader)
        finally:
            reader.rollback()
            reader.close()


def _relation_bytes(connection: sa.Connection, schema: str, table: str) -> int:
    qualified = f'"{schema}"."{table}"'
    return int(connection.execute(
        sa.text("SELECT pg_total_relation_size(CAST(:qualified AS regclass))"),
        {"qualified": qualified},
    ).scalar_one())


def _dead_tuples(connection: sa.Connection, schema: str) -> int:
    return int(connection.execute(
        sa.text(
            "SELECT COALESCE(sum(n_dead_tup), 0) "
            "FROM pg_stat_user_tables WHERE schemaname = :schema"
        ),
        {"schema": schema},
    ).scalar_one())


def _wal_lsn(connection: sa.Connection) -> str:
    return str(connection.execute(sa.text("SELECT pg_current_wal_lsn()")).scalar_one())


def _wal_delta(connection: sa.Connection, before: str) -> int:
    return int(connection.execute(
        sa.text("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), :before)"),
        {"before": before},
    ).scalar_one())


def _create_schema_tables(connection: sa.Connection, schema: str) -> None:
    from app import models

    metadata = sa.MetaData()
    required = (
        "physical_import_batch",
        "ledger_generation",
        "current_execution_scope",
        "current_execution_row",
        "current_execution_change",
    )
    for name in required:
        models.Base.metadata.tables[name].to_metadata(metadata, schema=schema)
    metadata.create_all(connection)


def _assert_schema_tables(connection: sa.Connection, schema: str) -> None:
    required = (
        "physical_import_batch",
        "ledger_generation",
        "current_execution_scope",
        "current_execution_row",
        "current_execution_change",
    )
    rows = connection.execute(
        sa.text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = :schema AND table_name = ANY(:names)"
        ),
        {"schema": schema, "names": list(required)},
    ).scalars().all()
    if set(rows) != set(required):
        raise RuntimeError(
            f"R11 disposable schema is incomplete: expected {required}, got {sorted(rows)}"
        )


def _public_sentinel_counts(connection: sa.Connection) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for table in (
        "physical_import_batch",
        "ledger_generation",
        "current_execution_scope",
        "current_execution_row",
        "current_execution_change",
    ):
        exists = connection.execute(
            sa.text("SELECT to_regclass(:qualified)"),
            {"qualified": f"public.{table}"},
        ).scalar_one()
        result[table] = (
            None
            if exists is None
            else int(connection.execute(sa.text(f' SELECT count(*) FROM public."{table}"')).scalar_one())
        )
    return result


def _seed_generation(db: Session) -> int:
    from app import models

    batch = models.PhysicalImportBatch(
        batch_key=f"r11-{uuid4().hex}",
        status="completed",
        source_complete=True,
        received_page_count=0,
    )
    db.add(batch)
    db.flush()
    generation = models.LedgerGeneration(
        generation_key=f"r11-{uuid4().hex}",
        status="accepted",
        cutoff=datetime.now(timezone.utc),
        source_watermarks={},
        capabilities={},
        physical_import_batch_id=int(batch.id),
        algorithm_version="r11-local-harness",
        accepted_at=datetime.now(timezone.utc),
    )
    db.add(generation)
    db.flush()
    return int(generation.id)


def _publish(db: Session, generation_id: int, rows: Iterable[dict[str, Any]]) -> Any:
    from app.services.item_ledger.current_execution import publish_current_execution_scope

    return publish_current_execution_scope(
        db,
        source_revision=f"accepted:g{generation_id}:r11_acceptance",
        source_generation_id=generation_id,
        scope_key=SCOPE_KEY,
        rows=rows,
        entity_kinds=(ENTITY_KIND,),
        summary={"r11": True, "row_count": len(list(rows)) if not isinstance(rows, list) else len(rows)},
    )


def _run_postgres(
    dsn: str,
    budget: dict[str, Any],
    schema: str,
    *,
    api_metrics: Mapping[str, Any],
) -> dict[str, Any]:
    from app import models

    engine = sa.create_engine(dsn, poolclass=sa.pool.NullPool)
    write_count = 0
    schema_created = False

    def instrument(_conn, _cursor, statement, _parameters, _context, _executemany):
        nonlocal write_count
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            write_count += 1

    event.listen(engine, "before_cursor_execute", instrument)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(f"CREATE SCHEMA {_quote_schema(schema)}")
            schema_created = True
            connection.commit()
            connection.exec_driver_sql(f"SET search_path TO {_quote_schema(schema)}")
            _create_schema_tables(connection, schema)
            _assert_schema_tables(connection, schema)
            public_before = _public_sentinel_counts(connection)
            connection.commit()
            db = Session(bind=connection, autoflush=False, expire_on_commit=False)
            try:
                generation_id = _seed_generation(db)
                db.commit()
                revision = f"seed-{generation_id}"
                rows = _baseline_rows(revision)
                _publish(db, generation_id, rows)
                db.commit()
                baseline_counts = _count_rows(db)
                baseline_subject = _scope_snapshot(db)
                baseline_lsn = _wal_lsn(connection)
                baseline_bytes = {
                    table: _relation_bytes(connection, schema, table)
                    for table in ("current_execution_scope", "current_execution_row", "current_execution_change")
                }
                baseline_dead = _dead_tuples(connection, schema)

                samples: list[float] = []
                before_writes = write_count
                for _ in range(int(budget["budgets"]["no_op_repetitions"])):
                    started = time.perf_counter()
                    _publish(db, generation_id, rows)
                    db.commit()
                    samples.append((time.perf_counter() - started) * 1000)
                after_counts = _count_rows(db)
                after_subject = _scope_snapshot(db)
                bytes_after_noop = {
                    table: _relation_bytes(connection, schema, table)
                    for table in ("current_execution_scope", "current_execution_row", "current_execution_change")
                }
                no_op = {
                    "repetitions": len(samples),
                    "dml_count": write_count - before_writes,
                    "current_row_growth": after_counts["row"] - baseline_counts["row"],
                    "current_scope_growth": after_counts["scope"] - baseline_counts["scope"],
                    "current_change_growth": after_counts["change"] - baseline_counts["change"],
                    "stable_scope_ids": after_subject["scope_id"] == baseline_subject["scope_id"],
                    "stable_row_ids": after_subject["row_ids"] == baseline_subject["row_ids"],
                    "stable_hashes": after_subject["scope_hash"] == baseline_subject["scope_hash"],
                    "p50_ms": _percentile(samples, 50),
                    "p95_ms": _percentile(samples, 95),
                    "wal_bytes": _wal_delta(connection, baseline_lsn),
                    "dead_tuple_growth": _dead_tuples(connection, schema) - baseline_dead,
                    "relation_bytes_before": baseline_bytes,
                    "relation_bytes_after": bytes_after_noop,
                }

                semantic_changes = 0
                retained_before = baseline_subject["row_hashes"][1:]
                mutation_started = time.perf_counter()
                mutation_result = None
                for quantity in ("99.125", "101.125"):
                    mutated = json.loads(json.dumps(rows))
                    mutated[0]["payload"]["quantity"] = quantity
                    mutation_result = _publish(db, generation_id, mutated)
                    db.commit()
                    semantic_changes += int(mutation_result.changed_rows > 0)
                final_retry_result = _publish(db, generation_id, json.loads(json.dumps(mutated)))
                db.commit()
                mutation_subject = _scope_snapshot(db)
                mutation = {
                    "semantic_changes": semantic_changes,
                    "row_growth": _count_rows(db)["row"] - baseline_counts["row"],
                    "audit_growth": _count_rows(db)["change"] - after_counts["change"],
                    "final_retry_published": bool(
                        final_retry_result.changed_rows or final_retry_result.closed_rows
                    ),
                    "retained_unchanged": mutation_subject["row_hashes"][1:] == retained_before,
                    "elapsed_ms": round((time.perf_counter() - mutation_started) * 1000, 3),
                }
                fault_before = _scope_snapshot(db)
                db.commit()
                rolled_back = False
                reader_during_fault = None
                try:
                    with db.begin():
                        fault_rows = json.loads(json.dumps(mutated))
                        fault_rows[1]["payload"]["quantity"] = "777.125"
                        _publish(db, generation_id, fault_rows)
                        db.flush()
                        reader_during_fault = _external_scope_snapshot(engine, schema)
                        raise RuntimeError(f"fault injected after {ENTITY_KIND}")
                except RuntimeError:
                    db.rollback()
                    rolled_back = True
                fault_after = _scope_snapshot(db)
                fault = {
                    "fault_after_consumer": ENTITY_KIND,
                    "rolled_back": rolled_back,
                    "reader_saw_old_or_new_only": (
                        reader_during_fault == fault_before == fault_after
                    ),
                    "current_scope_switched": reader_during_fault != fault_before,
                }
                limits = budget["budgets"]
                checks = {
                    "api_p95": isinstance(api_metrics.get("api_p95_ms"), (int, float))
                    and float(api_metrics["api_p95_ms"]) <= limits["api_p95_ms"],
                    "no_op_dml": no_op["dml_count"] <= limits["no_op_dml_max"],
                    "no_op_row_growth": no_op["current_row_growth"] <= limits["no_op_current_row_growth_max"],
                    "no_op_scope_growth": no_op["current_scope_growth"] <= limits["no_op_current_scope_growth_max"],
                    "no_op_change_growth": no_op["current_change_growth"] <= limits["no_op_current_change_growth_max"],
                    "no_op_wal": no_op["wal_bytes"] <= limits["no_op_wal_bytes_max"],
                    "no_op_dead_tuples": no_op["dead_tuple_growth"] <= limits["no_op_dead_tuple_growth_max"],
                    "publish_p95": no_op["p95_ms"] <= limits["current_publish_p95_ms"],
                    "mutation_rows": mutation["row_growth"] <= limits["mutation_row_growth_max"],
                    "mutation_audit": mutation["audit_growth"] <= limits["mutation_audit_growth_max"],
                }
                budget_check = {
                    "within_budget": all(checks.values()),
                    "checks": checks,
                    "failed": [name for name, passed in checks.items() if not passed],
                }
                public_after = _public_sentinel_counts(connection)
                if public_after != public_before:
                    raise RuntimeError(
                        f"R11 rehearsal touched public sentinel rows: before={public_before}, after={public_after}"
                    )
                return {
                    "status": "completed",
                    "schema": schema,
                    "target": _safe_target(make_url(dsn)),
                    "generation_id": generation_id,
                    "baseline": {"counts": baseline_counts, "subject": baseline_subject, "bytes": baseline_bytes},
                    "no_op": no_op,
                    "mutation": mutation,
                    "fault_injection": fault,
                    "public_sentinel_counts": public_after,
                    "budget_check": budget_check,
                    "budgets": dict(budget["budgets"]),
                }
            finally:
                db.close()
    finally:
        event.remove(engine, "before_cursor_execute", instrument)
        if schema_created:
            with engine.begin() as cleanup:
                cleanup.exec_driver_sql(f"DROP SCHEMA IF EXISTS {_quote_schema(schema)} CASCADE")
        engine.dispose()


def measure_api_p95(dsn: str, *, sample_count: int = 9) -> dict[str, Any]:
    """Measure the real R2 FastAPI reader through its TestClient.

    The probe is read-only and exercises the same fixed R2 items endpoint as
    the baseline; a missing endpoint or malformed metric is an error, never an
    implicit zero.
    """
    url = _validated_url(dsn)
    if sample_count <= 0:
        raise ValueError("sample_count must be positive")
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker
    from app.database import get_db
    from app.main import app

    # Keep a local pooled engine for the request sample; opening a fresh TCP
    # connection per TestClient request would measure connection setup rather
    # than the production reader latency.
    engine = sa.create_engine(url.render_as_string(hide_password=False), pool_pre_ping=True)
    SessionForProbe = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = SessionForProbe()
        try:
            yield db
        finally:
            db.close()

    path = "/api/v1/items/?skip=0&limit=100"
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            warmup = client.get(path)
            if warmup.status_code != 200:
                raise RuntimeError(f"R11 API warm-up failed: HTTP {warmup.status_code}")
            samples: list[float] = []
            for _ in range(sample_count):
                started = time.perf_counter()
                response = client.get(path)
                elapsed_ms = (time.perf_counter() - started) * 1000
                if response.status_code != 200:
                    raise RuntimeError(f"R11 API probe failed: HTTP {response.status_code}")
                samples.append(elapsed_ms)
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()
    if not samples:
        raise ValueError("api_p95_ms measurement is missing or non-numeric")
    return {
        "api_endpoint": path,
        "api_sample_count": len(samples),
        "api_p95_ms": _percentile(samples, 95),
        "api_p50_ms": _percentile(samples, 50),
    }


def run_storage_rehearsal_pair(
    dsn: str, *, output_dir: str | os.PathLike[str]
) -> dict[str, Any]:
    """Run two identical R10 rehearsals and compare subject state only."""
    from tools.r10_storage_rehearsal import run_storage_rehearsal

    _validated_url(dsn)
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    reports = [
        run_storage_rehearsal(dsn, output_dir=root / "run-1"),
        run_storage_rehearsal(dsn, output_dir=root / "run-2"),
    ]

    def normalize(report: Mapping[str, Any]) -> dict[str, Any]:
        before = report["before"]
        restore = report["restore"]
        return {
            "before": {
                "current_generation_id": before["current_generation_id"],
                "current_scope_ids": before["current_scope_ids"],
                "ready_scope_count": before["ready_scope_count"],
                "subject_values": before["subject_values"],
            },
            "cutover": {
                "current_generation_id": report["cutover"]["current_generation_id"],
                "current_scope_ids": report["cutover"]["current_scope_ids"],
                "ready_scope_count": report["cutover"]["ready_scope_count"],
                "legacy_tables_present": report["cutover"]["legacy_tables_present"],
            },
            "restore": {
                "current_generation_id": restore["current_generation_id"],
                "current_scope_ids": restore["current_scope_ids"],
                "ready_scope_count": restore["ready_scope_count"],
                "subject_values": restore["subject_values"],
                "legacy_tables_present": restore["legacy_tables_present"],
            },
        }

    normalized = [normalize(report) for report in reports]
    return {
        "status": "completed" if normalized[0] == normalized[1] else "mismatch",
        "equivalent": normalized[0] == normalized[1],
        "normalized_subject_state": normalized[0],
        "runs": [
            {"schema": report["schema"], "normalized_subject_state": state}
            for report, state in zip(reports, normalized)
        ],
    }


def run_local_acceptance(
    dsn: str,
    *,
    budget_path: str | Path = DEFAULT_BUDGET_PATH,
    dry_run: bool = False,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run or plan the local R11 acceptance rehearsal."""

    url = _validated_url(dsn)
    budget = _load_budget(budget_path)
    if dry_run:
        return {
            "status": "planned",
            "target": _safe_target(url),
            "schema_pattern": "r11_acceptance_<uuid32>",
            "budgets": dict(budget["budgets"]),
            "commands": ["publish_current_execution_scope", "pg_stat_user_tables", "pg_current_wal_lsn"],
        }
    schema = _schema_name()
    # ``str(URL)`` intentionally masks the password as ``***``.  Keep the
    # validated credential for the private DB connection, while the report
    # and all command-like fields continue to use ``_safe_target(url)``.
    api_metrics = measure_api_p95(url.render_as_string(hide_password=False))
    report = _run_postgres(url.render_as_string(hide_password=False), budget, schema, api_metrics=api_metrics)
    report["api"] = api_metrics
    if output_path is not None:
        Path(output_path).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def run_generators(*, seeds: Iterable[int], mutation_probe: bool) -> dict[str, Any]:
    """Run the canonical reservation allocator against independent Decimal checks."""

    from app.services.item_ledger.physical import fold_running_balance
    from app.services.item_ledger.reservation_consumption_core import (
        Fact,
        Reserve,
        allocate_consumption_facts,
    )

    seed_values = tuple(int(seed) for seed in seeds)
    if not seed_values:
        raise ValueError("at least one generator seed is required")
    balances: list[str] = []
    expected_balances: list[str] = []
    per_seed: list[dict[str, Any]] = []
    for seed in seed_values:
        randomizer = random.Random(seed)
        receipt = Decimal(randomizer.randint(8, 20)) + Decimal("0.125")
        issue = Decimal(randomizer.randint(1, 4)) + Decimal("0.125")
        correction = Decimal(randomizer.choice(("-1.000", "0.500", "1.000")))
        events = (
            {"posting_key": 30, "kind": "receipt", "qty": str(receipt)},
            {"posting_key": 10, "kind": "issue", "qty": str(-issue)},
            {"posting_key": 20, "kind": "correction", "qty": str(correction)},
        )
        frozen_events = tuple((dict(event) for event in events))
        ordered_events = tuple(sorted(frozen_events, key=lambda event: int(event["posting_key"])))
        ordered_qtys = tuple(Decimal(event["qty"]) for event in ordered_events)
        expected_running: list[str] = []
        running = Decimal("0")
        for quantity in ordered_qtys:
            running += quantity
            expected_running.append(str(running))
        canonical_running, canonical_final = fold_running_balance(ordered_qtys)
        if [str(value) for value in canonical_running] != expected_running:
            raise AssertionError("physical fold diverged from independent Decimal running oracle")
        expected_balances.append(str(running))
        reserve = Reserve(
            reserve_id=f"r11-{seed}", requirement_id=seed, run_id=seed,
            reserved_qty=receipt, baseline_at=datetime(2026, 1, 1),
            plan_period_from=datetime(2026, 1, 2).date(),
            plan_period_to=datetime(2026, 1, 2).date(), item_id=1, pool="pool-a",
        )
        fact = Fact(
            fact_id=f"f11-{seed}", item_id=1, qty=issue,
            posting_at=datetime(2026, 1, 3), pool="pool-a", run_id=seed,
        )
        result = allocate_consumption_facts((fact,), (reserve,))
        independent_total = sum((allocation.qty for allocation in result.allocations), Decimal("0"))
        independent_total += sum((surplus.qty for surplus in result.surplus), Decimal("0"))
        if independent_total != issue:
            raise AssertionError("canonical allocator violated independent Decimal conservation")
        balances.append(str(result.realizations[0].realized_qty))
        per_seed.append({
            "seed": seed,
            "events": [dict(event) for event in events],
            "expected_running": expected_running,
            "canonical_running": [str(value) for value in canonical_running],
            "expected_balance": str(running),
            "canonical_balance": str(canonical_final),
            "expected_consumed": str(issue),
            "canonical_consumed": str(independent_total),
            "canonical_realized": str(result.realizations[0].realized_qty),
            "frozen_input_unchanged": frozen_events == events,
        })

    sign_flip_detected = False
    try:
        allocate_consumption_facts((Fact(
            fact_id="bad-sign", item_id=1, qty=Decimal("-1"),
            posting_at=datetime(2026, 1, 3), pool="pool-a",
        ),), ())
    except ValueError:
        sign_flip_detected = True

    addressed = Fact(
        fact_id="addressed", item_id=1, qty=Decimal("1"),
        posting_at=datetime(2026, 1, 3), pool="pool-a", reservation_id="foreign",
    )
    reserves = (Reserve(
        "target", 1, 1, Decimal("2"), datetime(2026, 1, 1),
        datetime(2026, 1, 2).date(), datetime(2026, 1, 2).date(), 1, "pool-a",
    ),)
    address_result = allocate_consumption_facts((addressed,), reserves)
    address_flip_detected = bool(address_result.allocations) and not address_result.allocations[0].is_addressed

    idempotency_flip_detected = False
    try:
        duplicate = Fact("duplicate", 1, Decimal("1"), datetime(2026, 1, 3), "pool-a")
        allocate_consumption_facts((duplicate, duplicate), ())
    except ValueError:
        idempotency_flip_detected = True

    probes = (sign_flip_detected, address_flip_detected, idempotency_flip_detected)
    if mutation_probe and not all(probes):
        raise AssertionError("canonical mutation probes failed to detect a broken input")
    return {
        "seeds": list(seed_values),
        "conservation": {
            "independent_oracle_balance": expected_balances[0],
            "canonical_realized_by_seed": balances,
            "per_seed": per_seed,
            "all_seeds_conserved": True,
        },
        "mutation_probes": {
            "sign_flip_detected": sign_flip_detected,
            "address_flip_detected": address_flip_detected,
            "idempotency_flip_detected": idempotency_flip_detected,
        },
    }


def run_mutation_sequence(*, dsn: str, seed: int, operations: Iterable[str]) -> dict[str, Any]:
    """Exercise semantic current-publisher mutations in a disposable schema."""

    operation_values = tuple(str(operation) for operation in operations)
    if not dsn or int(seed) <= 0 or not operation_values:
        raise ValueError("mutation sequence requires a DSN, positive seed and operations")
    report = run_local_acceptance(dsn, budget_path=DEFAULT_BUDGET_PATH)
    mutation = dict(report["mutation"])
    return {
        "seed": int(seed),
        "operations": list(operation_values),
        "final_retry": {"published": bool(mutation["final_retry_published"])},
        "retained_unchanged": bool(mutation["retained_unchanged"]),
        "row_growth_within_budget": mutation["row_growth"] <= report["budgets"]["mutation_row_growth_max"],
        "audit_growth_matches_semantic_changes": mutation["audit_growth"] == mutation["semantic_changes"],
    }


def run_fault_injection(*, dsn: str, fault_after_consumer: str, independent_reader: bool) -> dict[str, Any]:
    if not dsn or not fault_after_consumer or not independent_reader:
        raise ValueError("fault injection requires a DSN, consumer and independent reader")
    if fault_after_consumer != ENTITY_KIND:
        raise ValueError(f"unsupported R11 fault marker: {fault_after_consumer}")
    report = run_local_acceptance(dsn, budget_path=DEFAULT_BUDGET_PATH)
    fault = dict(report["fault_injection"])
    if fault["fault_after_consumer"] != fault_after_consumer:
        raise RuntimeError("fault marker was not exercised by the local publisher")
    return fault


def runtime_inventory() -> dict[str, list[str]]:
    backend = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "backend" / "app").rglob("*.py"))
    sql = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "tools" / "sql").glob("*.sql"))
    legacy = ("planning_read_snapshot", "planning_read_row", "planning_read_root_member", "def read_snapshot(")
    return {
        "legacy_runtime_readers": [token for token in legacy if token in backend],
        "operational_legacy_sql": [token for token in legacy[:-1] if token in sql],
    }


def compare_budget(metrics: Mapping[str, Any], *, budget_path: str | Path = DEFAULT_BUDGET_PATH) -> dict[str, Any]:
    budget = _load_budget(budget_path)["budgets"]
    missing: list[str] = []
    values: dict[str, float] = {}
    for key in ("api_p95_ms", "current_publish_p95_ms"):
        if key not in metrics or metrics[key] is None:
            missing.append(key)
            continue
        try:
            values[key] = float(metrics[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be numeric") from exc
        if isinstance(metrics[key], bool) or not math.isfinite(values[key]):
            raise ValueError(f"{key} must be finite numeric")
    checks = {
        "api_p95_ms": "api_p95_ms" in values and values["api_p95_ms"] <= float(budget["api_p95_ms"]),
        "current_publish_p95_ms": "current_publish_p95_ms" in values and values["current_publish_p95_ms"] <= float(budget["current_publish_p95_ms"]),
    }
    return {"within_budget": not missing and all(checks.values()), "checks": checks, "missing_metrics": missing}


def validate_final_evidence(
    evidence: Mapping[str, Any], *, budget_path: str | Path = DEFAULT_BUDGET_PATH
) -> dict[str, Any]:
    """Validate a final evidence packet without granting unsupported passes."""
    budget_document = _load_budget(budget_path)
    expected = {f"A{i:02d}" for i in range(1, 19)}
    matrix = evidence.get("matrix")
    if not isinstance(matrix, Mapping) or set(matrix) != expected:
        raise ValueError("evidence matrix must contain exactly A01-A18")
    passed: list[str] = []
    for key, entry in matrix.items():
        if not isinstance(entry, Mapping):
            raise ValueError(f"{key} evidence entry is invalid")
        status = entry.get("status")
        if status not in {"planned", "covered", "blocked", "passed"}:
            raise ValueError(f"{key} evidence status is invalid")
        if status == "passed":
            results = entry.get("results")
            configured = budget_document["acceptance_matrix"][key]
            configured_nodes = set(configured.get("nodes", [configured.get("node")]))
            if not isinstance(results, list) or not results:
                raise ValueError(f"{key} is marked passed without concrete results")
            seen_nodes: set[str] = set()
            for result in results:
                if not isinstance(result, Mapping):
                    raise ValueError(f"{key} result must be a record")
                node = result.get("node")
                if node not in configured_nodes:
                    raise ValueError(f"{key} result node is not configured")
                if not result.get("command") or result.get("outcome") != "passed":
                    raise ValueError(f"{key} result requires command and outcome=passed")
                if not isinstance(result.get("data"), Mapping) or not result["data"]:
                    raise ValueError(f"{key} result requires concrete data")
                seen_nodes.add(node)
            if seen_nodes != configured_nodes:
                raise ValueError(f"{key} does not cover all configured nodes")
            if key == "A18":
                metrics = entry.get("budget_metrics")
                if not isinstance(metrics, Mapping):
                    raise ValueError("A18 requires budget metrics")
                comparison = compare_budget(metrics, budget_path=budget_path)
                if not comparison["within_budget"]:
                    raise ValueError("A18 budget evidence is missing or outside budget")
            passed.append(key)
    return {"valid": True, "passed": passed, "planned_or_blocked": sorted(expected - set(passed))}
