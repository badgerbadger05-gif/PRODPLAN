"""Convert pre-R10 read evidence into the canonical current publication.

This module is intentionally outside ``backend/app``.  It is a pre-drop
migration boundary: legacy table names are reflected with SQLAlchemy Core and
the resulting structured payloads are handed to the one current publisher.
Runtime services must not import this adapter.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import MetaData, Table, and_, func, inspect, select, text
from sqlalchemy.orm import Session


_KINDS = ("production", "purchase", "rework", "capacity")

# The canonical live-plan selector: a run is a live obligation while it is a
# fixed snapshot, regardless of which generation happened to compute it.  The
# runtime publisher uses exactly this predicate (``_fixed_run_ids``); binding
# the migration to the pointer generation instead silently published an empty
# ``mrp_result`` scope, because fixed runs stay anchored to the obligation
# refresh that created them while the pointer moves on with every physical
# refresh.
_FIXED_RUN_STATUS = "FIXED_SNAPSHOT"


class LegacyEvidenceConflict(RuntimeError):
    """Legacy read evidence is ambiguous, or empty while live owners exist.

    ``RuntimeError`` so the migration CLI reports a blocked phase instead of
    an unhandled traceback; an empty current scope must never be published as
    a "ready" result.
    """


def _table(session: Session, name: str) -> Table:
    # Reflect through the session's own connection.  Reflecting through the
    # Engine checks out a second connection, which cannot see the migration's
    # uncommitted work and, on a single-connection pool, ends by rolling it
    # back.
    return Table(name, MetaData(), autoload_with=session.connection())


def _table_names(session: Session) -> set[str]:
    # Inspect the session's own connection: with an in-memory SQLite engine
    # the Engine would otherwise open a second connection that cannot see the
    # uncommitted migration transaction.
    return set(inspect(session.connection()).get_table_names())


def _column_names(session: Session, table_name: str) -> set[str]:
    return {
        str(column["name"])
        for column in inspect(session.connection()).get_columns(table_name)
    }


def _optional_table(session: Session, name: str) -> Table | None:
    if name not in _table_names(session):
        return None
    return _table(session, name)


def _json_value(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("legacy payload JSON is malformed") from exc
        if isinstance(decoded, Mapping):
            return dict(decoded)
    raise ValueError("legacy payload is missing or malformed")


def _snapshot(
    session: Session,
    snapshots: Table,
    *,
    consumer: str,
    key: str | None,
    generation_id: int,
) -> dict[str, Any] | None:
    clauses = [
        snapshots.c.consumer == str(consumer),
        snapshots.c.ledger_generation_id == int(generation_id),
        snapshots.c.truth_status == "accepted",
    ]
    if key is not None:
        clauses.append(snapshots.c.snapshot_key == str(key))
    row = session.execute(select(snapshots).where(and_(*clauses))).mappings().first()
    return dict(row) if row is not None else None


def _latest_snapshot(
    session: Session,
    snapshots: Table,
    *,
    consumer: str,
    key: str,
) -> dict[str, Any] | None:
    """Resolve one consumer/key to its newest accepted evidence, any generation.

    This is the legacy reader's own semantics.  A run's snapshot stays at the
    generation that fixed it, while a later obligation refresh re-anchors
    ``planning_run.ledger_generation_id`` to the newest obligation generation,
    so the run row cannot locate its own evidence.  Ordering by generation (and
    then by id) picks the last published copy of that exact business key.
    """

    row = session.execute(
        select(snapshots)
        .where(and_(
            snapshots.c.consumer == str(consumer),
            snapshots.c.snapshot_key == str(key),
            snapshots.c.truth_status == "accepted",
        ))
        .order_by(
            snapshots.c.ledger_generation_id.desc(),
            snapshots.c.id.desc(),
        )
    ).mappings().first()
    return dict(row) if row is not None else None


def _rows_for_snapshot(
    session: Session,
    rows_table: Table,
    roots_table: Table,
    snapshot_id: int,
    *,
    include_row_kind: bool = False,
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(rows_table)
        .where(rows_table.c.snapshot_id == int(snapshot_id))
        .order_by(rows_table.c.sort_key.asc(), rows_table.c.id.asc())
    ).mappings()
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = _json_value(row.get("payload"))
        members = session.execute(
            select(roots_table.c.root_item_id)
            .where(
                roots_table.c.snapshot_id == int(snapshot_id),
                roots_table.c.row_id == int(row["id"]),
            )
            .order_by(roots_table.c.root_item_id.asc())
        ).scalars()
        payload["root_item_ids"] = [int(value) for value in members]
        if include_row_kind:
            # Only MRP rows carry a business row kind; the production journal
            # reader DTO is extra="forbid" and rejects these legacy columns.
            payload.setdefault("row_kind", str(row.get("row_kind") or ""))
            payload.setdefault("sort_key", str(row.get("sort_key") or ""))
        result.append(payload)
    return result


def _inline_rows(payload: Mapping[str, Any], *, consumer: str) -> list[dict[str, Any]]:
    raw = payload.get("rows")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise LegacyEvidenceConflict(
            f"legacy {consumer} inline payload rows are malformed"
        )
    rows: list[dict[str, Any]] = []
    for value in raw:
        if not isinstance(value, Mapping):
            raise LegacyEvidenceConflict(
                f"legacy {consumer} inline payload row is malformed"
            )
        rows.append(dict(value))
    return rows


def _row_identity_keys(rows: list[dict[str, Any]]) -> list[str] | None:
    keys: list[str] = []
    for row in rows:
        value = str(row.get("row_key") or "").strip()
        if not value:
            return None
        keys.append(value)
    return sorted(keys)


def _require_agreeing_rows(
    table_rows: list[dict[str, Any]],
    inline_rows: list[dict[str, Any]],
    *,
    consumer: str,
) -> None:
    """Fail closed when the two legacy row carriers describe different sets.

    Row identity, not field-by-field equality, is the comparison: the row
    table adds ``root_item_ids`` and JSON round-trips can re-type numbers, so
    only a genuinely different row set is a conflict.
    """

    if len(table_rows) != len(inline_rows):
        raise LegacyEvidenceConflict(
            f"legacy {consumer} evidence disagrees: the row table has "
            f"{len(table_rows)} rows and the inline payload has {len(inline_rows)}"
        )
    table_keys = _row_identity_keys(table_rows)
    inline_keys = _row_identity_keys(inline_rows)
    if table_keys is not None and inline_keys is not None and table_keys != inline_keys:
        raise LegacyEvidenceConflict(
            f"legacy {consumer} evidence disagrees on row identities"
        )


def _snapshot_rows(
    session: Session,
    rows_table: Table,
    roots_table: Table,
    snapshot: Mapping[str, Any],
    *,
    consumer: str,
    include_row_kind: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Resolve one legacy snapshot's rows from whichever carrier holds them.

    Two carriers exist in the historical corpus: normalized ``planning_read_row``
    children and rows kept inline in ``payload["rows"]``.  Which one a snapshot
    used depends on the worker that wrote it, so the migration must read the
    populated one instead of assuming the row table.  Unconditionally
    overwriting the inline rows with an empty row-table result is what
    published a 0-row purchase scope from 818 real rows.
    """

    payload = _json_value(snapshot.get("payload"))
    table_rows = _rows_for_snapshot(
        session,
        rows_table,
        roots_table,
        int(snapshot["id"]),
        include_row_kind=include_row_kind,
    )
    inline = _inline_rows(payload, consumer=consumer)
    if table_rows and inline:
        _require_agreeing_rows(table_rows, inline, consumer=consumer)
    return payload, (table_rows if table_rows else inline)


