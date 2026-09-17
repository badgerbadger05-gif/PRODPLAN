"""Manifest-first accepted-generation GC and explicit physical reclaim plan.

This tool has two deliberately separate responsibilities:

* ``dry-run`` / ``apply`` compact and remove only explicitly classified,
  non-current accepted generation evidence.
* ``reclaim-plan`` / ``reclaim`` plan or execute physical PostgreSQL reclaim for
  a small allowlist, one table at a time.

The tool never chooses a business owner by MAX/latest heuristics.  Retention is
an explicit operational policy over accepted_at (with id as a deterministic
tie-break), while the truth pointer, BUILDING generations and FK-referenced
generations are preserved or reported as blockers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Iterable
from urllib.parse import urlparse

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine

# The CLI is also imported directly by focused tests and by operators without
# installing the backend package.  Put the repository's backend on sys.path
# before importing model metadata so known tables are classified fail-closed
# rather than silently treated as unknown.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_ROOT = _REPO_ROOT / "backend"
if _BACKEND_ROOT.is_dir() and str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

try:
    from app.models import Base as _ModelBase
    _KNOWN_MODEL_TABLES = set(_ModelBase.metadata.tables)
except Exception:  # pragma: no cover - standalone CLI still has explicit names
    _KNOWN_MODEL_TABLES = set()

class GcBlocked(RuntimeError):
    """The manifest or safety precondition is not proven."""


DEFAULT_RETAIN_ACCEPTED = 2

# These tables contain generation-scoped evidence and may be removed only after
# their inbound dependencies are checked.  Physical stock facts are purposely
# absent: GC must never delete accepted facts merely to reclaim disk.
GENERATION_TABLES = (
    "drum_capacity_gap",
    "drum_slot",
    "assembly_readiness",
    "drum_schedule",
    "assembly_queue_line",
    "shelf_projection",
    "assembly_output_allocation",
    "assembly_output_fact_decision",
    "stock_ledger_supplier_receipt_provenance",
    "stock_bin",
    "ledger_future_supply",
    "reservation_consumption_allocation",
    "replenishment_work_item",
    "ledger_build_batch",
    "reservation_event",
    "reservation_entry",
    "ledger_generation",
)

# Direct generation columns for the allowlist.  Children use the parent
# schedule's generation and are still deleted before that RESTRICT parent.
GENERATION_COLUMNS = {
    table: "ledger_generation_id"
    for table in GENERATION_TABLES
    if table not in {"drum_slot", "drum_capacity_gap", "ledger_generation"}
}

RECLAIM_TABLES = (
    "ledger_future_supply",
    "stock_bin",
    "ledger_build_batch",
    "reservation_event",
    "reservation_entry",
    "assembly_output_fact_decision",
    "stock_ledger_supplier_receipt_provenance",
    "assembly_readiness",
    "replenishment_work_item",
    "drum_capacity_gap",
    "drum_slot",
    "drum_schedule",
    "assembly_queue_line",
    "shelf_projection",
    "assembly_output_allocation",
    "reservation_consumption_allocation",
)

_DELETE_ORDER = (
    "drum_capacity_gap",
    "drum_slot",
    "assembly_readiness",
    "drum_schedule",
    "assembly_queue_line",
    "shelf_projection",
    "assembly_output_allocation",
    "assembly_output_fact_decision",
    "stock_ledger_supplier_receipt_provenance",
    "stock_bin",
    "ledger_future_supply",
    "reservation_consumption_allocation",
    "replenishment_work_item",
    "reservation_event",
    "reservation_entry",
    "ledger_build_batch",
    "ledger_generation",
)

_GENERATION_REF_NAMES = {
    "ledger_generation_id",
    "generation_id",
    "source_generation_id",
    "parent_generation_id",
}
_CURRENT_FLAG_TABLES = {
    "ledger_future_supply",
    "reservation_consumption_allocation",
    "reservation_event",
    "reservation_entry",
}
_DELETE_ORDER_INDEX = {table: index for index, table in enumerate(_DELETE_ORDER)}
_EXPLICIT_PRESERVE_TABLES = {
    "planning_truth_state",
    "current_execution_scope",
    "current_execution_row",
    "current_execution_change",
    "reservation_current_change",
    "reservation_event_archive",
    "planning_run",
    "production_plan_header",
    "production_plan_line",
    "sync_link",
    "purchase_export_batch",
    "purchase_export_line_allocation",
    "stock_ledger_entry",
}


def _quote(value: str) -> str:
    if not value.replace("_", "").isalnum() or value[:1].isdigit():
        raise GcBlocked(f"unsafe SQL identifier: {value}")
    return '"' + value.replace('"', '""') + '"'


def _in_params(prefix: str, values: Iterable[int]) -> tuple[str, dict[str, int]]:
    ids = [int(value) for value in values]
    if not ids:
        return "NULL", {}
    params = {f"{prefix}{index}": value for index, value in enumerate(ids)}
    return ", ".join(f":{key}" for key in params), params


def _json_hash(value: Any) -> str:
    encoded = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> Any:
    """Return a deterministic JSON-shaped representation.

    Dependency discovery walks database metadata and FK rows, whose order is
    not part of the manifest contract.  Every list in the emitted manifest is
    therefore sorted by its canonical JSON representation; dictionary keys are
    sorted recursively.  This keeps dry-run and apply fingerprints identical
    when PostgreSQL returns the same facts in a different order.
    """
    if isinstance(value, dict):
        return {
            key: _canonical(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, list):
        items = [_canonical(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    return value


def _assert_local_database_url(database_url: str) -> None:
    parsed = urlparse(database_url)
    if parsed.scheme.startswith("sqlite"):
        return
    if (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise GcBlocked("generation GC is local-only; database host must be loopback")


def _assert_backup_ready(connection: Connection) -> None:
    if connection.dialect.name != "postgresql":
        raise GcBlocked("generation GC apply requires PostgreSQL backup guard")
    value = connection.execute(text(
        "SELECT current_setting('prodplan.execution_projection_backup_ready', true)"
    )).scalar()
    if str(value or "").strip().lower() != "on":
        raise GcBlocked(
            "verified backup guard is absent; set "
            "prodplan.execution_projection_backup_ready=on in this session"
        )


def _accepted_rows(connection: Connection) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT id, status, accepted_at FROM ledger_generation WHERE status='accepted'"
    )).mappings().all()
    # This ordering is only a declared retention policy, never a business
    # identity or reader fallback.  NULL accepted_at is least retainable.
    return sorted(
        [dict(row) for row in rows],
        key=lambda row: (
            row["accepted_at"] is not None,
            str(row["accepted_at"] or ""),
            int(row["id"]),
        ),
        reverse=True,
    )


def _current_and_building(connection: Connection) -> tuple[int | None, list[int]]:
    current = connection.execute(text(
        "SELECT current_generation_id FROM planning_truth_state WHERE id=1"
    )).scalar()
    building = connection.execute(text(
        "SELECT id FROM ledger_generation WHERE status='building' ORDER BY id"
    )).scalars().all()
    return (int(current) if current is not None else None), [int(value) for value in building]


def _table_columns(connection: Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in inspect(connection).get_columns(table)}


def _generation_row_count(connection: Connection, table: str, candidate_ids: list[int]) -> int:
    placeholders, params = _in_params("generation", candidate_ids)
    if table in {"drum_slot", "drum_capacity_gap"}:
        parent = "drum_schedule_id"
        return int(connection.execute(text(
            f"SELECT count(*) FROM {_quote(table)} child "
            f"JOIN drum_schedule parent ON parent.id=child.{_quote(parent)} "
            f"WHERE parent.ledger_generation_id IN ({placeholders})"
        ), params).scalar_one())
    if table == "ledger_generation":
        return int(connection.execute(text(
            f"SELECT count(*) FROM ledger_generation WHERE id IN ({placeholders})"
        ), params).scalar_one())
    column = GENERATION_COLUMNS.get(table)
    if column is None:
        return 0
    return int(connection.execute(text(
        f"SELECT count(*) FROM {_quote(table)} WHERE {_quote(column)} IN ({placeholders})"
    ), params).scalar_one())


def _candidate_entry_ids(connection: Connection, candidate_ids: list[int]) -> str:
    placeholders, params = _in_params("entry_generation", candidate_ids)
    # This helper is used only to compose SQL below; callers pass the params
    # separately and never interpolate data values.
    return f"SELECT id FROM reservation_entry WHERE ledger_generation_id IN ({placeholders})"


def _target_generation_expression(table: str) -> tuple[str, str]:
    """Return generation expression and required join for a target row."""
    if table == "ledger_generation":
        return "target.id", ""
    if table in {"drum_slot", "drum_capacity_gap"}:
        return "parent.ledger_generation_id", (
            f" JOIN drum_schedule parent ON parent.id=target.drum_schedule_id"
        )
    return "target.ledger_generation_id", ""


def _classify_source(source: str, target: str) -> str:
    if source in GENERATION_TABLES:
        if target in _DELETE_ORDER_INDEX and source in _DELETE_ORDER_INDEX:
            if _DELETE_ORDER_INDEX[source] < _DELETE_ORDER_INDEX[target]:
                return "gc-order"
            return "order-blocker"
        return "gc-order"
    if source in _KNOWN_MODEL_TABLES or source in _EXPLICIT_PRESERVE_TABLES:
        return "preserve" if target == "ledger_generation" else "evidence-preserve"
    return "unknown"


def _dependency_blockers(
    connection: Connection,
    candidate_ids: list[int],
    known_tables: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Inventory refs per target generation, not as one global stop flag."""
    if not candidate_ids:
        return [], []
    inspector = inspect(connection)
    names = set(inspector.get_table_names())
    placeholders, params = _in_params("candidate", candidate_ids)
    blockers: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for source in sorted(names):
        for fk in inspector.get_foreign_keys(source):
            target = str(fk.get("referred_table") or "")
            constrained = list(fk.get("constrained_columns") or [])
            referred_columns = list(fk.get("referred_columns") or [])
            if target not in GENERATION_TABLES or source == target:
                continue
            if len(constrained) != 1 or len(referred_columns) != 1:
                item = {
                    "table": source,
                    "referred_table": target,
                    "reason": "composite FK requires manual classification",
                    "classification": "unknown",
                }
                (unknown if target != "ledger_generation" else blockers).append(item)
                continue
            source_col, referred_col = constrained[0], referred_columns[0]
            generation_expr, extra_join = _target_generation_expression(target)
            query = (
                f"SELECT {generation_expr} AS generation_id, count(*) AS rows "
                f"FROM {_quote(source)} src JOIN {_quote(target)} target "
                f"ON target.{_quote(referred_col)}=src.{_quote(source_col)}"
                f"{extra_join} WHERE {generation_expr} IN ({placeholders}) "
                f"GROUP BY {generation_expr}"
            )
            for row in connection.execute(text(query), params).mappings().all():
                generation_id = int(row["generation_id"])
                classification = _classify_source(source, target)
                item = {
                    "generation_id": generation_id,
                    "table": source,
                    "column": source_col,
                    "referred_table": target,
                    "rows": int(row["rows"]),
                    "classification": classification,
                }
                (unknown if classification == "unknown" else blockers).append(item)

    # Current owners in generation-scoped tables are evidence blockers only for
    # that table/generation. Other GC-owned evidence can still be cleaned.
    for table in GENERATION_TABLES:
        if table not in names or table in {"drum_slot", "drum_capacity_gap", "ledger_generation"}:
            continue
        if "is_current" not in _table_columns(connection, table):
            continue
        column = GENERATION_COLUMNS.get(table)
        if not column:
            continue
        query = (
            f"SELECT {_quote(column)} AS generation_id, count(*) AS rows "
            f"FROM {_quote(table)} WHERE {_quote(column)} IN ({placeholders}) "
            "AND coalesce(is_current,false)=true GROUP BY " + _quote(column)
        )
        for row in connection.execute(text(query), params).mappings().all():
            blockers.append({
                "generation_id": int(row["generation_id"]),
                "table": table,
                "column": "is_current",
                "rows": int(row["rows"]),
                "classification": "current-owner",
            })

    # A generation-shaped column without an FK is unknown for metadata. It is
    # not evidence blocker unless it explicitly points at a GC row, which
    # cannot be proven without a target FK.
    for source in sorted(names - set(GENERATION_TABLES)):
        columns = _table_columns(connection, source)
        for column in sorted(columns & _GENERATION_REF_NAMES):
            has_fk = any(
                column in (fk.get("constrained_columns") or [])
                and str(fk.get("referred_table") or "") == "ledger_generation"
                for fk in inspector.get_foreign_keys(source)
            )
            if has_fk:
                continue
            query = (
                f"SELECT {_quote(column)} AS generation_id, count(*) AS rows "
                f"FROM {_quote(source)} WHERE {_quote(column)} IN ({placeholders}) "
                f"GROUP BY {_quote(column)}"
            )
            for row in connection.execute(text(query), params).mappings().all():
                unknown.append({
                    "generation_id": int(row["generation_id"]),
                    "table": source,
                    "column": column,
                    "rows": int(row["rows"]),
                    "classification": "unknown",
                })
    return blockers, unknown


