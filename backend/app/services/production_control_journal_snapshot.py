"""Persisted read boundary for the production-control journal.

The expensive journal projection is built while a Ledger generation is still
``building``.  Public GETs only page immutable rows belonging to the current
accepted generation; they never rebuild the projection or fall back to live
operational tables.

Mutation services intentionally remain separate.  They validate and write
commands against normalized state, while a later worker publication produces
the next read model.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from fastapi.encoders import jsonable_encoder
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from app import models
from app.services.bom_specification_resolver import BomSpecificationResolver
from app.services.planning_truth import (
    CAPABILITY_EXECUTION_ALLOCATIONS,
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_PLANNING_SNAPSHOTS,
    CAPABILITY_PRODUCTION_CONTROL_JOURNAL,
    CAPABILITY_RESERVATION_REPLAY,
    PlanningTruthReadiness,
    PlanningTruthUnavailable,
    get_latest_read_snapshot,
    get_truth_state,
)
from app.services.production_control_journal import STATUS_FILTER_GROUPS, list_journal


CONSUMER = "production_control_journal"
SNAPSHOT_KEY = "journal:v1"
ROW_KIND = "production_order"
REQUIRED = (
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_RESERVATION_REPLAY,
    CAPABILITY_EXECUTION_ALLOCATIONS,
    CAPABILITY_PLANNING_SNAPSHOTS,
    CAPABILITY_PRODUCTION_CONTROL_JOURNAL,
)
_PAGE_SIZE = 500


class ProductionControlJournalSnapshotUnavailable(RuntimeError):
    def __init__(self, detail: dict[str, Any]):
        self.detail = detail
        super().__init__(detail["reason"])

    def as_dict(self) -> dict[str, Any]:
        return dict(self.detail)


class ProductionControlJournalPromotionError(RuntimeError):
    """A building journal candidate cannot be exposed as accepted truth."""


def _unavailable(
    db: Session,
    reason: str,
    truth: Mapping[str, Any] | None = None,
) -> ProductionControlJournalSnapshotUnavailable:
    state = get_truth_state(db)
    detail: dict[str, Any] = {
        "code": "production_control_journal_snapshot_unavailable",
        "consumer": CONSUMER,
        "status": "unavailable",
        "truth_status": state.status,
        "ledger_generation": state.generation_id,
        "cutoff": state.cutoff.isoformat() if state.cutoff else None,
        "reason": reason,
    }
    if truth is not None:
        detail["truth"] = jsonable_encoder(dict(truth))
    return ProductionControlJournalSnapshotUnavailable(detail)


def _candidate_truth(generation: models.LedgerGeneration) -> PlanningTruthReadiness:
    return PlanningTruthReadiness(
        truth_status="building",
        ready=False,
        ledger_generation=int(generation.id),
        generation_key=str(generation.generation_key or ""),
        cutoff=generation.cutoff,
        source_watermarks=dict(generation.source_watermarks or {}),
        capabilities={
            str(name): bool(enabled)
            for name, enabled in dict(generation.capabilities or {}).items()
        },
        algorithm_version=generation.algorithm_version,
        replay_version=generation.replay_version,
        reason="unpublished production-control journal candidate",
        accepted_at=None,
    )


def _build_rows(
    db: Session,
    generation: models.LedgerGeneration,
    accepted_run_ids: Sequence[int],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    truth = _candidate_truth(generation)
    run_ids = tuple(sorted({int(value) for value in accepted_run_ids}))
    rows: list[dict[str, Any]] = []
    offset = 0
    total = 0
    latest_run_id: int | None = None
    latest_source_plan_id: int | None = None
    while True:
        page = list_journal(
            db,
            truth=truth,
            _accepted_run_ids_override=run_ids,
            limit=_PAGE_SIZE,
            offset=offset,
        )
        if offset == 0:
            total = int(page["total"])
            latest_run_id = page.get("latest_run_id")
            latest_source_plan_id = page.get("latest_source_plan_id")
        page_rows = page.get("rows")
        if not isinstance(page_rows, list):
            raise ValueError("production-control journal builder returned malformed rows")
        rows.extend(dict(row) for row in page_rows)
        if len(rows) >= total:
            break
        if not page_rows:
            raise ValueError("production-control journal builder stopped before total")
        offset += len(page_rows)

    if len(rows) != total:
        raise ValueError("production-control journal builder row count changed during build")
    product_ids = [int(row["product_id"]) for row in rows]
    if len(product_ids) != len(set(product_ids)):
        raise ValueError("production-control journal candidate has duplicate products")
    return rows, {
        "latest_run_id": latest_run_id,
        "latest_source_plan_id": latest_source_plan_id,
    }


def _root_membership_by_product(
    db: Session,
    *,
    rows: Sequence[Mapping[str, Any]],
    accepted_run_ids: Sequence[int],
) -> dict[int, set[int]]:
    run_ids = sorted({int(value) for value in accepted_run_ids})
    if not rows or not run_ids:
        return {}
    run_plan_rows = (
        db.query(models.PlanningRun.run_id, models.PlanningRun.source_plan_id)
        .filter(models.PlanningRun.run_id.in_(run_ids))
        .filter(models.PlanningRun.source_plan_id.isnot(None))
        .all()
    )
    plan_by_run = {int(run_id): int(plan_id) for run_id, plan_id in run_plan_rows}
    if not plan_by_run:
        return {}
    roots_by_plan: dict[int, set[int]] = {}
    for plan_id, item_id in (
        db.query(models.ProductionPlanLine.plan_id, models.ProductionPlanLine.item_id)
        .filter(models.ProductionPlanLine.plan_id.in_(sorted(set(plan_by_run.values()))))
        .all()
    ):
        roots_by_plan.setdefault(int(plan_id), set()).add(int(item_id))
    root_ids = sorted({root for roots in roots_by_plan.values() for root in roots})
    descendants = (
        BomSpecificationResolver(db).descendant_ids_by_root(root_ids)
        if root_ids
        else {}
    )
    result: dict[int, set[int]] = {}
    for row in rows:
        source_run_id = row.get("source_run_id")
        if source_run_id is None:
            continue
        plan_id = plan_by_run.get(int(source_run_id))
        if plan_id is None:
            continue
        item_id = int(row["item_id"])
        matched = {
            root_id
            for root_id in roots_by_plan.get(plan_id, set())
            if item_id in descendants.get(root_id, {root_id})
        }
        if matched:
            result[int(row["product_id"])] = matched
    return result


def _persisted_candidate_matches(
    db: Session,
    *,
    snapshot: models.PlanningReadSnapshot,
    payload: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    roots_by_product: Mapping[int, set[int]],
) -> bool:
    if (
        snapshot.truth_status != "building"
        or snapshot.reason is None
        or snapshot.payload != dict(payload)
    ):
        return False
    persisted = (
        db.query(models.PlanningReadRow)
        .filter(models.PlanningReadRow.snapshot_id == int(snapshot.id))
        .order_by(models.PlanningReadRow.row_key.asc())
        .all()
    )
    expected = {
        f"product:{int(row['product_id'])}": dict(row)
        for row in rows
    }
    if len(persisted) != len(expected):
        return False
    for row in persisted:
        if (
            row.row_kind != ROW_KIND
            or row.payload != expected.get(str(row.row_key))
            or int(row.item_id or -1) != int(row.payload.get("item_id") or -1)
        ):
            return False
        actual_roots = {int(member.root_item_id) for member in row.root_members}
        product_id = int(row.payload["product_id"])
        if actual_roots != set(roots_by_product.get(product_id, set())):
            return False
    return True


def build_candidate_snapshot(
    db: Session,
    generation_id: int,
    *,
    accepted_run_ids: Sequence[int],
) -> models.PlanningReadSnapshot:
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if (
        generation is None
        or str(generation.status) != "building"
        or generation.cutoff is None
    ):
        raise ValueError(
            "production-control journal candidate requires BUILDING Ledger generation"
        )
    run_ids = tuple(sorted({int(value) for value in accepted_run_ids}))
    rows, journal_meta = _build_rows(db, generation, run_ids)
    roots_by_product = _root_membership_by_product(
        db,
        rows=rows,
        accepted_run_ids=run_ids,
    )
    payload = {
        "meta": {
            "ledger_generation_id": int(generation.id),
            "cutoff": generation.cutoff.isoformat(),
            "truth_status": "building",
            "read_only": True,
            "row_count": len(rows),
            "accepted_run_ids": list(run_ids),
            **journal_meta,
        }
    }
    existing = (
        db.query(models.PlanningReadSnapshot)
        .filter_by(
            consumer=CONSUMER,
            snapshot_key=SNAPSHOT_KEY,
            ledger_generation_id=int(generation.id),
        )
        .one_or_none()
    )
    if existing is not None:
        if not _persisted_candidate_matches(
            db,
            snapshot=existing,
            payload=payload,
            rows=rows,
            roots_by_product=roots_by_product,
        ):
            raise ValueError("production-control journal candidate conflict")
        return existing

    snapshot = models.PlanningReadSnapshot(
        consumer=CONSUMER,
        snapshot_key=SNAPSHOT_KEY,
        ledger_generation_id=int(generation.id),
        cutoff=generation.cutoff,
        truth_status="building",
        reason="unpublished production-control journal",
        payload=payload,
        published_at=datetime.now(timezone.utc),
    )
    db.add(snapshot)
    db.flush()
    for position, payload_row in enumerate(rows):
        product_id = int(payload_row["product_id"])
        row = models.PlanningReadRow(
            snapshot_id=int(snapshot.id),
            row_key=f"product:{product_id}",
            row_kind=ROW_KIND,
            item_id=int(payload_row["item_id"]),
            sort_key=f"{position:012d}",
            payload=dict(payload_row),
        )
        db.add(row)
        db.flush()
        for root_item_id in sorted(roots_by_product.get(product_id, set())):
            db.add(
                models.PlanningReadRootMember(
                    snapshot_id=int(snapshot.id),
                    row_id=int(row.id),
                    root_key=f"item:{root_item_id}",
                    root_item_id=int(root_item_id),
                )
            )
    db.flush()
    return snapshot


def validate_candidate_snapshot(
    db: Session,
    candidate: models.PlanningReadSnapshot,
    generation: models.LedgerGeneration,
) -> None:
    payload = candidate.payload if isinstance(candidate.payload, dict) else None
    meta = payload.get("meta") if payload else None
    if (
        not isinstance(meta, dict)
        or meta.get("read_only") is not True
        or int(meta.get("ledger_generation_id") or -1) != int(generation.id)
        or meta.get("truth_status") != "building"
    ):
        raise ProductionControlJournalPromotionError(
            "production-control journal candidate is missing or stale"
        )
    try:
        expected_count = int(meta["row_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductionControlJournalPromotionError(
            "production-control journal candidate row count is malformed"
        ) from exc
    rows = (
        db.query(models.PlanningReadRow)
        .filter(
            models.PlanningReadRow.snapshot_id == int(candidate.id),
            models.PlanningReadRow.row_kind == ROW_KIND,
        )
        .all()
    )
    if expected_count < 0 or len(rows) != expected_count:
        raise ProductionControlJournalPromotionError(
            "production-control journal candidate rows are incomplete"
        )
    product_ids: set[int] = set()
    for row in rows:
        payload_row = row.payload if isinstance(row.payload, dict) else None
        try:
            product_id = int(payload_row["product_id"]) if payload_row else -1
            item_id = int(payload_row["item_id"]) if payload_row else -1
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductionControlJournalPromotionError(
                "production-control journal candidate row is malformed"
            ) from exc
        if (
            product_id <= 0
            or item_id <= 0
            or product_id in product_ids
            or row.row_key != f"product:{product_id}"
            or int(row.item_id or -1) != item_id
        ):
            raise ProductionControlJournalPromotionError(
                "production-control journal candidate row is malformed"
            )
        product_ids.add(product_id)


def promote_candidate_snapshot(
    db: Session,
    *,
    generation: models.LedgerGeneration,
    accepted_at: datetime,
) -> models.PlanningReadSnapshot | None:
    candidate = (
        db.query(models.PlanningReadSnapshot)
        .filter(
            models.PlanningReadSnapshot.consumer == CONSUMER,
            models.PlanningReadSnapshot.snapshot_key == SNAPSHOT_KEY,
            models.PlanningReadSnapshot.ledger_generation_id == int(generation.id),
            models.PlanningReadSnapshot.truth_status == "building",
            models.PlanningReadSnapshot.cutoff == generation.cutoff,
        )
        .one_or_none()
    )
    if candidate is None:
        return None
    validate_candidate_snapshot(db, candidate, generation)
    candidate.truth_status = "accepted"
    candidate.reason = None
    candidate.published_at = accepted_at
    db.flush()
    return candidate


def read_snapshot(
    db: Session,
    *,
    product_id: int | None = None,
    order_id: int | None = None,
    root_item_id: int | None = None,
    workshop_id: int | None = None,
    status: str | None = None,
    coverage_status: str | None = None,
    planning_contour: str | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    try:
        snapshot = get_latest_read_snapshot(
            db,
            consumer=CONSUMER,
            snapshot_key=SNAPSHOT_KEY,
            required_capabilities=REQUIRED,
        )
    except PlanningTruthUnavailable as exc:
        raise _unavailable(db, str(exc), exc.as_dict()) from exc
    if snapshot is None:
        raise _unavailable(
            db,
            "accepted production-control journal snapshot is missing",
        )
    payload = snapshot.payload if isinstance(snapshot.payload, dict) else None
    meta = payload.get("meta") if payload else None
    if (
        not isinstance(meta, dict)
        or meta.get("read_only") is not True
        or int(meta.get("ledger_generation_id") or -1)
        != int(snapshot.ledger_generation_id)
    ):
        raise _unavailable(
            db,
            "accepted production-control journal snapshot is malformed",
        )

    query = db.query(models.PlanningReadRow).filter(
        models.PlanningReadRow.snapshot_id == int(snapshot.id),
        models.PlanningReadRow.row_kind == ROW_KIND,
    )
    row_payload = models.PlanningReadRow.payload
    if product_id is not None:
        query = query.filter(row_payload["product_id"].as_integer() == int(product_id))
    if order_id is not None:
        query = query.filter(row_payload["order_id"].as_integer() == int(order_id))
    if root_item_id is not None:
        query = query.join(
            models.PlanningReadRootMember,
            models.PlanningReadRootMember.row_id == models.PlanningReadRow.id,
        ).filter(
            models.PlanningReadRootMember.snapshot_id == int(snapshot.id),
            models.PlanningReadRootMember.root_item_id == int(root_item_id),
        )
    if workshop_id is not None:
        query = query.filter(
            row_payload["workshop_id"].as_integer() == int(workshop_id)
        )
    if status:
        values = STATUS_FILTER_GROUPS.get(str(status), (str(status),))
        query = query.filter(row_payload["status"].as_string().in_(values))
    if coverage_status:
        query = query.filter(
            row_payload["coverage_status"].as_string() == str(coverage_status)
        )
    if planning_contour:
        contour = str(planning_contour).strip().lower()
        if contour not in {"mrp", "1c"}:
            raise ValueError("unknown planning_contour")
        query = query.filter(row_payload["order_source"].as_string() == contour)
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        query = query.filter(
            or_(
                row_payload["order_number"].as_string().ilike(pattern),
                row_payload["item_name"].as_string().ilike(pattern),
                row_payload["item_article"].as_string().ilike(pattern),
                row_payload["item_code"].as_string().ilike(pattern),
            )
        )
    if date_from:
        query = query.filter(row_payload["order_date"].as_string() >= str(date_from))
    if date_to:
        query = query.filter(row_payload["order_date"].as_string() <= str(date_to))

    total = int(query.with_entities(func.count(models.PlanningReadRow.id)).scalar() or 0)
    effective_limit = max(1, min(int(limit or 100), 500))
    requested_offset = max(0, int(offset or 0))
    max_offset = (
        max(0, ((total - 1) // effective_limit) * effective_limit)
        if total
        else 0
    )
    effective_offset = min(requested_offset, max_offset)
    sort_field = str(sort_by or "").strip().lower()
    descending = str(sort_dir or "").strip().lower() == "desc"
    if sort_field in {"planned_start_date", "planned_finish_date"}:
        expression = row_payload[sort_field].as_string()
        ordering = expression.desc() if descending else expression.asc()
        query = query.order_by(
            case((expression.is_(None), 1), else_=0),
            ordering,
            row_payload["order_number"].as_string().asc(),
            row_payload["line_number"].as_integer().asc(),
        )
    else:
        query = query.order_by(
            row_payload["order_date"].as_string().desc(),
            row_payload["order_number"].as_string().asc(),
            row_payload["line_number"].as_integer().asc(),
        )
    records = query.offset(effective_offset).limit(effective_limit).all()
    return {
        "rows": [dict(record.payload) for record in records],
        "total": total,
        "limit": effective_limit,
        "offset": effective_offset,
        "latest_run_id": meta.get("latest_run_id"),
        "latest_source_plan_id": meta.get("latest_source_plan_id"),
    }
