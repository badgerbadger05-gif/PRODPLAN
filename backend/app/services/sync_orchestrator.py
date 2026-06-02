"""Automatic, time-staggered 1C synchronisation orchestrator.

Goal: keep the local DB fresh from 1C without ever hammering the server. Instead
of running every sync at once, a worker calls ``tick()`` on a short cadence
(~2 min) and the orchestrator runs **at most one** due job per tick. Effect:

* never more than one OData pull in flight (no spikes, no overlap);
* heavy reference syncs run rarely (12–24h), volatile ones (stock/orders) often;
* dependency order is respected — a parent reference (nomenclature, units…) is
  always refreshed before the data that depends on it.

State (last run / status / duration per job) lives in a JSON file next to
``odata_config.json`` so the feature needs no DB migration and is fully
reversible. Everything is read-only against 1C.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from ..schemas import ODataSyncRequest
from .odata_config import load_odata_config

# Sync service callables (read-only).
from .nomenclature_sync import sync_nomenclature_from_odata
from .category_sync import sync_categories_from_odata
from .units_sync import sync_units_from_odata, backfill_units_from_items
from .specification_sync import sync_specifications_from_odata
from .default_specification_sync import sync_default_specifications_from_odata
from .production_stage_sync import sync_production_stages_from_odata
from .production_kind_sync import sync_production_kinds_from_odata
from .operations_sync import sync_operations_from_odata
from .employee_sync import sync_employees_from_odata
from .odata_stock_sync import sync_stock_from_odata, sync_stock_warehouses_from_odata
from .production_order_sync import sync_production_orders_from_odata, sync_production_fact_from_odata
from .supplier_order_sync import sync_supplier_orders_from_odata
from .nomenclature_groups_sync import refresh_nomenclature_groups


STATE_PATH = Path("config") / "sync_schedule.json"

# Only one job may run at a time across the process.
_run_lock = threading.Lock()

# Categories need an explicit projection (mirrors routers/sync.py).
_CATEGORY_SELECT = [
    "Ref_Key", "Code", "Description", "Parent_Key", "IsFolder",
    "Predefined", "PredefinedDataName", "DataVersion", "DeletionMark",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build_payload(config: Dict[str, Any], entity_name: str, **overrides: Any) -> ODataSyncRequest:
    return ODataSyncRequest(
        base_url=str(config.get("base_url") or ""),
        entity_name=entity_name,
        username=config.get("username") or None,
        password=config.get("password") or None,
        token=config.get("token") or None,
        dry_run=False,
        zero_missing=False,
        **overrides,
    )


# --- Per-job runners. Each returns a small dict summary. ---------------------

def _run_nomenclature(db: Session, config: Dict[str, Any]) -> Dict[str, Any]:
    # Composite, mirrors POST /v1/sync/nomenclature-odata: units → categories →
    # nomenclature → backfill missing units.
    units = sync_units_from_odata(db, _build_payload(config, "Catalog_ЕдиницыИзмерения"))
    cats = sync_categories_from_odata(
        db, _build_payload(config, "Catalog_КатегорииНоменклатуры", select_fields=_CATEGORY_SELECT)
    )
    nom = sync_nomenclature_from_odata(db, _build_payload(config, "Catalog_Номенклатура"))
    backfill = backfill_units_from_items(db, _build_payload(config, "Catalog_ЕдиницыИзмерения"))
    return {"nomenclature": nom, "units": units, "categories": cats, "units_backfill": backfill}


def _run_nomenclature_groups(db: Session, config: Dict[str, Any]) -> Dict[str, Any]:
    # Refresh the nomenclature folder list for the group-selection UI. Does not
    # touch the user's selection. db is unused (file-backed cache).
    return refresh_nomenclature_groups(config)


def _single(entity: str, service: Callable[[Session, ODataSyncRequest], Any]) -> Callable[[Session, Dict[str, Any]], Any]:
    def runner(db: Session, config: Dict[str, Any]) -> Any:
        return service(db, _build_payload(config, entity))
    return runner


@dataclass(frozen=True)
class SyncJob:
    id: str
    title: str
    default_interval_s: int
    runner: Callable[[Session, Dict[str, Any]], Any]


# Order in this list == dependency order (parents before dependents),
# mirroring the frontend `fullSyncOrder`. The orchestrator prefers the
# lowest-index due job so references are refreshed before dependents.
SYNC_JOBS: List[SyncJob] = [
    SyncJob("nomenclature", "Номенклатура + ЕИ + категории", 86_400, _run_nomenclature),
    SyncJob("nomenclatureGroups", "Группы номенклатуры (папки)", 86_400, _run_nomenclature_groups),
    SyncJob("productionKinds", "Виды производства", 86_400, _single("Catalog_ВидыПроизводства", sync_production_kinds_from_odata)),
    SyncJob("employees", "Сотрудники", 86_400, _single("Catalog_Сотрудники", sync_employees_from_odata)),
    SyncJob("operations", "Операции", 43_200, _single("Catalog_Спецификации_Операции", sync_operations_from_odata)),
    SyncJob("specifications", "Спецификации", 43_200, _single("Catalog_Спецификации", sync_specifications_from_odata)),
    SyncJob("defaultSpecifications", "Спецификации по умолчанию", 43_200, _single("InformationRegister_СпецификацииПоУмолчанию", sync_default_specifications_from_odata)),
    SyncJob("productionStages", "Этапы производства", 86_400, _single("Catalog_ЭтапыПроизводства", sync_production_stages_from_odata)),
    SyncJob("warehouses", "Склады", 86_400, _single("AccumulationRegister_ЗапасыНаСкладах", sync_stock_warehouses_from_odata)),
    SyncJob("stock", "Остатки", 1_800, _single("AccumulationRegister_ЗапасыНаСкладах", sync_stock_from_odata)),
    SyncJob("productionOrders", "Заказы на производство", 3_600, _single("Document_ЗаказНаПроизводство", sync_production_orders_from_odata)),
    SyncJob("productionFacts", "Факт выпуска", 3_600, _single("Document_СборкаЗапасов", sync_production_fact_from_odata)),
    SyncJob("supplierOrders", "Заказы поставщику", 3_600, _single("Document_ЗаказПоставщику", sync_supplier_orders_from_odata)),
]

_JOB_BY_ID: Dict[str, SyncJob] = {j.id: j for j in SYNC_JOBS}
_ORDER_INDEX: Dict[str, int] = {j.id: i for i, j in enumerate(SYNC_JOBS)}


# --- State persistence -------------------------------------------------------

def _load_state() -> Dict[str, Any]:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text("utf-8") or "{}")
    except Exception:
        pass
    return {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        # Operational state only — never fail a sync because state couldn't be saved.
        pass


def _job_state(state: Dict[str, Any], job_id: str) -> Dict[str, Any]:
    return (state.get("jobs") or {}).get(job_id, {}) or {}


def _interval_for(state: Dict[str, Any], job: SyncJob) -> int:
    override = _job_state(state, job.id).get("interval_seconds")
    try:
        if override is not None:
            return max(60, int(override))
    except Exception:
        pass
    return job.default_interval_s


def _is_enabled(state: Dict[str, Any], job_id: str) -> bool:
    val = _job_state(state, job_id).get("enabled")
    return True if val is None else bool(val)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _due_jobs(state: Dict[str, Any], now: datetime) -> List[SyncJob]:
    """Jobs whose interval has elapsed (or never ran), enabled only."""
    due: List[SyncJob] = []
    for job in SYNC_JOBS:
        if not _is_enabled(state, job.id):
            continue
        last = _parse_iso(_job_state(state, job.id).get("last_run_at"))
        if last is None:
            due.append(job)
            continue
        if (now - last).total_seconds() >= _interval_for(state, job):
            due.append(job)
    return due


def _pick_next(due: List[SyncJob], state: Dict[str, Any], now: datetime) -> Optional[SyncJob]:
    """Dependency order first (lowest index), then the most overdue."""
    if not due:
        return None

    def overdue_seconds(job: SyncJob) -> float:
        last = _parse_iso(_job_state(state, job.id).get("last_run_at"))
        if last is None:
            return float("inf")
        return (now - last).total_seconds() - _interval_for(state, job)

    return sorted(due, key=lambda j: (_ORDER_INDEX[j.id], -overdue_seconds(j)))[0]


# --- Public API --------------------------------------------------------------

def tick(db: Session, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Run at most one due sync job. Designed to be called frequently (~2 min) by a
    worker; the per-job interval keeps each entity from running more often than
    needed, and the one-job-per-tick rule staggers load across time.
    """
    config = load_odata_config()
    if not str(config.get("base_url") or "").strip():
        return {"status": "skipped", "reason": "OData connection is not configured"}

    if not _run_lock.acquire(blocking=False):
        return {"status": "busy", "reason": "another sync is running"}
    try:
        now = now or _now()
        state = _load_state()
        due = _due_jobs(state, now)
        job = _pick_next(due, state, now)
        if job is None:
            return {"status": "idle", "due": 0}

        started = time.time()
        result: Dict[str, Any] = {
            "status": "ok",
            "job": job.id,
            "title": job.title,
            "due_count": len(due),
        }
        job_state: Dict[str, Any] = dict(_job_state(state, job.id))
        try:
            summary = job.runner(db, config)
            job_state["last_status"] = "ok"
            job_state["last_error"] = None
            result["summary"] = summary if isinstance(summary, dict) else {"result": str(summary)}
        except Exception as exc:  # noqa: BLE001 — one job's failure must not kill the loop
            db.rollback()
            job_state["last_status"] = "error"
            job_state["last_error"] = str(exc)[:1000]
            result["status"] = "error"
            result["error"] = str(exc)
        finally:
            # Always stamp last_run_at so a failing job waits its interval before
            # retry instead of being hammered every tick.
            job_state["last_run_at"] = now.isoformat()
            job_state["last_duration_ms"] = int((time.time() - started) * 1000)
            state.setdefault("jobs", {})[job.id] = job_state
            _save_state(state)

        result["duration_ms"] = job_state["last_duration_ms"]
        return result
    finally:
        _run_lock.release()


