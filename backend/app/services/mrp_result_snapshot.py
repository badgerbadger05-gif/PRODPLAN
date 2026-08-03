"""Immutable, Ledger-bound read snapshots for the MRP result screen.

Building a snapshot is an explicit worker/command operation.  HTTP GET handlers
must call :func:`read_mrp_result_rows` (or
:func:`read_mrp_result_manifest`) and never invoke the builder.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from typing import Any, Callable, Mapping

from sqlalchemy import and_, func
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
from app.services.planning_run_candidate import _resolve_parent_generation_id


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
    allowed_generation_ids = {int(generation_id)}
    cursor = db.get(models.LedgerGeneration, int(generation_id))
    while cursor is not None:
        marks = dict(cursor.source_watermarks or {})
        try:
            parent_id = int(marks["parent_generation_id"])
        except (KeyError, TypeError, ValueError):
            break
        if parent_id in allowed_generation_ids:
            break
        allowed_generation_ids.add(parent_id)
        cursor = db.get(models.LedgerGeneration, parent_id)
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
            (lineage_column.is_(None))
            | (~lineage_column.in_(sorted(allowed_generation_ids)))
        ).scalar()
        if int(invalid or 0):
            raise ValueError(
                f"{model.__tablename__} contains NULL or foreign Ledger generation rows"
            )


def _collect_snapshot_payload(
    db: Session, run: models.PlanningRun
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Collect frozen MRP rows once for either an accepted or candidate build.

    This is deliberately a builder-only helper.  Read handlers only query the
    persisted ``PlanningRead*`` tables and must never call it.
    """
    run_id = int(run.run_id)
    rows_by_kind = {
        "production": _collect_all(planning_service.get_run_production, db, run_id),
        "purchase": _collect_all(planning_service.get_run_purchases, db, run_id),
        "rework": _collect_all(planning_service.get_run_rework, db, run_id),
        "capacity": _collect_all(planning_service.get_run_capacity, db, run_id),
    }
    manifest = {
        "run_id": run_id,
        "summary": planning_service.get_run_summary(db, run_id),
        "row_counts": {kind: len(rows) for kind, rows in rows_by_kind.items()},
        "total_qty": {
            kind: float(sum(float(row.get("qty") or 0) for row in rows))
            for kind, rows in rows_by_kind.items()
        },
    }
    return rows_by_kind, manifest


def _row_specs(
    rows_by_kind: dict[str, list[dict[str, Any]]],
) -> list[tuple[str, str, int | None, str, dict[str, Any]]]:
    """Create deterministic persisted row identities without writing them."""
    specs: list[tuple[str, str, int | None, str, dict[str, Any]]] = []
    for kind, rows in rows_by_kind.items():
        for index, source_payload in enumerate(rows):
            payload = dict(source_payload)
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
            specs.append(
                (
                    f"{kind}:{identity}:{index}",
                    kind,
                    item_id,
                    f"{bucket}|{item_id or 0:012d}|{index:012d}",
                    payload,
                )
            )
    return specs


def _candidate_snapshot_matches(
    db: Session,
    snapshot: models.PlanningReadSnapshot,
    *,
    manifest: dict[str, Any],
    row_specs: list[tuple[str, str, int | None, str, dict[str, Any]]],
    membership: dict[int, set[int]],
) -> bool:
    """An existing candidate is reusable only for the exact same frozen data."""
    if dict(snapshot.payload or {}) != manifest:
        return False
    rows = (
        db.query(models.PlanningReadRow)
        .filter(models.PlanningReadRow.snapshot_id == int(snapshot.id))
        .order_by(models.PlanningReadRow.id)
        .all()
    )
    if len(rows) != len(row_specs):
        return False
    for row, (row_key, kind, item_id, sort_key, payload) in zip(rows, row_specs):
        if (
            row.row_key != row_key
            or row.row_kind != kind
            or row.item_id != item_id
            or row.sort_key != sort_key
            or dict(row.payload or {}) != payload
        ):
            return False
        expected_roots = sorted(membership.get(item_id, ())) if item_id is not None else []
        actual_roots = [
            int(root_id)
            for (root_id,) in db.query(models.PlanningReadRootMember.root_item_id)
            .filter(
                models.PlanningReadRootMember.snapshot_id == int(snapshot.id),
                models.PlanningReadRootMember.row_id == int(row.id),
            )
            .order_by(models.PlanningReadRootMember.root_item_id)
            .all()
        ]
        if actual_roots != expected_roots:
            return False
    return True