def build_gc_manifest(engine: Engine, *, retain_accepted: int = DEFAULT_RETAIN_ACCEPTED) -> dict[str, Any]:
    if int(retain_accepted) < 0:
        raise ValueError("retain_accepted must be non-negative")
    with engine.connect() as connection:
        accepted = _accepted_rows(connection)
        current_id, building_ids = _current_and_building(connection)
        policy_ids = [int(row["id"]) for row in accepted[: int(retain_accepted)]]
        preserved_ids = set(policy_ids) | set(building_ids)
        if current_id is not None:
            preserved_ids.add(current_id)
        candidate_ids = [
            int(row["id"]) for row in accepted if int(row["id"]) not in preserved_ids
        ]
        candidate_ids.sort()
        known_tables = set(inspect(connection).get_table_names())
        blockers, unknown = _dependency_blockers(connection, candidate_ids, known_tables)
        tables = {
            table: {"rows": _generation_row_count(connection, table, candidate_ids), "action": "delete"}
            for table in GENERATION_TABLES
            if table in known_tables
        }
        if "reservation_event" in tables:
            event_placeholders, event_params = _in_params("event_generation", candidate_ids)
            tables["reservation_event"]["archive_rows"] = int(connection.execute(text(
                "SELECT count(*) FROM reservation_event "
                f"WHERE ledger_generation_id IN ({event_placeholders}) AND coalesce(is_current,false)=false"
            ), event_params).scalar_one())
        metadata_blockers = [
            blocker for blocker in blockers
            if str(blocker.get("referred_table")) == "ledger_generation"
            and str(blocker.get("classification")) != "gc-order"
        ]
        metadata_blockers.extend(
            item for item in unknown
            if str(item.get("referred_table") or "ledger_generation") == "ledger_generation"
        )
        evidence_blockers = [
            blocker for blocker in blockers
            if str(blocker.get("referred_table")) != "ledger_generation"
            and str(blocker.get("classification")) != "gc-order"
        ]
        evidence_blockers.extend(
            item for item in unknown
            if str(item.get("referred_table")) not in {"ledger_generation", ""}
        )
        metadata_by_generation: dict[int, list[dict[str, Any]]] = {}
        evidence_by_table_generation: dict[str, set[int]] = {}
        for item in metadata_blockers:
            ids = [int(item["generation_id"])] if item.get("generation_id") is not None else candidate_ids
            for generation_id in ids:
                metadata_by_generation.setdefault(generation_id, []).append(item)
        for item in evidence_blockers:
            table = str(item.get("referred_table") or item.get("table") or "")
            ids = [int(item["generation_id"])] if item.get("generation_id") is not None else candidate_ids
            evidence_by_table_generation.setdefault(table, set()).update(ids)
            # Any retained generation-scoped evidence still owns an inbound
            # FK to ledger_generation; metadata cannot be removed around it.
            for generation_id in ids:
                metadata_by_generation.setdefault(generation_id, []).append(item)
        deletable_generation_ids = [
            generation_id for generation_id in candidate_ids
            if generation_id not in metadata_by_generation
        ]
        retained_metadata_generation_ids = [
            generation_id for generation_id in candidate_ids
            if generation_id not in set(deletable_generation_ids)
        ]
        evidence_delete_generation_ids = {
            table: [
                generation_id for generation_id in candidate_ids
                if generation_id not in evidence_by_table_generation.get(table, set())
            ]
            for table in GENERATION_TABLES
            if table in known_tables and table != "ledger_generation"
        }
        # Unknown generation-shaped references block metadata only. Unknown
        # FKs to a GC-owned target remain evidence blockers for that target.
        metadata_unknown = [
            item for item in unknown
            if str(item.get("referred_table") or "ledger_generation") == "ledger_generation"
        ]
        manifest = _canonical({
            "status": "ready",
            "metadata_status": "blocked" if metadata_blockers or metadata_unknown else "ready",
            "policy": {"retain_accepted": int(retain_accepted), "ordering": "accepted_at_desc,id_desc"},
            "current_generation_id": current_id,
            "building_generation_ids": building_ids,
            "policy_retained_accepted_generation_ids": policy_ids,
            "candidate_generation_ids": candidate_ids,
            "cleanup_candidate_generation_ids": candidate_ids,
            "deletable_generation_ids": deletable_generation_ids,
            "retained_metadata_generation_ids": retained_metadata_generation_ids,
            "preserved_generation_ids": sorted(preserved_ids),
            "dependency_blockers": blockers,
            "metadata_blockers_by_generation": metadata_by_generation,
            "evidence_blockers": evidence_blockers,
            "evidence_delete_generation_ids": evidence_delete_generation_ids,
            "unknown_dependencies": unknown,
            "tables": tables,
            "archive": {"owner": "reservation_event_archive", "idempotent": True},
        })
        manifest["fingerprint"] = _json_hash({
            "candidate_generation_ids": candidate_ids,
            "cleanup_candidate_generation_ids": candidate_ids,
            "deletable_generation_ids": deletable_generation_ids,
            "retained_metadata_generation_ids": retained_metadata_generation_ids,
            "preserved_generation_ids": sorted(preserved_ids),
            "dependency_blockers": blockers,
            "metadata_blockers_by_generation": metadata_by_generation,
            "evidence_blockers": evidence_blockers,
            "evidence_delete_generation_ids": evidence_delete_generation_ids,
            "unknown_dependencies": unknown,
            "tables": tables,
        })
        return manifest


