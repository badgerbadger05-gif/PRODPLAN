"""Read-only operational inventory for the weekend migration rehearsal.

Uses the canonical R10 row digest, not a second inventory/hash algorithm.
Only the empty purchase export schema transition documented by revisions
20260911_01/02 is supported. Nonempty anchor migration needs its own proved
mapping and remains blocked by this verifier.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.current_execution_migration import _table_digest


TABLES = (
    "production_manufactures", "production_manufacture_operations",
    "production_material_issues", "production_material_issue_lines",
    "production_material_custody_event", "production_piecework_commands",
    "production_orders", "production_products", "production_order_lines",
    "sync_link", "production_day_close", "production_day_close_item",
    "purchase_export_batch", "purchase_export_line_allocation",
    "purchase_export_obligation_allocation", "production_plan_header",
    "production_plan_line",
)
# This historical alias is not a table in the source schema; capture its
# absence explicitly instead of silently omitting unavailable tables.
OPTIONAL_TABLES = frozenset({"production_order_lines"})
SOURCE_REVISION = "20260909_02"
TARGET_REVISION = "20261009_01"
PURCHASE_ADDITIONS = frozenset({
    "current_execution_scope_id", "current_execution_source_revision",
})
PURCHASE_REMOVALS = frozenset({"planning_read_snapshot_id"})
PURCHASE_TARGET_DEFINITIONS = {
    "current_execution_scope_id": {"type": "BIGINT", "nullable": False},
    "current_execution_source_revision": {"type": "VARCHAR(256)", "nullable": False},
}
ALGORITHM = "sha256-row-json-v1"


def capture_inventory(engine, *, tables=TABLES, source=None):
    """Capture all source fields in one database-enforced read-only snapshot."""
    entries = {}
    with engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
        with connection.begin():
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
            schema = inspect(connection)
            available = set(schema.get_table_names())
            if "alembic_version" not in available:
                raise ValueError("alembic_version is absent")
            revisions = list(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
            if len(revisions) != 1:
                raise ValueError("exactly one Alembic revision is required")
            for table in tables:
                if table not in available:
                    entries[table] = {"present": False}
                    continue
                definitions = schema.get_columns(table)
                columns = [str(column["name"]) for column in definitions]
                count, checksum = _table_digest(connection, schema, table, columns=columns)
                entries[table] = {
                    "present": True, "columns": columns,
                    "row_count": count, "checksum": checksum,
                    "column_definitions": {
                        str(c["name"]): {"type": str(c["type"]), "nullable": bool(c["nullable"])}
                        for c in definitions
                    },
                    "primary_key": schema.get_pk_constraint(table).get("constrained_columns") or [],
                }
                if source and source["tables"][table]["present"]:
                    projected = [c for c in source["tables"][table]["columns"] if c in columns]
                    if projected == columns:
                        projected_count, projected_checksum = count, checksum
                    else:
                        projected_count, projected_checksum = _table_digest(
                            connection, schema, table, columns=projected,
                        )
                    entries[table]["source_projection"] = {
                        "row_count": projected_count, "checksum": projected_checksum,
                    }
            database = (
                connection.execute(text("SELECT current_database()")).scalar_one()
                if connection.dialect.name == "postgresql" else engine.url.database
            )
    return {
        "inventory_version": 1, "checksum_algorithm": ALGORITHM,
        "read_only": True, "database": database,
        "revision": str(revisions[0]), "tables": entries,
    }


def _validate_inventory(inventory, tables):
    if inventory.get("inventory_version") != 1 or inventory.get("checksum_algorithm") != ALGORITHM:
        raise ValueError("unsupported inventory format/hash algorithm; recapture source")
    if set(inventory.get("tables", {})) != set(tables):
        raise ValueError("inventory does not contain the complete operational allowlist")
    for table, entry in inventory["tables"].items():
        if not entry.get("present"):
            if table not in OPTIONAL_TABLES:
                raise ValueError(f"required operational table absent: {table}")
            continue
        columns = entry.get("columns")
        if not columns or len(columns) != len(set(columns)):
            raise ValueError(f"invalid source columns: {table}")
        if set(entry.get("column_definitions", {})) != set(columns) or "primary_key" not in entry:
            raise ValueError(f"incomplete source schema evidence: {table}")
        if not isinstance(entry.get("row_count"), int) or entry["row_count"] < 0:
            raise ValueError(f"invalid source row count: {table}")
        checksum = entry.get("checksum", "")
        if len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise ValueError(f"invalid source checksum: {table}")


def capture_before(engine):
    """Validated source baseline; call before permitting the migration runner."""
    before = capture_inventory(engine)
    _validate_inventory(before, TABLES)
    if before["revision"] != SOURCE_REVISION:
        raise ValueError("unexpected source revision")
    return before


def verify_preservation(engine, before, *, tables=TABLES):
    """Compare actual source columns; every unexplained difference is blocked."""
    _validate_inventory(before, tables)
    after = capture_inventory(engine, tables=tables, source=before)
    findings = []
    checks = {}
    transition = (before["revision"], after["revision"]) == (SOURCE_REVISION, TARGET_REVISION)
    if before["database"] != after["database"]:
        findings.append({"reason": "database identity changed"})
    if not transition:
        findings.append({"reason": "unproved schema revision pair", "before": before["revision"], "after": after["revision"]})
    for table in tables:
        source = before["tables"][table]
        target = after["tables"][table]
        if not source["present"]:
            passed = not target["present"]
            checks[table] = {"preserved": passed, "source_absent": True}
            if not passed:
                findings.append({"table": table, "reason": "previously absent operational table appeared"})
            continue
        if not target["present"]:
            checks[table] = {"preserved": False}
            findings.append({"table": table, "reason": "operational table disappeared"})
            continue
        actual = set(target["columns"])
        removed = set(source["columns"]) - actual
        added = actual - set(source["columns"])
        empty_purchase_rule = (
            transition and table == "purchase_export_batch"
            and source["row_count"] == 0 and target["row_count"] == 0
            and removed == PURCHASE_REMOVALS and added == PURCHASE_ADDITIONS
            and all(target["column_definitions"][c] == definition for c, definition in PURCHASE_TARGET_DEFINITIONS.items())
        )
        if (removed or added) and not empty_purchase_rule:
            checks[table] = {"preserved": False}
            findings.append({"table": table, "reason": "unproved schema change (nonempty anchor transformation is unsupported)", "removed": sorted(removed), "added": sorted(added)})
            continue
        changed_definitions = [
            c for c in source["columns"] if c in actual
            and source["column_definitions"][c] != target["column_definitions"][c]
        ]
        if changed_definitions or source["primary_key"] != target["primary_key"]:
            checks[table] = {"preserved": False}
            findings.append({"table": table, "reason": "unproved column type/nullability or primary key change", "columns": changed_definitions})
            continue
        count = target["source_projection"]["row_count"]
        checksum = target["source_projection"]["checksum"]
        passed = count == source["row_count"] and checksum == source["checksum"]
        checks[table] = {"preserved": passed, "row_count": count, "checksum": checksum}
        if empty_purchase_rule:
            checks[table]["schema_rule"] = "20260911_01/02: empty purchase export anchor transition"
        if not passed:
            findings.append({"table": table, "reason": "operational row count or values changed"})
    return {
        "status": "passed" if not findings else "blocked", "read_only": True,
        "before": before, "after": after, "checks": checks, "findings": findings,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture-before", "verify-after"))
    parser.add_argument("--database-url", help="Prefer DATABASE_URL environment variable to avoid credentials in shell history")
    parser.add_argument("--before", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    url = args.database_url or os.environ.get("DATABASE_URL")
    if not url:
        parser.error("set DATABASE_URL or --database-url")
    if args.action == "verify-after" and not args.before:
        parser.error("verify-after requires --before")
    if args.output.exists():
        parser.error("receipt already exists; use a new output path")
    engine = None
    try:
        engine = create_engine(url)
        if args.action == "capture-before":
            payload = capture_before(engine)
        else:
            before = json.loads(args.before.read_text(encoding="utf-8"))
            payload = verify_preservation(engine, before)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive create preserves receipts even when two runners race.
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
        print(json.dumps({"status": payload.get("status", "captured"), "receipt": str(args.output)}))
        return 0 if payload.get("status", "passed") == "passed" else 1
    except Exception as exc:
        # SQLAlchemy errors may embed connection strings/parameters. Never
        # echo their full text or write it into portable evidence.
        failure = {"status": "blocked", "read_only": True, "error_type": type(exc).__name__}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with args.output.open("x", encoding="utf-8") as output:
                json.dump(failure, output, indent=2)
        except FileExistsError:
            pass
        print(json.dumps(failure), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
