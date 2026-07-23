"""Item-ledger admin — операционный запуск леджера-1 (design Прил. A §4).

Единственная дыра конвейера, которую закрывает этот роутер: у
``seed_from_balance`` не было прод-вызова — якорь T0 никто не создавал, и
леджер физически не мог начаться (дренаж очереди пуллов уже встроен в
sync_orchestrator и работает сам, но без сида ему не от чего отталкиваться).

POST /api/v1/item-ledger/admin/seed?dry_run=true&force=false

* Снимает свежий 1С ``/Balance`` (тот же ``get_stock_from_1c_odata``, что и
  штатный свип), нормализует в ключи леджера (``build_balance_snapshot``) и
  сеет ledger-1: seed-SLE + stock_bin + stock_ledger_anchor(T0) на каждый
  ненулевой ключ (A §4.1–4.2). T0 = момент снимка: пуллы применяют только
  строки с posting_at > T0 (anchor guard, A §4.3) — двойного учёта нет.
* ``dry_run`` (default true) — только посчитать и показать сводку, БД не
  трогается.
* Идемпотентность: повторный сид при уже существующих якорях, включая
  ``force=true``, → 409. Shared physical history and accepted generations are
  immutable; destructive reseed is intentionally unsupported.

Отдельный файл (не item_ledger.py), чтобы не конфликтовать с параллельными
правками read-API. Никаких записей в 1С (INV-1way / INV-no-write).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import time
from typing import Any, Dict, List, Mapping, Optional
from urllib.error import URLError

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..services.item_ledger.ingest import REGISTER_ENTITY, is_inflight_pull
from ..services.item_ledger.generation_lifecycle import (
    GenerationValidationError,
    accept_generation_build,
)
from ..services.item_ledger.generation_bootstrap import (
    ALGORITHM_VERSION as HISTORICAL_BOOTSTRAP_ALGORITHM_VERSION,
    GenerationBootstrapError,
    _lineage_values,
    create_historical_generation,
    historical_generation_status,
    resume_historical_generation_import,
)
from ..services.item_ledger.historical_bootstrap_phase0 import (
    Phase0BootstrapError,
    evaluate_historical_balance_convergence,
    seed_historical_opening_balance,
)
from ..services.item_ledger.physical import (
    EPS,
    LedgerKey,
    canonical_content_hash,
    seed_from_balance,
)
from ..services.item_ledger.reconcile import build_balance_snapshot
from ..services.odata_client import get_stock_from_1c_odata
from ..services.odata_client import OData1CClient
from ..services.odata_config import load_odata_config, sanitize_base_url
from ..services.planning_truth import (
    PlanningTruthInvalidationConflict,
    invalidate_current_generation,
)

router = APIRouter(prefix="/v1/item-ledger/admin", tags=["item-ledger-admin"])


class SeedReseedStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries_deleted: int
    anchors_deleted: int
    bins_deleted: int
    pull_rows_deleted: int


class SeedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool
    force: bool
    anchor_period: str
    posting_at: str
    balance_rows: int          # сырых строк Balance от 1С
    keys_total: int            # разрешённых ключей леджера в снимке
    keys_nonzero: int          # ключей к посеву (|qty| > EPS)
    keys_skipped_zero: int     # нулевые ключи — не сеются (A §4.2)
    total_qty: float           # Σ qty по сеемым ключам
    anchors_existing: int      # якорей в БД до вызова
    anchors_created: int       # фактически создано (0 при dry_run)
    entries_created: int       # seed-SLE создано (== anchors_created)
    inflight_pulls: int        # pending / retriable error в очереди (справочно)
    reseed: Optional[SeedReseedStats]  # только при force и не-dry-run
    physical_import_batch_id: Optional[int]
    ledger_generation_id: Optional[int]
    ledger_generation_status: Optional[str]


class GenerationAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation_id: int
    replay_from: datetime
    explicit_empty_physical: bool = False


class GenerationInvalidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_generation_id: int
    status: str
    reason: str


class TruthLifecycleResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ledger_generation: Optional[int] = None
    truth_status: str
    ready: bool
    reason: Optional[str] = None


class HistoricalBootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation_key: str
    opening_at: datetime
    replay_from: datetime
    cutoff: datetime


class HistoricalImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_windows: int = Field(1, ge=1, le=24)
    window_hours: int = Field(24, ge=1, le=168)
    page_size: int = Field(1000, ge=1, le=5000)
    max_pages_per_window: int = Field(10_000, ge=1, le=100_000)
    pause_seconds: float = Field(0, ge=0, le=300)


def _fetch_balance_rows(config: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Снять конвертированные строки Balance из 1С (форма get_stock_from_1c_odata).

    Вынесено в модульную функцию, чтобы тесты могли подменить источник без 1С.
    """
    base_url = sanitize_base_url(str(config.get("base_url") or ""))
    return get_stock_from_1c_odata(
        base_url=base_url,
        entity_name=REGISTER_ENTITY,  # клиент сам переключит на /Balance
        username=config.get("username") or None,
        password=config.get("password") or None,
        token=config.get("token") or None,
    )