def _archive_events(connection: Connection, candidate_ids: list[int]) -> int:
    placeholders, params = _in_params("archive_generation", candidate_ids)
    statement = f"""
    INSERT INTO reservation_event_archive
        (source_generation_id, source_reservation_id, business_identity,
         event_identity, origin_kind, occurrence_count,
         first_source_generation_id, last_source_generation_id)
    SELECT min(event.ledger_generation_id), min(event.reservation_id),
           coalesce(nullif(trim(entry.current_identity), ''),
                    'reservation:req:' || cast(entry.requirement_id AS text) ||
                    ':mode:' || trim(coalesce(entry.realization_mode, ''))),
           coalesce(nullif(trim(event.event_identity), ''),
                    'legacy:' || md5(concat_ws('|',
                       coalesce(nullif(trim(entry.current_identity), ''),
                                'reservation:req:' || cast(entry.requirement_id AS text) ||
                                ':mode:' || trim(coalesce(entry.realization_mode, ''))),
                       coalesce(event.event_kind, ''), coalesce(cast(event.sle_id AS text), ''),
                       trim(coalesce(event.fact_ref, '')), trim(coalesce(event.fact_line_ref, '')),
                       to_char(coalesce(event.reserved_delta, 0), 'FM999999999999990.000'),
                       to_char(coalesce(event.realized_delta, 0), 'FM999999999999990.000'),
                       trim(coalesce(event.match_rule, ''))))),
           CASE WHEN event.event_kind = 'unrealize'
                  OR coalesce(event.realized_delta, 0) < 0 THEN 'correction'
                WHEN event.sle_id IS NULL AND coalesce(event.realized_delta, 0) = 0
                  THEN 'obligation'
                ELSE 'factual' END,
           count(*), min(event.ledger_generation_id), max(event.ledger_generation_id)
      FROM reservation_event event
      JOIN reservation_entry entry ON entry.id = event.reservation_id
     WHERE event.ledger_generation_id IN ({placeholders})
       AND coalesce(event.is_current, false) = false
       AND coalesce(entry.is_current, false) = false
     GROUP BY 3, 4, 5
    ON CONFLICT (business_identity, event_identity) DO UPDATE
       SET occurrence_count = reservation_event_archive.occurrence_count + excluded.occurrence_count,
           first_source_generation_id = least(
               reservation_event_archive.first_source_generation_id,
               excluded.first_source_generation_id),
           last_source_generation_id = greatest(
               reservation_event_archive.last_source_generation_id,
               excluded.last_source_generation_id)
    """
    return int(connection.execute(text(statement), params).rowcount or 0)


