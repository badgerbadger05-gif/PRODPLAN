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
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from ..schemas import ODataSyncRequest
from .odata_config import load_odata_config

# Sync service callables (read-only).
from .nomenclature_sync import sync_nomenclature_from_odata
from .category_sync import sync_categories_from_odata
from .units_sync import UNIT_CLASSIFIER_ENTITY, sync_units_from_odata, backfill_units_from_items
from .specification_sync import sync_specifications_from_odata
from .specification_rebase_worker import run_one_pending_specification_rebase
from .default_specification_sync import sync_default_specifications_from_odata
from .production_stage_sync import sync_production_stages_from_odata
from .production_kind_sync import sync_production_kinds_from_odata
from .operations_sync import sync_operations_from_odata
from .employee_sync import sync_employees_from_odata
from .odata_stock_sync import sync_stock_from_odata, sync_stock_warehouses_from_odata
from .production_order_sync import sync_production_orders_from_odata, sync_production_facts
from .supplier_order_sync import sync_supplier_orders_from_odata
from .processing_stock_sync import (
    processing_stock_status,
    sync_processing_stock_from_odata,
)
from .nomenclature_groups_sync import refresh_nomenclature_groups
from .item_ledger.ingest import is_retryable_error, _build_client
from .item_ledger.reconcile import (
    BalanceSnapshotItemResolutionError,
    build_balance_snapshot,
)
from .item_ledger.physical_refresh_discard import (
    discard_physical_refresh_candidate,
)
from .item_ledger.physical_refresh_orchestrator import (
    PhysicalRefreshBalanceConvergenceError,
    run_physical_refresh,
)
from .odata_client import get_stock_from_1c_odata
from .. import models


logger = logging.getLogger(__name__)

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


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _odata_datetime(value: datetime) -> str:
    if value.tzinfo is not None and value.utcoffset() is not None:
        value = value.astimezone(ZoneInfo("Europe/Moscow"))
    return value.replace(tzinfo=None, microsecond=0).isoformat()


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
    units = sync_units_from_odata(db, _build_payload(config, UNIT_CLASSIFIER_ENTITY))
    cats = sync_categories_from_odata(
        db, _build_payload(config, "Catalog_КатегорииНоменклатуры", select_fields=_CATEGORY_SELECT)
    )
    nom = sync_nomenclature_from_odata(db, _build_payload(config, "Catalog_Номенклатура"))
    backfill = backfill_units_from_items(db, _build_payload(config, UNIT_CLASSIFIER_ENTITY))
    return {"nomenclature": nom, "units": units, "categories": cats, "units_backfill": backfill}


def _run_nomenclature_groups(db: Session, config: Dict[str, Any]) -> Dict[str, Any]:
    # Refresh the nomenclature folder list for the group-selection UI. Does not
    # touch the user's selection. db is unused (file-backed cache).
    return refresh_nomenclature_groups(config)


def _run_stock(db: Session, config: Dict[str, Any]) -> Dict[str, Any]:
    stock = sync_stock_from_odata(db, _build_payload(config, "AccumulationRegister_ЗапасыНаСкладах"))
    return {"stock": stock}


def _single(entity: str, service: Callable[[Session, ODataSyncRequest], Any]) -> Callable[[Session, Dict[str, Any]], Any]:
    def runner(db: Session, config: Dict[str, Any]) -> Any:
        return service(db, _build_payload(config, entity))
    return runner


