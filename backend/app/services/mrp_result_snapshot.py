"""Immutable, Ledger-bound read snapshots for the MRP result screen.

Building a snapshot is an explicit worker/command operation.  HTTP GET handlers
must call :func:`read_mrp_result_rows` (or
:func:`read_mrp_result_manifest`) and never invoke the builder.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import date
from typing import Any, Callable

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.services import planning_service
from app.services.planning_truth import (
    CAPABILITY_EXECUTION_ALLOCATIONS,
    CAPABILITY_PLANNING_SNAPSHOTS,
    PlanningTruthUnavailable,
    get_latest_read_snapshot,
    get_readiness,
    publish_read_snapshot,
    require_accepted_truth,
)


CONSUMER = "mrp_result"
REQUIRED_CAPABILITIES = (
    CAPABILITY_EXECUTION_ALLOCATIONS,
    CAPABILITY_PLANNING_SNAPSHOTS,
)
ROW_KINDS = frozenset({"production", "purchase", "rework", "capacity"})
_MAX_PAGE = 5000


def _snapshot_key(run_id: int) -> str:
    return f"run:{int(run_id)}"


def _unavailable(
    db: Session,
    run_id: int,
    reason: str | None = None,
    *,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    truth = get_readiness(db)
    return {
        "snapshot_id": None,
        "run_id": int(run_id),
        "ledger_generation": truth.generation_id,
        "cutoff": truth.cutoff.isoformat() if truth.cutoff else None,
        "truth_status": truth.status,
        "truth_reason": reason or truth.reason or "MRP result snapshot is unavailable",
        "rows": [],
        "total": 0,
        "total_qty": 0.0,
        "limit": int(limit),
        "offset": int(offset),
    }


def _collect_all(
    getter: Callable[..., dict[str, Any]],
    db: Session,
    run_id: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        result = getter(
            db=db,
            run_id=int(run_id),
            limit=_MAX_PAGE,
            offset=offset,
        )
        page = list(result.get("rows") or [])
        rows.extend(dict(row) for row in page)
        total = int(result.get("total") or 0)
        offset += len(page)
        if not page or offset >= total:
            return rows


def _frozen_root_membership(
    db: Session,
    run: models.PlanningRun,
    item_ids: set[int],
) -> dict[int, set[int]]:
    """Resolve item-to-root membership from the run's frozen BOM only."""
    version = run.active_freeze_version
    if version is None:
        raise ValueError(f"fixed run {run.run_id} has no active freeze version")
    edges = (
        db.query(
            models.MrpFreezeComponent.parent_item_id,
            models.MrpFreezeComponent.component_item_id,
        )
        .filter(
            models.MrpFreezeComponent.run_id == int(run.run_id),
            models.MrpFreezeComponent.freeze_version == int(version),
        )
        .all()
    )
    parents_by_child: dict[int, set[int]] = defaultdict(set)
    parent_ids: set[int] = set()
    child_ids: set[int] = set()
    for parent_id, child_id in edges:
        parent = int(parent_id)
        child = int(child_id)
        parents_by_child[child].add(parent)
        parent_ids.add(parent)
        child_ids.add(child)
    frozen_roots = parent_ids - child_ids

    result: dict[int, set[int]] = {}
    for item_id in item_ids:
        roots: set[int] = set()
        queue: deque[int] = deque([int(item_id)])
        seen: set[int] = set()
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            if current in frozen_roots:
                roots.add(current)
            queue.extend(parents_by_child.get(current, ()))
        result[int(item_id)] = roots
    return result


def _validate_obligation_lineage(
    db: Session,
    run_id: int,
    generation_id: int,
) -> None:
    """Reject every obligation row whose accepted-generation origin is unknown."""
    for model in (models.PlannedOrder, models.PlannedPurchase, models.PlannedRework):
        lineage_column = getattr(model, "ledger_generation_id", None)
        base = db.query(func.count()).select_from(model).filter(
            model.run_id == int(run_id)
        )
        if lineage_column is None:
            if int(base.scalar() or 0):
                raise ValueError(
                    f"{model.__tablename__} rows have no Ledger generation lineage"
                )
            continue
        invalid = base.filter(
            (lineage_column.is_(None)) | (lineage_column != int(generation_id))
        ).scalar()
        if int(invalid or 0):
            raise ValueError(
                f"{model.__tablename__} contains NULL or foreign Ledger generation rows"
            )