def apply_gc_manifest(
    engine: Engine,
    manifest: dict[str, Any],
    *,
    writers_stopped: bool,
) -> dict[str, Any]:
    if not writers_stopped:
        raise GcBlocked("explicit writers-stopped acknowledgement is required")
    if manifest.get("status") != "ready":
        raise GcBlocked("dry-run manifest is blocked; no destructive action allowed")
    with engine.connect() as connection:
        fresh = build_gc_manifest(engine, retain_accepted=int(manifest["policy"]["retain_accepted"]))
        if fresh.get("fingerprint") != manifest.get("fingerprint"):
            raise GcBlocked("dry-run manifest is stale; rebuild it before apply")
    candidate_ids = [
        int(value) for value in manifest.get(
            "cleanup_candidate_generation_ids",
            manifest.get("candidate_generation_ids", []),
        )
    ]
    if not candidate_ids:
        return {"status": "ready", "deleted": {}, "archived": 0, "candidate_generation_ids": []}
    with engine.begin() as connection:
        _assert_backup_ready(connection)
        evidence_ids = {
            table: [int(value) for value in ids]
            for table, ids in (manifest.get("evidence_delete_generation_ids") or {}).items()
        }
        archive_ids = evidence_ids.get("reservation_event", [])
        archived = _archive_events(connection, archive_ids) if archive_ids else 0
        deleted: dict[str, int] = {}
        for table in _DELETE_ORDER:
            if table == "ledger_generation":
                continue
            if table not in manifest.get("tables", {}):
                continue
            table_ids = evidence_ids.get(table, [])
            if not table_ids:
                continue
            placeholders, params = _in_params(f"delete_{table}", table_ids)
            if table in {"drum_slot", "drum_capacity_gap"}:
                query = (
                    f"DELETE FROM {_quote(table)} child WHERE child.drum_schedule_id IN "
                    f"(SELECT id FROM drum_schedule WHERE ledger_generation_id IN ({placeholders}))"
                )
            elif table == "reservation_event":
                query = (
                    f"DELETE FROM {_quote(table)} WHERE ledger_generation_id IN ({placeholders}) "
                    "AND coalesce(is_current,false)=false"
                )
            elif table == "reservation_entry":
                query = (
                    f"DELETE FROM {_quote(table)} WHERE ledger_generation_id IN ({placeholders}) "
                    "AND coalesce(is_current,false)=false"
                )
            else:
                column = GENERATION_COLUMNS.get(table)
                if column is None:
                    continue
                query = f"DELETE FROM {_quote(table)} WHERE {_quote(column)} IN ({placeholders})"
                if table in _CURRENT_FLAG_TABLES:
                    query += " AND coalesce(is_current,false)=false"
            deleted[table] = int(connection.execute(text(query), params).rowcount or 0)
        # Never cascade a generation delete.  The fresh manifest has already
        # proved inbound dependencies are classified; PostgreSQL's RESTRICT
        # constraint remains the final race-safe guard.  Any failure rolls the
        # whole transaction back and reports the generation as retained.
        metadata_ids = [int(value) for value in manifest.get("deletable_generation_ids", [])]
        if not metadata_ids:
            return {
                "status": "ready",
                "candidate_generation_ids": candidate_ids,
                "archived": archived,
                "deleted": deleted,
                "deletable_generation_ids": [],
                "retained_metadata_generation_ids": manifest.get(
                    "retained_metadata_generation_ids", []
                ),
            }
        metadata_placeholders, metadata_params = _in_params("delete_metadata", metadata_ids)
        try:
            deleted["ledger_generation"] = int(connection.execute(text(
                f"DELETE FROM ledger_generation WHERE id IN ({metadata_placeholders})"
            ), metadata_params).rowcount or 0)
        except Exception as exc:
            raise GcBlocked(
                "candidate generations retained by an unremoved RESTRICT dependency"
            ) from exc
    return {
        "status": "ready",
        "candidate_generation_ids": candidate_ids,
        "deletable_generation_ids": manifest.get("deletable_generation_ids", []),
        "retained_metadata_generation_ids": manifest.get(
            "retained_metadata_generation_ids", []
        ),
        "archived": archived,
        "deleted": deleted,
    }