def active_current_buy_owner_count(session: Session) -> int:
    """Count the live current BUY owners the purchase journal must describe."""

    if "reservation_entry" not in _table_names(session):
        return 0
    columns = _column_names(session, "reservation_entry")
    if not {"is_current", "owner_kind", "realization_mode"} <= columns:
        return 0
    clauses = [
        "is_current",
        "owner_kind = 'current'",
        "realization_mode = 'buy'",
    ]
    if "lifecycle_status" in columns:
        clauses.append("lifecycle_status = 'active'")
    statement = text(
        "SELECT count(*) FROM reservation_entry WHERE " + " AND ".join(clauses)
    )
    return int(session.execute(statement).scalar_one() or 0)


def live_production_order_line_count(session: Session) -> int:
    """Count the live 1C order lines the production journal must describe.

    The predicate mirrors the canonical journal reader
    (``production_control_journal.list_journal``) and imports its constants
    rather than restating them; it is an emptiness guard for the migration,
    never a second journal engine.
    """

    names = _table_names(session)
    if not {"production_products", "production_orders"} <= names:
        return 0
    from app.services.production_control_common import DONE_STATE_KEY

    clauses = [
        "(o.deletion_mark IS NULL OR NOT o.deletion_mark)",
        "(o.order_state_key IS NULL OR lower(o.order_state_key) <> :done_state)",
        "coalesce(p.quantity, 0) > coalesce(p.produced_qty, 0)",
    ]
    joins = ""
    params: dict[str, Any] = {"done_state": DONE_STATE_KEY}
    if "production_order_line_states" in names:
        from app.services.production_control_journal import _TERMINAL_LINE_STATUSES

        joins = " LEFT JOIN production_order_line_states s ON s.product_id = p.product_id"
        terminal = sorted(str(value) for value in _TERMINAL_LINE_STATUSES)
        placeholders = ", ".join(f":terminal_{index}" for index in range(len(terminal)))
        params.update({f"terminal_{index}": value for index, value in enumerate(terminal)})
        clauses.append(f"coalesce(s.status, 'shortage') NOT IN ({placeholders})")
    statement = text(
        "SELECT count(*) FROM production_products p "
        "JOIN production_orders o ON o.order_id = p.order_id"
        + joins
        + " WHERE "
        + " AND ".join(clauses)
    )
    return int(session.execute(statement, params).scalar_one() or 0)


