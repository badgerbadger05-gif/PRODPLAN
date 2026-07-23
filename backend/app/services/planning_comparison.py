"""Append-only comparison of stable and shadow planning results.

The stable contour is accessed exclusively through its read-only HTTP API.
Canonical rows use 1C references (falling back to business codes), never local
integer ids, so the two databases remain completely independent.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from typing import Any, Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models


RESULT_MODELS = {
    "production": (models.PlannedOrder, models.PlannedOrder.bucket_date),
    "purchase": (models.PlannedPurchase, models.PlannedPurchase.bucket_date),
    "rework": (models.PlannedRework, models.PlannedRework.bucket_date),
}

LEGACY_RESULT_PATHS = {
    "production": "production",
    "purchase": "purchases",
    "rework": "rework",
}


class StableAPIReadError(RuntimeError):
    def __init__(self, path: str, cause: Exception, status_code: Optional[int] = None):
        super().__init__(f"stable API read failed for {path}: {cause}")
        self.path = path
        self.status_code = status_code


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(type(value).__name__)


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def _item_key(item: models.Item) -> str:
    if str(item.item_ref1c or "").strip():
        return f"ref1c:{str(item.item_ref1c).strip().lower()}"
    if str(item.item_code or "").strip():
        return f"code:{str(item.item_code).strip()}"
    raise ValueError(f"item {item.item_id} has neither item_ref1c nor item_code")


def _latest_run(db: Session) -> Optional[models.PlanningRun]:
    return (
        db.query(models.PlanningRun)
        .filter(models.PlanningRun.status.in_(("COMPLETED", "SUCCESS", "completed", "success")))
        .order_by(models.PlanningRun.finished_at.desc(), models.PlanningRun.run_id.desc())
        .first()
        or db.query(models.PlanningRun).order_by(models.PlanningRun.started_at.desc(), models.PlanningRun.run_id.desc()).first()
    )


def _watermark(db: Session, model: Any) -> Optional[str]:
    value = db.query(func.max(model.updated_at)).scalar()
    return value.isoformat() if value else None


def _component(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    canonical = sorted(list(rows), key=_canonical_json)
    return {"count": len(canonical), "hash": payload_hash(canonical)}


def _planning_input_components(db: Session) -> Dict[str, Dict[str, Any]]:
    """Canonical planning inputs containing no database-local identities."""
    items = {int(row.item_id): _item_key(row) for row in db.query(models.Item).all()}
    specs = {
        int(row.spec_id): (
            f"ref1c:{str(row.spec_ref1c).strip().lower()}"
            if str(row.spec_ref1c or "").strip()
            else f"code:{str(row.spec_code or row.spec_name).strip()}"
        )
        for row in db.query(models.Specification).all()
    }
    operations = {
        int(row.operation_id): (
            f"ref1c:{str(row.operation_ref1c).strip().lower()}"
            if str(row.operation_ref1c or "").strip()
            else f"name:{str(row.operation_name or '').strip()}"
        )
        for row in db.query(models.Operation).all()
    }
    resources = {
        int(row.resource_id): str(row.resource_name or "").strip()
        for row in db.query(models.ProductionResource).all()
    }
    stages = {
        int(row.stage_id): str(row.stage_name or "").strip()
        for row in db.query(models.ProductionStage).all()
    }
    plan_rows = []
    for plan, line in (
        db.query(models.ProductionPlanHeader, models.ProductionPlanLine)
        .join(models.ProductionPlanLine, models.ProductionPlanLine.plan_id == models.ProductionPlanHeader.id)
        .all()
    ):
        plan_rows.append({
            "plan": [plan.name, plan.period_from, plan.period_to, plan.status],
            "item": items.get(int(line.item_id), f"missing:{line.item_id}"),
            "bucket_date": line.bucket_date,
            "qty": line.qty,
        })
    defaults = [{
        "item": items.get(int(row.item_id), f"missing:{row.item_id}"),
        "characteristic": str(row.characteristic_id or "").lower(),
        "spec": specs.get(int(row.spec_id), f"missing:{row.spec_id}"),
    } for row in db.query(models.DefaultSpecification).all()]
    components = [{
        "spec": specs.get(int(row.spec_id), f"missing:{row.spec_id}"),
        "item": items.get(int(row.item_id), f"missing:{row.item_id}"),
        "qty": row.quantity,
        "stage": stages.get(int(row.stage_id), "") if row.stage_id else "",
        "type": row.component_type,
        "component_spec_ref1c": str(row.component_spec_ref1c or "").lower(),
    } for row in db.query(models.SpecComponent).all()]
    routes = [{
        "spec": specs.get(int(row.spec_id), f"missing:{row.spec_id}"),
        "operation": operations.get(int(row.operation_id), f"missing:{row.operation_id}"),
        "stage": stages.get(int(row.stage_id), "") if row.stage_id else "",
        "time_norm": row.time_norm,
    } for row in db.query(models.SpecOperation).all()]
    resource_rows = [{
        "resource": resources[int(row.resource_id)],
        "shift_offset": row.shift_offset,
        "planning_range": row.planning_range,
        "capacity": row.capacity,
        "work_schedule": row.work_schedule,
        "daily_work_hours": row.daily_work_hours,
        "buffer_days": row.buffer_days,
    } for row in db.query(models.ProductionResource).all()]
    resource_stages = [{
        "resource": resources.get(int(row.resource_id), f"missing:{row.resource_id}"),
        "stage": stages.get(int(row.stage_id), f"missing:{row.stage_id}"),
    } for row in db.query(models.ResourceStage).all()]
    calendars = [{
        "date": row.date, "is_workday": row.is_workday, "comment": row.comment,
    } for row in db.query(models.WorkCalendarDay).all()]
    settings = [{
        "version": row.version, "active": row.is_active, "config": row.config,
    } for row in db.query(models.PlanningConfigVersion).all()]
    warehouses = [{
        "ref1c": str(row.warehouse_ref1c).lower(),
        "code": row.warehouse_code,
        "selected": row.is_selected,
        "finished_goods": row.is_finished_goods,
    } for row in db.query(models.StockWarehouse).all()]
    return {
        "period_plan": _component(plan_rows),
        "default_specifications": _component(defaults),
        "bom_components": _component(components),
        "operations_routes": _component(routes),
        "resources": _component(resource_rows + resource_stages),
        "calendar": _component(calendars),
        "planning_settings": _component(settings),
        "warehouse_selections": _component(warehouses),
    }


def canonical_result_rows(db: Session, run_id: int) -> List[Dict[str, Any]]:
    items = {int(row.item_id): row for row in db.query(models.Item).all()}
    specs = {int(row.spec_id): row for row in db.query(models.Specification).all()}
    result: List[Dict[str, Any]] = []
    for kind, (model, _) in RESULT_MODELS.items():
        rows = db.query(model).filter(model.run_id == int(run_id)).all()
        aggregated: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            item = items.get(int(row.item_id))
            if item is None:
                raise ValueError(f"{kind} row references missing item {row.item_id}")
            item_key = _item_key(item)
            bucket = row.bucket_date or row.need_date
            axes: List[str] = [kind, item_key, bucket.isoformat() if bucket else ""]
            payload: Dict[str, Any] = {
                "item_key": item_key,
                "item_code": item.item_code,
                "item_ref1c": item.item_ref1c,
                "bucket_date": bucket.isoformat() if bucket else None,
            }
            if kind == "purchase":
                supplier = str(row.supplier_ref1c or "").strip().lower()
                axes.append(f"supplier:{supplier}")
                payload["supplier_ref1c"] = supplier or None
            elif kind == "rework":
                spec = specs.get(int(row.spec_id)) if row.spec_id is not None else None
                spec_key = (
                    f"ref1c:{str(spec.spec_ref1c).strip().lower()}"
                    if spec and str(spec.spec_ref1c or "").strip()
                    else f"code:{str(spec.spec_code).strip()}"
                    if spec and str(spec.spec_code or "").strip()
                    else "unspecified"
                )
                axes.append(f"spec:{spec_key}")
                payload["spec_key"] = spec_key
            canonical_key = "|".join(axes)
            qty = _decimal(row.qty)
            if canonical_key not in aggregated:
                aggregated[canonical_key] = {
                    "result_kind": kind,
                    "canonical_key": canonical_key,
                    "item_key": item_key,
                    "bucket_date": bucket.isoformat() if bucket else None,
                    "quantity": qty,
                    "payload": payload,
                }
            else:
                aggregated[canonical_key]["quantity"] += qty
        for value in aggregated.values():
            value["quantity"] = format(value["quantity"], "f")
            value["raw_payload_hash"] = payload_hash(
                {"payload": value["payload"], "quantity": value["quantity"]}
            )
            result.append(value)
    return sorted(result, key=lambda row: row["canonical_key"])


def input_fingerprint(db: Session, *, include_results: bool = False) -> Dict[str, Any]:
    run = _latest_run(db)
    watermarks = {
        "items": _watermark(db, models.Item),
        "stock": _watermark(db, models.ItemWarehouseStock),
        "production_orders": _watermark(db, models.ProductionOrder),
        "supplier_orders": _watermark(db, models.SupplierOrder),
    }
    counts = {
        "items": int(db.query(func.count(models.Item.item_id)).scalar() or 0),
        "stock_rows": int(db.query(func.count()).select_from(models.ItemWarehouseStock).scalar() or 0),
        "production_orders": int(db.query(func.count(models.ProductionOrder.order_id)).scalar() or 0),
        "supplier_orders": int(db.query(func.count(models.SupplierOrder.order_id)).scalar() or 0),
    }
    run_data = {
        "run_key": f"run:{run.started_at.isoformat()}:{run.period_from}:{run.period_to}" if run else None,
        "started_at": run.started_at.isoformat() if run and run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run and run.finished_at else None,
        "period_from": run.period_from.isoformat() if run and run.period_from else None,
        "period_to": run.period_to.isoformat() if run and run.period_to else None,
        "status": run.status if run else None,
        "config_hash": payload_hash(run.config_snapshot or {}) if run else None,
    }
    components = _planning_input_components(db)
    core = {
        "watermarks": watermarks,
        "counts": counts,
        "run": run_data,
        "components": components,
    }
    # Execution timestamps/run ids describe the output, not its input. Exclude
    # them so two contours can receive an exact grade when they used the same
    # watermarks, period and config but started at different wall-clock times.
    core["fingerprint"] = payload_hash({
        "watermarks": watermarks,
        "counts": counts,
        "period_from": run_data["period_from"],
        "period_to": run_data["period_to"],
        "config_hash": run_data["config_hash"],
        "components": components,
    })
    core["generated_at"] = datetime.now(timezone.utc).isoformat()
    if include_results:
        core["results"] = canonical_result_rows(db, int(run.run_id)) if run else []
    return core


def _stable_get(base_url: str, path: str, query: Optional[Dict[str, Any]] = None) -> Any:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{urlencode(query)}"
    headers = {"Accept": "application/json"}
    token = str(os.getenv("STABLE_PRODPLAN_API_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=float(os.getenv("PLANNING_COMPARISON_HTTP_TIMEOUT", "20"))) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise StableAPIReadError(path, exc, getattr(exc, "code", None)) from exc


def _legacy_page_settings() -> tuple[int, int]:
    page_size = min(max(int(os.getenv("PLANNING_COMPARISON_PAGE_SIZE", "100")), 1), 5000)
    max_pages = min(max(int(os.getenv("PLANNING_COMPARISON_MAX_PAGES", "100")), 1), 1000)
    return page_size, max_pages


def _stable_paginated(base_url: str, path: str) -> Dict[str, Any]:
    page_size, max_pages = _legacy_page_settings()
    pages: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    offset = 0
    for _ in range(max_pages):
        payload = _stable_get(base_url, path, {"limit": page_size, "offset": offset})
        if not isinstance(payload, dict):
            raise RuntimeError(f"stable API returned non-object page for {path}")
        page_rows = payload.get("rows") or []
        if not isinstance(page_rows, list):
            raise RuntimeError(f"stable API returned non-list rows for {path}")
        pages.append(payload)
        rows.extend(dict(row) for row in page_rows)
        total = int(payload.get("total") or len(rows))
        if not page_rows or len(rows) >= total:
            return {"rows": rows, "pages": pages, "total": total}
        offset += len(page_rows)
    raise RuntimeError(
        f"stable API pagination limit exceeded for {path}: "
        f"{max_pages} pages x {page_size} rows"
    )


def _external_key(row: Dict[str, Any], *, prefix: str = "item") -> str:
    ref = str(row.get(f"{prefix}_ref1c") or row.get(f"{prefix}_ref") or "").strip().lower()
    if ref:
        return f"ref1c:{ref}"
    code = str(row.get(f"{prefix}_code") or "").strip()
    if code:
        return f"code:{code}"
    raise ValueError(f"legacy stable {prefix} row has neither 1C reference nor business code")


def _canonical_legacy_rows(raw_by_kind: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    aggregated: Dict[str, Dict[str, Any]] = {}
    for kind, rows in raw_by_kind.items():
        for row in rows:
            item_key = _external_key(row)
            bucket = row.get("bucket_date") or row.get("need_date")
            axes = [kind, item_key, str(bucket or "")]
            payload: Dict[str, Any] = {
                "item_key": item_key,
                "item_code": row.get("item_code"),
                "item_ref1c": row.get("item_ref1c") or row.get("item_ref"),
                "bucket_date": bucket,
            }
            if kind == "purchase":
                supplier = str(row.get("supplier_ref1c") or "").strip().lower()
                axes.append(f"supplier:{supplier}")
                payload["supplier_ref1c"] = supplier or None
            elif kind == "rework":
                spec_key = _external_key(row, prefix="spec")
                axes.append(f"spec:{spec_key}")
                payload["spec_key"] = spec_key
            canonical_key = "|".join(axes)
            qty = _decimal(row.get("qty") if row.get("qty") is not None else row.get("planned_qty"))
            current = aggregated.setdefault(canonical_key, {
                "result_kind": kind,
                "canonical_key": canonical_key,
                "item_key": item_key,
                "bucket_date": bucket,
                "quantity": Decimal("0"),
                "payload": payload,
            })
            current["quantity"] += qty
    result: List[Dict[str, Any]] = []
    for value in aggregated.values():
        value["quantity"] = format(value["quantity"], "f")
        value["raw_payload_hash"] = payload_hash(
            {"payload": value["payload"], "quantity": value["quantity"]}
        )
        result.append(value)
    return sorted(result, key=lambda row: row["canonical_key"])


def _enrich_legacy_item_keys(
    base_url: str, raw_by_kind: Dict[str, List[Dict[str, Any]]]
) -> Dict[str, Any]:
    missing_ids = sorted({
        int(row["item_id"])
        for rows in raw_by_kind.values()
        for row in rows
        if row.get("item_id") is not None
        and not str(row.get("item_ref1c") or row.get("item_ref") or row.get("item_code") or "").strip()
    })
    max_lookups = min(
        max(int(os.getenv("PLANNING_COMPARISON_MAX_ITEM_LOOKUPS", "10000")), 1),
        100000,
    )
    if len(missing_ids) > max_lookups:
        raise RuntimeError(
            f"legacy stable item lookup limit exceeded: {len(missing_ids)} > {max_lookups}"
        )
    items: Dict[int, Dict[str, Any]] = {}
    for item_id in missing_ids:
        payload = _stable_get(base_url, f"/api/v1/items/{item_id}")
        if not isinstance(payload, dict):
            raise RuntimeError(f"stable API returned non-object item {item_id}")
        items[item_id] = payload
    for rows in raw_by_kind.values():
        for row in rows:
            item = items.get(int(row["item_id"])) if row.get("item_id") is not None else None
            if item:
                row["item_ref1c"] = item.get("item_ref1c")
                row["item_code"] = item.get("item_code")
    return {str(key): value for key, value in items.items()}


def _legacy_watermarks(sync_status: Dict[str, Any]) -> Dict[str, Optional[str]]:
    aliases = {
        "stock": ("stock", "остат"),
        "production_orders": ("production", "производ"),
        "supplier_orders": ("supplier", "постав"),
    }
    result: Dict[str, Optional[str]] = {}
    for target, needles in aliases.items():
        matches = [
            job for job in (sync_status.get("jobs") or [])
            if any(needle in f"{job.get('id', '')} {job.get('title', '')}".lower() for needle in needles)
        ]
        values = [job.get("last_run_at") for job in matches if job.get("last_run_at")]
        result[target] = max(values, key=lambda value: _parse_dt(value) or datetime.min.replace(tzinfo=timezone.utc)) if values else None
    return result


def _legacy_cutoff_grade(
    stable: Dict[str, Any], shadow: Dict[str, Any], max_skew_seconds: int
) -> tuple[str, str]:
    stable_run, shadow_run = stable.get("run") or {}, shadow.get("run") or {}
    if stable_run.get("period_from") != shadow_run.get("period_from") or stable_run.get("period_to") != shadow_run.get("period_to"):
        return "invalid", "legacy stable API: planning periods differ"
    skews: List[float] = []
    for key, stable_value in (stable.get("watermarks") or {}).items():
        shadow_value = (shadow.get("watermarks") or {}).get(key)
        left, right = _parse_dt(stable_value), _parse_dt(shadow_value)
        if left is None or right is None:
            return "invalid", f"legacy stable API: missing comparable watermark: {key}"
        skews.append(abs((left - right).total_seconds()))
    greatest = max(skews or [0.0])
    if greatest <= max_skew_seconds:
        return "near", (
            "legacy stable API has no input fingerprint; periods match and "
            f"maximum available sync watermark skew is {greatest:.3f}s"
        )
    return "invalid", (
        "legacy stable API has no input fingerprint; "
        f"sync watermark skew {greatest:.3f}s exceeds {max_skew_seconds}s"
    )


def _legacy_stable_snapshot(
    base_url: str, stable_sync: Dict[str, Any], stable_runs: Dict[str, Any]
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    run_rows = stable_runs.get("rows") or []
    if not run_rows:
        fingerprint = {
            "fingerprint": None, "watermarks": _legacy_watermarks(stable_sync),
            "counts": {}, "run": {}, "results": [], "source": "legacy_api",
        }
        return fingerprint, {"run": None, "result_pages": {}}
    run = dict(run_rows[0])
    run_id = int(run["run_id"])
    raw_pages: Dict[str, Any] = {}
    raw_rows: Dict[str, List[Dict[str, Any]]] = {}
    for kind, suffix in LEGACY_RESULT_PATHS.items():
        page_data = _stable_paginated(base_url, f"/api/v1/plan/results/{run_id}/{suffix}")
        raw_pages[kind] = page_data
        raw_rows[kind] = page_data["rows"]
    raw_items = _enrich_legacy_item_keys(base_url, raw_rows)
    run_data = {
        "run_key": f"legacy-run:{run_id}:{run.get('started_at')}:{run.get('period_from')}:{run.get('period_to')}",
        "started_at": run.get("started_at"), "finished_at": run.get("finished_at"),
        "period_from": run.get("period_from"), "period_to": run.get("period_to"),
        "status": run.get("status"), "config_hash": None,
    }
    fingerprint = {
        "fingerprint": None,
        "watermarks": _legacy_watermarks(stable_sync),
        "counts": {},
        "run": run_data,
        "results": _canonical_legacy_rows(raw_rows),
        "source": "legacy_api",
    }
    return fingerprint, {"run": run, "result_pages": raw_pages, "items": raw_items}


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def cutoff_grade(stable: Dict[str, Any], shadow: Dict[str, Any], max_skew_seconds: int) -> tuple[str, str]:
    if stable.get("fingerprint") and stable.get("fingerprint") == shadow.get("fingerprint"):
        return "exact", "input fingerprints are identical"
    stable_marks = stable.get("watermarks") or {}
    shadow_marks = shadow.get("watermarks") or {}
    skews: List[float] = []
    for key in sorted(set(stable_marks) | set(shadow_marks)):
        left, right = _parse_dt(stable_marks.get(key)), _parse_dt(shadow_marks.get(key))
        if left is None or right is None:
            return "invalid", f"missing watermark: {key}"
        skews.append(abs((left - right).total_seconds()))
    stable_run, shadow_run = stable.get("run") or {}, shadow.get("run") or {}
    if stable_run.get("period_from") != shadow_run.get("period_from") or stable_run.get("period_to") != shadow_run.get("period_to"):
        return "invalid", "planning periods differ"
    if stable_run.get("config_hash") != shadow_run.get("config_hash"):
        return "invalid", "planning configuration differs"
    if (stable.get("counts") or {}) != (shadow.get("counts") or {}):
        return "invalid", "input row counts differ"
    greatest = max(skews or [0.0])
    if greatest <= max_skew_seconds:
        return "near", f"maximum input watermark skew is {greatest:.3f}s"
    return "invalid", f"input watermark skew {greatest:.3f}s exceeds {max_skew_seconds}s"


def _snapshot(db: Session, batch_id: int, contour: str, kind: str, payload: Any) -> None:
    db.add(models.PlanningComparisonSnapshot(
        batch_id=batch_id, contour=contour, snapshot_kind=kind,
        raw_payload_hash=payload_hash(payload), payload=payload,
    ))


def capture(db: Session, *, capture_key: Optional[str] = None, max_skew_seconds: int = 300) -> Dict[str, Any]:
    base_url = str(os.getenv("STABLE_PRODPLAN_API_URL") or "").strip()
    if not base_url:
        raise ValueError("STABLE_PRODPLAN_API_URL is required")
    stable_sync = _stable_get(base_url, "/api/v1/sync/auto/status")
    stable_runs = _stable_get(base_url, "/api/v1/plan/runs", {"limit": 1, "offset": 0})
    stable_run_rows = stable_runs.get("rows") or []
    stable_summary = (
        _stable_get(base_url, f"/api/v1/plan/results/{stable_run_rows[0]['run_id']}")
        if stable_run_rows else {}
    )
    legacy_raw: Optional[Dict[str, Any]] = None
    try:
        stable_fp = _stable_get(
            base_url,
            "/api/v1/planning-comparison/input-fingerprint",
            {"include_results": "true"},
        )
    except StableAPIReadError as exc:
        if exc.status_code != 404:
            raise
        stable_fp, legacy_raw = _legacy_stable_snapshot(base_url, stable_sync, stable_runs)
    shadow_fp = input_fingerprint(db, include_results=True)
    grade, reason = (
        _legacy_cutoff_grade(stable_fp, shadow_fp, max_skew_seconds)
        if legacy_raw is not None
        else cutoff_grade(stable_fp, shadow_fp, max_skew_seconds)
    )
    generated_key = payload_hash({
        "stable": stable_fp.get("fingerprint"), "shadow": shadow_fp.get("fingerprint"),
        "stable_run": (stable_fp.get("run") or {}).get("run_key"),
        "shadow_run": (shadow_fp.get("run") or {}).get("run_key"),
    })
    key = str(capture_key or generated_key)
    existing = db.query(models.PlanningComparisonBatch).filter_by(capture_key=key).first()
    if existing:
        return batch_detail(db, int(existing.id))

    batch = models.PlanningComparisonBatch(
        capture_key=key, stable_base_url=base_url, cutoff_grade=grade, cutoff_reason=reason,
        stable_run_key=(stable_fp.get("run") or {}).get("run_key"),
        shadow_run_key=(shadow_fp.get("run") or {}).get("run_key"), metrics={},
    )
    db.add(batch)
    db.flush()
    _snapshot(db, batch.id, "stable", "fingerprint", stable_fp)
    _snapshot(db, batch.id, "stable", "sync_status", stable_sync)
    _snapshot(db, batch.id, "stable", "latest_run", {"runs": stable_runs, "summary": stable_summary})
    if legacy_raw is not None:
        _snapshot(db, batch.id, "stable", "legacy_result_pages", legacy_raw)
    _snapshot(db, batch.id, "shadow", "fingerprint", shadow_fp)

    indexed: Dict[str, Dict[str, Dict[str, Any]]] = {"stable": {}, "shadow": {}}
    for contour, source in (("stable", stable_fp.get("results") or []), ("shadow", shadow_fp.get("results") or [])):
        for raw in source:
            row = dict(raw)
            key_axis = str(row["canonical_key"])
            qty = _decimal(row.get("quantity"))
            indexed[contour][key_axis] = row
            db.add(models.PlanningComparisonRow(
                batch_id=batch.id, contour=contour, result_kind=row["result_kind"],
                canonical_key=key_axis, item_key=row["item_key"],
                bucket_date=date.fromisoformat(row["bucket_date"]) if row.get("bucket_date") else None,
                quantity=qty, raw_payload_hash=payload_hash(row),
                payload=row.get("payload") or row,
            ))

    metrics: Dict[str, Any] = {"cutoff_grade": grade, "by_kind": {}}
    all_keys = sorted(set(indexed["stable"]) | set(indexed["shadow"]))
    for key_axis in all_keys:
        left, right = indexed["stable"].get(key_axis), indexed["shadow"].get(key_axis)
        sample = left or right or {}
        left_qty, right_qty = _decimal((left or {}).get("quantity")), _decimal((right or {}).get("quantity"))
        delta = right_qty - left_qty
        classification = "equal" if delta == 0 else "shadow_only" if left is None else "stable_only" if right is None else "changed"
        kind = str(sample.get("result_kind"))
        bucket = metrics["by_kind"].setdefault(kind, {"equal": 0, "changed": 0, "stable_only": 0, "shadow_only": 0, "absolute_delta": "0"})
        bucket[classification] += 1
        bucket["absolute_delta"] = format(_decimal(bucket["absolute_delta"]) + abs(delta), "f")
        db.add(models.PlanningComparisonDiff(
            batch_id=batch.id, result_kind=kind, canonical_key=key_axis,
            item_key=str(sample.get("item_key")), stable_quantity=left_qty,
            shadow_quantity=right_qty, delta_quantity=delta, classification=classification,
        ))
    metrics["rows"] = len(all_keys)
    batch.metrics = metrics
    db.add(models.PlanningComparisonEvent(batch_id=batch.id, event_type="captured", payload={"grade": grade, "reason": reason}))
    db.commit()
    return batch_detail(db, int(batch.id))


def batch_detail(db: Session, batch_id: int) -> Dict[str, Any]:
    batch = db.query(models.PlanningComparisonBatch).filter_by(id=batch_id).first()
    if batch is None:
        raise LookupError("comparison batch not found")
    diffs = db.query(models.PlanningComparisonDiff).filter_by(batch_id=batch_id).order_by(models.PlanningComparisonDiff.id).all()
    return {
        "id": int(batch.id), "capture_key": batch.capture_key, "created_at": batch.created_at,
        "cutoff_grade": batch.cutoff_grade, "cutoff_reason": batch.cutoff_reason,
        "stable_run_key": batch.stable_run_key, "shadow_run_key": batch.shadow_run_key,
        "metrics": batch.metrics,
        "diffs": [{
            "result_kind": row.result_kind, "canonical_key": row.canonical_key,
            "item_key": row.item_key, "stable_quantity": format(row.stable_quantity, "f"),
            "shadow_quantity": format(row.shadow_quantity, "f"), "delta_quantity": format(row.delta_quantity, "f"),
            "classification": row.classification,
        } for row in diffs],
    }


def list_batches(db: Session, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    query = db.query(models.PlanningComparisonBatch)
    rows = query.order_by(models.PlanningComparisonBatch.id.desc()).offset(max(offset, 0)).limit(min(max(limit, 1), 200)).all()
    return {"rows": [{
        "id": int(row.id), "capture_key": row.capture_key, "created_at": row.created_at,
        "cutoff_grade": row.cutoff_grade, "metrics": row.metrics,
    } for row in rows], "total": query.count(), "limit": limit, "offset": offset}
