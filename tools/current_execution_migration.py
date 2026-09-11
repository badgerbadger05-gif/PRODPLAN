"""Read-only R10 preflight for the legacy-to-current execution transition.

This module deliberately produces a plan only.  It never mutates schema or
rows.  A non-ready manifest is a hard stop for the eventual migration runner.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine


_PRESERVE_REASONS = {
    "current_execution_scope": "canonical current publication manifest",
    "current_execution_row": "canonical current business result",
    "current_execution_change": "bounded audit of real current-result changes",
    "planning_live_pointer": "canonical active MRP pointer",
    "planning_run_successor": "business successor history",
    "stock_ledger_entry": "accepted physical fact ledger",
    "stock_ledger_business_identity_map": "stable fact identity mapping",
    "stock_ledger_supplier_receipt_provenance": "typed supplier provenance",
    "stock_ledger_fact_supersession": "fact correction history",
    "stock_ledger_anchor": "sealed physical anchor provenance",
    "stock_bin": "compact accepted physical projection",
    "reservation_entry": "frozen obligation and reservation owner",
    "reservation_event": "reservation/fact event history",
    "reservation_consumption_allocation": "role-separated allocation history",
    "production_plan_execution_fact": "accepted accumulated plan output",
    "production_material_custody_event": "append-only custody fact history",
    "production_material_custody_projection_manifest": "retained custody rewind manifest",
    "production_material_custody_projection": "compact current custody projection",
    "ledger_future_supply_current": "compact future-supply current owner",
    "ledger_future_supply_current_change": "future-supply change history",
    "sync_link": "durable external-action idempotency evidence",
    "purchase_export_batch": "external export/read-back history",
    "purchase_export_line_allocation": "export allocation evidence",
    "purchase_export_obligation_allocation": "obligation-to-export allocation evidence",
    "mrp_freeze_baseline": "frozen-basis provenance",
    "mrp_freeze_allocation": "frozen allocation evidence",
    "mrp_freeze_component": "frozen BOM obligation",
    "mrp_freeze_bom_node": "frozen BOM structure",
    "mrp_freeze_component_cumulative": "frozen cumulative obligation",
    "alembic_version": "schema migration history",
    "planning_truth_state": "accepted-truth pointer",
}

_MIGRATE_REASONS = {
    "ledger_generation": "generation provenance must be reduced to explicit accepted/fact history",
    "ledger_build_batch": "technical generation build evidence is not a current owner",
    "physical_import_batch": "import evidence maps to accepted fact/import progress",
    "physical_import_page": "import page evidence maps to completeness/import history",
    "planning_read_snapshot": "immutable worker evidence maps to current publication rows",
    "planning_read_row": "generation-local read rows map to stable current identities",
    "planning_read_root_member": "snapshot root membership maps to stable current scope",
    "ledger_future_supply": "generation evidence maps to compact future-supply current/history",
    "assembly_queue_line": "generation queue evidence maps to compact current queue",
    "assembly_readiness": "generation readiness evidence maps to current readiness",
    "drum_schedule": "generation drum evidence maps to current schedule",
    "drum_slot": "generation drum slots map to stable current slots/manual input",
    "drum_capacity_gap": "generation drum gaps map to stable current gaps",
    "shelf_projection": "generation shelf evidence maps to compact current shelf",
}

_DELETE_REASONS = {
    "closed_plan_snapshot": "obsolete full-generation archive; delete only after mapped evidence is verified",
}

_LEGACY_REFERENCE_KEYS = {
    "snapshot_id",
    "planning_read_snapshot_id",
    "generation_id",
    "ledger_generation_id",
    "parent_generation_id",
}
_LEGACY_ID_KEYS = {"legacy_row_id", "planning_read_row_id", "source_row_id"}


def _known_schema_tables() -> set[str]:
    names = set(_PRESERVE_REASONS) | set(_MIGRATE_REASONS) | set(_DELETE_REASONS)
    try:
        from app.models import Base

        names.update(Base.metadata.tables)
    except Exception:
        # The explicit inventory remains usable from a minimal standalone
        # environment; the manifest will still flag tables absent from it.
        pass
    return names


def _row_count(connection, table_name: str) -> int:
    # table_name comes from SQLAlchemy Inspector, never user input.
    return int(connection.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar() or 0)


def _entry(table_name: str, row_count: int, reason: str) -> dict[str, Any]:
    return {"table": table_name, "row_count": int(row_count), "reason": reason}


def _json_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


def _current_dependencies(connection, table_names: set[str]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if "current_execution_row" not in table_names:
        return findings

    rows = connection.execute(text(
        "SELECT entity_kind, scope_key, business_identity, payload "
        "FROM current_execution_row"
    )).mappings()
    stable_identities: dict[tuple[str, str, str], int] = defaultdict(int)
    legacy_map: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        identity_key = (
            str(row.get("entity_kind") or ""),
            str(row.get("scope_key") or ""),
            str(row.get("business_identity") or ""),
        )
        stable_identities[identity_key] += 1
        payload = _json_payload(row.get("payload"))
        if payload is None:
            findings.append({
                "kind": "unknown",
                "table": "current_execution_row",
                "key": "payload",
                "reason": "current row payload is not valid object JSON",
            })
            continue
        identity = identity_key[2]
        for key, value in payload.items():
            if key in _LEGACY_ID_KEYS and value not in (None, ""):
                legacy_map[f"{key}:{value}"].add(identity)
            if key in _LEGACY_REFERENCE_KEYS and value not in (None, ""):
                findings.append({
                    "kind": "unknown",
                    "table": "current_execution_row",
                    "key": key,
                    "legacy_id": str(value),
                    "identity": identity,
                    "reason": "current payload retains a generation/snapshot reference without an explicit stable mapping",
                })

    for key, count in sorted(stable_identities.items()):
        if count > 1:
            findings.append({
                "kind": "ambiguous",
                "table": "current_execution_row",
                "key": "business_identity",
                "legacy_id": key[2],
                "identities": [key[2]],
                "reason": "more than one current row owns the same scoped stable identity",
            })
    for key, identities in sorted(legacy_map.items()):
        if len(identities) > 1:
            prefix, legacy_id = key.split(":", 1)
            findings.append({
                "kind": "ambiguous",
                "table": "current_execution_row",
                "key": prefix,
                "legacy_id": legacy_id,
                "identities": sorted(identities),
                "reason": "one legacy row maps to multiple stable current identities",
            })
    return sorted(findings, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))


def build_manifest(engine: Engine) -> dict[str, Any]:
    """Build a deterministic, read-only migration manifest for one database."""

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    categories: dict[str, dict[str, dict[str, Any]]] = {
        "preserve": {},
        "migrate": {},
        "delete": {},
        "unknown": {},
    }
    known_schema_tables = _known_schema_tables()
    with engine.connect() as connection:
        for table_name in sorted(table_names):
            count = _row_count(connection, table_name)
            if table_name in _PRESERVE_REASONS:
                category, reason = "preserve", _PRESERVE_REASONS[table_name]
            elif table_name in _MIGRATE_REASONS:
                category, reason = "migrate", _MIGRATE_REASONS[table_name]
            elif table_name in _DELETE_REASONS:
                category, reason = "delete", _DELETE_REASONS[table_name]
            elif table_name in known_schema_tables:
                category, reason = "preserve", "known application table; retain until a later explicit R10 dependency rule"
            else:
                category, reason = "unknown", "table is not classified by the R10 inventory"
            categories[category][table_name] = _entry(table_name, count, reason)

        dependencies_unknown = _current_dependencies(connection, table_names)
        fk_unknown: list[dict[str, Any]] = []
        for table_name in sorted(table_names):
            for foreign_key in inspector.get_foreign_keys(table_name):
                target = str(foreign_key.get("referred_table") or "")
                if target and target not in table_names:
                    fk_unknown.append({
                        "kind": "unknown",
                        "table": table_name,
                        "key": "foreign_key",
                        "legacy_id": target,
                        "reason": "foreign-key target is absent from the database",
                    })
        dependencies_unknown.extend(fk_unknown)
        dependencies_unknown.sort(key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))

    blocked = bool(categories["unknown"] or dependencies_unknown)
    return {
        "manifest_version": 1,
        "status": "blocked" if blocked else "ready",
        "read_only": True,
        "categories": categories,
        "dependencies": {"unknown": dependencies_unknown},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    engine = create_engine(args.database_url, future=True)
    manifest = build_manifest(engine)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if manifest["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