def build_reclaim_plan(
    engine: Engine,
    *,
    tables: Iterable[str] = RECLAIM_TABLES,
    available_free_bytes: int | None = None,
) -> dict[str, Any]:
    requested = list(dict.fromkeys(str(table) for table in tables))
    unknown = sorted(set(requested) - set(RECLAIM_TABLES))
    if unknown:
        raise GcBlocked("reclaim table is outside allowlist: " + ", ".join(unknown))
    with engine.connect() as connection:
        if connection.dialect.name != "postgresql":
            raise GcBlocked("physical reclaim plan requires PostgreSQL")
        inspector = inspect(connection)
        existing = set(inspector.get_table_names())
        missing = sorted(set(requested) - existing)
        if missing:
            raise GcBlocked("reclaim table is absent: " + ", ".join(missing))
        rows = []
        total = 0
        for table in requested:
            size = int(connection.execute(text(
                "SELECT pg_total_relation_size(c.oid) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=current_schema() AND c.relname=:table"
            ), {"table": table}).scalar_one())
            total += size
            rows.append({"table": table, "bytes": size})
        if available_free_bytes is None:
            raw_free = os.environ.get("PRODPLAN_AVAILABLE_FREE_BYTES")
            if raw_free:
                available_free_bytes = int(raw_free)
        if available_free_bytes is None or int(available_free_bytes) < 0:
            raise GcBlocked(
                "explicit --available-free-bytes or "
                "PRODPLAN_AVAILABLE_FREE_BYTES is required; "
                "the PostgreSQL server path may be inside Docker"
            )
        repack = shutil.which("pg_repack")
        method = "pg_repack" if repack else "vacuum_full"
        # Operations run one table at a time.  Required temporary space is the
        # largest single relation, with a conservative method-specific factor,
        # not the sum of all tables.
        factor = 2.0 if repack else 1.25
        per_table_required = [
            {"table": row["table"], "bytes": int(row["bytes"]),
             "required_free_bytes": int(int(row["bytes"]) * factor)}
            for row in rows
        ]
        required = max((row["required_free_bytes"] for row in per_table_required), default=0)
        plan = {
            "status": "ready" if int(available_free_bytes) >= int(required) else "blocked",
            "method": method,
            "tables": per_table_required,
            "estimated_relation_bytes": total,
            "estimated_required_free_bytes": required,
            "available_free_bytes": int(available_free_bytes),
            "space_factor": factor,
            "pg_repack": repack,
            "warning": "physical reclaim is outage/lock intensive and is never part of GC apply",
        }
        plan["fingerprint"] = _json_hash({
            "method": method,
            "tables": [(row["table"], row["bytes"]) for row in rows],
            "available_free_bytes": int(available_free_bytes),
        })
        return plan


