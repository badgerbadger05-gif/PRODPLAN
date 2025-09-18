# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import sqlite3

from src.database import get_connection, DEFAULT_DB_PATH


@dataclass
class PlanRow:
    item_id: int
    item_code: str
    item_name: str
    month_plan: float

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _conn(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    if db_path is None:
        return get_connection()
    return get_connection(Path(db_path))


def fetch_stages(db_path: Optional[str | Path] = None) -> List[Dict[str, Any]]:
    """
    Возвращает список этапов производства: [{'value': stage_id, 'label': stage_name}, ...]
    """
    with _conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT stage_id, stage_name
            FROM production_stages
            ORDER BY COALESCE(stage_order, 9999), stage_name
            """
        ).fetchall()
        return [{"value": int(r["stage_id"]), "label": str(r["stage_name"])} for r in rows]


def fetch_plan_overview(
    start_date_str: str,
    days: int = 30,
    stage_id: Optional[int] = None,
    limit: int = 200,
    db_path: Optional[str | Path] = None,
) -> List[Dict[str, Any]]:
    """
    Возвращает агрегированный план по изделиям за период [start_date, start_date + days).
    """
    try:
        start = date.fromisoformat(start_date_str)
    except Exception:
        start = date.today()
    end = start + timedelta(days=max(1, int(days)))

    params: Dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": int(limit),
    }
    stage_clause = ""
    if stage_id is not None:
        stage_clause = "AND p.stage_id = :stage_id"
        params["stage_id"] = stage_id

    sql = f"""
    SELECT
        i.item_id,
        i.item_code,
        i.item_name,
        COALESCE(SUM(p.planned_qty), 0) AS month_plan
    FROM items i
    LEFT JOIN production_plan_entries p
        ON p.item_id = i.item_id
       AND p.date >= :start
       AND p.date <  :end
       {stage_clause}
    GROUP BY i.item_id
    ORDER BY i.item_name
    LIMIT :limit
    """
    with _conn(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        result = [
            PlanRow(
                item_id=int(r["item_id"]),
                item_code=str(r["item_code"]),
                item_name=str(r["item_name"]),
                month_plan=float(r["month_plan"] or 0.0),
            ).as_dict()
            for r in rows
        ]
        return result


def upsert_plan_entry(
    item_id: int,
    date_str: str,
    planned_qty: float,
    stage_id: Optional[int] = None,
    db_path: Optional[str | Path] = None,
) -> None:
    """
    Идемпотентно вставляет/обновляет запись плана на указанную дату.
    """
    # Валидации и нормализация
    try:
        d = date.fromisoformat(date_str)
    except Exception:
        d = date.today()
    qty = float(planned_qty or 0.0)

    with _conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO production_plan_entries
                (item_id, stage_id, date, planned_qty, completed_qty, status, notes, updated_at)
            VALUES
                (:item_id, :stage_id, :date, :planned_qty, 0.0, 'GREEN', NULL, datetime('now'))
            ON CONFLICT(item_id, stage_id, date) DO UPDATE SET
                planned_qty = excluded.planned_qty,
                updated_at  = datetime('now')
            """,
            {
                "item_id": int(item_id),
                "stage_id": stage_id,
                "date": d.isoformat(),
                "planned_qty": qty,
            },
        )
        conn.commit()

# --- Шаг 2.2: server-side выборка и экспорт набора плана ---

from typing import Tuple


def query_plan_overview_paginated(
    start_date_str: str,
    days: int = 30,
    stage_id: Optional[int] = None,
    page: int = 1,
    page_size: int = 50,
    sort_by: str = 'item_name',
    sort_dir: str = 'asc',
    db_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """
    Возвращает страницу агрегированного плана с общим количеством строк.

    Поля сортировки (whitelist):
      - item_name
      - item_code
      - month_plan
    Направление: asc|desc
    """
    try:
        start = date.fromisoformat(start_date_str)
    except Exception:
        start = date.today()
    end = start + timedelta(days=max(1, int(days)))

    # Безопасная сортировка (whitelist)
    sort_by = (sort_by or 'item_name').lower()
    allowed_sort = {'item_name', 'item_code', 'month_plan'}
    if sort_by not in allowed_sort:
        sort_by = 'item_name'
    sort_dir = (sort_dir or 'asc').lower()
    if sort_dir not in {'asc', 'desc'}:
        sort_dir = 'asc'

    # Пагинация
    p = max(1, int(page or 1))
    ps = max(1, int(page_size or 50))
    offset = (p - 1) * ps

    stage_clause = ""
    params: Dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": ps,
        "offset": offset,
    }
    if stage_id is not None:
        stage_clause = "AND p.stage_id = :stage_id"
        params["stage_id"] = stage_id

    sql_rows = f"""
    SELECT
        i.item_id,
        i.item_code,
        i.item_name,
        COALESCE(SUM(p.planned_qty), 0) AS month_plan
    FROM items i
    LEFT JOIN production_plan_entries p
        ON p.item_id = i.item_id
       AND p.date >= :start
       AND p.date <  :end
       {stage_clause}
    GROUP BY i.item_id
    ORDER BY {sort_by} {sort_dir}
    LIMIT :limit OFFSET :offset
    """

    # Для суммарного количества возьмем количество изделий (как в overview: список по items)
    sql_total = "SELECT COUNT(1) AS cnt FROM items"

    with _conn(db_path) as conn:
        rows = conn.execute(sql_rows, params).fetchall()
        total = int(conn.execute(sql_total).fetchone()["cnt"])

    result_rows = [
        PlanRow(
            item_id=int(r["item_id"]),
            item_code=str(r["item_code"]),
            item_name=str(r["item_name"]),
            month_plan=float(r["month_plan"] or 0.0),
        ).as_dict()
        for r in rows
    ]
    return {"rows": result_rows, "total": total, "page": p, "page_size": ps}


def fetch_plan_dataset(
    start_date_str: str,
    days: int = 30,
    stage_id: Optional[int] = None,
    db_path: Optional[str | Path] = None,
) -> List[Dict[str, Any]]:
    """
    Полный набор данных для экспорта (без пагинации).
    """
    try:
        start = date.fromisoformat(start_date_str)
    except Exception:
        start = date.today()
    end = start + timedelta(days=max(1, int(days)))

    params: Dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    stage_clause = ""
    if stage_id is not None:
        stage_clause = "AND p.stage_id = :stage_id"
        params["stage_id"] = stage_id

    sql = f"""
    SELECT
        i.item_id,
        i.item_code,
        i.item_name,
        COALESCE(SUM(p.planned_qty), 0) AS month_plan
    FROM items i
    LEFT JOIN production_plan_entries p
        ON p.item_id = i.item_id
       AND p.date >= :start
       AND p.date <  :end
       {stage_clause}
    GROUP BY i.item_id
    ORDER BY i.item_name
    """
    with _conn(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        PlanRow(
            item_id=int(r["item_id"]),
            item_code=str(r["item_code"]),
            item_name=str(r["item_name"]),
            month_plan=float(r["month_plan"] or 0.0),
        ).as_dict()
        for r in rows
    ]