def _run_specification_rebase(db: Session, _config: Dict[str, Any]) -> Any:
    return run_one_pending_specification_rebase(
        db,
        started_by="sync_orchestrator:specification_rebase",
    )


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
    SyncJob("brigades", "Бригады", 86_400, _single("Catalog_Бригады", sync_employees_from_odata)),
    SyncJob("operations", "Операции", 43_200, _single("Catalog_Спецификации_Операции", sync_operations_from_odata)),
    SyncJob("specifications", "Спецификации", 43_200, _single("Catalog_Спецификации", sync_specifications_from_odata)),
    # Sync fills the durable queue, but automatic consumption stays disabled
    # until the run-scoped rebase implementation is accepted. The previous
    # plan-successor/full-refresh worker took 7-8 hours and produced invalid
    # plan execution, so it must never become enabled merely because state has
    # no explicit per-job override.
    SyncJob(
        "specificationRebase",
        "Пересчёт MRP по изменённым спецификациям",
        3_600,
        _run_specification_rebase,
    ),
    SyncJob("defaultSpecifications", "Спецификации по умолчанию", 43_200, _single("InformationRegister_СпецификацииПоУмолчанию", sync_default_specifications_from_odata)),
    SyncJob("productionStages", "Этапы производства", 86_400, _single("Catalog_ЭтапыПроизводства", sync_production_stages_from_odata)),
    SyncJob("warehouses", "Склады", 86_400, _single("AccumulationRegister_ЗапасыНаСкладах", sync_stock_warehouses_from_odata)),
    SyncJob("stock", "Остатки + обеспечение журнала", 1_800, _run_stock),
    SyncJob("productionOrders", "Заказы на производство", 3_600, _single("Document_ЗаказНаПроизводство", sync_production_orders_from_odata)),
    # Факт выпуска не тянется из 1С: тик пересчитывает кэш produced_qty из
    # принятого поколения Item Ledger. Каденс = задержка кэша за поколением.
    SyncJob("productionFacts", "Факт выпуска", 3_600, _single("Document_СборкаЗапасов", sync_production_facts)),
    SyncJob("supplierOrders", "Заказы поставщику", 3_600, _single("Document_ЗаказПоставщику", sync_supplier_orders_from_odata)),
    SyncJob(
        "processingStock",
        "Остатки у переработчиков",
        3_600,
        _single("AccumulationRegister_ЗапасыПереданные/Balance", sync_processing_stock_from_odata),
    ),
]

_JOB_BY_ID: Dict[str, SyncJob] = {j.id: j for j in SYNC_JOBS}
_ORDER_INDEX: Dict[str, int] = {j.id: i for i, j in enumerate(SYNC_JOBS)}

_PHYSICAL_REFRESH_RETRY_BASE_SECONDS = 300
_PHYSICAL_REFRESH_RETRY_MAX_SECONDS = 3600
_PHYSICAL_REFRESH_INTERVAL_SECONDS = 3600
_PHYSICAL_REFRESH_ENTITY = "AccumulationRegister_ЗапасыНаСкладах/Balance"
# Signed bigint, stable across processes and deployments.
_SYNC_ORCHESTRATOR_LOCK_KEY = 0x73796E632D6F7263  # 'sync-orc'


# --- State persistence -------------------------------------------------------

def _load_state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        try:
            value = json.loads(STATE_PATH.read_text("utf-8") or "{}")
        except Exception as exc:
            raise RuntimeError(
                f"sync schedule state is unreadable: {STATE_PATH}"
            ) from exc
        if not isinstance(value, dict):
            raise RuntimeError(
                f"sync schedule state must be a JSON object: {STATE_PATH}"
            )
        return value
    return {}


def _save_state(state: Dict[str, Any], *, required: bool = False) -> None:
    temp_path: Path | None = None
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            state, ensure_ascii=False, indent=2
        ).encode("utf-8")
        fd, raw_path = tempfile.mkstemp(
            prefix=f".{STATE_PATH.name}.",
            suffix=".tmp",
            dir=str(STATE_PATH.parent),
        )
        temp_path = Path(raw_path)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, STATE_PATH)
        temp_path = None
        # os.replace() already swapped the file atomically; the directory fsync
        # below is only a durability barrier. Windows cannot open a directory
        # handle (PermissionError), so the barrier is skipped there instead of
        # failing an otherwise successful state write.
        if os.name != "nt":
            try:
                dir_fd = os.open(STATE_PATH.parent, os.O_RDONLY)
            except OSError:
                dir_fd = None
            if dir_fd is not None:
                try:
                    os.fsync(dir_fd)
                except OSError:
                    pass
                finally:
                    os.close(dir_fd)
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
        if required:
            raise


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
    if val is None:
        return job_id != "specificationRebase"
    return bool(val)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _rollback_if_possible(db: Optional[Session]) -> None:
    if db is not None and hasattr(db, "rollback"):
        try:
            db.rollback()
        except Exception:
            pass


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


def _physical_refresh_state(state: Dict[str, Any]) -> Dict[str, Any]:
    return state.setdefault("physical_refresh_maintenance", {})


