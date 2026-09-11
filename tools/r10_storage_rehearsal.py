"""Local-only R10 backup/restore and physical-space rehearsal.

The rehearsal owns a disposable PostgreSQL schema.  It never writes the
public schema and never invokes a shell: pg_dump/pg_restore are passed an
argument vector, using host binaries or ``wsl.exe /usr/bin/...``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


_LEGACY_TABLES = (
    "planning_read_root_member",
    "planning_read_row",
    "planning_read_snapshot",
)
_EXPECTED_SCOPES = (
    ("production_control_journal", "production:all-live-orders"),
    ("purchase_control_journal", "purchase:all-live-plans"),
    ("mrp_result", "mrp:all-live-plans"),
    ("period_plan_execution", "period-plan:all-live-plans"),
)
_SCHEMA_PATTERN = re.compile(r"^r10_storage_[0-9a-f]{32}$")


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _validate_schema(schema: str) -> str:
    if not _SCHEMA_PATTERN.fullmatch(schema):
        raise ValueError("rehearsal schema name is not controlled")
    return schema


def _validate_dsn(dsn: str) -> None:
    if not isinstance(dsn, str) or not dsn:
        raise ValueError("a local PostgreSQL DSN is required")
    from app.r2_local_contract import validate_r2_dsn

    validate_r2_dsn(dsn)


def _tool_prefix(name: str) -> list[str]:
    host = shutil.which(name)
    if host:
        return [host]
    wsl = shutil.which("wsl.exe") or shutil.which("wsl")
    if wsl:
        return [wsl, "--", f"/usr/bin/{name}"]
    raise RuntimeError(f"{name} or wsl.exe /usr/bin/{name} is unavailable")


def _safe_connection_args(dsn: str) -> tuple[list[str], str | None]:
    """Return pg args plus password separately; never put credentials in argv."""
    url = sa.engine.make_url(dsn)
    if not url.host or not url.database:
        raise ValueError("the local DSN must include host and database")
    args = ["--host", str(url.host)]
    if url.port is not None:
        args.extend(["--port", str(url.port)])
    if url.username is not None:
        args.extend(["--username", str(url.username)])
    args.extend(["--dbname", str(url.database)])
    return args, url.password


def _tool_environment(prefix: Sequence[str], password: str | None) -> dict[str, str] | None:
    if password is None:
        return None
    environment = os.environ.copy()
    environment["PGPASSWORD"] = password
    if any(Path(part).name.lower() in {"wsl", "wsl.exe"} for part in prefix):
        existing = [part for part in environment.get("WSLENV", "").split(":") if part]
        existing = [part for part in existing if part.split("/", 1)[0] != "PGPASSWORD"]
        existing.append("PGPASSWORD/u")
        environment["WSLENV"] = ":".join(existing)
    return environment


def _run_tool(
    name: str,
    args: Sequence[str],
    *,
    dsn: str | None = None,
    stdin=None,
    stdout=subprocess.PIPE,
) -> subprocess.CompletedProcess:
    prefix = _tool_prefix(name)
    command = [*prefix, *args]
    environment = None
    if dsn is not None:
        connection_args, password = _safe_connection_args(dsn)
        command.extend(connection_args)
        environment = _tool_environment(prefix, password)
    result = subprocess.run(
        command,
        stdin=stdin,
        stdout=stdout,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{name} failed with exit code {result.returncode}")
    return result


def _tool_version(name: str) -> str:
    result = _run_tool(name, ["--version"])
    return result.stdout.decode("utf-8", errors="replace").strip()


def _load_drop_migration():
    root = Path(__file__).resolve().parents[1]
    candidates = sorted(
        (root / "backend" / "alembic" / "versions").glob(
            "20260911_03_r10_drop_planning_read_storage.py"
        )
    )
    if len(candidates) != 1:
        raise RuntimeError("20260911_03 drop migration is missing or ambiguous")
    spec = importlib.util.spec_from_file_location("r10_storage_drop", candidates[0])
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load 20260911_03 drop migration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_drop_migration(connection) -> None:
    module = _load_drop_migration()
    context = MigrationContext.configure(connection)
    module.op = Operations(context)
    module.upgrade()


def _create_seed(connection) -> None:
    statements = (
        "CREATE TABLE ledger_generation ("
        "id BIGINT PRIMARY KEY, status VARCHAR(32) NOT NULL, cutoff TIMESTAMPTZ NOT NULL)",
        "CREATE TABLE planning_truth_state ("
        "id INTEGER PRIMARY KEY, current_generation_id BIGINT NOT NULL)",
        "CREATE TABLE current_execution_scope ("
        "id BIGINT PRIMARY KEY, entity_kind VARCHAR(128) NOT NULL, "
        "scope_key VARCHAR(256) NOT NULL, source_generation_id BIGINT NOT NULL, "
        "source_revision VARCHAR(256) NOT NULL, result_ready BOOLEAN NOT NULL, summary JSONB NOT NULL)",
        "CREATE TABLE planning_read_snapshot ("
        "id BIGINT PRIMARY KEY, consumer VARCHAR(128) NOT NULL, "
        "snapshot_key VARCHAR(256) NOT NULL, ledger_generation_id BIGINT NOT NULL, "
        "cutoff TIMESTAMPTZ NOT NULL, truth_status VARCHAR(16) NOT NULL, "
        "payload JSONB NOT NULL, published_at TIMESTAMPTZ NOT NULL)",
        "CREATE TABLE planning_read_row ("
        "id BIGINT PRIMARY KEY, snapshot_id BIGINT NOT NULL REFERENCES planning_read_snapshot(id), "
        "row_key VARCHAR(256) NOT NULL, row_kind VARCHAR(64) NOT NULL, "
        "item_id INTEGER, sort_key VARCHAR(256), payload JSONB NOT NULL)",
        "CREATE TABLE planning_read_root_member ("
        "id BIGINT PRIMARY KEY, snapshot_id BIGINT NOT NULL REFERENCES planning_read_snapshot(id), "
        "row_id BIGINT NOT NULL REFERENCES planning_read_row(id), root_key VARCHAR(256) NOT NULL, "
        "root_item_id INTEGER, payload JSONB NOT NULL)",
    )
    for statement in statements:
        connection.execute(sa.text(statement))
    connection.execute(sa.text(
        "INSERT INTO ledger_generation(id,status,cutoff) "
        "VALUES (1,'accepted','2026-09-11 00:00:00+00:00')"
    ))
    connection.execute(sa.text(
        "INSERT INTO planning_truth_state(id,current_generation_id) VALUES (1,1)"
    ))
    for index, (kind, scope) in enumerate(_EXPECTED_SCOPES, start=1):
        connection.execute(
            sa.text(
                "INSERT INTO current_execution_scope "
                "(id,entity_kind,scope_key,source_generation_id,source_revision,result_ready,summary) "
                "VALUES (:id,:kind,:scope,1,:revision,:ready,:summary)"
            ),
            {
                "id": index,
                "kind": kind,
                "scope": scope,
                "revision": f"accepted:g1:{kind}",
                "ready": True,
                "summary": json.dumps({"truth_status": "accepted", "total_rows": 0}),
            },
        )

    snapshots = (
        (100, "production_control_journal", "journal:v1", {"rows": []}),
        (101, "purchase_control_journal", "journal:v1", {"rows": []}),
        (102, "mrp_result", "run:41", {"run_id": 41, "rows": []}),
        (103, "period_plan_execution", "plan=7;run=41", {
            "plan": {"id": 7}, "run_id": 41, "truth_status": "accepted",
            "summary": {"truth_status": "accepted", "total_items": 0},
            "facets": {"bom_levels": [], "flows": []},
            "plan_output_rows": [], "rows": [],
        }),
    )
    for snapshot_id, consumer, key, payload in snapshots:
        connection.execute(
            sa.text(
                "INSERT INTO planning_read_snapshot "
                "(id,consumer,snapshot_key,ledger_generation_id,cutoff,truth_status,payload,published_at) "
                "VALUES (:id,:consumer,:key,1,'2026-09-11 00:00:00+00:00','accepted',:payload,'2026-09-11 00:00:00+00:00')"
            ),
            {"id": snapshot_id, "consumer": consumer, "key": key, "payload": json.dumps(payload)},
        )
    # Enough rows to make relation-size reclamation measurable while keeping
    # the rehearsal fast and deterministic.
    for index in range(1, 81):
        snapshot_id = 100 if index % 2 else 101
        row_id = 1000 + index
        connection.execute(
            sa.text(
                "INSERT INTO planning_read_row "
                "(id,snapshot_id,row_key,row_kind,item_id,sort_key,payload) "
                "VALUES (:id,:snapshot,:key,'production',:item,:sort,:payload)"
            ),
            {
                "id": row_id,
                "snapshot": snapshot_id,
                "key": f"work-item:{index}",
                "item": index,
                "sort": f"{index:08d}",
                "payload": json.dumps({
                    "current_identity": f"work-item:{index}",
                    "planned_qty": 2,
                    "root_item_ids": [index],
                    "subject": f"subject-{index}",
                }),
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO planning_read_root_member "
                "(id,snapshot_id,row_id,root_key,root_item_id,payload) "
                "VALUES (:id,:snapshot,:row,:root,:item,:payload)"
            ),
            {
                "id": 2000 + index,
                "snapshot": snapshot_id,
                "row": row_id,
                "root": f"root:{index}",
                "item": index,
                "payload": json.dumps({"subject": f"subject-{index}"}),
            },
        )


def _relation_bytes(connection, schema: str) -> int:
    value = connection.execute(
        sa.text(
            "SELECT COALESCE(sum(pg_total_relation_size(c.oid)),0) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=:schema AND c.relname IN "
            "('planning_read_root_member','planning_read_row','planning_read_snapshot')"
        ),
        {"schema": schema},
    ).scalar_one()
    return int(value or 0)


def _subject_state(connection) -> dict[str, Any]:
    scopes = connection.execute(sa.text(
        "SELECT id,entity_kind,scope_key,source_generation_id,source_revision,result_ready "
        "FROM current_execution_scope ORDER BY id"
    )).mappings().all()
    snapshots = connection.execute(sa.text(
        "SELECT id,consumer,snapshot_key,ledger_generation_id FROM planning_read_snapshot ORDER BY id"
    )).mappings().all()
    rows = connection.execute(sa.text(
        "SELECT id,snapshot_id,row_key FROM planning_read_row ORDER BY id"
    )).mappings().all()
    roots = connection.execute(sa.text(
        "SELECT id,row_id,root_key FROM planning_read_root_member ORDER BY id"
    )).mappings().all()
    generation_id = connection.execute(sa.text(
        "SELECT current_generation_id FROM planning_truth_state WHERE id=1"
    )).scalar_one()
    return {
        "current_generation_id": int(generation_id),
        "current_scope_ids": [int(row["id"]) for row in scopes],
        "ready_scope_count": sum(bool(row["result_ready"]) for row in scopes),
        "subject_values": {
            "snapshots": [dict(row) for row in snapshots],
            "rows": [dict(row) for row in rows],
            "roots": [dict(row) for row in roots],
        },
    }


def _current_state(connection) -> dict[str, Any]:
    scopes = connection.execute(sa.text(
        "SELECT id,entity_kind,scope_key,source_generation_id,source_revision,result_ready "
        "FROM current_execution_scope ORDER BY id"
    )).mappings().all()
    generation_id = connection.execute(sa.text(
        "SELECT current_generation_id FROM planning_truth_state WHERE id=1"
    )).scalar_one()
    return {
        "current_generation_id": int(generation_id),
        "current_scope_ids": [int(row["id"]) for row in scopes],
        "ready_scope_count": sum(bool(row["result_ready"]) for row in scopes),
    }


def _restore_state(connection) -> dict[str, Any]:
    state = _subject_state(connection)
    state["legacy_tables_present"] = [
        name for name in _LEGACY_TABLES
        if name in sa.inspect(connection).get_table_names()
    ]
    return state


def _dump_and_verify(dsn: str, schema: str, dump_path: Path) -> dict[str, Any]:
    dump_args = [
        "--format=custom", f"--schema={schema}", "--no-owner", "--no-privileges",
    ]
    with dump_path.open("wb") as stream:
        _run_tool("pg_dump", dump_args, dsn=dsn, stdout=stream)
    digest = hashlib.sha256(dump_path.read_bytes()).hexdigest()
    size = dump_path.stat().st_size
    with dump_path.open("rb") as stream:
        listed = _run_tool("pg_restore", ["--list"], stdin=stream)
    entries = [line for line in listed.stdout.decode("utf-8", errors="replace").splitlines()
               if line and not line.startswith(";")]
    return {
        "format": "custom",
        "bytes": int(size),
        "sha256": digest,
        "restore_list_entries": len(entries),
        "commands": {
            "dump": ["pg_dump", *dump_args],
            "restore": ["pg_restore", "--no-owner", "--no-privileges"],
        },
    }


def _restore_dump(dsn: str, schema: str, dump_path: Path) -> None:
    with dump_path.open("rb") as stream:
        _run_tool(
            "pg_restore",
            ["--no-owner", "--no-privileges", f"--schema={schema}"],
            dsn=dsn,
            stdin=stream,
        )


def run_storage_rehearsal(dsn: str, *, output_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Run the disposable-schema R10 backup, drop, reclaim and restore proof."""
    _validate_dsn(dsn)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    schema = _validate_schema(f"r10_storage_{uuid4().hex}")
    dump_path = output / f"{schema}.dump"
    admin = sa.create_engine(dsn, poolclass=sa.pool.NullPool)
    scoped = None
    report: dict[str, Any] | None = None
    try:
        with admin.begin() as connection:
            connection.execute(sa.text(f"CREATE SCHEMA {_quote_identifier(schema)}"))
        scoped = sa.create_engine(
            dsn,
            poolclass=sa.pool.NullPool,
            connect_args={"options": f"-csearch_path={schema}"},
        )
        with scoped.begin() as connection:
            _create_seed(connection)
            before = _subject_state(connection)
            bytes_before = _relation_bytes(connection, schema)
        dump = _dump_and_verify(dsn, schema, dump_path)
        with scoped.begin() as connection:
            _run_drop_migration(connection)
            cutover = _current_state(connection)
            bytes_after = _relation_bytes(connection, schema)
            assert not set(_LEGACY_TABLES) & set(sa.inspect(connection).get_table_names())
        with admin.begin() as connection:
            connection.execute(sa.text(f"DROP SCHEMA {_quote_identifier(schema)} CASCADE"))
            # pg_restore's --schema filter restores objects in the named
            # namespace but does not replay its CREATE SCHEMA TOC entry.
            # Recreate only this disposable namespace before restore.
            connection.execute(sa.text(f"CREATE SCHEMA {_quote_identifier(schema)}"))
        _restore_dump(dsn, schema, dump_path)
        with scoped.begin() as connection:
            restored = _restore_state(connection)
        versions = {name: _tool_version(name) for name in ("pg_dump", "pg_restore")}
        report = {
            "schema": schema,
            "tools": versions,
            "before": before,
            "cutover": {**cutover, "legacy_tables_present": []},
            "reclaim": {
                "bytes_before": bytes_before,
                "bytes_after": bytes_after,
                "bytes_reclaimed": max(0, bytes_before - bytes_after),
                "metric": "pg_total_relation_size over dropped legacy relations",
            },
            "dump": dump,
            "restore": restored,
        }
        return report
    finally:
        if scoped is not None:
            scoped.dispose()
        with admin.begin() as connection:
            connection.execute(sa.text(f"DROP SCHEMA IF EXISTS {_quote_identifier(schema)} CASCADE"))
        admin.dispose()
