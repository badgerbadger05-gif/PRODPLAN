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
* Идемпотентность: повторный сид при уже существующих якорях без ``force`` →
  409 с пояснением.
* ``force`` — пере-сид по A §4.5: леджер — восстановимая производная (истина
  в 1С), поэтому DELETE stock_ledger_entry целиком, якоря и бины сбрасываются,
  очередь пуллов очищается, затем сид заново от нового T0.

Отдельный файл (не item_ledger.py), чтобы не конфликтовать с параллельными
правками read-API. Никаких записей в 1С (INV-1way / INV-no-write).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..services.item_ledger.ingest import REGISTER_ENTITY, is_inflight_pull
from ..services.item_ledger.physical import EPS, LedgerKey, seed_from_balance
from ..services.item_ledger.reconcile import build_balance_snapshot
from ..services.odata_client import get_stock_from_1c_odata
from ..services.odata_config import load_odata_config, sanitize_base_url

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


def _count_inflight_pulls(db: Session) -> int:
    rows = db.query(
        models.StockRecorderPull.status, models.StockRecorderPull.attempts
    ).all()
    return sum(1 for status, attempts in rows if is_inflight_pull(status, attempts))


@router.post("/seed", response_model=SeedResponse)
def seed_ledger(
    dry_run: bool = Query(True, description="Только сводка, БД не трогается"),
    force: bool = Query(False, description="Пере-сид по A §4.5 при существующих якорях"),
    db: Session = Depends(get_db),
) -> SeedResponse:
    """Сид якоря T0 леджера-1 из свежего 1С /Balance (design Прил. A §4)."""
    config = load_odata_config()
    if not str(config.get("base_url") or "").strip():
        raise HTTPException(status_code=400, detail="OData connection is not configured (base_url missing)")

    anchors_existing = int(db.query(models.StockLedgerAnchor).count() or 0)
    if anchors_existing > 0 and not force:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Леджер уже засеян: в stock_ledger_anchor {anchors_existing} якорей. "
                "Повторный сид без force запрещён (идемпотентность). Если нужен "
                "пере-сид (диагностика/развал, design Прил. A §4.5) — вызовите с "
                "force=true: леджер будет удалён целиком и засеян заново от нового "
                "T0 (леджер — восстановимая производная, истина в 1С)."
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
    )

    if dry_run:
        return SeedResponse(**summary)

    reseed_stats: Optional[Dict[str, int]] = None
    if force and anchors_existing > 0:
        # Пере-сид (A §4.5): DELETE stock_ledger_entry целиком, якоря и бины
        # сбрасываются (unique (key, period) не позволил бы пере-сид в тот же
        # день поверх старых якорей), очередь пуллов очищается — все прежние
        # движения либо уже в новом Balance (Period<=T0), либо придут пуллом
        # с posting_at>T0.
        reseed_stats = {
            "bins_deleted": int(db.query(models.StockBin).delete(synchronize_session=False)),
            "anchors_deleted": int(db.query(models.StockLedgerAnchor).delete(synchronize_session=False)),
            "entries_deleted": int(db.query(models.StockLedgerEntry).delete(synchronize_session=False)),
            "pull_rows_deleted": int(db.query(models.StockRecorderPull).delete(synchronize_session=False)),
        }
        db.flush()

    created = seed_from_balance(
        db, nonzero, anchor_period=anchor_period, posting_at=posting_at, ingest_source="seed"
    )
    db.commit()

    summary["anchors_created"] = len(created)
    summary["entries_created"] = len(created)
    summary["reseed"] = reseed_stats
    return SeedResponse(**summary)