def _current_accepted_parent(db: Session) -> models.LedgerGeneration:
    truth = db.get(models.PlanningTruthState, 1)
    if truth is None or truth.current_generation_id is None:
        raise RuntimeError("planning truth is not initialized")
    generation = db.get(models.LedgerGeneration, int(truth.current_generation_id))
    if generation is None or str(generation.status) != "accepted" or generation.cutoff is None:
        raise RuntimeError("current planning truth is not an accepted generation")
    return generation


def _has_current_accepted_parent(db: Optional[Session]) -> bool:
    if db is None:
        return False
    try:
        return _current_accepted_parent(db) is not None
    except Exception:
        return False


def _building_physical_refreshes(
    db: Session,
    parent_generation_id: int,
) -> list[models.LedgerGeneration]:
    result: list[models.LedgerGeneration] = []
    for generation in (
        db.query(models.LedgerGeneration)
        .filter(models.LedgerGeneration.status == "building")
        .order_by(models.LedgerGeneration.id.asc())
        .all()
    ):
        marks = dict(generation.source_watermarks or {})
        if (
            marks.get("generation_kind") == "physical_refresh"
            and int(marks.get("parent_generation_id") or -1)
            == int(parent_generation_id)
        ):
            result.append(generation)
    return result


def _physical_refresh_inventory(
    db: Optional[Session], parent_generation_id: Optional[int]
) -> Dict[str, Any]:
    """Inventory every BUILDING generation that looks like a physical refresh."""
    inventory: Dict[str, Any] = {
        "total": 0,
        "recoverable": [],
        "unexpected": [],
    }
    if db is None or not hasattr(db, "query"):
        return inventory
    rows = (
        db.query(models.LedgerGeneration)
        .filter(models.LedgerGeneration.status == "building")
        .order_by(models.LedgerGeneration.id.asc())
        .all()
    )
    for generation in rows:
        marks = dict(generation.source_watermarks or {})
        key = str(generation.generation_key or "")
        algorithm = str(generation.algorithm_version or "")
        looks_like = (
            marks.get("generation_kind") == "physical_refresh"
            or key.startswith("physical-refresh:")
            or "physical-refresh" in algorithm
        )
        if not looks_like:
            continue
        inventory["total"] += 1
        parent_id = marks.get("parent_generation_id")
        valid_parent = False
        try:
            valid_parent = parent_generation_id is not None and int(parent_id) == int(parent_generation_id)
        except (TypeError, ValueError):
            valid_parent = False
        well_formed = (
            marks.get("generation_kind") == "physical_refresh"
            and valid_parent
            and generation.cutoff is not None
            and bool(key)
        )
        if well_formed:
            inventory["recoverable"].append(generation)
            continue
        inventory["unexpected"].append({
            "generation_id": int(generation.id),
            "generation_key": key,
            "parent_generation_id": parent_id,
            "generation_kind": marks.get("generation_kind"),
            "algorithm_version": algorithm,
            "reason": "malformed_or_non_current_parent",
        })
    return inventory


def _physical_terminal_preflight(
    db: Optional[Session], parent_generation: Optional[models.LedgerGeneration]
) -> Dict[str, Any]:
    accepted_id = (
        int(parent_generation.physical_import_batch_id)
        if parent_generation is not None and parent_generation.physical_import_batch_id is not None
        else None
    )
    result: Dict[str, Any] = {
        "accepted_physical_terminal_id": accepted_id,
        "global_physical_terminal_id": None,
        "terminal_conflict": False,
        "terminal_conflict_batch": None,
        "explained_by_generation_id": None,
    }
    if db is None or not hasattr(db, "query"):
        return result
    global_id = db.query(func.max(models.PhysicalImportBatch.id)).scalar()
    result["global_physical_terminal_id"] = int(global_id) if global_id is not None else None
    if accepted_id is None or global_id is None or int(global_id) == accepted_id:
        return result
    current_parent_id = int(parent_generation.id) if parent_generation is not None else None
    explained = (
        db.query(models.LedgerGeneration)
        .filter(
            models.LedgerGeneration.status == "building",
            models.LedgerGeneration.physical_import_batch_id == int(global_id),
        )
        .all()
    )
    for generation in explained:
        marks = dict(generation.source_watermarks or {})
        try:
            same_parent = int(marks.get("parent_generation_id")) == current_parent_id
        except (TypeError, ValueError):
            same_parent = False
        if marks.get("generation_kind") == "physical_refresh" and same_parent:
            result["explained_by_generation_id"] = int(generation.id)
            return result
    batch = db.get(models.PhysicalImportBatch, int(global_id))
    result["terminal_conflict"] = True
    if batch is not None:
        result["terminal_conflict_batch"] = {
            "id": int(batch.id),
            "batch_key": str(batch.batch_key),
            "status": str(batch.status),
            "cutoff": batch.cutoff.isoformat() if batch.cutoff is not None else None,
            "source": dict(batch.source_watermarks or {}).get("source"),
        }
    return result