def status(db: Optional[Session] = None, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Schedule snapshot for diagnostics / UI: per-job last run, interval, next due."""
    now = now or _now()
    state = _load_state()
    config = load_odata_config()
    jobs_out: List[Dict[str, Any]] = []
    for job in SYNC_JOBS:
        js = _job_state(state, job.id)
        last = _parse_iso(js.get("last_run_at"))
        interval = _interval_for(state, job)
        next_due = None
        if last is not None:
            from datetime import timedelta

            next_due = (last + timedelta(seconds=interval)).isoformat()
        jobs_out.append({
            "id": job.id,
            "title": job.title,
            "interval_seconds": interval,
            "enabled": _is_enabled(state, job.id),
            "last_run_at": js.get("last_run_at"),
            "last_status": js.get("last_status"),
            "last_error": js.get("last_error"),
            "last_duration_ms": js.get("last_duration_ms"),
            "next_due_at": next_due,
            "due_now": last is None or (now - last).total_seconds() >= interval,
        })
    return {
        "status": "ok",
        "configured": bool(str(config.get("base_url") or "").strip()),
        "jobs": jobs_out,
    }


def update_config(updates: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Patch per-job schedule settings. `updates` = {job_id: {interval_seconds?, enabled?}}.
    Unknown job ids are ignored. Returns the new status.
    """
    state = _load_state()
    jobs = state.setdefault("jobs", {})
    for job_id, patch in (updates or {}).items():
        if job_id not in _JOB_BY_ID:
            continue
        js = jobs.setdefault(job_id, {})
        if "interval_seconds" in patch and patch["interval_seconds"] is not None:
            js["interval_seconds"] = max(60, int(patch["interval_seconds"]))
        if "enabled" in patch and patch["enabled"] is not None:
            js["enabled"] = bool(patch["enabled"])
    _save_state(state)
    return status()
