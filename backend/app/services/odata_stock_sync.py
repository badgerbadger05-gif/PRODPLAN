from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Optional, List

from sqlalchemy.orm import Session

from .odata_client import get_stock_from_1c_odata
from ..models import Item
from ..schemas import ODataSyncRequest


@dataclass
class _Stats:
    items_total: int = 0
    matched_in_odata: int = 0
    unmatched_zeroed: int = 0
    items_updated: int = 0
    items_unchanged: int = 0
    dry_run: bool = False
    odata_url: str = ""
    odata_entity: str = ""


def _norm_code(s: str) -> str:
    """
    Нормализация кодов для устойчивого сопоставления:
    - убрать пробелы
    - привести буквы к верхнему регистру
    - заменить запятую на точку
    - если это целое число '1234' или '1234.0' — привести к '1234' без ведущих нулей
    """
    import re

    t = str(s or "").strip()
    if not t:
        return ""
    t = re.sub(r"\s+", "", t).upper()
    t = t.replace(",", ".")
    if re.fullmatch(r"\d+(?:\.0+)?", t):
        t = t.split(".")[0]
        t = t.lstrip("0") or "0"
    return t


def _fetch_db_code_maps(db: Session) -> Dict[str, str]:
    """
    Вернуть отображение:
      raw_code -> normalized_code
    Также формируем обратную карту при использовании (через построение set/lookup по норм-коду).
    """
    result: Dict[str, str] = {}
    for it in db.query(Item).with_entities(Item.item_code).all():
        raw = str(it[0]).strip()
        result[raw] = _norm_code(raw)
    return result


def sync_stock_from_odata(db: Session, req: ODataSyncRequest) -> dict:
    """
    Синхронизация остатков из 1С через OData.
    Алгоритм аналогичен PRODPLANOLD/src/odata_stock_sync.py:
      - чтение всех item_code из БД и нормализация
      - загрузка остатков из 1С и агрегация по нормализованным кодам
      - безопасное обновление stock_qty в одной транзакции
      - флаги dry_run / zero_missing
    """
    stats = _Stats(
        dry_run=bool(req.dry_run),
        odata_url=req.base_url,
        odata_entity=req.entity_name,
    )

    # Карта кодов из БД
    db_code_to_norm = _fetch_db_code_maps(db)
    stats.items_total = len(db_code_to_norm)

    # Получение данных из OData
    stock_data = get_stock_from_1c_odata(
        base_url=req.base_url,
        entity_name=req.entity_name,
        username=req.username,
        password=req.password,
        token=req.token,
        filter_query=req.filter_query,
        select_fields=req.select_fields,
    )

    # Если 1С ничего не вернула — не обновляем, чтобы не обнулять случайно
    if not stock_data:
        stats.dry_run = True
        return asdict(stats)

    # Агрегируем по нормализованным кодам
    odata_map_norm_to_qty: Dict[str, float] = {}
    for rec in stock_data:
        norm = _norm_code(rec.get("code", ""))
        if not norm:
            continue
        qty = float(rec.get("qty") or 0.0)
        odata_map_norm_to_qty[norm] = odata_map_norm_to_qty.get(norm, 0.0) + qty

    if not odata_map_norm_to_qty:
        stats.dry_run = True
        return asdict(stats)

    # Подсчёт совпавших норм-кодов
    matched = sum(1 for norm in db_code_to_norm.values() if norm in odata_map_norm_to_qty)
    stats.matched_in_odata = matched

    zeroed_count = 0
    updated = 0
    unchanged = 0

    # Обновим все записи items
    items: List[Item] = db.query(Item).all()
    try:
        for it in items:
            raw_code = str(it.item_code or "").strip()
            norm_code = db_code_to_norm.get(raw_code, _norm_code(raw_code))
            old_qty = float(it.stock_qty or 0.0)
            if norm_code in odata_map_norm_to_qty:
                new_qty = float(odata_map_norm_to_qty[norm_code])
            else:
                if req.zero_missing:
                    new_qty = 0.0
                else:
                    new_qty = old_qty

            if abs(old_qty - new_qty) > 1e-9:
                if (norm_code not in odata_map_norm_to_qty) and req.zero_missing and old_qty != 0.0:
                    zeroed_count += 1
                it.stock_qty = new_qty
                updated += 1
            else:
                unchanged += 1

        stats.unmatched_zeroed = zeroed_count
        stats.items_updated = updated
        stats.items_unchanged = unchanged

        if req.dry_run:
            db.rollback()
        else:
            db.commit()
    except Exception:
        db.rollback()
        raise

    return asdict(stats)