def _has_active_physical_identity(physical_state: Dict[str, Any]) -> bool:
    return bool(
        str(physical_state.get("active_generation_key") or "").strip()
        and _parse_iso(physical_state.get("active_cutoff")) is not None
    )


def _physical_refresh_block(
    terminal_preflight: Dict[str, Any],
    inventory: Dict[str, Any],
    physical_state: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Why the physical-refresh slot must not run, or None when it may.

    These conditions used to abort the whole tick. They are local to the
    physical contour, so they now fence that one slot: the 15 reference syncs
    keep their schedule while an operator reviews the conflict.
    """
    if terminal_preflight.get("terminal_conflict"):
        return {
            "code": "terminal_conflict",
            "message": (
                "physical import terminal conflicts with accepted planning truth: "
                + json.dumps(
                    terminal_preflight.get("terminal_conflict_batch"),
                    ensure_ascii=False,
                )
            ),
            "details": terminal_preflight.get("terminal_conflict_batch"),
        }
    if inventory.get("unexpected"):
        return {
            "code": "unexpected_building_generations",
            "message": (
                "unexpected BUILDING physical refresh generations require review: "
                + json.dumps(inventory["unexpected"], ensure_ascii=False)
            ),
            "details": inventory["unexpected"],
        }
    if not _has_active_physical_identity(physical_state) and len(
        inventory.get("recoverable") or []
    ) > 1:
        return {
            "code": "multiple_building_refreshes",
            "message": "multiple BUILDING physical refresh generations require review",
            "details": [int(g.id) for g in inventory["recoverable"]],
        }
    return None


def _record_physical_block(
    physical_state: Dict[str, Any],
    block: Optional[Dict[str, Any]],
    now: datetime,
) -> bool:
    """Persist the block marker; returns True when the stored value changed."""
    previous_code = physical_state.get("blocked_code")
    previous_reason = physical_state.get("blocked_reason")
    if block is None:
        if previous_code is None and previous_reason is None:
            return False
        physical_state["blocked_code"] = None
        physical_state["blocked_reason"] = None
        physical_state["blocked_details"] = None
        physical_state["blocked_since"] = None
        return True
    changed = previous_code != block["code"] or previous_reason != block["message"]
    if changed:
        logger.warning(
            "physical refresh slot blocked (%s): %s", block["code"], block["message"]
        )
        physical_state["blocked_since"] = now.isoformat()
    physical_state["blocked_code"] = block["code"]
    physical_state["blocked_reason"] = block["message"]
    physical_state["blocked_details"] = block["details"]
    return changed


def _physical_refresh_retry_pending(state: Dict[str, Any], now: datetime) -> bool:
    """True while an earlier failure's backoff window has not elapsed."""
    maintenance = _physical_refresh_state(state)
    if int(maintenance.get("failure_count") or 0) <= 0:
        return False
    retry_at = _parse_iso(maintenance.get("next_retry_at"))
    return retry_at is not None and now < retry_at


def _physical_refresh_due(state: Dict[str, Any], now: datetime) -> bool:
    maintenance = _physical_refresh_state(state)
    if int(maintenance.get("failure_count") or 0) > 0:
        retry_at = _parse_iso(maintenance.get("next_retry_at"))
        return retry_at is None or now >= retry_at
    last_success = _parse_iso(maintenance.get("last_success_at"))
    if last_success is None:
        return True
    return (now - last_success).total_seconds() >= _PHYSICAL_REFRESH_INTERVAL_SECONDS


def _run_physical_refresh_job(
    db: Session,
    target_cutoff: datetime,
    generation_key: str,
) -> Dict[str, Any]:
    parent = _current_accepted_parent(db)
    target_cutoff = _to_utc(target_cutoff).replace(microsecond=0)
    existing = db.query(models.LedgerGeneration).filter(
        models.LedgerGeneration.generation_key == generation_key
    ).one_or_none()
    if (
        existing is not None
        and str(existing.status) == "accepted"
        and _to_utc(parent.cutoff) >= target_cutoff
    ):
        return {
            "parent_generation_id": int(
                dict(existing.source_watermarks or {}).get(
                    "parent_generation_id", existing.id
                )
            ),
            "physical_generation_id": int(existing.id),
            "published_generation_id": int(parent.id),
            "target_cutoff": target_cutoff.isoformat(),
            "published": True,
            "result": {"accepted": True, "candidate_runs": 0, "recovered": True},
        }
    client = _build_client()
    filter_query = f"Period le datetime'{_odata_datetime(target_cutoff)}'"
    balance_rows = get_stock_from_1c_odata(
        base_url=client.base_url,
        entity_name=_PHYSICAL_REFRESH_ENTITY,
        username=client.username,
        password=client.password,
        token=client.token,
        filter_query=filter_query,
    )
    balance_snapshot = build_balance_snapshot(db, balance_rows, strict=True)

    def _load_opening_balance(opening_at: datetime) -> Dict[Any, Any]:
        """1C's Balance as of the anchor, re-asked on every refresh.

        Documents backdated behind the anchor change this answer after the seed
        was taken; the refresh materializes the difference as an adjustment.
        """
        rows = get_stock_from_1c_odata(
            base_url=client.base_url,
            entity_name=_PHYSICAL_REFRESH_ENTITY,
            username=client.username,
            password=client.password,
            token=client.token,
            filter_query=f"Period le datetime'{_odata_datetime(opening_at)}'",
        )
        return build_balance_snapshot(db, rows, strict=True)

    result = run_physical_refresh(
        db,
        generation_key=generation_key,
        target_cutoff=target_cutoff,
        client=client,
        balance_snapshot=balance_snapshot,
        opening_balance_loader=_load_opening_balance,
    )
    return {
        "parent_generation_id": result.parent_generation_id,
        "physical_generation_id": result.physical_generation_id,
        "published_generation_id": result.published_generation_id,
        "target_cutoff": result.cutoff.isoformat(),
        "published": bool(result.published),
        "result": {
            "accepted": bool(result.published),
            "candidate_runs": len(result.candidate_run_ids),
            "opening_adjusted_keys": (
                result.opening_reconcile.adjusted_keys
                if result.opening_reconcile is not None
                else 0
            ),
        },
    }


class ClusterLockError:
    """A lock attempt that failed for an infrastructure reason, not contention.

    Falsy on purpose so any legacy ``if not lock`` check keeps failing closed,
    while callers that care can tell a broken DB from a busy peer.
    """

    __slots__ = ("error",)

    def __init__(self, error: str) -> None:
        self.error = error

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"ClusterLockError({self.error!r})"


def _acquire_cluster_lock(db: Optional[Session]):
    if db is None:
        return True
    if not hasattr(db, "get_bind"):
        return True
    try:
        bind = db.get_bind()
        dialect = getattr(bind, "dialect", None)
        if getattr(dialect, "name", "") != "postgresql":
            return True
    except Exception as exc:  # noqa: BLE001
        logger.exception("sync scheduler could not inspect the DB bind")
        return ClusterLockError(str(exc))

    connection = None
    try:
        # Session commits/rollbacks may return their connection to the pool.
        # Keep the advisory lock on a dedicated connection for the whole tick.
        connection = bind.connect()
        (acquired,) = connection.execute(
            text("SELECT pg_try_advisory_lock(:k)"),
            {"k": _SYNC_ORCHESTRATOR_LOCK_KEY},
        ).fetchone()
        connection.commit()
        if not acquired:
            connection.close()
            return False
        return connection
    except Exception as exc:  # noqa: BLE001
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        # Previously any DB failure here was reported as "busy", so an
        # unreachable database looked exactly like a peer holding the lock.
        logger.exception("sync scheduler advisory lock failed")
        return ClusterLockError(str(exc))


def _release_cluster_lock(lock) -> None:
    if lock is True or lock is False or lock is None or isinstance(lock, ClusterLockError):
        return
    try:
        lock.execute(
            text("SELECT pg_advisory_unlock(:k)"),
            {"k": _SYNC_ORCHESTRATOR_LOCK_KEY},
        )
        lock.commit()
    except Exception:
        try:
            lock.rollback()
        except Exception:
            pass
    finally:
        try:
            lock.close()
        except Exception:
            pass


def pull_queue_health(db: Optional[Session]) -> Dict[str, int]:
    """Small operational snapshot; no mutation and safe for status diagnostics."""
    if db is None:
        return {"pending": 0, "error_retryable": 0, "error_exhausted": 0, "ready": 0}
    # Keep the scheduling unit testable with the tiny rollback-only session
    # doubles used by legacy callers.
    if not hasattr(db, "query"):
        return {"pending": 0, "error_retryable": 0, "error_exhausted": 0, "ready": 0}
    rows = db.query(models.StockRecorderPull.status, models.StockRecorderPull.attempts).all()
    # Retryability comes from the single shared predicate (ingest.py), so this
    # health snapshot, the drain filter and the reconcile in-flight guard can
    # never disagree on which error rows still count.
    pending = sum(1 for status, _attempts in rows if status == "pending")
    retryable = sum(1 for status, attempts in rows if is_retryable_error(status, attempts))
    exhausted = sum(
        1 for status, attempts in rows
        if status == "error" and not is_retryable_error(status, attempts)
    )
    return {"pending": pending, "error_retryable": retryable, "error_exhausted": exhausted, "ready": pending + retryable}


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
    cluster_locked: Any = False
    try:
        cluster_locked = _acquire_cluster_lock(db)
        if isinstance(cluster_locked, ClusterLockError):
            return {
                "status": "error",
                "reason": "cluster lock could not be evaluated",
                "lock": "error",
                "error": cluster_locked.error,
            }
        if not cluster_locked:
            return {"status": "busy", "reason": "another sync is running (cluster lock)", "lock": "busy"}

        now = now or _now()
        state = _load_state()

        # Recorder pulls are part of the physical-refresh BUILDING generation.
        # They must never create shared physical batches outside that lifecycle.
        ledger_health = pull_queue_health(db)

        physical_state = _physical_refresh_state(state)
        active_parent_for_inventory = _current_accepted_parent(db) if _has_current_accepted_parent(db) else None
        terminal_preflight = _physical_terminal_preflight(db, active_parent_for_inventory)
        physical_inventory = _physical_refresh_inventory(
            db,
            int(active_parent_for_inventory.id) if active_parent_for_inventory is not None else None,
        )
        physical_block = _physical_refresh_block(
            terminal_preflight, physical_inventory, physical_state
        )
        block_changed = _record_physical_block(physical_state, physical_block, now)

        # The physical slot competes with the reference schedule; both the
        # "queue has work" and the "interval elapsed" reasons must respect the
        # failure backoff, otherwise a permanently failing refresh retries every
        # tick instead of 300s → 3600s.
        physical_ready = (
            physical_block is None
            and active_parent_for_inventory is not None
            and not _physical_refresh_retry_pending(state, now)
            and bool(ledger_health["ready"] or _physical_refresh_due(state, now))
        )

        due = _due_jobs(state, now)
        job = _pick_next(due, state, now)

        # Fairness: while the recorder queue drains, the refresh is ready on
        # every single tick and strict priority starves the reference syncs
        # indefinitely. Alternate slots whenever both want the same tick — the
        # refresh still owns every tick nobody else needs.
        if physical_ready and job is not None and state.get("last_tick_slot") == "physical":
            physical_ready = False

        if physical_ready:
            state["last_tick_slot"] = "physical"
            started = time.time()
            active_cutoff = _parse_iso(physical_state.get("active_cutoff"))
            active_key = str(physical_state.get("active_generation_key") or "").strip()
            if active_key:
                persisted_candidate = (
                    db.query(models.LedgerGeneration)
                    .filter(models.LedgerGeneration.generation_key == active_key)
                    .one_or_none()
                )
                if (
                    persisted_candidate is not None
                    and str(persisted_candidate.status) != "building"
                ):
                    # The candidate this identity belonged to left BUILDING
                    # outside the orchestrator (admin discard/rollback).
                    # Reusing its cutoff would rebuild a dead candidate against
                    # a moved 1C balance forever, and the accumulated backoff
                    # belongs to those dead attempts — reset both.
                    active_cutoff = None
                    active_key = ""
                    physical_state["active_cutoff"] = None
                    physical_state["active_generation_key"] = None
                    physical_state["next_retry_at"] = None
                    physical_state["failure_count"] = 0
            if active_cutoff is None or not active_key:
                active_parent = active_parent_for_inventory
                recoverable = physical_inventory["recoverable"]
                if recoverable:
                    candidate = recoverable[0]
                    active_cutoff = _to_utc(candidate.cutoff).replace(
                        microsecond=0
                    )
                    active_key = str(candidate.generation_key)
                else:
                    active_cutoff = _to_utc(now).replace(microsecond=0)
                    active_key = (
                        f"physical-refresh:{active_parent.id}:"
                        f"{active_cutoff.isoformat()}"
                    )
                physical_state["active_cutoff"] = active_cutoff.isoformat()
                physical_state["active_generation_key"] = active_key
                # The retry identity must survive a worker/process crash before
                # any remote read or durable import checkpoint.
                _save_state(state, required=True)
            try:
                summary = _run_physical_refresh_job(
                    db, active_cutoff, active_key
                )
                physical_state["last_status"] = "ok"
                physical_state["last_error"] = None
                physical_state["failure_count"] = 0
                physical_state["next_retry_at"] = None
                physical_state["last_attempt_at"] = now.isoformat()
                physical_state["last_success_at"] = now.isoformat()
                physical_state["last_cutoff"] = summary["target_cutoff"]
                physical_state["last_result"] = summary
                physical_state["active_cutoff"] = None
                physical_state["active_generation_key"] = None
                physical_state["last_duration_ms"] = int((time.time() - started) * 1000)
                result = {"status": "ok", "job": "physicalRefresh", "summary": summary}
            except Exception as exc:  # noqa: BLE001
                _rollback_if_possible(db)
                dependency_job_forced_due = None
                discarded_candidate_id = None
                if isinstance(exc, PhysicalRefreshBalanceConvergenceError):
                    # Convergence compares an immutable candidate cutoff with
                    # a balance snapshot read from 1C.  Once that comparison
                    # fails, reusing the same candidate on a later tick can
                    # never prove a current snapshot: 1C keeps moving while
                    # the candidate remains frozen.  Roll it back through the
                    # checked discard path and let the next retry fork from the
                    # accepted parent with a new cutoff.
                    discard_physical_refresh_candidate(
                        db,
                        ledger_generation_id=exc.ledger_generation_id,
                        reason="automatic rotation after balance convergence failure",
                    )
                    db.commit()
                    discarded_candidate_id = exc.ledger_generation_id
                    physical_state["active_cutoff"] = None
                    physical_state["active_generation_key"] = None
                if isinstance(exc, BalanceSnapshotItemResolutionError):
                    # A new item may appear in stock or production between the
                    # daily nomenclature pulls. Do not leave physical truth
                    # stale until the next 24-hour slot: force the parent
                    # reference job due, then the normal fairness rule runs it
                    # before the backed-off physical retry.
                    nomenclature_state = dict(
                        _job_state(state, "nomenclature")
                    )
                    nomenclature_state["last_run_at"] = None
                    nomenclature_state["forced_due_at"] = now.isoformat()
                    nomenclature_state["forced_due_reason"] = (
                        "physical_refresh_missing_item"
                    )
                    state.setdefault("jobs", {})["nomenclature"] = (
                        nomenclature_state
                    )
                    dependency_job_forced_due = "nomenclature"
                failures = int(physical_state.get("failure_count") or 0) + 1
                backoff = min(
                    _PHYSICAL_REFRESH_RETRY_BASE_SECONDS * (2 ** (failures - 1)),
                    _PHYSICAL_REFRESH_RETRY_MAX_SECONDS,
                )
                physical_state["last_status"] = "error"
                physical_state["last_error"] = str(exc)[:1000]
                physical_state["failure_count"] = failures
                physical_state["next_retry_at"] = (now + timedelta(seconds=backoff)).isoformat()
                physical_state["last_attempt_at"] = now.isoformat()
                physical_state["last_duration_ms"] = int((time.time() - started) * 1000)
                physical_state["last_cutoff"] = _to_utc(now).isoformat()
                result = {"status": "error", "job": "physicalRefresh", "error": str(exc)}
                if dependency_job_forced_due is not None:
                    result["dependency_job_forced_due"] = (
                        dependency_job_forced_due
                    )
                if discarded_candidate_id is not None:
                    result["discarded_candidate_id"] = discarded_candidate_id
            _save_state(state, required=True)
            result["duration_ms"] = physical_state["last_duration_ms"]
            return result

        if job is None:
            if block_changed:
                _save_state(state, required=True)
            idle: Dict[str, Any] = {"status": "idle", "due": 0}
            if physical_block is not None:
                idle["physical_refresh_blocked_reason"] = physical_block["message"]
            return idle

        state["last_tick_slot"] = "jobs"
        started = time.time()
        result: Dict[str, Any] = {
            "status": "ok",
            "job": job.id,
            "title": job.title,
            "due_count": len(due),
        }
        if physical_block is not None:
            result["physical_refresh_blocked_reason"] = physical_block["message"]
        job_state: Dict[str, Any] = dict(_job_state(state, job.id))
        try:
            summary = job.runner(db, config)
            job_state["last_status"] = "ok"
            job_state["last_error"] = None
            result["summary"] = summary if isinstance(summary, dict) else {"result": str(summary)}
        except Exception as exc:  # noqa: BLE001 — one job's failure must not kill the loop
            _rollback_if_possible(db)
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
            _save_state(state, required=True)

        result["duration_ms"] = job_state["last_duration_ms"]
        return result
    finally:
        if cluster_locked:
            _release_cluster_lock(cluster_locked)
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
    current_parent_id: int | None = None
    physical_inventory: Dict[str, Any] = {
        "total": 0,
        "recoverable": [],
        "unexpected": [],
    }
    terminal_preflight: Dict[str, Any] = _physical_terminal_preflight(
        db,
        _current_accepted_parent(db) if db is not None and _has_current_accepted_parent(db) else None,
    )
    if db is not None:
        try:
            current_parent_id = int(_current_accepted_parent(db).id)
        except Exception:
            pass
        physical_inventory = _physical_refresh_inventory(db, current_parent_id)
    physical_state = _physical_refresh_state(state)
    if db is not None:
        block = _physical_refresh_block(
            terminal_preflight, physical_inventory, physical_state
        )
        blocked_reason = block["message"] if block else None
        blocked_code = block["code"] if block else None
        blocked_details = block["details"] if block else None
    else:
        blocked_reason = physical_state.get("blocked_reason")
        blocked_code = physical_state.get("blocked_code")
        blocked_details = physical_state.get("blocked_details")
    return {
        "status": "ok",
        "configured": bool(str(config.get("base_url") or "").strip()),
        "jobs": jobs_out,
        "ledger_pull_queue": pull_queue_health(db),
        # Surfaced at the top level too: an operator looking at "overdue" needs
        # the reason the physical contour stopped without reading nested state.
        "physical_refresh_blocked_reason": blocked_reason,
        "physical_refresh": {
            "blocked_reason": blocked_reason,
            "blocked_code": blocked_code,
            "blocked_details": blocked_details,
            "blocked_since": physical_state.get("blocked_since"),
            "last_status": _physical_refresh_state(state).get("last_status"),
            "last_error": _physical_refresh_state(state).get("last_error"),
            "failure_count": int(_physical_refresh_state(state).get("failure_count") or 0),
            "next_retry_at": _physical_refresh_state(state).get("next_retry_at"),
            "last_attempt_at": _physical_refresh_state(state).get("last_attempt_at"),
            "last_success_at": _physical_refresh_state(state).get("last_success_at"),
            "last_cutoff": _physical_refresh_state(state).get("last_cutoff"),
            "active_cutoff": _physical_refresh_state(state).get("active_cutoff"),
            "active_generation_key": _physical_refresh_state(state).get("active_generation_key"),
            "last_duration_ms": _physical_refresh_state(state).get("last_duration_ms"),
            "last_result": _physical_refresh_state(state).get("last_result"),
            "building_inventory_total": int(physical_inventory["total"]),
            "recoverable_building_count": len(physical_inventory["recoverable"]),
            "unexpected_building_count": len(physical_inventory["unexpected"]),
            "unexpected_buildings": physical_inventory["unexpected"],
            "accepted_physical_terminal_id": terminal_preflight["accepted_physical_terminal_id"],
            "global_physical_terminal_id": terminal_preflight["global_physical_terminal_id"],
            "terminal_conflict": terminal_preflight["terminal_conflict"],
            "terminal_conflict_batch": terminal_preflight["terminal_conflict_batch"],
            "terminal_explained_by_generation_id": terminal_preflight["explained_by_generation_id"],
        },
        "processing_stock": processing_stock_status(db) if db is not None else None,
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
    _save_state(state, required=True)
    return status()
