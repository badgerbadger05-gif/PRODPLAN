# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
import json
from src.database import get_connection

# Локальные утилиты нормализации (без зависимости от Streamlit UI)
_CYR_TO_LAT_MAP = {
    "А": "A", "а": "a",
    "В": "B", "в": "b",
    "С": "C", "с": "c",
    "Е": "E", "е": "e",
    "Н": "H", "н": "h",
    "К": "K", "к": "k",
    "М": "M", "м": "m",
    "О": "O", "о": "o",
    "Р": "P", "р": "p",
    "Т": "T", "т": "t",
    "Х": "X", "х": "x",
    "У": "Y", "у": "y",
    "Г": "G", "г": "g",
    "Ё": "E", "ё": "e",
    "І": "I", "і": "i",
    "Ј": "J", "ј": "j",
}

def _to_lat_lookalike(s: str) -> str:
    if not isinstance(s, str):
        s = str(s or "")
    return "".join(_CYR_TO_LAT_MAP.get(ch, ch) for ch in s)

def _normalize_for_match(s: str) -> str:
    if not isinstance(s, str):
        s = str(s or "")
    t = _to_lat_lookalike(s)
    t = t.replace(" ", "").replace("-", "").replace("_", "")
    return t.upper()

def search_items(query: str, limit: int = 10, db_path=None) -> List[Dict[str, Any]]:
    """
    Поиск номенклатуры по БД (fallback-строковый, быстрый):
    - LIKE по item_name, item_article, item_code
    - Нормализованный поиск по item_article и item_code (удаление разделителей, upper)
    - Кириллица→латиница для артикула
    Возвращает [{item_id, item_name, item_code, item_article}]
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []

    like = f"%{q}%"
    alt = _to_lat_lookalike(q)
    alt_like = f"%{alt}%"
    norm = _normalize_for_match(q)
    norm_like = f"%{norm}%"
    no_separators = q.replace("-", "").replace("_", "").replace(" ", "")
    no_separators_like = f"%{no_separators}%" if len(no_separators) > 2 else norm_like

    sql = """
    SELECT
        i.item_id,
        i.item_name,
        i.item_code,
        COALESCE(i.item_article, '') AS item_article
    FROM items i
    WHERE
        i.item_name LIKE :like
        OR i.item_article LIKE :like
        OR i.item_article LIKE :alt_like
        OR UPPER(REPLACE(REPLACE(REPLACE(COALESCE(i.item_article, ''), '-', ''), '_', ''), ' ', '')) LIKE :norm_like
        OR i.item_code LIKE :like
        OR UPPER(REPLACE(REPLACE(REPLACE(i.item_code, '-', ''), '_', ''), ' ', '')) LIKE :no_sep_like
    LIMIT 200
    """
    params = {
        "like": like,
        "alt_like": alt_like,
        "norm_like": norm_like,
        "no_sep_like": no_separators_like,
    }

    rows: list = []
    with get_connection(db_path) as conn:
        try:
            rows = conn.execute(sql, params).fetchall()
        except Exception:
            return []

    # Простейшее ранжирование
    q_low = q.lower()
    q_norm = norm

    def _score(rec: Dict[str, Any]) -> int:
        name = str(rec.get("item_name") or "")
        code = str(rec.get("item_code") or "")
        article = str(rec.get("item_article") or "")

        score = 0
        a_low = article.lower()
        c_low = code.lower()
        n_low = name.lower()

        a_norm = _normalize_for_match(article)
        c_norm = _normalize_for_match(code)

        if article and (a_low == q_low or a_norm == q_norm):
            score += 100
        if code and (c_low == q_low or c_norm == q_norm):
            score += 90
        if article and (q_low in a_low or q_norm in a_norm):
            score += 60
        if code and (q_low in c_low or q_norm in c_norm):
            score += 50
        if name and (q_low in n_low):
            score += 30

        # Доп. буст за непустой артикул
        if article:
            score += 5

        return score

    results: List[Dict[str, Any]] = []
    for r in rows:
        results.append({
            "item_id": int(r["item_id"]),
            "item_name": str(r["item_name"] or ""),
            "item_code": str(r["item_code"] or ""),
            "item_article": str(r["item_article"] or ""),
        })

    results.sort(key=lambda x: (-_score(x), x["item_name"], x["item_code"]))
    return results[: max(1, int(limit))]

# --- Семантический фолбэк по локальному индексу output/nomenclature_index.json ---
def _load_index_items(path: Path) -> List[Dict[str, Any]]:
    """
    Устойчивое чтение локального индекса номенклатуры с поддержкой разных структур и кодировок.
    Поддерживаемые структуры верхнего уровня:
      - список записей: [...]
      - словарь с ключами: value | items | records | data
    Поля записи: Code|item_code|code, Description|item_name|name, Артикул|item_article|article
    """
    try:
        if not path.exists():
            return []
        raw = path.read_bytes()
        data = None
        # пробуем UTF-8
        try:
            data = json.loads(raw.decode('utf-8'))
        except Exception:
            # пробуем cp1251 (иногда встречается)
            try:
                data = json.loads(raw.decode('windows-1251'))
            except Exception:
                return []

        # Достаем список элементов
        items: Any = data
        if isinstance(data, dict):
            for key in ('value', 'items', 'records', 'data'):
                if key in data and isinstance(data[key], list):
                    items = data[key]
                    break

        if not isinstance(items, list):
            return []

        out: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            name = str(it.get('Description') or it.get('item_name') or it.get('name') or '').strip()
            code = str(it.get('Code') or it.get('item_code') or it.get('code') or '').strip()
            article_val = it.get('Артикул', it.get('item_article', it.get('article', '')))
            article = None if article_val is None else str(article_val).strip()
            if not code and not name:
                continue
            out.append({'item_name': name, 'item_code': code, 'item_article': article})
        return out
    except Exception:
        return []

def _rank_index(query: str, items: List[Dict[str, Any]], limit: int = 20) -> List[Dict[str, Any]]:
    q = (query or '').strip()
    if len(q) < 2:
        return []
    q_low = q.lower()
    q_norm = _normalize_for_match(q)
    def _score(rec: Dict[str, Any]) -> int:
        name = str(rec.get("item_name") or "")
        code = str(rec.get("item_code") or "")
        article = str(rec.get("item_article") or "")
        score = 0
        a_low = article.lower()
        c_low = code.lower()
        n_low = name.lower()
        a_norm = _normalize_for_match(article)
        c_norm = _normalize_for_match(code)
        if article and (a_low == q_low or a_norm == q_norm):
            score += 100
        if code and (c_low == q_low or c_norm == q_norm):
            score += 90
        if article and (q_low in a_low or q_norm in a_norm):
            score += 60
        if code and (q_low in c_low or q_norm in c_norm):
            score += 50
        if name and (q_low in n_low):
            score += 30
        if article:
            score += 5
        return score
    arr = [it for it in items]
    arr.sort(key=lambda x: (-_score(x), x.get('item_name') or '', x.get('item_code') or ''))
    # Возвращаем только поля item_name/item_code/item_article (без item_id — его найдём по БД при добавлении)
    return arr[: max(1, int(limit))]

def search_items_with_index(query: str, limit: int = 10, db_path=None) -> List[Dict[str, Any]]:
    """
    Комбинированный поиск:
    1) Быстрый строковый по БД (search_items)
    2) Фолбэк по локальному индексу output/nomenclature_index.json (без эмбеддингов)
    Результаты объединяются и дедуплицируются по item_code.
    """
    # 1) Поиск по БД
    primary = search_items(query, limit=limit)
    # Если уже достаточно — возвращаем
    if len(primary) >= limit:
        return primary

    # 2) Локальный индекс
    index_path = Path('output') / 'nomenclature_index.json'
    idx_items = _load_index_items(index_path)
    ranked = _rank_index(query, idx_items, limit=limit * 3)  # возьмём больше для объединения

    # Объединение и дедупликация по item_code
    by_code: Dict[str, Dict[str, Any]] = {}
    for rec in primary:
        by_code[str(rec.get('item_code') or '')] = rec

    for it in ranked:
        code = str(it.get('item_code') or '')
        if not code:
            continue
        if code not in by_code:
            # без item_id (не знаем заранее), но этого достаточно для добавления через ensure_root_product_by_code
            by_code[code] = {
                'item_id': -1,
                'item_name': str(it.get('item_name') or ''),
                'item_code': code,
                'item_article': str(it.get('item_article') or ''),
            }

    # Итого
    merged = list(by_code.values())
    # Простое ранжирование аналогично БД-версии
    q_low = (query or '').lower()
    q_norm = _normalize_for_match(query or '')
    def _score_merged(rec: Dict[str, Any]) -> int:
        name = str(rec.get("item_name") or "")
        code = str(rec.get("item_code") or "")
        article = str(rec.get("item_article") or "")
        score = 0
        a_low = article.lower()
        c_low = code.lower()
        n_low = name.lower()
        a_norm = _normalize_for_match(article)
        c_norm = _normalize_for_match(code)
        if article and (a_low == q_low or a_norm == q_norm):
            score += 100
        if code and (c_low == q_low or c_norm == q_norm):
            score += 90
        if article and (q_low in a_low or q_norm in a_norm):
            score += 60
        if code and (q_low in c_low or q_norm in c_norm):
            score += 50
        if name and (q_low in n_low):
            score += 30
        if article:
            score += 5
        return score
    merged.sort(key=lambda x: (-_score_merged(x), x.get('item_name') or '', x.get('item_code') or ''))
    return merged[: max(1, int(limit))]