def _require_sealed_candidate_manifest(
    db: Session,
    generation: models.LedgerGeneration,
    run: models.PlanningRun,
) -> None:
    """Prove that ``run`` belongs to the closed refresh batch.

    A BUILDING generation alone is intentionally insufficient: otherwise a
    stray run could obtain persisted result rows and later look publishable.
    The manifest hash, complete candidate set, and each action-specific run
    lineage are checked before any snapshot rows are written.
    """
    marks = dict(generation.source_watermarks or {})
    payload = marks.get("obligation_refresh_manifest")
    content_hash = marks.get("obligation_refresh_manifest_hash")
    if not isinstance(payload, Mapping) or not isinstance(content_hash, str):
        raise ValueError("candidate snapshot target lacks a sealed obligation_refresh_manifest")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if sha256(canonical.encode("utf-8")).hexdigest() != content_hash:
        raise ValueError("candidate snapshot obligation_refresh_manifest hash conflicts")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("candidate snapshot obligation_refresh_manifest is malformed")

    declared_ids: set[int] = set()
    declared_plan_ids: set[int] = set()
    requested_found = False
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("candidate snapshot obligation_refresh_manifest entry is malformed")
        try:
            action = str(entry["action"])
            plan_id = int(entry["plan_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "candidate snapshot obligation_refresh_manifest entry identity is malformed"
            ) from exc
        if action in {"retain", "retire"}:
            # ``retire`` differs from ``retain`` only in what the publisher does
            # with the named parent run; neither owns a candidate, so the sealed
            # -set proof is the same.  Without this branch a refresh that both
            # adds and closes a plan died on ``int(None)`` here.
            if (
                plan_id <= 0
                or plan_id in declared_plan_ids
                or entry.get("candidate_run_id") is not None
                or entry.get("parent_run_id") is None
            ):
                raise ValueError(
                    "candidate snapshot obligation_refresh_manifest has invalid "
                    f"{action} entry"
                )
            declared_plan_ids.add(plan_id)
            continue
        try:
            candidate_id = int(entry["candidate_run_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "candidate snapshot obligation_refresh_manifest candidate identity is malformed"
            ) from exc
        if (
            action != "add"
            or candidate_id <= 0
            or plan_id <= 0
            or candidate_id in declared_ids
            or plan_id in declared_plan_ids
        ):
            raise ValueError("candidate snapshot obligation_refresh_manifest has invalid entries")
        candidate = db.get(models.PlanningRun, candidate_id)
        if (
            candidate is None
            or str(candidate.status or "") != "BUILDING_SNAPSHOT"
            or int(candidate.ledger_generation_id or -1) != int(generation.id)
            or int(candidate.source_plan_id or -1) != plan_id
        ):
            raise ValueError("candidate snapshot manifest candidate lineage conflicts")
        if entry.get("parent_run_id") is not None or candidate.prior_run_id is not None:
            raise ValueError("candidate snapshot add entry must not have a parent run")
        plan = db.get(models.ProductionPlanHeader, plan_id)
        if (
            plan is None
            or str(plan.status or "") != "fixed"
            or candidate.period_from != plan.period_from
            or candidate.period_to != plan.period_to
        ):
            raise ValueError("candidate snapshot add candidate parent conflicts")
        declared_ids.add(candidate_id)
        declared_plan_ids.add(plan_id)
        requested_found = requested_found or candidate_id == int(run.run_id)

    actual_ids = {
        int(candidate_id)
        for (candidate_id,) in db.query(models.PlanningRun.run_id).filter(
            models.PlanningRun.ledger_generation_id == int(generation.id),
            models.PlanningRun.status == "BUILDING_SNAPSHOT",
        ).all()
    }
    if actual_ids != declared_ids:
        raise ValueError("candidate snapshot manifest has missing or extra candidates")
    if not requested_found:
        raise ValueError("candidate run is absent from sealed obligation_refresh_manifest")


def build_mrp_result_candidate_snapshot(
    db: Session, run_id: int
) -> models.PlanningReadSnapshot:
    """Persist an unpublished MRP result snapshot for a building candidate.

    Candidate snapshots are bound to their BUILDING ``obligation_refresh``
    Ledger generation.  They intentionally bypass accepted-truth readiness and
    ``publish_read_snapshot``: nothing built here is visible to normal GET
    reads until the outer publish transaction promotes its run and generation.
    The caller owns the transaction; this function never commits or rolls back.
    """
    run = db.get(models.PlanningRun, int(run_id))
    if run is None:
        raise ValueError(f"planning run {run_id} not found")
    if str(run.status or "") != "BUILDING_SNAPSHOT":
        raise ValueError("candidate MRP result snapshot requires a building run")
    if run.ledger_generation_id is None:
        raise ValueError("candidate run has no Ledger generation")
    generation = db.get(models.LedgerGeneration, int(run.ledger_generation_id))
    if generation is None or str(generation.status or "") != "building":
        raise ValueError("candidate run is not bound to a BUILDING Ledger generation")
    if (generation.source_watermarks or {}).get("generation_kind") != "obligation_refresh":
        raise ValueError("candidate Ledger generation is not an obligation_refresh")
    if run.ledger_cutoff is None or generation.cutoff is None or run.ledger_cutoff != generation.cutoff:
        raise ValueError("candidate run cutoff differs from the BUILDING Ledger cutoff")
    _require_sealed_candidate_manifest(db, generation, run)
    _validate_obligation_lineage(db, int(run.run_id), int(generation.id))

    rows_by_kind, manifest = _collect_snapshot_payload(db, run)
    row_specs = _row_specs(rows_by_kind)
    membership = _frozen_root_membership(
        db, run, {item_id for _, _, item_id, _, _ in row_specs if item_id is not None}
    )
    existing = (
        db.query(models.PlanningReadSnapshot)
        .filter(
            models.PlanningReadSnapshot.consumer == CONSUMER,
            models.PlanningReadSnapshot.snapshot_key == _snapshot_key(run.run_id),
            models.PlanningReadSnapshot.ledger_generation_id == int(generation.id),
        )
        .one_or_none()
    )
    if existing is not None:
        if (
            existing.cutoff != generation.cutoff
            or existing.truth_status != "building"
            or not _candidate_snapshot_matches(
                db, existing, manifest=manifest, row_specs=row_specs, membership=membership
            )
        ):
            raise ValueError("candidate MRP result snapshot conflicts with persisted data")
        return existing

    with db.begin_nested():
        snapshot = models.PlanningReadSnapshot(
            consumer=CONSUMER,
            snapshot_key=_snapshot_key(run.run_id),
            ledger_generation_id=int(generation.id),
            cutoff=generation.cutoff,
            truth_status="building",
            reason="unpublished candidate snapshot",
            payload=manifest,
            published_at=datetime.now(timezone.utc),
        )
        db.add(snapshot)
        db.flush()
        persisted: list[tuple[models.PlanningReadRow, int | None]] = []
        for row_key, kind, item_id, sort_key, payload in row_specs:
            row = models.PlanningReadRow(
                snapshot_id=int(snapshot.id), row_key=row_key, row_kind=kind,
                item_id=item_id, sort_key=sort_key, payload=payload,
            )
            db.add(row)
            persisted.append((row, item_id))
        db.flush()
        for row, item_id in persisted:
            if item_id is None:
                continue
            for root_id in sorted(membership.get(item_id, ())):
                db.add(models.PlanningReadRootMember(
                    snapshot_id=int(snapshot.id), row_id=int(row.id),
                    root_key=str(root_id), root_item_id=root_id,
                    payload={"source": "mrp_freeze_component"},
                ))
        db.flush()
    return snapshot


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
    supplier_ref1c: str | None = None,
    category_id: int | None = None,
    category_ref1c: str | None = None,
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
    if supplier_ref1c is not None:
        if supplier_ref1c == "__missing_supplier_name":
            supplier_name = func.trim(
                func.coalesce(models.PlanningReadRow.payload["supplier_name"].as_string(), "")
            )
            query = query.filter(
                supplier_name == ""
            )
        else:
            query = query.filter(
                func.coalesce(
                    models.PlanningReadRow.payload["supplier_ref1c"].as_string(),
                    "",
                )
                == str(supplier_ref1c)
            )
    if category_id is not None:
        query = query.filter(
            models.PlanningReadRow.payload["category_id"].as_integer() == int(category_id)
        )
    elif category_ref1c is not None:
        if category_ref1c == "__missing_category":
            category_ref = func.trim(
                func.coalesce(
                    models.PlanningReadRow.payload["category_ref1c"].as_string(),
                    "",
                )
            )
            query = query.filter(
                and_(
                    models.PlanningReadRow.payload["category_id"].as_integer().is_(None),
                    category_ref == "",
                )
            )
        else:
            query = query.filter(
                func.coalesce(
                    models.PlanningReadRow.payload["category_ref1c"].as_string(),
                    "",
                )
                == str(category_ref1c)
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