def _fetch_balance_at(
    config: Mapping[str, Any],
    at: datetime,
) -> List[Dict[str, Any]]:
    base_url = sanitize_base_url(str(config.get("base_url") or ""))
    return get_stock_from_1c_odata(
        base_url=base_url,
        entity_name=f"{REGISTER_ENTITY}/Balance",
        username=config.get("username") or None,
        password=config.get("password") or None,
        token=config.get("token") or None,
        filter_query=(
            "Period le datetime'"
            f"{at.replace(tzinfo=None, microsecond=0).isoformat()}'"
        ),
    )


def _count_inflight_pulls(db: Session) -> int:
    rows = db.query(
        models.StockRecorderPull.status, models.StockRecorderPull.attempts
    ).all()
    return sum(1 for status, attempts in rows if is_inflight_pull(status, attempts))


def _odata_client_if_configured() -> OData1CClient | None:
    config = load_odata_config()
    base_url = sanitize_base_url(str(config.get("base_url") or ""))
    if not base_url:
        return None
    return OData1CClient(
        base_url=base_url,
        username=config.get("username") or None,
        password=config.get("password") or None,
        token=config.get("token") or None,
    )


def _require_odata_config() -> Mapping[str, Any]:
    config = load_odata_config()
    if not str(config.get("base_url") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="OData connection is not configured (base_url missing)",
        )
    return config


