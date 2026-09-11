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

from sqlalchemy import create_engine, inspect, text
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


class PreflightBlocked(RuntimeError):
    """The migration may not start because its read-only preflight is unsafe."""


class PostflightBlocked(RuntimeError):
    """The canonical publication did not produce an unambiguous current view."""


_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_ROOT = _REPO_ROOT / "backend"
if _BACKEND_ROOT.is_dir() and str(_BACKEND_ROOT) not in sys.path:
    # The supported CLI invocation is `python tools/...` from the repository
    # root; in that mode Python does not add backend/ to import search paths.
    sys.path.insert(0, str(_BACKEND_ROOT))

try:
    # Kept as a module-level seam so tests can prove the transaction policy
    # without calling OData, workers, or a live integration.
    from app.services.item_ledger.current_execution import (
        publish_current_obligation_views_from_generation,
    )
except Exception:  # pragma: no cover - standalone preflight remains usable
    publish_current_obligation_views_from_generation = None


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
    "planning_run_bucket_modes": "historical bucket-mode evidence maps to canonical run semantics",
    "mrp_bucket_type_legacy": "historical bucket-type evidence maps to canonical bucket semantics",
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
    not.  MRP and period evidence is scoped to fixed runs/plans when those
    tables are available; the small policy-test schema instead must provide at
    least one accepted source snapshot for each consumer.
    """

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "planning_read_snapshot" not in table_names:
        raise PreflightBlocked("legacy obligation source planning_read_snapshot is absent")

    with engine.connect() as connection:
        all_rows = connection.execute(text(
            "SELECT id, consumer, snapshot_key, truth_status "
            "FROM planning_read_snapshot WHERE ledger_generation_id = :generation_id "
            "ORDER BY consumer, snapshot_key, id"
        ), {"generation_id": int(generation_id)}).mappings().all()

        fixed_runs: list[dict[str, Any]] = []
        if "planning_run" in table_names:
            fixed_runs = [dict(row) for row in connection.execute(text(
                "SELECT run_id, source_plan_id FROM planning_run "
                "WHERE ledger_generation_id = :generation_id AND status = 'FIXED_SNAPSHOT' "
                "ORDER BY run_id"
            ), {"generation_id": int(generation_id)}).mappings().all()]

    accepted = [row for row in all_rows if str(row.get("truth_status") or "") == "accepted"]
    by_consumer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in accepted:
        by_consumer[str(row["consumer"])].append(dict(row))

    evidence: dict[str, Any] = {}

    def require_exact(consumer: str, snapshot_key: str) -> None:
        matches = [
            row for row in by_consumer.get(consumer, [])
            if str(row.get("snapshot_key") or "") == snapshot_key
        ]
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

    def require_scoped(consumer: str, prefixes: list[str], applicable: list[str]) -> None:
        candidates = by_consumer.get(consumer, [])
        if not applicable:
            # A database with no fixed runs/plans has a legitimately empty
            # obligation scope; no fabricated empty source is accepted.
            evidence[consumer] = {"applicable": [], "snapshot_ids": []}
            return
        selected: list[dict[str, Any]] = []
        missing: list[str] = []
        for key in applicable:
            matches = [
                row for row in candidates
                if any(str(row.get("snapshot_key") or "").startswith(prefix) for prefix in prefixes)
                and str(row.get("snapshot_key") or "").startswith(key)
            ]
            if len(matches) != 1:
                missing.append(key)
            else:
                selected.append(matches[0])
        if missing:
            raise PreflightBlocked(
                f"source evidence for {consumer} is missing or ambiguous for {missing}"
            )
        evidence[consumer] = {
            "applicable": list(applicable),
            "snapshot_ids": sorted(int(row["id"]) for row in selected),
        }

    if "planning_run" not in table_names:
        # Minimal policy schemas must explicitly stub evidence rather than
        # allowing a publisher to manufacture empty current scopes.
        require_scoped("mrp_result", ["run:"], ["run:"])
        require_scoped("period_plan_execution", ["plan:"], ["plan:"])
    else:
        run_keys = [f"run:{int(row['run_id'])}:" for row in fixed_runs]
        plan_keys = sorted({
            f"plan:{int(row['source_plan_id'])}:"
            for row in fixed_runs
            if row.get("source_plan_id") is not None
        })
        require_scoped("mrp_result", ["run:"], run_keys)
        require_scoped("period_plan_execution", ["plan:"], plan_keys)

    return {
        "status": "ready",
        "generation_id": int(generation_id),
        "consumers": evidence,
    }


def _postflight_on_session(session: Session, generation_id: int) -> dict[str, Any]:
    """Validate current scopes/rows while the publication transaction is held."""

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

    return {
        "status": "ready",
        "generation_id": int(generation_id),
        "scopes": scopes,
        "unique_identities": True,
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
    all current-row semantics remain in
    ``publish_current_obligation_views_from_generation``.
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
    if publish_current_obligation_views_from_generation is None:
        raise PreflightBlocked("canonical current obligation publisher is unavailable")

    source_evidence = _source_evidence_report(engine, generation_id)
    before_changes = _count_changes(engine)
    publisher_result: Any = None
    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        with session.begin():
            publisher_result = publish_current_obligation_views_from_generation(session, generation_id)
            if fault_after_consumer is not None:
                raise RuntimeError(f"fault injection after consumer {fault_after_consumer}")
            postflight = _postflight_on_session(session, generation_id)

    after_changes = _count_changes(engine)
    return {
        "phase": "apply",
        "status": "ready",
        "generation_id": int(generation_id),
        "preflight": manifest,
        "source_evidence": source_evidence,
        "postflight": postflight,
        "publisher_consumers": sorted(str(key) for key in (publisher_result or {})),
        "change_rows_before": before_changes,
        "change_rows_after": after_changes,
        "idempotent": after_changes == before_changes,
    }


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
        choices=("preflight", "apply", "postflight"),
        default="preflight",
        help="read-only manifest, atomic current publication, or read-only postflight",
    )
    parser.add_argument(
        "--writers-stopped",
        action="store_true",
        help="explicit acknowledgement required by --phase apply",
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