def build_mrp_result_snapshot(db: Session, run_id: int) -> models.PlanningReadSnapshot:
    """Build and publish one immutable result snapshot for a fixed run.

    The run must already be bound to the currently accepted Ledger generation.
    Planned result tables are treated as frozen obligations; no legacy fact
    fields are consulted here.
    """
    truth = require_accepted_truth(
        db, CONSUMER, required_capabilities=REQUIRED_CAPABILITIES
    )
    run = db.get(models.PlanningRun, int(run_id))
    if run is None:
        raise ValueError(f"planning run {run_id} not found")
    if str(run.status or "") != "FIXED_SNAPSHOT":
        raise ValueError("MRP result snapshot requires a fixed run")
    if run.ledger_generation_id != truth.generation_id:
        raise ValueError("fixed run is not bound to the accepted Ledger generation")
    if run.ledger_cutoff != truth.cutoff:
        raise ValueError("fixed run cutoff differs from the accepted Ledger cutoff")
    _validate_obligation_lineage(db, int(run_id), int(truth.generation_id))

    existing = get_latest_read_snapshot(
        db,
        consumer=CONSUMER,
        snapshot_key=_snapshot_key(run_id),
        required_capabilities=REQUIRED_CAPABILITIES,
    )
    if existing is not None:
        return existing

    with db.begin_nested():
        rows_by_kind = {
            "production": _collect_all(
                planning_service.get_run_production, db, int(run_id)
            ),
            "purchase": _collect_all(
                planning_service.get_run_purchases, db, int(run_id)
            ),
            "rework": _collect_all(
                planning_service.get_run_rework, db, int(run_id)
            ),
            "capacity": _collect_all(
                planning_service.get_run_capacity, db, int(run_id)
            ),
        }
        summary = planning_service.get_run_summary(db, int(run_id))
        manifest = {
            "run_id": int(run_id),
            "summary": summary,
            "row_counts": {kind: len(rows) for kind, rows in rows_by_kind.items()},
            "total_qty": {
                kind: float(sum(float(row.get("qty") or 0) for row in rows))
                for kind, rows in rows_by_kind.items()
            },
        }
        snapshot = publish_read_snapshot(
            db,
            consumer=CONSUMER,
            snapshot_key=_snapshot_key(run_id),
            payload=manifest,
            required_capabilities=REQUIRED_CAPABILITIES,
        )

        persisted: list[tuple[models.PlanningReadRow, int | None]] = []
        for kind, rows in rows_by_kind.items():
            for index, payload in enumerate(rows):
                item_id = int(payload["item_id"]) if payload.get("item_id") is not None else None
                identity = (
                    payload.get("order_id")
                    or payload.get("purchase_id")
                    or payload.get("rework_id")
                    or payload.get("agg_key")
                    or index
                )
                bucket = str(
                    payload.get("bucket_date")
                    or payload.get("need_date")
                    or payload.get("start_date")
                    or ""
                )
                row = models.PlanningReadRow(
                    snapshot_id=int(snapshot.id),
                    row_key=f"{kind}:{identity}:{index}",
                    row_kind=kind,
                    item_id=item_id,
                    sort_key=f"{bucket}|{item_id or 0:012d}|{index:012d}",
                    payload=dict(payload),
                )
                db.add(row)
                persisted.append((row, item_id))
        db.flush()

        membership = _frozen_root_membership(
            db, run, {item_id for _, item_id in persisted if item_id is not None}
        )
        for row, item_id in persisted:
            if item_id is None:
                continue
            for root_id in sorted(membership.get(item_id, ())):
                db.add(
                    models.PlanningReadRootMember(
                        snapshot_id=int(snapshot.id),
                        row_id=int(row.id),
                        root_key=str(root_id),
                        root_item_id=root_id,
                        payload={"source": "mrp_freeze_component"},
                    )
                )
        db.flush()
    return snapshot


def _resolve_snapshot(
    db: Session, run_id: int, snapshot_id: int | None
) -> models.PlanningReadSnapshot | None:
    latest = get_latest_read_snapshot(
        db,
        consumer=CONSUMER,
        snapshot_key=_snapshot_key(run_id),
        required_capabilities=REQUIRED_CAPABILITIES,
    )
    if latest is None:
        return None
    if snapshot_id is None:
        return latest
    requested = db.get(models.PlanningReadSnapshot, int(snapshot_id))
    if (
        requested is None
        or requested.id != latest.id
        or requested.consumer != CONSUMER
        or requested.snapshot_key != _snapshot_key(run_id)
    ):
        return None
    return requested