def fixed_planning_runs(session: Session) -> list[dict[str, Any]]:
    """Return every fixed-snapshot run with its own anchoring generation."""

    planning_run = _optional_table(session, "planning_run")
    if planning_run is None:
        return []
    columns = [planning_run.c.run_id, planning_run.c.ledger_generation_id]
    if "source_plan_id" in planning_run.c.keys():
        columns.append(planning_run.c.source_plan_id)
    rows = session.execute(
        select(*columns)
        .where(planning_run.c.status == _FIXED_RUN_STATUS)
        .order_by(planning_run.c.run_id.asc())
    ).mappings().all()
    return [dict(row) for row in rows]


def legacy_journal_row_count(
    session: Session,
    *,
    consumer: str,
    key: str,
    generation_id: int,
) -> int:
    """Count the rows the legacy journal snapshot actually carried.

    Used by the migration postflight: a published scope with zero rows while
    the source snapshot had rows is a lost scope, not an empty business.
    """

    names = _table_names(session)
    if not {"planning_read_snapshot", "planning_read_row"} <= names:
        return 0
    snapshots = _table(session, "planning_read_snapshot")
    snapshot = _snapshot(
        session, snapshots, consumer=consumer, key=key, generation_id=int(generation_id)
    )
    if snapshot is None:
        return 0
    rows_table = _table(session, "planning_read_row")
    table_count = int(session.execute(
        select(func.count()).select_from(rows_table).where(
            rows_table.c.snapshot_id == int(snapshot["id"])
        )
    ).scalar_one() or 0)
    if table_count:
        return table_count
    if "payload" not in _column_names(session, "planning_read_snapshot"):
        return 0
    try:
        payload = _json_value(snapshot.get("payload"))
    except ValueError:
        return 0
    return len(_inline_rows(payload, consumer=consumer))


def _mrp_payload(
    session: Session,
    snapshot: Mapping[str, Any],
    rows_table: Table,
    roots_table: Table,
    *,
    run_id: int,
) -> dict[str, Any]:
    from app.services.item_ledger.current_execution import (
        _mrp_current_identity,
        _mrp_current_payload,
    )

    rows: list[dict[str, Any]] = []
    counts = {kind: 0 for kind in _KINDS}
    source_payload, source_rows = _snapshot_rows(
        session,
        rows_table,
        roots_table,
        snapshot,
        consumer="mrp_result",
        include_row_kind=True,
    )
    for payload in source_rows:
        payload = dict(payload)
        payload["run_id"] = int(run_id)
        kind = str(payload.get("row_kind") or "").strip().lower()
        if kind not in counts:
            raise ValueError("legacy MRP row kind is malformed")
        counts[kind] += 1
        identity = _mrp_current_identity(payload, run_id=int(run_id), row_kind=kind)
        rows.append({
            "current_identity": identity,
            "payload": _mrp_current_payload(payload, business_identity=identity),
        })
    payload = dict(source_payload)
    payload.update({"run_id": int(run_id), "row_counts": counts, "rows": rows})
    return payload


