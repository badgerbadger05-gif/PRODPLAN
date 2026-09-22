"""Read-only R10 preflight for the legacy-to-current execution transition.

This module deliberately produces a plan only.  It never mutates schema or
rows.  A non-ready manifest is a hard stop for the eventual migration runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import bindparam, create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


# This is the deliberately small obligation hand-off surface for R10.  The
# tuple is (publisher consumer, current scope key, current entity kind).  It
# must remain aligned with the canonical publisher; it is not a second reader
# or a second publication engine.
EXPECTED_CURRENT_SCOPES = (
    ("production_control_journal", "production:all-live-orders", "production_control_journal"),
    ("purchase_control_journal", "purchase:all-live-plans", "purchase_control_journal"),
    ("mrp_result", "mrp:all-live-plans", "mrp_result"),
    ("period_plan_execution", "period-plan:all-live-plans", "period_plan_execution"),
)

# The execution compatibility owner is published from the exact accepted
# generation immediately before the obligation hand-off.  These manifests are
# required by the destructive projection cutover; they are intentionally kept
# separate from EXPECTED_CURRENT_SCOPES because the legacy obligation adapter
# publishes only the four journal consumers below.
EXPECTED_EXECUTION_SCOPES = (
    ("assembly_queue", "assembly:all-live-plans", "assembly_queue"),
    ("assembly_readiness", "assembly:all-live-plans", "assembly_readiness"),
    ("drum_schedule", "drum:all-live-plans", "drum_schedule"),
    ("drum_slot", "drum:all-live-plans", "drum_slot"),
    ("drum_gap", "drum:all-live-plans", "drum_gap"),
    ("drum_excluded", "drum:all-live-plans", "drum_excluded"),
    ("shelf_projection", "shelf:all-live-mrps", "shelf_projection"),
)


class PreflightBlocked(RuntimeError):
    """The migration may not start because its read-only preflight is unsafe."""


class PostflightBlocked(RuntimeError):
    """The canonical publication did not produce an unambiguous current view."""


_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_ROOT = _REPO_ROOT / "backend"
if _REPO_ROOT.is_dir() and str(_REPO_ROOT) not in sys.path:
    # When invoked as ``python tools/current_execution_migration.py`` Python
    # puts ``tools/`` (not the repository root) on sys.path.  The adapter is
    # a supported ``tools.*`` import and must resolve in that invocation too.
    sys.path.insert(0, str(_REPO_ROOT))
if _BACKEND_ROOT.is_dir() and str(_BACKEND_ROOT) not in sys.path:
    # The supported CLI invocation is `python tools/...` from the repository
    # root; in that mode Python does not add backend/ to import search paths.
    sys.path.insert(0, str(_BACKEND_ROOT))

# These are supported CLI dependencies, not optional preflight plugins.  Do
# not turn an import regression into a misleading "publisher unavailable"
# apply response; fail at startup with the real traceback instead.
from tools.current_execution_legacy_adapter import (
    active_current_buy_owner_count,
    fixed_planning_runs,
    legacy_journal_row_count,
    publish_current_obligation_views_from_snapshots,
)
from app.services.item_ledger.current_execution import (
    publish_current_execution_from_generation,
)
from app.services.item_ledger.current_replenishment import (
    apply_current_replenishment_for_accepted_generation,
)
from app.services.item_ledger.physical_refresh_supplier_evidence import (
    supplier_document_type_filter,
)
from app.services.item_ledger.physical_visibility import (
    visible_sle_query_for_generation,
)


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
    "closed_plan_snapshot": "immutable business closure history",
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
    "planning_run_bucket_modes": "historical bucket-mode evidence maps to canonical run semantics",
    "mrp_bucket_type_legacy": "historical bucket-type evidence maps to canonical bucket semantics",
}

_DELETE_REASONS = {}

_LEGACY_REFERENCE_KEYS = {
    "snapshot_id",
    "planning_read_snapshot_id",
    "generation_id",
    "ledger_generation_id",
    "parent_generation_id",
}
_LEGACY_ID_KEYS = {"legacy_row_id", "planning_read_row_id", "source_row_id"}

_EXECUTION_PROJECTION_TABLES = frozenset({
    "assembly_queue_line",
    "assembly_readiness",
    "drum_schedule",
    "drum_slot",
    "drum_capacity_gap",
    "shelf_projection",
})


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


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _table_digest(connection, inspector, table_name: str) -> tuple[int, str]:
    """Hash a table in bounded batches, with deterministic key ordering."""

    columns = [str(column["name"]) for column in inspector.get_columns(table_name)]
    if not columns:
        return 0, hashlib.sha256(b"").hexdigest()
    primary_key = [str(value) for value in (inspector.get_pk_constraint(table_name).get("constrained_columns") or [])]
    ordering = primary_key or columns
    select_list = ", ".join(_quote_identifier(column) for column in columns)
    order_by = ", ".join(_quote_identifier(column) for column in ordering)
    statement = text(
        f"SELECT {select_list} FROM {_quote_identifier(table_name)} "
        f"ORDER BY {order_by}"
    )
    digest = hashlib.sha256()
    count = 0
    result = connection.execution_options(stream_results=True).execute(statement)
    try:
        while True:
            batch = result.fetchmany(512)
            if not batch:
                break
            for row in batch:
                values = [row._mapping[column] for column in columns]
                encoded = json.dumps(values, ensure_ascii=False, sort_keys=False, default=str, separators=(",", ":")).encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
                count += 1
    finally:
        # PostgreSQL stream_results uses a server-side cursor.  Explicitly
        # close it so a read-only manifest cannot leave an idle transaction or
        # block cleanup of an isolated rehearsal schema.
        result.close()
    return count, digest.hexdigest()


def _entry(table_name: str, row_count: int, reason: str) -> dict[str, Any]:
    return {"table": table_name, "row_count": int(row_count), "reason": reason}


def _json_payload(value: Any) -> dict[str, Any] | list[Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return None
        return decoded if isinstance(decoded, (dict, list)) else None
    return None


def _walk_json(value: Any, path: str = ""):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield str(key), child, child_path
            yield from _walk_json(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_json(child, f"{path}[{index}]")


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
        for key, value, path in _walk_json(payload):
            if key in _LEGACY_ID_KEYS and value not in (None, ""):
                legacy_map[f"{key}:{value}"].add(identity)
            if key in _LEGACY_REFERENCE_KEYS and value not in (None, ""):
                findings.append({
                    "kind": "unknown",
                    "table": "current_execution_row",
                    "key": key,
                    "path": path,
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
            count, checksum = _table_digest(connection, inspector, table_name)
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
            entry = _entry(table_name, count, reason)
            entry["checksum"] = checksum
            entry["checksum_algorithm"] = "sha256-row-json-v1"
            categories[category][table_name] = entry

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


def _accepted_truth_generation(engine: Engine) -> int:
    """Resolve exactly the accepted generation named by the truth pointer.

    Deliberately no ``MAX(id)``/latest-row heuristic is allowed here.  The
    pointer is the only source of the migration source generation.
    """

    with engine.connect() as connection:
        pointer = connection.execute(text(
            "SELECT current_generation_id FROM planning_truth_state WHERE id = 1"
        )).scalar_one_or_none()
        if pointer is None:
            raise PreflightBlocked("planning_truth_state has no current_generation_id")
        status = connection.execute(text(
            "SELECT status FROM ledger_generation WHERE id = :generation_id"
        ), {"generation_id": int(pointer)}).scalar_one_or_none()
    if str(status or "") != "accepted":
        raise PreflightBlocked(
            f"truth pointer generation {int(pointer)} is not accepted (status={status!r})"
        )
    return int(pointer)


def _source_evidence_report(engine: Engine, generation_id: int) -> dict[str, Any]:
    """Verify legacy obligation evidence before invoking the current writer.

    Empty *rows* are valid input, but an absent/unaccepted source snapshot is
    not.  Fixed runs are selected by status alone, exactly as the runtime
    publisher does: binding the selection to the pointer generation made every
    stand with a physical pointer report "no fixed runs" and publish an empty
    ``mrp_result`` scope.

    Per-run evidence is then resolved the way the legacy reader resolves it:
    the newest accepted snapshot for that exact business key, across all
    generations.  ``planning_run.ledger_generation_id`` cannot locate it —
    an obligation refresh re-anchors every fixed run to the newest obligation
    generation, while each run's ``mrp_result`` snapshot stays at the
    generation that fixed that run.  An accepted snapshot carrying no rows is
    legitimate evidence: old fixed plans read empty today too.

    The small policy-test schema without ``planning_run`` instead must provide
    at least one accepted source snapshot for each consumer.
    """

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "planning_read_snapshot" not in table_names:
        raise PreflightBlocked("legacy obligation source planning_read_snapshot is absent")

    with engine.connect() as connection:
        fixed_runs: list[dict[str, Any]] = []
        if "planning_run" in table_names:
            fixed_runs = [dict(row) for row in connection.execute(text(
                "SELECT run_id, source_plan_id, ledger_generation_id FROM planning_run "
                "WHERE status = 'FIXED_SNAPSHOT' ORDER BY run_id"
            )).mappings().all()]

        pointer_rows = connection.execute(text(
            "SELECT id, consumer, snapshot_key, truth_status, ledger_generation_id "
            "FROM planning_read_snapshot WHERE ledger_generation_id = :generation_id "
            "ORDER BY consumer, snapshot_key, id"
        ), {"generation_id": int(generation_id)}).mappings().all()

        run_keys = [f"run:{int(row['run_id'])}" for row in fixed_runs]
        plan_keys = sorted({
            f"plan={int(row['source_plan_id'])};run={int(row['run_id'])}"
            for row in fixed_runs
            if row.get("source_plan_id") is not None
        })
        keyed_rows: list[dict[str, Any]] = []
        if run_keys or plan_keys:
            # Scoped to the exact business keys in play, never a whole-corpus
            # scan: the legacy snapshot table is one of the largest here.
            statement = text(
                "SELECT id, consumer, snapshot_key, truth_status, ledger_generation_id "
                "FROM planning_read_snapshot "
                "WHERE (consumer = 'mrp_result' AND snapshot_key IN :run_keys) "
                "   OR (consumer = 'period_plan_execution' AND snapshot_key IN :plan_keys) "
                "ORDER BY consumer, snapshot_key, ledger_generation_id, id"
            ).bindparams(
                bindparam("run_keys", expanding=True),
                bindparam("plan_keys", expanding=True),
            )
            keyed_rows = [dict(row) for row in connection.execute(statement, {
                "run_keys": run_keys or [""],
                "plan_keys": plan_keys or [""],
            }).mappings().all()]

    def _accepted(rows) -> list[dict[str, Any]]:
        return [
            dict(row) for row in rows
            if str(row.get("truth_status") or "") == "accepted"
        ]

    by_consumer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _accepted(pointer_rows):
        by_consumer[str(row["consumer"])].append(row)
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in _accepted(keyed_rows):
        by_key[(str(row["consumer"]), str(row["snapshot_key"]))].append(row)

    def _matches(consumer: str, snapshot_key: str, at_generation: int) -> list[dict[str, Any]]:
        return [
            row for row in by_consumer.get(consumer, [])
            if str(row.get("snapshot_key") or "") == snapshot_key
            and int(row.get("ledger_generation_id") or 0) == int(at_generation)
        ]

    def _latest(consumer: str, snapshot_key: str) -> dict[str, Any] | None:
        candidates = by_key.get((consumer, snapshot_key), [])
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda row: (int(row["ledger_generation_id"]), int(row["id"])),
        )

    evidence: dict[str, Any] = {}

    def require_exact(consumer: str, snapshot_key: str) -> None:
        matches = _matches(consumer, snapshot_key, int(generation_id))
        if len(matches) != 1:
            raise PreflightBlocked(
                f"source evidence for {consumer}/{snapshot_key} is missing or ambiguous "
                f"(accepted matches={len(matches)})"
            )
        evidence[consumer] = {
            "applicable": [snapshot_key],
            "snapshot_ids": [int(matches[0]["id"])],
        }

    require_exact("production_control_journal", "journal:v1")
    require_exact("purchase_control_journal", "journal:v1")

    def require_single_unscoped(consumer: str) -> None:
        # A minimal policy schema has no run/plan catalog from which to
        # derive applicability.  It must still provide one unambiguous
        # accepted source rather than allowing an empty publisher result.
        candidates = by_consumer.get(consumer, [])
        if len(candidates) != 1:
            raise PreflightBlocked(
                f"source evidence for {consumer} is missing or ambiguous "
                f"(accepted matches={len(candidates)})"
            )
        evidence[consumer] = {
            "applicable": [str(candidates[0]["snapshot_key"])],
            "snapshot_ids": [int(candidates[0]["id"])],
        }

    def require_per_key(
        consumer: str,
        keys: list[str],
        *,
        prefer_pointer: bool = False,
    ) -> None:
        """Require one accepted snapshot per business key, newest wins.

        ``prefer_pointer`` keeps the period rule: the pointer generation's own
        copy is the preferred evidence, and only a key it never republished
        falls back to the newest accepted copy elsewhere.
        """

        if not keys:
            # A database with no fixed runs/plans has a legitimately empty
            # obligation scope; no fabricated empty source is accepted.
            evidence[consumer] = {
                "applicable": [], "snapshot_ids": [], "snapshot_generations": {},
            }
            return
        selected: list[dict[str, Any]] = []
        generations: dict[str, int] = {}
        missing: list[str] = []
        for key in keys:
            chosen: dict[str, Any] | None = None
            if prefer_pointer:
                at_pointer = _matches(consumer, key, int(generation_id))
                if len(at_pointer) == 1:
                    chosen = at_pointer[0]
                elif len(at_pointer) > 1:
                    raise PreflightBlocked(
                        f"source evidence for {consumer}/{key} is ambiguous at the "
                        f"pointer generation (accepted matches={len(at_pointer)})"
                    )
            if chosen is None:
                chosen = _latest(consumer, key)
            if chosen is None:
                missing.append(key)
            else:
                selected.append(chosen)
                generations[key] = int(chosen["ledger_generation_id"])
        if missing:
            raise PreflightBlocked(
                f"source evidence for {consumer} has no accepted snapshot for {missing}"
            )
        evidence[consumer] = {
            "applicable": list(keys),
            "snapshot_ids": sorted(int(row["id"]) for row in selected),
            "snapshot_generations": generations,
        }

    if "planning_run" not in table_names:
        # Minimal policy schemas must explicitly stub evidence rather than
        # allowing a publisher to manufacture empty current scopes.
        require_single_unscoped("mrp_result")
        require_single_unscoped("period_plan_execution")
    else:
        require_per_key("mrp_result", run_keys)
        require_per_key("period_plan_execution", plan_keys, prefer_pointer=True)

    return {
        "status": "ready",
        "generation_id": int(generation_id),
        "fixed_run_ids": sorted(int(row["run_id"]) for row in fixed_runs),
        "consumers": evidence,
    }


def _migrate_purchase_export_anchors(session: Session, generation_id: int) -> dict[str, int]:
    """Move legacy purchase export evidence to the one canonical current scope."""

    # Inspect on the session's checked-out connection.  Inspecting the Engine
    # here can acquire a second SQLite in-memory connection and make the
    # uncommitted publisher rows appear to have vanished.
    inspector = inspect(session.connection())
    if "purchase_export_batch" not in set(inspector.get_table_names()):
        return {"legacy_before": 0, "legacy_after": 0, "current": 0}

    batch_columns = {column["name"] for column in inspector.get_columns("purchase_export_batch")}
    required_current = {"current_execution_scope_id", "current_execution_source_revision"}
    if not required_current <= batch_columns:
        raise PreflightBlocked(
            "purchase export batch has no complete current execution anchor columns"
        )
    # Post-cutover schemas have no legacy selector at all.  They are already
    # migrated; validate the compact current anchor rather than attempting to
    # query a dropped column.
    if "planning_read_snapshot_id" not in batch_columns:
        incomplete = session.execute(text(
            "SELECT count(*) FROM purchase_export_batch "
            "WHERE current_execution_scope_id IS NULL "
            "   OR current_execution_source_revision IS NULL"
        )).scalar_one()
        if int(incomplete or 0):
            raise PreflightBlocked(
                f"purchase export batch has {int(incomplete)} incomplete current anchors"
            )
        current = session.execute(text(
            "SELECT count(*) FROM purchase_export_batch"
        )).scalar_one()
        return {"legacy_before": 0, "legacy_after": 0, "current": int(current or 0)}

    batches = session.execute(text(
        "SELECT id, planning_read_snapshot_id, current_execution_scope_id, "
        "current_execution_source_revision "
        "FROM purchase_export_batch ORDER BY id"
    )).mappings().all()
    legacy = [row for row in batches if row["planning_read_snapshot_id"] is not None]
    for batch in legacy:
        if batch["current_execution_scope_id"] is not None or batch["current_execution_source_revision"] is not None:
            raise PreflightBlocked(
                f"purchase export batch {int(batch['id'])} has ambiguous legacy/current anchors"
            )

    scope_rows = session.execute(text(
        "SELECT id, source_generation_id, source_revision, result_ready "
        "FROM current_execution_scope "
        "WHERE entity_kind='purchase_control_journal' AND scope_key='purchase:all-live-plans'"
    )).mappings().all()
    if len(scope_rows) != 1:
        raise PreflightBlocked(
            f"purchase export migration requires exactly one canonical purchase scope, found {len(scope_rows)}"
        )
    scope = scope_rows[0]
    if not bool(scope["result_ready"]):
        raise PreflightBlocked("purchase export migration requires a ready canonical purchase scope")

    for batch in legacy:
        snapshot = session.execute(text(
            "SELECT s.id, s.consumer, s.snapshot_key, s.ledger_generation_id, s.truth_status, "
            "g.status AS generation_status "
            "FROM planning_read_snapshot s "
            "LEFT JOIN ledger_generation g ON g.id=s.ledger_generation_id "
            "WHERE s.id=:snapshot_id"
        ), {"snapshot_id": int(batch["planning_read_snapshot_id"])}).mappings().all()
        if len(snapshot) != 1:
            raise PreflightBlocked(
                f"purchase export snapshot {int(batch['planning_read_snapshot_id'])} is missing or ambiguous"
            )
        source = snapshot[0]
        if (
            str(source["consumer"] or "") != "purchase_control_journal"
            or str(source["snapshot_key"] or "") != "journal:v1"
            or str(source["truth_status"] or "") != "accepted"
            or str(source["generation_status"] or "") != "accepted"
        ):
            raise PreflightBlocked(
                f"purchase export snapshot {int(batch['planning_read_snapshot_id'])} is not an accepted purchase journal basis"
            )
        session.execute(text(
            "UPDATE purchase_export_batch SET "
            "current_execution_scope_id=:scope_id, "
            "current_execution_source_revision=:revision, "
            "planning_read_snapshot_id=NULL WHERE id=:batch_id"
        ), {
            "scope_id": int(scope["id"]),
            "revision": f"accepted:g{int(source['ledger_generation_id'])}:purchase_control_journal",
            "batch_id": int(batch["id"]),
        })

    return {
        "legacy_before": len(legacy),
        "legacy_after": 0,
        "current": sum(1 for row in batches if row["current_execution_scope_id"] is not None) + len(legacy),
    }


def _execution_contour_mode(session: Session) -> str:
    """Return ``full`` or ``absent`` for the one-off rehearsal schema.

    The real local database has the complete generation-scoped execution
    contour and must publish/verify it.  Tiny obligation-only test schemas from
    the earlier R10 adapter intentionally omit those tables; they are not a
    production migration target.  A partially present contour is unsafe and
    blocks rather than silently bypassing the manifest.
    """
    names = set(inspect(session.connection()).get_table_names())
    present = names & _EXECUTION_PROJECTION_TABLES
    if not present:
        return "absent"
    if present != _EXECUTION_PROJECTION_TABLES:
        missing = sorted(_EXECUTION_PROJECTION_TABLES - present)
        raise PreflightBlocked(
            "execution contour is incomplete; missing projection tables: "
            + ", ".join(missing)
        )
    return "full"


def _current_scope_row_count(session: Session, *, entity_kind: str, scope_key: str) -> int:
    """Count the rows a reader would actually see in one published scope."""

    return int(session.execute(text(
        "SELECT count(*) FROM current_execution_row "
        "WHERE entity_kind = :entity_kind AND scope_key = :scope_key "
        "AND result_status = 'accepted' AND result_ready"
    ), {"entity_kind": str(entity_kind), "scope_key": str(scope_key)}).scalar_one() or 0)


def _owner_index(
    session: Session,
) -> dict[tuple[int, str], set[tuple[int, str, str, str, str]]]:
    """Current owners indexed by ``(item_id, scope_mode)``, read once.

    The backlog used to resolve every fact against the whole owner list,
    which is quadratic on a real stand.  Owners are collapsed to their
    canonical scope key here, so each fact below is a dict lookup.
    """
    from app import models
    from app.services.item_ledger.current_replenishment import scope_mode_for_owner
    from app.services.mrp_freeze import distribution_scope_for

    index: dict[tuple[int, str], set[tuple[int, str, str, str, str]]] = {}
    for owner in session.query(models.ReservationEntry).filter(
        models.ReservationEntry.is_current.is_(True),
        models.ReservationEntry.owner_kind == "current",
        models.ReservationEntry.lifecycle_status == "active",
    ).all():
        scope_mode = scope_mode_for_owner(str(owner.realization_mode or ""))
        key = (int(owner.item_id), scope_mode)
        index.setdefault(key, set()).add(distribution_scope_for(
            int(owner.item_id),
            str(owner.characteristic_ref or ""),
            str(owner.organization_ref or ""),
            mode=scope_mode,
        ))
    return index


def _backlog_rows(
    facts: list[Any],
    index: dict[tuple[int, str], set[tuple[int, str, str, str, str]]],
    scope_mode: str,
) -> tuple[list[int], list[dict[str, Any]]]:
    """Owed facts, plus the ambiguous ones listed instead of raising.

    A fact whose item resolves to more than one distribution pool is exactly
    what the publication gates refuse, but a *report* that raised on it would
    hide every other number it was asked for.  It is listed as a row.
    """
    owed: list[int] = []
    ambiguous: list[dict[str, Any]] = []
    for row in facts:
        scopes = index.get((int(row.item_id), scope_mode))
        if not scopes:
            continue
        if len(scopes) > 1:
            ambiguous.append({
                "stock_ledger_entry_id": int(row.id),
                "item_id": int(row.item_id),
                "scopes": sorted(list(scope) for scope in scopes),
            })
            continue
        owed.append(int(row.id))
    return owed, ambiguous


def pre_deploy_backlog(engine: Engine) -> dict[str, Any]:
    """Read-only report of what the bounded refresh gates would see.

    Its own phase and its own session: it is bounded but not free, and the
    postflight holds a publication transaction open while it runs.

    The gates themselves judge only a refresh's delta; this is the historical
    view over the whole visible prefix, which is the part they deliberately
    do not judge.  Both counts use the publication's own canonical scope key,
    never a second predicate written here.
    """
    generation_id = _accepted_truth_generation(engine)
    empty: dict[str, Any] = {
        "phase": "pre-deploy-backlog",
        "generation_id": int(generation_id),
        "untyped_buy_owed_receipts": None,
        "unallocated_make_owed_outputs": None,
        "evaluated": False,
    }
    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        names = set(inspect(session.connection()).get_table_names())
        if not {
            "stock_ledger_entry", "stock_ledger_supplier_receipt_provenance",
            "reservation_entry", "reservation_consumption_allocation",
            "stock_warehouses", "ledger_generation",
        }.issubset(names):
            return {
                **empty, "status": "ready",
                "reason": "schema does not carry the physical contour",
            }
        try:
            from app import models
            from app.services.item_ledger.physical_refresh_supplier_evidence import (
                supplier_document_type_filter,
            )
            from app.services.item_ledger.physical_visibility import (
                visible_sle_query_for_generation,
            )
            from app.services.planning_pool_resolver import (
                resolve_planning_pool_by_warehouse,
            )
            from sqlalchemy import and_ as _and, exists as _exists

            contour = sorted(resolve_planning_pool_by_warehouse(session))
            index = _owner_index(session)

            typed = _exists().where(
                models.StockLedgerSupplierReceiptProvenance.stock_ledger_entry_id
                == models.StockLedgerEntry.id
            )
            receipts = (
                visible_sle_query_for_generation(session, int(generation_id))
                .filter(supplier_document_type_filter(
                    models.StockLedgerEntry.recorder_type
                ))
                .filter(models.StockLedgerEntry.movement_kind.in_(
                    ("receipt", "supplier_receipt")
                ))
                .filter(models.StockLedgerEntry.active.is_(True))
                .filter(models.StockLedgerEntry.qty != 0)
                .filter(models.StockLedgerEntry.warehouse_ref1c.in_(contour))
                .filter(~typed)
                .order_by(None)
                .all()
            )
            untyped_buy, buy_ambiguous = _backlog_rows(receipts, index, "buy")

            allocated = _exists().where(_and(
                models.ReservationConsumptionAllocation.sle_id
                == models.StockLedgerEntry.id,
                models.ReservationConsumptionAllocation.is_current.is_(True),
            ))
            outputs = (
                visible_sle_query_for_generation(session, int(generation_id))
                .filter(models.StockLedgerEntry.movement_kind == "assembly_in")
                .filter(models.StockLedgerEntry.active.is_(True))
                .filter(models.StockLedgerEntry.qty != 0)
                .filter(models.StockLedgerEntry.warehouse_ref1c.in_(contour))
                .filter(~allocated)
                .order_by(None)
                .all()
            )
            make_owed, make_ambiguous = _backlog_rows(outputs, index, "make")
        except Exception as exc:  # noqa: BLE001 - a report may not fail the phase
            return {
                **empty, "status": "ready",
                "reason": f"{type(exc).__name__}: {exc}"[:200],
            }
        session.rollback()
    return {
        "phase": "pre-deploy-backlog",
        "status": "ready",
        "generation_id": int(generation_id),
        "untyped_buy_owed_receipts": len(untyped_buy),
        "untyped_buy_owed_receipt_sample": untyped_buy[:8],
        "unallocated_make_owed_outputs": len(make_owed),
        "unallocated_make_owed_output_sample": make_owed[:8],
        "ambiguous_distribution_pools": buy_ambiguous[:8] + make_ambiguous[:8],
        "ambiguous_distribution_pool_count": len(buy_ambiguous) + len(make_ambiguous),
        "evaluated": True,
    }


def _postflight_on_session(session: Session, generation_id: int) -> dict[str, Any]:
    """Validate current scopes/rows while the publication transaction is held.

    A ready manifest is not enough: an empty scope published over live
    business owners is the silent failure this postflight exists to catch, so
    emptiness is checked against the owners that must be described.
    """

    scopes: dict[str, dict[str, Any]] = {}
    for consumer, scope_key, entity_kind in EXPECTED_CURRENT_SCOPES:
        rows = session.execute(text(
            "SELECT entity_kind, scope_key, source_generation_id, source_revision, result_ready "
            "FROM current_execution_scope WHERE entity_kind = :entity_kind AND scope_key = :scope_key"
        ), {"entity_kind": entity_kind, "scope_key": scope_key}).mappings().all()
        if len(rows) != 1:
            raise PostflightBlocked(
                f"expected exactly one current scope for {consumer}, found {len(rows)}"
            )
        row = rows[0]
        expected_revision = f"accepted:g{int(generation_id)}:{consumer}"
        if int(row["source_generation_id"] or 0) != int(generation_id):
            raise PostflightBlocked(f"scope {consumer} has wrong source generation")
        if str(row["source_revision"] or "") != expected_revision:
            raise PostflightBlocked(f"scope {consumer} has wrong source revision")
        if not bool(row["result_ready"]):
            raise PostflightBlocked(f"scope {consumer} is not ready")
        scopes[consumer] = {
            "entity_kind": entity_kind,
            "scope_key": scope_key,
            "source_generation_id": int(row["source_generation_id"]),
            "source_revision": str(row["source_revision"]),
            "result_ready": bool(row["result_ready"]),
            "row_count": _current_scope_row_count(
                session, entity_kind=entity_kind, scope_key=scope_key
            ),
        }

    # An empty scope is legitimate only when nothing owns it.  Each check
    # names the live owner set the scope failed to describe.
    if not scopes["purchase_control_journal"]["row_count"]:
        buy_owners = active_current_buy_owner_count(session)
        if buy_owners:
            raise PostflightBlocked(
                "purchase current scope has 0 rows while "
                f"{buy_owners} active current BUY owners exist"
            )
    if not scopes["production_control_journal"]["row_count"]:
        production_evidence = legacy_journal_row_count(
            session,
            consumer="production_control_journal",
            key="journal:v1",
            generation_id=int(generation_id),
        )
        if production_evidence:
            raise PostflightBlocked(
                "production current scope has 0 rows while the legacy production "
                f"snapshot carried {production_evidence} rows"
            )
    if not scopes["mrp_result"]["row_count"]:
        fixed_runs = fixed_planning_runs(session)
        if fixed_runs:
            raise PostflightBlocked(
                "mrp_result current scope has 0 rows while "
                f"{len(fixed_runs)} fixed runs exist"
            )

    execution_scopes: dict[str, dict[str, Any]] = {}
    if _execution_contour_mode(session) == "full":
        for consumer, scope_key, entity_kind in EXPECTED_EXECUTION_SCOPES:
            rows = session.execute(text(
                "SELECT entity_kind, scope_key, source_generation_id, source_revision, result_ready "
                "FROM current_execution_scope WHERE entity_kind = :entity_kind AND scope_key = :scope_key"
            ), {"entity_kind": entity_kind, "scope_key": scope_key}).mappings().all()
            if len(rows) != 1:
                raise PostflightBlocked(
                    f"expected exactly one execution scope for {consumer}, found {len(rows)}"
                )
            row = rows[0]
            if int(row["source_generation_id"] or 0) != int(generation_id):
                raise PostflightBlocked(f"execution scope {consumer} has wrong source generation")
            if str(row["source_revision"] or "") != f"accepted:g{int(generation_id)}":
                raise PostflightBlocked(f"execution scope {consumer} has wrong source revision")
            if not bool(row["result_ready"]):
                raise PostflightBlocked(f"execution scope {consumer} is not ready")
            execution_scopes[consumer] = {
                "entity_kind": entity_kind,
                "scope_key": scope_key,
                "source_generation_id": int(row["source_generation_id"]),
                "source_revision": str(row["source_revision"]),
                "result_ready": bool(row["result_ready"]),
                "row_count": _current_scope_row_count(
                    session, entity_kind=entity_kind, scope_key=scope_key
                ),
            }

    duplicates = session.execute(text(
        "SELECT entity_kind, scope_key, business_identity, count(*) AS row_count "
        "FROM current_execution_row "
        "WHERE entity_kind IN (:production, :purchase, :mrp, :period) "
        "GROUP BY entity_kind, scope_key, business_identity HAVING count(*) > 1"
    ), {
        "production": "production_control_journal",
        "purchase": "purchase_control_journal",
        "mrp": "mrp_result",
        "period": "period_plan_execution",
    }).mappings().all()
    if duplicates:
        raise PostflightBlocked(
            "duplicate current business identities: "
            + json.dumps([dict(row) for row in duplicates], sort_keys=True)
        )

    purchase_export_anchors = {"legacy": 0, "current": 0}
    if "purchase_export_batch" in set(inspect(session.connection()).get_table_names()):
        batch_columns = {
            column["name"]
            for column in inspect(session.connection()).get_columns("purchase_export_batch")
        }
        if not {"current_execution_scope_id", "current_execution_source_revision"} <= batch_columns:
            raise PostflightBlocked("purchase export batch has no complete current anchor columns")
        current_expr = (
            "sum(CASE WHEN current_execution_scope_id IS NOT NULL "
            "AND current_execution_source_revision IS NOT NULL THEN 1 ELSE 0 END) AS current"
        )
        if "planning_read_snapshot_id" in batch_columns:
            anchor_counts = session.execute(text(
                "SELECT "
                "sum(CASE WHEN planning_read_snapshot_id IS NOT NULL THEN 1 ELSE 0 END) AS legacy, "
                f"{current_expr} FROM purchase_export_batch"
            )).one()
            purchase_export_anchors = {
                "legacy": int(anchor_counts[0] or 0),
                "current": int(anchor_counts[1] or 0),
            }
        else:
            anchor_counts = session.execute(text(
                f"SELECT {current_expr} FROM purchase_export_batch"
            )).one()
            purchase_export_anchors = {"legacy": 0, "current": int(anchor_counts[0] or 0)}
        if purchase_export_anchors["legacy"]:
            raise PostflightBlocked(
                f"purchase export legacy anchors remain: {purchase_export_anchors['legacy']}"
            )

    return {
        "status": "ready",
        "generation_id": int(generation_id),
        "scopes": scopes,
        "execution_scopes": execution_scopes,
        "row_counts": {
            consumer: int(scope["row_count"]) for consumer, scope in scopes.items()
        },
        "unique_identities": True,
        "purchase_export_anchors": purchase_export_anchors,
    }


def postflight_manifest(engine: Engine, *, generation_id: int) -> dict[str, Any]:
    """Read-only postflight for an already published current obligation set."""

    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        return _postflight_on_session(session, int(generation_id))


def _count_changes(engine: Engine) -> int:
    inspector = inspect(engine)
    if "current_execution_change" not in set(inspector.get_table_names()):
        return 0
    with engine.connect() as connection:
        return int(connection.execute(text("SELECT count(*) FROM current_execution_change")).scalar_one())


def apply_current_obligation_migration(
    engine: Engine,
    *,
    writers_stopped: bool,
    fault_after_consumer: str | None = None,
) -> dict[str, Any]:
    """Atomically publish legacy obligation evidence into current execution.

    This function owns transaction orchestration only.  The domain mapping and
    all current-row semantics remain in the explicitly named
    ``tools.current_execution_legacy_adapter``.
    """

    if not writers_stopped:
        raise PreflightBlocked("explicit writers-stopped acknowledgement is required")

    manifest = build_manifest(engine)
    if manifest["status"] != "ready":
        raise PreflightBlocked(
            "R10 preflight is blocked by unknown or ambiguous dependencies: "
            + json.dumps(manifest["dependencies"], sort_keys=True)
        )

    generation_id = _accepted_truth_generation(engine)
    if publish_current_obligation_views_from_snapshots is None:
        raise PreflightBlocked("canonical current obligation publisher is unavailable")

    source_evidence = _source_evidence_report(engine, generation_id)
    before_changes = _count_changes(engine)
    publisher_result: Any = None
    execution_publisher_result: Any = None
    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        with session.begin():
            # The destructive projection cutover requires a complete current
            # execution contour first.  Publish it from the exact accepted
            # generation in this same transaction, before the obligation
            # adapter; no empty/fallback manifest is fabricated.
            if _execution_contour_mode(session) == "full":
                if publish_current_execution_from_generation is None:
                    raise PreflightBlocked(
                        "canonical current execution publisher is unavailable"
                    )
                execution_publisher_result = publish_current_execution_from_generation(
                    session, int(generation_id)
                )
            publisher_result = publish_current_obligation_views_from_snapshots(session, generation_id)
            if fault_after_consumer is not None:
                raise RuntimeError(f"fault injection after consumer {fault_after_consumer}")
            _migrate_purchase_export_anchors(session, generation_id)
            postflight = _postflight_on_session(session, generation_id)

    after_changes = _count_changes(engine)
    return {
        "phase": "apply",
        "status": "ready",
        "generation_id": int(generation_id),
        "preflight": manifest,
        "source_evidence": source_evidence,
        "postflight": postflight,
        "publisher_consumers": sorted(
            [str(key) for key in (publisher_result or {})]
            + (["current_execution"] if execution_publisher_result is not None else [])
        ),
        "change_rows_before": before_changes,
        "change_rows_after": after_changes,
        "idempotent": after_changes == before_changes,
    }


def _exact_supplier_provenance_owners(session: Session) -> list[dict[str, int]]:
    rows = session.execute(text(
        "SELECT ledger_generation_id AS generation_id, count(*) AS row_count "
        "FROM stock_ledger_supplier_receipt_provenance "
        "WHERE match_status = 'exact' "
        "GROUP BY ledger_generation_id ORDER BY ledger_generation_id"
    )).mappings().all()
    return [
        {"generation_id": int(row["generation_id"]), "row_count": int(row["row_count"])}
        for row in rows
    ]


def _exact_supplier_provenance_with_qty(session: Session, generation_id: int) -> int:
    """Count exact supplier provenance rows whose fact still carries quantity."""

    columns = {
        str(column["name"])
        for column in inspect(session.connection()).get_columns("stock_ledger_entry")
    }
    clauses = [
        "p.ledger_generation_id = :generation_id",
        "p.match_status = 'exact'",
        "e.qty <> 0",
    ]
    if "active" in columns:
        clauses.append("e.active")
    return int(session.execute(text(
        "SELECT count(*) FROM stock_ledger_supplier_receipt_provenance p "
        "JOIN stock_ledger_entry e ON e.id = p.stock_ledger_entry_id "
        "WHERE " + " AND ".join(clauses)
    ), {"generation_id": int(generation_id)}).scalar_one() or 0)


def _provenance_rows_at_generation(session: Session, generation_id: int) -> int:
    """Every supplier provenance row the pointer generation owns, not just exact."""

    return int(session.execute(text(
        "SELECT count(*) FROM stock_ledger_supplier_receipt_provenance "
        "WHERE ledger_generation_id = :generation_id"
    ), {"generation_id": int(generation_id)}).scalar_one() or 0)


def _visible_supplier_receipt_sle_count(session: Session, generation_id: int) -> int:
    """Supplier-document ledger rows visible at the pointer generation.

    Same visibility helper and same document-type predicate the provenance
    writer uses, counted in SQL so the preflight does not materialise the
    whole physical prefix.
    """

    from app import models

    return int(
        visible_sle_query_for_generation(session, int(generation_id))
        .filter(supplier_document_type_filter(models.StockLedgerEntry.recorder_type))
        .filter(models.StockLedgerEntry.active.is_(True))
        .filter(models.StockLedgerEntry.qty != 0)
        .order_by(None)
        .count()
    )


def _current_allocations_by_role(session: Session) -> dict[str, int]:
    rows = session.execute(text(
        "SELECT allocation_role, count(*) AS row_count "
        "FROM reservation_consumption_allocation WHERE is_current "
        "GROUP BY allocation_role ORDER BY allocation_role"
    )).mappings().all()
    return {str(row["allocation_role"]): int(row["row_count"]) for row in rows}


def _replenishment_bootstrap_on_session(
    session: Session, generation_id: int
) -> dict[str, Any]:
    """Build the complete R4 current replenishment state for the pointer.

    The bounded physical path only replays the BUY scopes a delta touched, so
    a freshly migrated database has no current replenishment state at all.
    The accepted-generation writer is the one canonical owner of that state;
    this phase only calls it and refuses to report success on an empty result.
    """

    names = set(inspect(session.connection()).get_table_names())
    for required in (
        "stock_ledger_supplier_receipt_provenance",
        "current_replenishment_state",
        "reservation_consumption_allocation",
    ):
        if required not in names:
            raise PreflightBlocked(
                f"current replenishment bootstrap requires table {required}"
            )

    owners = _exact_supplier_provenance_owners(session)
    owned_here = next(
        (row["row_count"] for row in owners if row["generation_id"] == int(generation_id)),
        0,
    )
    if not owned_here and owners:
        # Provenance stays at the generation that computed it; a bounded
        # physical refresh never re-tags it.  Running the writer against a
        # later pointer would silently build state with zero allocations.
        detail = ", ".join(
            f"g{row['generation_id']}({row['row_count']} rows)" for row in owners
        )
        raise PreflightBlocked(
            f"accepted pointer generation {int(generation_id)} owns no exact supplier "
            f"receipt provenance; it is owned by {detail}"
        )

    # ``owned_here == 0 and owners`` only catches provenance that moved to
    # another generation.  On a stand whose provenance table is empty the
    # preflight passed, the writer saw no receipts, and the phase reported
    # success having published zero allocations over live supplier facts.
    provenance_rows_at_pointer = _provenance_rows_at_generation(
        session, int(generation_id)
    )
    supplier_receipt_sle_visible = _visible_supplier_receipt_sle_count(
        session, int(generation_id)
    )
    if not provenance_rows_at_pointer and supplier_receipt_sle_visible:
        raise PreflightBlocked(
            f"accepted pointer generation {int(generation_id)} has "
            f"{supplier_receipt_sle_visible} visible supplier-receipt ledger "
            "rows but no supplier receipt provenance rows; type the supplier "
            "evidence before bootstrapping current replenishment"
        )

    exact_with_qty = _exact_supplier_provenance_with_qty(session, int(generation_id))
    results = apply_current_replenishment_for_accepted_generation(
        session, generation_id=int(generation_id)
    )
    session.flush()

    changed_pairs = sum(int(result.changed_pairs) for result in results)
    allocations = _current_allocations_by_role(session)
    total_allocations = sum(allocations.values())
    state_rows = int(session.execute(text(
        "SELECT count(*) FROM current_replenishment_state"
    )).scalar_one() or 0)
    if not total_allocations and exact_with_qty:
        raise PostflightBlocked(
            "current replenishment bootstrap produced 0 current allocations while "
            f"{exact_with_qty} exact supplier receipt provenance rows with quantity "
            f"exist at generation {int(generation_id)}"
        )
    return {
        "phase": "replenishment-bootstrap",
        "status": "ready",
        "generation_id": int(generation_id),
        "exact_provenance_rows": int(owned_here),
        "exact_provenance_rows_with_qty": int(exact_with_qty),
        "provenance_rows_at_pointer": int(provenance_rows_at_pointer),
        "supplier_receipt_sle_visible": int(supplier_receipt_sle_visible),
        "scopes": len(results),
        "changed_pairs": int(changed_pairs),
        "inserted": sum(int(result.inserted) for result in results),
        "updated": sum(int(result.updated) for result in results),
        "deleted": sum(int(result.deleted) for result in results),
        "current_replenishment_state": state_rows,
        "reservation_consumption_allocation_current": {
            "total": int(total_allocations),
            "by_allocation_role": allocations,
        },
        "idempotent": changed_pairs == 0,
    }


def _provenance_source_generations(
    session: Session, pointer_generation_id: int
) -> list[int]:
    """Every other generation that still owns typed evidence, newest first."""

    rows = session.execute(text(
        "SELECT DISTINCT ledger_generation_id "
        "FROM stock_ledger_supplier_receipt_provenance "
        "WHERE ledger_generation_id <> :pointer "
        "ORDER BY ledger_generation_id DESC"
    ), {"pointer": int(pointer_generation_id)}).scalars().all()
    return [int(value) for value in rows]


def _supplier_provenance_repair_on_session(
    session: Session, generation_id: int
) -> dict[str, Any]:
    """Give the accepted pointer back the typed evidence of its own prefix.

    A lightweight physical refresh used to fork without carrying supplier
    provenance, and the retention prune then deleted the parent's copy at the
    next publication.  The facts stayed accepted and visible; only the typing
    that says which supplier order they belong to was left behind at an older
    generation.  This phase re-owns what still exists, newest source first.

    It does not invent typing.  A visible supplier fact that no generation
    ever typed is reported and left alone - that is the ordinary "outside the
    planning contour" case, not damage.  A database in which nothing is typed
    at all cannot be repaired by re-owning anything, so it fails closed and
    the ledger rebuild runbook applies.
    """

    from app.services.item_ledger.physical_refresh_generation import (
        _provenance_rows,
        _provenance_value,
    )
    from app.services.item_ledger.physical_refresh_supplier_evidence import (
        lost_supplier_receipt_provenance_sle_ids,
        untyped_supplier_receipt_sle_ids,
    )
    from app import models

    names = set(inspect(session.connection()).get_table_names())
    for required in (
        "stock_ledger_supplier_receipt_provenance",
        "stock_ledger_entry",
        "ledger_generation",
    ):
        if required not in names:
            raise PreflightBlocked(
                f"supplier provenance repair requires table {required}"
            )

    def _owned_here() -> int:
        return int(session.execute(text(
            "SELECT count(*) FROM stock_ledger_supplier_receipt_provenance "
            "WHERE ledger_generation_id = :generation_id"
        ), {"generation_id": int(generation_id)}).scalar_one() or 0)

    owned_before = _owned_here()
    lost_before = lost_supplier_receipt_provenance_sle_ids(
        session, ledger_generation_id=int(generation_id)
    )
    untyped_anywhere = untyped_supplier_receipt_sle_ids(
        session, ledger_generation_id=int(generation_id)
    )
    sources = _provenance_source_generations(session, int(generation_id))

    if not owned_before and not lost_before and untyped_anywhere:
        raise PreflightBlocked(
            f"pointer generation {int(generation_id)} owns no supplier receipt "
            f"provenance and {len(untyped_anywhere)} visible supplier facts are "
            "typed nowhere in this database; the ledger rebuild runbook applies"
        )

    reowned = 0
    if lost_before:
        wanted = {int(value) for value in lost_before}
        for source_id in sources:
            if not wanted:
                break
            for source in _provenance_rows(session, int(source_id)):
                sle_id = int(source.stock_ledger_entry_id)
                if sle_id not in wanted:
                    continue
                # One row per (generation, SLE): a fact already owned here is
                # never duplicated, and a superseded revision keeps its own
                # row instead of inheriting its predecessor's.
                session.add(models.StockLedgerSupplierReceiptProvenance(
                    ledger_generation_id=int(generation_id),
                    **_provenance_value(source),
                ))
                wanted.discard(sle_id)
                reowned += 1
        session.flush()

    lost_after = lost_supplier_receipt_provenance_sle_ids(
        session, ledger_generation_id=int(generation_id)
    )
    if lost_after:
        raise PostflightBlocked(
            f"supplier provenance repair re-owned {reowned} rows but generation "
            f"{int(generation_id)} still lacks evidence for {len(lost_after)} "
            "visible supplier facts (first sle_ids="
            f"{[int(value) for value in lost_after[:8]]})"
        )
    return {
        "phase": "supplier-provenance-repair",
        "status": "ready",
        "generation_id": int(generation_id),
        "source_generation_ids": [int(value) for value in sources],
        "provenance_rows_before": owned_before,
        "provenance_rows_after": _owned_here(),
        "reowned_rows": int(reowned),
        "lost_before": len(lost_before),
        "lost_after": 0,
        # Reported, never repaired: a supplier document nobody ever typed.
        "untyped_anywhere": len(untyped_anywhere),
        "idempotent": reowned == 0,
    }


def apply_supplier_provenance_repair(
    engine: Engine, *, writers_stopped: bool
) -> dict[str, Any]:
    """Run the supplier-provenance repair in one transaction."""

    if not writers_stopped:
        raise PreflightBlocked("explicit writers-stopped acknowledgement is required")
    generation_id = _accepted_truth_generation(engine)
    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        with session.begin():
            return _supplier_provenance_repair_on_session(session, int(generation_id))


def apply_current_replenishment_bootstrap(
    engine: Engine, *, writers_stopped: bool
) -> dict[str, Any]:
    """Run the R4 current replenishment bootstrap in one transaction.

    Operationally this phase runs after the schema is at alembic head and
    before generation GC: it needs the accepted pointer generation to still
    own its ``stock_ledger_supplier_receipt_provenance`` rows.
    """

    if not writers_stopped:
        raise PreflightBlocked("explicit writers-stopped acknowledgement is required")
    generation_id = _accepted_truth_generation(engine)
    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        with session.begin():
            return _replenishment_bootstrap_on_session(session, int(generation_id))


def _assert_local_database_url(database_url: str) -> None:
    parsed = urlparse(database_url)
    if parsed.scheme.startswith("sqlite"):
        return
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("R10 apply is local-only; database host must be loopback")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument(
        "--phase",
        choices=(
            "preflight",
            "apply",
            "postflight",
            "replenishment-bootstrap",
            "supplier-provenance-repair",
            "pre-deploy-backlog",
        ),
        default="preflight",
        help=(
            "read-only manifest, atomic current publication, read-only postflight, "
            "or the one-off R4 current replenishment bootstrap"
        ),
    )
    parser.add_argument(
        "--writers-stopped",
        action="store_true",
        help=(
            "explicit acknowledgement required by --phase apply/"
            "replenishment-bootstrap/supplier-provenance-repair"
        ),
    )
    parser.add_argument("--generation-id", type=int)
    parser.add_argument("--fault-after-consumer")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    try:
        _assert_local_database_url(args.database_url)
    except ValueError as exc:
        parser.error(str(exc))
    engine = create_engine(args.database_url, future=True)
    try:
        if args.phase == "preflight":
            report = build_manifest(engine)
        elif args.phase == "apply":
            report = apply_current_obligation_migration(
                engine,
                writers_stopped=args.writers_stopped,
                fault_after_consumer=args.fault_after_consumer,
            )
        elif args.phase == "pre-deploy-backlog":
            report = pre_deploy_backlog(engine)
        elif args.phase == "supplier-provenance-repair":
            report = apply_supplier_provenance_repair(
                engine, writers_stopped=bool(args.writers_stopped)
            )
        elif args.phase == "replenishment-bootstrap":
            report = apply_current_replenishment_bootstrap(
                engine, writers_stopped=args.writers_stopped
            )
        else:
            generation_id = args.generation_id
            if generation_id is None:
                generation_id = _accepted_truth_generation(engine)
            report = postflight_manifest(engine, generation_id=int(generation_id))
    except (PreflightBlocked, PostflightBlocked, RuntimeError) as exc:
        report = {"phase": args.phase, "status": "blocked", "reason": str(exc)}
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("status") == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
