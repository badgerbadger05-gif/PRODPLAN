from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional, Tuple, List

from .database import get_connection
from .odata_client import get_stock_from_1c_odata


@dataclass
class ODataStockSyncStats:
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
    - если это целое число в виде '1234' или '1234.0' — оставить без ведущих нулей
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


def _fetch_all_db_codes(conn: sqlite3.Connection) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Читать все item_code из БД и вернуть:
    - db_code_to_norm: {item_code_from_db -> normalized_code}
    - norm_to_db_code: {normalized_code -> first_seen_item_code_from_db}
    """
    rows = conn.execute("SELECT item_code FROM items").fetchall()
    db_code_to_norm: Dict[str, str] = {}
    norm_to_db_code: Dict[str, str] = {}
    for r in rows:
        raw = str(r[0]).strip()
        norm = _norm_code(raw)
        db_code_to_norm[raw] = norm
        if norm not in norm_to_db_code:
            norm_to_db_code[norm] = raw
    return db_code_to_norm, norm_to_db_code


def sync_stock_from_odata(
    base_url: str,
    entity_name: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    token: Optional[str] = None,
    filter_query: Optional[str] = None,
    select_fields: Optional[List[str]] = None,
    db_path: Path | str | None = None,
    dry_run: bool = False,
    zero_missing: bool = False,
) -> ODataStockSyncStats:
    """
    Синхронизация остатков из 1С через OData:
    - читает данные об остатках из OData сервиса 1С,
    - сопоставляет по нормализованным кодам,
    - пишет items.stock_qty только для найденных в OData (по умолчанию НЕ обнуляет отсутствующие),
    - опционально может обнулять отсутствующие (zero_missing=True),
    - работает в одной транзакции.
 
    Возвращает статистику ODataStockSyncStats. Печатает JSON со сводкой.
    """
    with get_connection(Path(db_path) if db_path else None) as conn:
        # Прочитать все коды из БД и построить соответствия
        db_code_to_norm, norm_to_db_code = _fetch_all_db_codes(conn)
        items_total = len(db_code_to_norm)

        # Загрузить агрегаты остатков по нормализованным кодам (только по кодам из БД)
        stock_data = get_stock_from_1c_odata(
            base_url=base_url,
            entity_name=entity_name,
            username=username,
            password=password,
            token=token,
            filter_query=filter_query,
            select_fields=select_fields
        )
        # Безопасный выход: если из OData не получены данные, ничего не изменяем (во избежание обнуления остатков)
        if not stock_data:
            print(json.dumps({
                "error": "odata_no_data",
                "message": "OData вернул пустой набор. Обновление БД отменено.",
                "odata_url": base_url,
                "entity": entity_name
            }, ensure_ascii=False))
            return ODataStockSyncStats(
                items_total=items_total,
                matched_in_odata=0,
                unmatched_zeroed=0,
                items_updated=0,
                items_unchanged=items_total,
                dry_run=True,
                odata_url=base_url,
                odata_entity=entity_name,
            )
        
        # Преобразовать данные в карту {нормализованный_код: количество}
        odata_map_norm_to_qty: Dict[str, float] = {}
        for item in stock_data:
            norm_code = _norm_code(item["code"])
            qty = float(item["qty"])
            odata_map_norm_to_qty[norm_code] = odata_map_norm_to_qty.get(norm_code, 0.0) + qty

        # Если после преобразования карта пуста — ничего не обновляем (во избежание массового обнуления)
        if not odata_map_norm_to_qty:
            print(json.dumps({
                "error": "odata_empty_aggregation",
                "message": "Не удалось сопоставить данные OData с кодами из БД. Обновление БД отменено.",
                "odata_url": base_url,
                "entity": entity_name
            }, ensure_ascii=False))
            return ODataStockSyncStats(
                items_total=items_total,
                matched_in_odata=0,
                unmatched_zeroed=0,
                items_updated=0,
                items_unchanged=items_total,
                dry_run=True,
                odata_url=base_url,
                odata_entity=entity_name,
            )

        matched = sum(1 for norm in db_code_to_norm.values() if norm in odata_map_norm_to_qty)
        unmatched = items_total - matched

        stats = ODataStockSyncStats(
            items_total=items_total,
            matched_in_odata=matched,
            unmatched_zeroed=0,
            items_updated=0,
            items_unchanged=0,
            dry_run=dry_run,
            odata_url=base_url,
            odata_entity=entity_name,
        )

        cur = conn.cursor()
        try:
            # Обновляем карточки:
            # - найденные в OData — записываем qty
            # - отсутствующие: если zero_missing=True — обнуляем, иначе оставляем без изменений
            zeroed_count = 0
            for db_code, norm_code in db_code_to_norm.items():
                # Прочитать текущее значение
                row = cur.execute(
                    "SELECT COALESCE(stock_qty, 0.0) FROM items WHERE item_code = ?",
                    (db_code,),
                ).fetchone()
                old_qty = float(row[0]) if row and row[0] is not None else 0.0

                if norm_code in odata_map_norm_to_qty:
                    new_qty = float(odata_map_norm_to_qty[norm_code])
                else:
                    if zero_missing:
                        new_qty = 0.0
                    else:
                        # сохраняем текущее значение
                        new_qty = old_qty

                if abs(old_qty - new_qty) > 1e-9:
                    if not (norm_code in odata_map_norm_to_qty) and zero_missing and old_qty != 0.0:
                        zeroed_count += 1
                    stats.items_updated += 1
                    if not dry_run:
                        cur.execute(
                            """
                            UPDATE items
                            SET stock_qty = ?, updated_at = datetime('now')
                            WHERE item_code = ?
                            """,
                            (new_qty, db_code),
                        )
                else:
                    stats.items_unchanged += 1

            stats.unmatched_zeroed = zeroed_count

            if dry_run:
                conn.rollback()
            else:
                conn.commit()
        except Exception:
            conn.rollback()
            raise

        # Вывести краткую сводку в stdout (можно парсить в батнике/CI)
        print(json.dumps(asdict(stats), ensure_ascii=False))
        return stats