def publish_current_obligation_views_from_snapshots(
    session: Session,
    generation_id: int,
):
    """Publish legacy evidence through the direct current owner.

    The caller owns the transaction.  No ORM model or legacy class is
    referenced here, so this adapter remains usable after those classes are
    removed in the post-drop migration.
    """
    snapshots = _table(session, "planning_read_snapshot")
    rows_table = _table(session, "planning_read_row")
    roots_table = _table(session, "planning_read_root_member")

    purchase = _snapshot(
        session, snapshots, consumer="purchase_control_journal", key="journal:v1",
        generation_id=generation_id,
    )
    if purchase is None:
        raise ValueError("migration purchase snapshot evidence is missing")
    production = _snapshot(
        session, snapshots, consumer="production_control_journal", key="journal:v1",
        generation_id=generation_id,
    )
    if production is None:
        raise ValueError("migration production snapshot evidence is missing")

    purchase_payload, purchase_rows = _snapshot_rows(
        session, rows_table, roots_table, purchase, consumer="purchase_control_journal",
    )
    purchase_payload["rows"] = purchase_rows
    production_payload, production_rows = _snapshot_rows(
        session, rows_table, roots_table, production, consumer="production_control_journal",
    )
    production_payload["rows"] = production_rows

    if not purchase_rows:
        owners = active_current_buy_owner_count(session)
        if owners:
            raise LegacyEvidenceConflict(
                "legacy purchase journal evidence is empty while "
                f"{owners} active current BUY owners exist"
            )
    if not production_rows:
        lines = live_production_order_line_count(session)
        if lines:
            raise LegacyEvidenceConflict(
                "legacy production journal evidence is empty while "
                f"{lines} live production order lines exist"
            )

    fixed_runs = fixed_planning_runs(session)
    mrp_payloads: dict[str, dict[str, Any]] = {}
    if not fixed_runs and "planning_run" not in _table_names(session):
        # A minimal policy schema has no run catalog; the pointer generation's
        # own snapshots are then the only applicable evidence.
        for snapshot in session.execute(
            select(snapshots)
            .where(
                snapshots.c.consumer == "mrp_result",
                snapshots.c.ledger_generation_id == int(generation_id),
                snapshots.c.truth_status == "accepted",
            )
            .order_by(snapshots.c.snapshot_key.asc())
        ).mappings():
            marker = str(snapshot["snapshot_key"]).removeprefix("run:").split(":", 1)[0]
            if not marker.isdigit():
                raise ValueError("migration MRP snapshot key is malformed")
            mrp_payloads[marker] = _mrp_payload(
                session, snapshot, rows_table, roots_table, run_id=int(marker)
            )
    else:
        for run in fixed_runs:
            run_id = int(run["run_id"])
            snapshot = _latest_snapshot(
                session, snapshots, consumer="mrp_result", key=f"run:{run_id}"
            )
            if snapshot is None:
                # Only a run with no accepted evidence anywhere is a blocker.
                # An accepted snapshot that carries no rows is legitimate: old
                # fixed plans read empty through the legacy reader too.
                raise LegacyEvidenceConflict(
                    f"migration MRP snapshot evidence is missing for fixed run {run_id}"
                )
            mrp_payloads[str(run_id)] = _mrp_payload(
                session, snapshot, rows_table, roots_table, run_id=run_id
            )

    period_payloads: dict[str, dict[str, Any]] = {}

    def _period_payload(snapshot: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        parts = {
            part.split("=", 1)[0]: part.split("=", 1)[1]
            for part in str(snapshot["snapshot_key"]).split(";")
            if "=" in part
        }
        if not parts.get("plan") or not parts.get("run"):
            raise ValueError("migration period execution snapshot key is malformed")
        payload = _json_value(snapshot.get("payload"))
        payload.setdefault("run_id", int(parts["run"]))
        payload.setdefault("plan", {"id": int(parts["plan"])})
        payload.setdefault("facets", {"bom_levels": []})
        payload.setdefault("plan_output_rows", [])
        return f"plan:{int(parts['plan'])}:run:{int(parts['run'])}", payload

    # The pointer generation's own period snapshots are preferred evidence.
    for snapshot in session.execute(
        select(snapshots)
        .where(
            snapshots.c.consumer == "period_plan_execution",
            snapshots.c.ledger_generation_id == int(generation_id),
            snapshots.c.truth_status == "accepted",
        )
        .order_by(snapshots.c.snapshot_key.asc())
    ).mappings():
        key, payload = _period_payload(snapshot)
        period_payloads[key] = payload
    # A fixed run whose period evidence the pointer never republished keeps it
    # at the generation that published it last.
    for run in fixed_runs:
        plan_id = run.get("source_plan_id")
        if plan_id is None:
            continue
        run_id = int(run["run_id"])
        if f"plan:{int(plan_id)}:run:{run_id}" in period_payloads:
            continue
        snapshot = _latest_snapshot(
            session, snapshots, consumer="period_plan_execution",
            key=f"plan={int(plan_id)};run={run_id}",
        )
        if snapshot is None:
            # The canonical publisher already fails closed for a live run that
            # has no period payload; a non-live fixed run must not block here.
            continue
        key, payload = _period_payload(snapshot)
        period_payloads[key] = payload

    from app.services.item_ledger.current_execution import (
        publish_current_obligation_views_from_generation,
    )

    return publish_current_obligation_views_from_generation(
        session,
        int(generation_id),
        purchase_payload=purchase_payload,
        production_payload=production_payload,
        mrp_payloads=mrp_payloads,
        period_payloads=period_payloads,
    )