def execute_reclaim(
    database_url: str,
    *,
    plan: dict[str, Any],
    writers_stopped: bool,
) -> dict[str, Any]:
    if not writers_stopped:
        raise GcBlocked("explicit writers-stopped acknowledgement is required")
    if plan.get("status") != "ready":
        raise GcBlocked("reclaim plan is blocked by insufficient free space")
    _assert_local_database_url(database_url)
    engine = create_engine(database_url, future=True)
    try:
        fresh = build_reclaim_plan(
            engine,
            tables=[str(row["table"]) for row in plan.get("tables", [])],
            available_free_bytes=int(plan["available_free_bytes"]),
        )
        if fresh.get("fingerprint") != plan.get("fingerprint"):
            raise GcBlocked(
                "reclaim plan is stale; relation sizes/method changed, rebuild it"
            )
        with engine.connect() as connection:
            _assert_backup_ready(connection)
        method = str(plan["method"])
        executed = []
        if method == "pg_repack":
            for row in plan["tables"]:
                subprocess.run(
                    [str(fresh["pg_repack"]), "--dbname", database_url, "--table", str(row["table"])],
                    check=True,
                )
                executed.append(str(row["table"]))
        else:
            for row in plan["tables"]:
                with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                    connection.exec_driver_sql(
                        f"VACUUM (FULL, ANALYZE) {_quote(str(row['table']))}"
                    )
                executed.append(str(row["table"]))
        return {"status": "ready", "method": method, "executed": executed}
    finally:
        engine.dispose()


