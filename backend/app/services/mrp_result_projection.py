"""Current, Ledger-bound MRP result projection.

The pure payload builder is used by accepted-generation publication. HTTP GET
handlers read only the compact current execution scope and rows.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import date
from hashlib import sha256
import json
from typing import Any, Callable, Mapping

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.services import planning_service
from app.services.item_ledger.live_plan_scope import (
    sealed_generation_lineage_ids,
)
from app.services.planning_truth import (
    CAPABILITY_EXECUTION_ALLOCATIONS,
    CAPABILITY_PLANNING_SNAPSHOTS,
    get_readiness,
)
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    load_current_execution_rows,
    require_current_execution_scope,
)


CONSUMER = "mrp_result"
REQUIRED_CAPABILITIES = (
    CAPABILITY_EXECUTION_ALLOCATIONS,
    CAPABILITY_PLANNING_SNAPSHOTS,
)
ROW_KINDS = frozenset({"production", "purchase", "rework", "capacity"})
_MAX_PAGE = 5000



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
        "current_scope_id": None,
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


def _accepted_generation(
    db: Session, generation_id: int
) -> models.LedgerGeneration:
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None:
        raise ValueError(f"Ledger generation {int(generation_id)} not found")
    return generation


def _validate_obligation_lineage(
    db: Session,
    run_id: int,
    generation_id: int,
) -> None:
    """Reject every obligation row whose accepted-generation origin is unknown."""
    allowed_generation_ids = set(
        sealed_generation_lineage_ids(db, _accepted_generation(db, generation_id))
    )
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


def _collect_mrp_payload(
    db: Session, run: models.PlanningRun
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Collect frozen MRP rows once for an accepted or candidate payload build.

    This is deliberately a builder-only helper. Read handlers never call it.
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


def build_mrp_result_current_payload(
    db: Session,
    run_id: int,
) -> dict[str, Any]:
    """Build the direct current-owner payload for one MRP run.

    This is a pure candidate builder: it reads the canonical planning
    projection and returns a validated manifest/row payload for the compact
    current execution owner.
    """
    run = db.get(models.PlanningRun, int(run_id))
    if run is None:
        raise ValueError(f"planning run {run_id} not found")
    status = str(run.status or "")
    if status == "BUILDING_SNAPSHOT":
        if run.ledger_generation_id is None:
            raise ValueError("candidate run has no Ledger generation")
        generation = db.get(models.LedgerGeneration, int(run.ledger_generation_id))
        if generation is None or str(generation.status or "") != "building":
            raise ValueError("candidate run is not bound to a BUILDING Ledger generation")
        if (generation.source_watermarks or {}).get("generation_kind") != "obligation_refresh":
            raise ValueError("candidate Ledger generation is not an obligation_refresh")
        if run.ledger_cutoff != generation.cutoff:
            raise ValueError("candidate run cutoff differs from the BUILDING Ledger cutoff")
        _require_sealed_candidate_manifest(db, generation, run)
        _validate_obligation_lineage(db, int(run.run_id), int(generation.id))
    elif status == "FIXED_SNAPSHOT":
        if run.ledger_generation_id is None:
            raise ValueError("fixed run has no Ledger generation")
        generation = db.get(models.LedgerGeneration, int(run.ledger_generation_id))
        if generation is None or str(generation.status or "") != "accepted":
            raise ValueError("fixed run is not bound to an accepted Ledger generation")
        if run.ledger_cutoff != generation.cutoff:
            raise ValueError("fixed run cutoff differs from the accepted Ledger cutoff")
        _validate_obligation_lineage(db, int(run.run_id), int(generation.id))
    else:
        raise ValueError("MRP current payload requires a fixed or building run")

    rows_by_kind, manifest = _collect_mrp_payload(db, run)
    specs = _row_specs(rows_by_kind)
    membership = _frozen_root_membership(
        db, run, {item_id for _, _, item_id, _, _ in specs if item_id is not None}
    )
    # Import lazily: current_execution imports this module for the migration
    # adapter, while the direct builder itself remains dependency-light.
    from app.services.item_ledger.current_execution import (
        _mrp_current_identity,
        _mrp_current_payload,
    )

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row_key, kind, item_id, sort_key, source in specs:
        payload = dict(source)
        payload.update({
            "run_id": int(run.run_id),
            "row_kind": kind,
            "sort_key": sort_key,
            "root_item_ids": sorted(membership.get(int(item_id), ())) if item_id is not None else [],
        })
        identity = _mrp_current_identity(
            payload, run_id=int(run.run_id), row_kind=kind
        )
        if identity in seen:
            raise ValueError("MRP current payload contains duplicate identity")
        seen.add(identity)
        rows.append({
            "current_identity": identity,
            "payload": _mrp_current_payload(payload, business_identity=identity),
        })
    manifest = dict(manifest)
    manifest["rows"] = rows
    manifest["meta"] = {"row_count": len(rows), "run_id": int(run.run_id)}
    return manifest



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
            action not in {"add", "replace"}
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
        if action == "add" and entry.get("parent_run_id") is not None:
            raise ValueError("candidate snapshot add entry must not claim a parent run")
        plan = db.get(models.ProductionPlanHeader, plan_id)
        if (
            plan is None
            or str(plan.status or "") != "fixed"
            or (
                action == "add"
                and candidate.prior_run_id != plan.predecessor_run_id
            )
            or (
                action == "replace"
                and int(candidate.prior_run_id or -1)
                != int(entry.get("parent_run_id") or -1)
            )
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



def _current_mrp_scope(db: Session):
    """Resolve the sole user-facing MRP result owner."""

    return require_current_execution_scope(
        db,
        entity_kind="mrp_result",
        scope_key="mrp:all-live-plans",
    )


def _current_mrp_row_value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _read_current_mrp_rows(
    db: Session,
    run_id: int,
    *,
    row_kind: str,
    item_id: int | None,
    root_item_id: int | None,
    area_id: int | None,
    date_from: str | date | None,
    date_to: str | date | None,
    supplier_ref1c: str | None,
    category_id: int | None,
    category_ref1c: str | None,
    limit: int,
    offset: int,
    sort_dir: str,
    current_identity: str | None,
) -> dict[str, Any]:
    kind = str(row_kind or "").strip().lower()
    if kind not in ROW_KINDS:
        raise ValueError(f"unsupported MRP result row kind: {row_kind}")
    scope = _current_mrp_scope(db)
    run_meta = dict((scope.summary or {}).get("runs") or {}).get(str(int(run_id)))
    if not isinstance(run_meta, dict):
        raise CurrentExecutionUnavailable("current MRP run is missing")
    all_rows: list[tuple[Any, dict[str, Any]]] = []
    for current in load_current_execution_rows(
        db,
        entity_kind="mrp_result",
        scope_key="mrp:all-live-plans",
    ):
        payload = dict(current.payload or {})
        if int(payload.get("run_id") or 0) != int(run_id):
            continue
        if str(payload.get("row_kind") or "").strip().lower() != kind:
            continue
        if current_identity is not None and str(current.business_identity) != str(current_identity).strip():
            continue
        all_rows.append((current, payload))

    def _as_text(value: Any) -> str:
        return str(value or "")

    filtered: list[tuple[Any, dict[str, Any]]] = []
    start = date_from.isoformat() if isinstance(date_from, date) else str(date_from or "")
    end = date_to.isoformat() if isinstance(date_to, date) else str(date_to or "")
    for current, payload in all_rows:
        if item_id is not None and int(payload.get("item_id") or 0) != int(item_id):
            continue
        roots = payload.get("root_item_ids") or payload.get("root_item_id")
        if root_item_id is not None:
            if isinstance(roots, list):
                if int(root_item_id) not in {int(value) for value in roots}:
                    continue
            elif int(roots or 0) != int(root_item_id):
                continue
        if area_id is not None and int(payload.get("area_id") or 0) != int(area_id):
            continue
        if supplier_ref1c is not None:
            supplier = _as_text(payload.get("supplier_ref1c"))
            supplier_name = _as_text(payload.get("supplier_name")).strip()
            if supplier_ref1c == "__missing_supplier_name":
                if supplier_name:
                    continue
            elif supplier != str(supplier_ref1c):
                continue
        if category_id is not None and int(payload.get("category_id") or 0) != int(category_id):
            continue
        if category_ref1c is not None:
            category_ref = _as_text(payload.get("category_ref1c")).strip()
            if category_ref1c == "__missing_category":
                if int(payload.get("category_id") or 0) or category_ref:
                    continue
            elif category_ref != str(category_ref1c):
                continue
        sort_key = _as_text(payload.get("sort_key"))
        row_date = _as_text(_current_mrp_row_value(
            payload, "date", "need_date", "required_date", "period_from", "sort_key"
        ))
        if start and (sort_key and sort_key < f"{start}|" or not sort_key and row_date < start):
            continue
        if end and (sort_key and sort_key >= f"{end}|\uffff" or not sort_key and row_date > end):
            continue
        filtered.append((current, payload))

    filtered.sort(key=lambda pair: str(pair[0].business_identity))
    filtered.sort(
        key=lambda pair: _as_text(pair[1].get("sort_key") or pair[1].get("date") or ""),
        reverse=str(sort_dir or "").lower() == "desc",
    )
    total = len(filtered)
    effective_limit = max(1, min(int(limit or 100), _MAX_PAGE))
    effective_offset = max(0, int(offset or 0))
    page = filtered[effective_offset : effective_offset + effective_limit]
    generation = db.get(models.LedgerGeneration, int(scope.source_generation_id or 0))
    cutoff = generation.cutoff.isoformat() if generation and generation.cutoff else None
    source_revision = str(scope.source_revision)
    response_rows = []
    for current, payload in page:
        row_payload = dict(payload)
        row_payload.setdefault("current_identity", str(current.business_identity))
        row_payload.setdefault("source_revision", source_revision)
        response_rows.append(row_payload)
    unfiltered = not any((item_id, root_item_id, area_id, date_from, date_to, supplier_ref1c, category_id, category_ref1c))
    persisted_total = dict(run_meta.get("total_qty") or {}).get(kind)
    total_qty = float(persisted_total) if unfiltered and persisted_total is not None else sum(
        float((payload or {}).get("qty") or 0) for _, payload in filtered
    )
    return {
        "current_scope_id": int(scope.id),
        "current_identity": f"mrp-run:{int(run_id)}",
        "source_revision": source_revision,
        "run_id": int(run_id),
        "ledger_generation": int(scope.source_generation_id or 0),
        "cutoff": cutoff,
        "truth_status": "accepted",
        "truth_reason": None,
        "rows": response_rows,
        "total": total,
        "total_qty": total_qty,
        "limit": effective_limit,
        "offset": effective_offset,
    }


def read_mrp_result_manifest(
    db: Session, run_id: int, *, current_scope_id: int | None = None
) -> dict[str, Any]:
    scope = _current_mrp_scope(db)
    if current_scope_id is not None and int(current_scope_id) != int(scope.id):
        raise CurrentExecutionUnavailable("current MRP manifest identity does not match")
    run_meta = dict((scope.summary or {}).get("runs") or {}).get(str(int(run_id)))
    if not isinstance(run_meta, dict):
        raise CurrentExecutionUnavailable("current MRP run is missing")
    generation = db.get(models.LedgerGeneration, int(scope.source_generation_id or 0))
    if generation is None or generation.cutoff is None:
        raise CurrentExecutionUnavailable("current MRP source cutoff is missing")
    summary = dict(run_meta.get("summary") or {})
    row_counts = dict(run_meta.get("row_counts") or {})
    total_qty = dict(run_meta.get("total_qty") or {})
    if run_meta.get("row_counts") is not None:
        summary["row_counts"] = row_counts
    if run_meta.get("total_qty") is not None:
        summary["total_qty"] = total_qty
    summary["snapshot_counts"] = row_counts
    summary["snapshot_total_qty"] = total_qty
    return {
        "current_scope_id": int(scope.id),
        "current_identity": f"mrp-run:{int(run_id)}",
        "source_revision": str(scope.source_revision),
        "run_id": int(run_id),
        "ledger_generation": int(scope.source_generation_id or 0),
        "cutoff": generation.cutoff.isoformat(),
        "truth_status": "accepted",
        "truth_reason": None,
        **summary,
    }


def read_mrp_result_rows(
    db: Session,
    run_id: int,
    *,
    row_kind: str,
    current_scope_id: int | None = None,
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
    current_identity: str | None = None,
) -> dict[str, Any]:
    scope = _current_mrp_scope(db)
    if current_scope_id is not None and int(current_scope_id) != int(scope.id):
        raise CurrentExecutionUnavailable("current MRP manifest identity does not match")
    return _read_current_mrp_rows(
        db,
        int(run_id),
        row_kind=row_kind,
        item_id=item_id,
        root_item_id=root_item_id,
        area_id=area_id,
        date_from=date_from,
        date_to=date_to,
        supplier_ref1c=supplier_ref1c,
        category_id=category_id,
        category_ref1c=category_ref1c,
        limit=limit,
        offset=offset,
        sort_dir=sort_dir,
        current_identity=current_identity,
    )