def read_mrp_result_manifest(
    db: Session, run_id: int, *, snapshot_id: int | None = None
) -> dict[str, Any]:
    """Read the immutable manifest; never calculate or publish."""
    try:
        snapshot = _resolve_snapshot(db, int(run_id), snapshot_id)
    except PlanningTruthUnavailable as exc:
        return _unavailable(db, run_id, exc.readiness.reason)
    if snapshot is None:
        return _unavailable(db, run_id, "MRP result snapshot is missing")
    payload = dict(snapshot.payload or {})
    summary = dict(payload.pop("summary", {}) or {})
    return {
        "snapshot_id": int(snapshot.id),
        "run_id": int(run_id),
        "ledger_generation": int(snapshot.ledger_generation_id),
        "cutoff": snapshot.cutoff.isoformat(),
        "truth_status": snapshot.truth_status,
        "truth_reason": snapshot.reason,
        **summary,
        "snapshot_counts": payload.get("row_counts", {}),
        "snapshot_total_qty": payload.get("total_qty", {}),
    }


def read_mrp_result_rows(
    db: Session,
    run_id: int,
    *,
    row_kind: str,
    snapshot_id: int | None = None,
    item_id: int | None = None,
    root_item_id: int | None = None,
    area_id: int | None = None,
    date_from: str | date | None = None,
    date_to: str | date | None = None,
    limit: int = 100,
    offset: int = 0,
    sort_dir: str = "asc",
) -> dict[str, Any]:
    """Filter and paginate one immutable snapshot using SQL."""
    kind = str(row_kind or "").strip().lower()
    if kind not in ROW_KINDS:
        raise ValueError(f"unsupported MRP result row kind: {row_kind}")
    effective_limit = max(1, min(int(limit or 100), _MAX_PAGE))
    effective_offset = max(0, int(offset or 0))
    try:
        snapshot = _resolve_snapshot(db, int(run_id), snapshot_id)
    except PlanningTruthUnavailable as exc:
        return _unavailable(
            db, run_id, exc.readiness.reason,
            limit=effective_limit, offset=effective_offset,
        )
    if snapshot is None:
        return _unavailable(
            db, run_id, "MRP result snapshot is missing",
            limit=effective_limit, offset=effective_offset,
        )

    query = db.query(models.PlanningReadRow).filter(
        models.PlanningReadRow.snapshot_id == int(snapshot.id),
        models.PlanningReadRow.row_kind == kind,
    )
    if item_id is not None:
        query = query.filter(models.PlanningReadRow.item_id == int(item_id))
    if root_item_id is not None:
        query = query.join(
            models.PlanningReadRootMember,
            models.PlanningReadRootMember.row_id == models.PlanningReadRow.id,
        ).filter(
            models.PlanningReadRootMember.snapshot_id == int(snapshot.id),
            models.PlanningReadRootMember.root_item_id == int(root_item_id),
        )
    if area_id is not None:
        query = query.filter(
            models.PlanningReadRow.payload["area_id"].as_integer()
            == int(area_id)
        )
    if date_from:
        value = date_from.isoformat() if isinstance(date_from, date) else str(date_from)
        query = query.filter(models.PlanningReadRow.sort_key >= f"{value}|")
    if date_to:
        value = date_to.isoformat() if isinstance(date_to, date) else str(date_to)
        query = query.filter(models.PlanningReadRow.sort_key < f"{value}|\uffff")

    total = int(query.with_entities(func.count(models.PlanningReadRow.id)).scalar() or 0)
    ordering = models.PlanningReadRow.sort_key.desc() if sort_dir == "desc" else models.PlanningReadRow.sort_key.asc()
    records = query.order_by(ordering, models.PlanningReadRow.id).offset(
        effective_offset
    ).limit(effective_limit).all()
    payloads = [dict(record.payload or {}) for record in records]
    if (
        item_id is None
        and root_item_id is None
        and area_id is None
        and not date_from
        and not date_to
    ):
        total_qty = float(
            ((snapshot.payload or {}).get("total_qty") or {}).get(kind, 0.0)
        )
    else:
        total_qty = float(
            sum(
                float((payload or {}).get("qty") or 0)
                for (payload,) in query.with_entities(
                    models.PlanningReadRow.payload
                ).all()
            )
        )
    return {
        "snapshot_id": int(snapshot.id),
        "run_id": int(run_id),
        "ledger_generation": int(snapshot.ledger_generation_id),
        "cutoff": snapshot.cutoff.isoformat(),
        "truth_status": snapshot.truth_status,
        "truth_reason": snapshot.reason,
        "rows": payloads,
        "total": total,
        "total_qty": total_qty,
        "limit": effective_limit,
        "offset": effective_offset,
    }