@router.post("/historical-generations/bootstrap", response_model=dict)
def bootstrap_historical_generation(
    payload: HistoricalBootstrapRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Create/resume historical lineage and seed its immutable opening Balance."""
    config = _require_odata_config()
    try:
        rows = _fetch_balance_at(config, payload.opening_at)
        if not rows:
            raise HTTPException(
                status_code=502,
                detail="1С returned an empty historical Balance",
            )
        snapshot = build_balance_snapshot(db, rows, strict=True)
        if not snapshot:
            raise HTTPException(
                status_code=409,
                detail="Historical Balance has no resolvable non-zero Ledger keys",
            )
        created = create_historical_generation(
            db,
            generation_key=payload.generation_key,
            historical_from_exclusive=payload.opening_at,
            replay_from=payload.replay_from,
            cutoff=payload.cutoff,
        )
        opening = seed_historical_opening_balance(
            db,
            ledger_generation_id=created.ledger_generation_id,
            balance_snapshot=snapshot,
        )
        return {
            **historical_generation_status(db, created.ledger_generation_id),
            "created": created.created,
            "opening": {
                "created": opening.created,
                "opening_at": opening.opening_at,
                "content_hash": opening.content_hash,
                "entries_created": opening.entries_created,
                "physical_import_batch_id": opening.physical_import_batch_id,
            },
        }
    except HTTPException:
        raise
    except URLError as exc:
        db.rollback()
        raise HTTPException(
            status_code=502,
            detail=f"Historical Balance OData request failed: {exc.reason}",
        ) from exc
    except (ValueError, GenerationBootstrapError, Phase0BootstrapError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/historical-generations/{generation_id}/import", response_model=dict)
def import_historical_generation(
    generation_id: int,
    payload: HistoricalImportRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Process a bounded number of durable windows, pausing between 1C reads."""
    client = _odata_client_if_configured()
    if client is None:
        raise HTTPException(status_code=400, detail="OData connection is not configured")
    totals = {
        "windows_completed": 0,
        "windows_resumed": 0,
        "recorders_pulled": 0,
        "movements_inserted": 0,
    }
    last = None
    try:
        for index in range(payload.max_windows):
            last = resume_historical_generation_import(
                db,
                ledger_generation_id=generation_id,
                client=client,
                window_size=timedelta(hours=payload.window_hours),
                page_size=payload.page_size,
                max_pages_per_window=payload.max_pages_per_window,
                max_windows=1,
            )
            for key in totals:
                totals[key] += int(getattr(last, key))
            if last.complete:
                break
            if payload.pause_seconds and index + 1 < payload.max_windows:
                time.sleep(payload.pause_seconds)
        assert last is not None
        return {
            **historical_generation_status(db, generation_id),
            **totals,
            "complete": last.complete,
            "physical_import_batch_id": last.physical_import_batch_id,
        }
    except (ValueError, GenerationBootstrapError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/historical-generations/{generation_id}", response_model=dict)
def get_historical_generation(
    generation_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        status = historical_generation_status(db, generation_id)
        generation = db.get(models.LedgerGeneration, generation_id)
        watermarks = dict(generation.source_watermarks or {})
        return {
            **status,
            "opening_balance": watermarks.get("opening_balance"),
            "balance_convergence": watermarks.get("balance_convergence"),
            "import_complete": status["completed_through"] == status["cutoff"],
        }
    except GenerationBootstrapError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/historical-generations/{generation_id}/verify-balance",
    response_model=dict,
)
def verify_historical_generation_balance(
    generation_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    config = _require_odata_config()
    try:
        status = historical_generation_status(db, generation_id)
        if status["completed_through"] != status["cutoff"]:
            raise Phase0BootstrapError(
                "historical import must complete through cutoff before convergence"
            )
        cutoff = datetime.fromisoformat(status["cutoff"])
        rows = _fetch_balance_at(config, cutoff)
        if not rows:
            raise HTTPException(status_code=502, detail="1С returned an empty cutoff Balance")
        snapshot = build_balance_snapshot(db, rows, strict=True)
        result = evaluate_historical_balance_convergence(
            db,
            ledger_generation_id=generation_id,
            balance_snapshot=snapshot,
        )
        return {
            "ledger_generation_id": result.ledger_generation_id,
            "cutoff": result.cutoff,
            "checked_at": result.checked_at,
            "valid": result.valid,
            "content_hash": result.content_hash,
            "compared": result.compared,
            "matched": result.matched,
            "mismatched": result.mismatched,
            "physical_import_batch_id": result.terminal_batch_id,
            "deltas": [
                {
                    "item_id": row.item_id,
                    "organization_ref": row.organization_ref,
                    "warehouse_ref1c": row.warehouse_ref1c,
                    "balance_qty": row.balance_qty,
                    "ledger_qty": row.ledger_qty,
                    "delta_qty": row.delta_qty,
                    "matched": row.matched,
                }
                for row in result.deltas
            ],
        }
    except HTTPException:
        raise
    except URLError as exc:
        db.rollback()
        raise HTTPException(
            status_code=502,
            detail=f"Cutoff Balance OData request failed: {exc.reason}",
        ) from exc
    except (ValueError, GenerationBootstrapError, Phase0BootstrapError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/generations/accept", response_model=dict)
def accept_generation(
    payload: GenerationAcceptRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Build and atomically publish one explicitly selected generation."""
    try:
        generation = db.get(models.LedgerGeneration, payload.generation_id)
        if (
            generation is not None
            and str(generation.algorithm_version or "")
            == HISTORICAL_BOOTSTRAP_ALGORITHM_VERSION
        ):
            _historical_from, sealed_replay_from = _lineage_values(generation)
            requested_replay_from = (
                payload.replay_from.replace(tzinfo=sealed_replay_from.tzinfo)
                if payload.replay_from.tzinfo is None
                else payload.replay_from.astimezone(sealed_replay_from.tzinfo)
            )
            if requested_replay_from != sealed_replay_from:
                raise GenerationValidationError(
                    "historical acceptance replay_from differs from sealed bootstrap lineage"
                )
        result = accept_generation_build(
            db,
            payload.generation_id,
            replay_from=payload.replay_from,
            odata_client=_odata_client_if_configured(),
            explicit_empty_physical=payload.explicit_empty_physical,
        )
        db.commit()
        return {
            **result,
            "ledger_generation": int(payload.generation_id),
            "truth_status": str(result["status"]),
            "ready": True,
            "reason": None,
        }
    except GenerationValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.post("/generations/invalidate", response_model=TruthLifecycleResponse)
def invalidate_generation(
    payload: GenerationInvalidateRequest,
    db: Session = Depends(get_db),
) -> TruthLifecycleResponse:
    """Fail-close the current generation without falling back to older truth."""
    try:
        readiness = invalidate_current_generation(
            db,
            expected_generation_id=payload.expected_generation_id,
            status=payload.status,
            reason=payload.reason,
        )
        db.commit()
        return TruthLifecycleResponse(
            ledger_generation=readiness.ledger_generation,
            truth_status=readiness.truth_status,
            ready=readiness.ready,
            reason=readiness.reason,
        )
    except (ValueError, PlanningTruthInvalidationConflict) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/seed", response_model=SeedResponse)
def seed_ledger(
    dry_run: bool = Query(True, description="Только сводка, БД не трогается"),
    force: bool = Query(
        False,
        description=(
            "Совместимый параметр; разрушительный пере-сид отключён и при "
            "существующей истории возвращает 409"
        ),
    ),
    db: Session = Depends(get_db),
) -> SeedResponse:
    """Сид якоря T0 леджера-1 из свежего 1С /Balance (design Прил. A §4)."""
    config = load_odata_config()
    if not str(config.get("base_url") or "").strip():
        raise HTTPException(status_code=400, detail="OData connection is not configured (base_url missing)")

    anchors_existing = int(db.query(models.StockLedgerAnchor).count() or 0)
    if anchors_existing > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Леджер уже засеян: в stock_ledger_anchor {anchors_existing} якорей. "
                "Повторный сид запрещён: физические факты и принятые поколения "
                "не удаляются. force=true также не стирает историю; создайте "
                "отдельный контролируемый rebuild-процесс с новой lineage."
            ),
        )

    # T0 = момент снимка Balance: фиксируем ДО запроса, чтобы anchor guard
    # (posting_at <= T0 отсекается) не пропустил документ, проведённый между
    # снимком и записью якоря.
    posting_at = datetime.now()
    anchor_period = posting_at.date()

    balance_rows = _fetch_balance_rows(config)
    if not balance_rows:
        raise HTTPException(
            status_code=502,
            detail="1С вернула пустой Balance — сид отклонён (пустой снимок посеял бы нулевой леджер)",
        )

    snapshot = build_balance_snapshot(db, balance_rows)
    nonzero: Dict[LedgerKey, Decimal] = {
        k: q for k, q in snapshot.items() if abs(q) > EPS
    }
    total_qty = float(sum(nonzero.values(), Decimal("0")))
    inflight = _count_inflight_pulls(db)

    summary = dict(
        dry_run=dry_run,
        force=force,
        anchor_period=anchor_period.isoformat(),
        posting_at=posting_at.isoformat(),
        balance_rows=len(balance_rows),
        keys_total=len(snapshot),
        keys_nonzero=len(nonzero),
        keys_skipped_zero=len(snapshot) - len(nonzero),
        total_qty=total_qty,
        anchors_existing=anchors_existing,
        anchors_created=0,
        entries_created=0,
        inflight_pulls=inflight,
        reseed=None,
        physical_import_batch_id=None,
        ledger_generation_id=None,
        ledger_generation_status=None,
    )

    if dry_run:
        return SeedResponse(**summary)

    content_hash = canonical_content_hash(
        [
            [list(key), str(qty.normalize())]
            for key, qty in sorted(nonzero.items(), key=lambda pair: tuple(pair[0]))
        ]
    )
    source_watermarks = {
        "source": "admin_balance_seed",
        "content_hash": content_hash,
        "posting_at": posting_at.isoformat(),
        "anchor_period": anchor_period.isoformat(),
        "keys_nonzero": len(nonzero),
    }
    import_batch = models.PhysicalImportBatch(
        batch_key=(
            f"admin-seed:{posting_at.strftime('%Y%m%dT%H%M%S%f')}:"
            f"{content_hash[:24]}"
        ),
        status="building",
        cutoff=posting_at,
        source_watermarks=source_watermarks,
    )
    generation = models.LedgerGeneration(
        generation_key=(
            f"admin-seed:{posting_at.strftime('%Y%m%dT%H%M%S%f')}:"
            f"{content_hash[:24]}"
        ),
        status="building",
        cutoff=posting_at,
        source_watermarks=source_watermarks,
        capabilities={"physical_ledger": True},
        physical_import_batch=import_batch,
        algorithm_version="admin-seed/1",
    )
    db.add(generation)
    db.flush()

    created = seed_from_balance(
        db,
        nonzero,
        anchor_period=anchor_period,
        posting_at=posting_at,
        ingest_source="seed",
        import_batch=import_batch,
        ledger_generation_id=generation.id,
    )
    import_batch.status = "completed"
    import_batch.completed_at = datetime.now()
    db.commit()

    summary["anchors_created"] = len(created)
    summary["entries_created"] = len(created)
    summary["physical_import_batch_id"] = int(import_batch.id)
    summary["ledger_generation_id"] = int(generation.id)
    summary["ledger_generation_status"] = str(generation.status)
    return SeedResponse(**summary)