def _load_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json_atomic(path: str, value: dict[str, Any]) -> None:
    target = Path(path)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--phase", choices=("dry-run", "apply", "reclaim-plan", "reclaim"), default="dry-run")
    parser.add_argument("--retain-accepted", type=int, default=int(os.environ.get("PRODPLAN_RETAIN_ACCEPTED", DEFAULT_RETAIN_ACCEPTED)))
    parser.add_argument("--manifest")
    parser.add_argument("--plan")
    parser.add_argument("--output", help="atomically write the manifest/plan/result JSON")
    parser.add_argument("--available-free-bytes", type=int)
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    try:
        _assert_local_database_url(args.database_url)
        engine = create_engine(args.database_url, future=True)
        if args.phase == "dry-run":
            result = build_gc_manifest(engine, retain_accepted=args.retain_accepted)
        elif args.phase == "apply":
            if not args.manifest:
                raise GcBlocked("--manifest from a successful dry-run is required")
            result = apply_gc_manifest(engine, _load_json(args.manifest), writers_stopped=args.writers_stopped)
        elif args.phase == "reclaim-plan":
            result = build_reclaim_plan(
                engine,
                available_free_bytes=args.available_free_bytes,
            )
        else:
            if not args.plan:
                raise GcBlocked("--plan from reclaim-plan is required")
            result = execute_reclaim(args.database_url, plan=_load_json(args.plan), writers_stopped=args.writers_stopped)
        if args.output:
            _write_json_atomic(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0 if result.get("status") == "ready" else 2
    except (GcBlocked, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
