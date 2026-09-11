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

from sqlalchemy import MetaData, Table, and_, select
from sqlalchemy.orm import Session


_KINDS = ("production", "purchase", "rework", "capacity")


def _table(session: Session, name: str) -> Table:
    return Table(name, MetaData(), autoload_with=session.get_bind())


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


def _rows_for_snapshot(
    session: Session,
    rows_table: Table,
    roots_table: Table,
    snapshot_id: int,
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
        payload.setdefault("row_kind", str(row.get("row_kind") or ""))
        payload.setdefault("sort_key", str(row.get("sort_key") or ""))
        result.append(payload)
    return result


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
    for payload in _rows_for_snapshot(session, rows_table, roots_table, int(snapshot["id"])):
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
    payload = _json_value(snapshot.get("payload"))
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

    purchase_payload = _json_value(purchase.get("payload"))
    purchase_payload["rows"] = _rows_for_snapshot(
        session, rows_table, roots_table, int(purchase["id"])
    )
    production_payload = _json_value(production.get("payload"))
    production_payload["rows"] = _rows_for_snapshot(
        session, rows_table, roots_table, int(production["id"])
    )

    mrp_payloads: dict[str, dict[str, Any]] = {}
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

    period_payloads: dict[str, dict[str, Any]] = {}
    for snapshot in session.execute(
        select(snapshots)
        .where(
            snapshots.c.consumer == "period_plan_execution",
            snapshots.c.ledger_generation_id == int(generation_id),
            snapshots.c.truth_status == "accepted",
        )
        .order_by(snapshots.c.snapshot_key.asc())
    ).mappings():
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
        period_payloads[f"plan:{int(parts['plan'])}:run:{int(parts['run'])}"] = payload

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
