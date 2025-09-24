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
    item_article: Optional[str] | None
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
        i.item_article,
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
                item_article=str(r["item_article"]) if r["item_article"] is not None else None,
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
    НЕ использует ON CONFLICT (нет уникального индекса), вместо этого:
      1) UPDATE по (item_id, COALESCE(stage_id,-1), date)
      2) если обновлено 0 строк — INSERT
    """
    # Валидации и нормализация
    try:
        d = date.fromisoformat(date_str)
    except Exception:
        d = date.today()
    qty = float(planned_qty or 0.0)

    with _conn(db_path) as conn:
        # UPDATE сначала
        cur = conn.execute(
            """
            UPDATE production_plan_entries
               SET planned_qty = :planned_qty,
                   updated_at  = datetime('now')
             WHERE item_id = :item_id
               AND date    = :date
               AND COALESCE(stage_id, -1) = COALESCE(:stage_id, -1)
            """,
            {
                "item_id": int(item_id),
                "stage_id": stage_id,
                "date": d.isoformat(),
                "planned_qty": qty,
            },
        )
        if int(getattr(cur, "rowcount", 0) or 0) == 0:
            # INSERT если UPDATE не затронул ни одной строки
            conn.execute(
                """
                INSERT INTO production_plan_entries
                    (item_id, stage_id, date, planned_qty, completed_qty, status, notes, updated_at)
                VALUES
                    (:item_id, :stage_id, :date, :planned_qty, 0.0, 'GREEN', NULL, datetime('now'))
                """,
                {
                    "item_id": int(item_id),
                    "stage_id": stage_id,
                    "date": d.isoformat(),
                    "planned_qty": qty,
                },
            )
        conn.commit()

# --- Bulk upsert: пакетная запись изменений плана (в одной транзакции) ---
def bulk_upsert_plan_entries(
    entries: List[Dict[str, Any]],
    db_path: Optional[str | Path] = None,
) -> int:
    """
    Пакетное сохранение записей плана в одной транзакции.
    entries: [{item_id: int, date: 'YYYY-MM-DD', qty: int, stage_id: Optional[int]}]
    Возвращает количество успешно обработанных записей.
    """
    if not entries:
        return 0

    # Предвалидация и нормализация
    normalized: List[Dict[str, Any]] = []
    for e in entries:
        try:
            iid = int(e.get('item_id'))
            d = str(e.get('date') or '').strip()
            # если дата некорректна, пропускаем
            _ = date.fromisoformat(d)
            qty = int(e.get('qty') or 0)
            stg = e.get('stage_id', None)
            stg = int(stg) if (stg is not None and str(stg).strip() != '') else None
        except Exception:
            continue
        normalized.append({'item_id': iid, 'date': d, 'qty': qty, 'stage_id': stg})

    if not normalized:
        return 0

    with _conn(db_path) as conn:
        try:
            conn.execute("BEGIN")
            saved = 0
            for e in normalized:
                # 1) UPDATE
                cur = conn.execute(
                    """
                    UPDATE production_plan_entries
                       SET planned_qty = :planned_qty,
                           updated_at  = datetime('now')
                     WHERE item_id = :item_id
                       AND date    = :date
                       AND COALESCE(stage_id, -1) = COALESCE(:stage_id, -1)
                    """,
                    {
                        'item_id': e['item_id'],
                        'stage_id': e['stage_id'],
                        'date': e['date'],
                        'planned_qty': float(e['qty'] or 0),
                    },
                )
                rc = int(getattr(cur, "rowcount", 0) or 0)
                if rc == 0:
                    # 2) INSERT
                    conn.execute(
                        """
                        INSERT INTO production_plan_entries
                            (item_id, stage_id, date, planned_qty, completed_qty, status, notes, updated_at)
                        VALUES
                            (:item_id, :stage_id, :date, :planned_qty, 0.0, 'GREEN', NULL, datetime('now'))
                        """,
                        {
                            'item_id': e['item_id'],
                            'stage_id': e['stage_id'],
                            'date': e['date'],
                            'planned_qty': float(e['qty'] or 0),
                        },
                    )
                saved += 1
            conn.commit()
            return saved
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return 0
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
    allowed_sort = {'item_name', 'item_code', 'item_article', 'month_plan'}
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
        i.item_article,
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
            item_article=str(r["item_article"]) if r["item_article"] is not None else None,
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
        i.item_article,
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
            item_article=str(r["item_article"]) if r["item_article"] is not None else None,
            month_plan=float(r["month_plan"] or 0.0),
        ).as_dict()
        for r in rows
    ]
# --- Utility: ensure item exists and upsert basic fields ---
def ensure_item_exists(
    item_code: str,
    item_name: Optional[str] | None = None,
    item_article: Optional[str] | None = None,
    db_path: Optional[str | Path] = None,
) -> int:
    """
    Обеспечивает наличие записи в items по item_code.
    - Вставляет новую запись при отсутствии.
    - Обновляет name/article при наличии (мягко).
    Возвращает item_id.
    """
    code = str(item_code or "").strip()
    if not code:
        raise ValueError("Пустой item_code")
    name = (str(item_name).strip() if item_name is not None else None) or None
    article = (str(item_article).strip() if item_article is not None else None) or None

    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT item_id, item_name, item_article FROM items WHERE item_code = ?",
            (code,),
        ).fetchone()
        if row:
            item_id = int(row["item_id"])
            # Мягкое обновление name/article при наличии новых значений
            try:
                conn.execute(
                    """
                    UPDATE items
                       SET item_name = COALESCE(?, item_name),
                           item_article = COALESCE(?, item_article),
                           updated_at = datetime('now')
                     WHERE item_id = ?
                    """,
                    (name, article, item_id),
                )
                conn.commit()
            except Exception:
                pass
            return item_id

        # Вставка новой записи
        cur = conn.execute(
            """
            INSERT INTO items (item_code, item_name, item_article, created_at, updated_at)
            VALUES (?, COALESCE(?, ?), ?, datetime('now'), datetime('now'))
            """,
            (code, name, code, article),
        )
        item_id = int(cur.lastrowid)
        conn.commit()
        return item_id

# --- Utility: ensure root product row exists (for plan rows like in Excel) ---
def ensure_root_product_by_code(
    item_code: str,
    item_name: Optional[str] | None = None,
    item_article: Optional[str] | None = None,
    db_path: Optional[str | Path] = None,
) -> int:
    """
    Гарантирует наличие строки плана (root_products) для указанного item_code.
    1) Обеспечивает наличие записи в items (мягкий upsert полей name/article)
    2) Вставляет строку в root_products (INSERT OR IGNORE)
    Возвращает item_id.
    """
    item_id = ensure_item_exists(item_code=item_code, item_name=item_name, item_article=item_article, db_path=db_path)
    with _conn(db_path) as conn:
        conn.execute("INSERT OR IGNORE INTO root_products (item_id) VALUES (?)", (int(item_id),))
        conn.commit()
    return item_id
# --- Матрица плана по дням (server-side подготовка данных для AG-Grid) ---
def query_plan_matrix_paginated(
    start_date_str: str,
    days: int = 30,
    stage_id: Optional[int] = None,
    page: int = 1,
    page_size: int = 30,
    sort_by: str = 'item_name',
    sort_dir: str = 'asc',
    db_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """
    Возвращает страницу данных плана в виде матрицы по дням для заданного горизонта.
    На один ряд — одно изделие; внутри ряда словарь days[YYYY-MM-DD] -> qty (int).

    Возвращаемая структура:
    {
      'rows': [
        {
          'item_id': int,
          'item_code': str,
          'item_name': str,
          'item_article': Optional[str],
          'month_plan': float,
          'days': { 'YYYY-MM-DD': int, ... }   # только дни из окна [start, start+days)
        },
        ...
      ],
      'dates': ['YYYY-MM-DD', ...],           # список дат окна (для построения колонок на UI)
      'total': int,                           # всего изделий (по таблице items)
      'page': int,
      'page_size': int,
    }
    """
    try:
        start = date.fromisoformat(start_date_str)
    except Exception:
        start = date.today()
    horizon_days = max(1, int(days or 1))
    end = start + timedelta(days=horizon_days)

    # Безопасная сортировка
    sort_by = (sort_by or 'item_name').lower()
    allowed_sort = {'item_name', 'item_code', 'item_article', 'month_plan'}
    if sort_by not in allowed_sort:
        sort_by = 'item_name'
    sort_dir = (sort_dir or 'asc').lower()
    if sort_dir not in {'asc', 'desc'}:
        sort_dir = 'asc'

    # Пагинация
    p = max(1, int(page or 1))
    ps = max(1, int(page_size or 30))
    offset = (p - 1) * ps

    params: Dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": ps,
        "offset": offset,
    }
    stage_join_clause = ""
    if stage_id is not None:
        stage_join_clause = "AND p.stage_id = :stage_id"
        params["stage_id"] = stage_id

    # Порядок сортировки: month_plan агрегат из sums, остальные поля из items (i.*)
    order_field = 's.month_plan' if sort_by == 'month_plan' else f'i.{sort_by}'

    # БАЗОВЫЙ НАБОР СТРОК — КОРНЕВЫЕ ИЗДЕЛИЯ (как в Excel) И/ИЛИ ИЗДЕЛИЯ С ПЛАНОМ В ОКНЕ
    # Дополнительно учитываем "динамические корни" на основе BOM: parent, не встречающийся как child.
    sql_page = f"""
    WITH sums AS (
        SELECT
            p.item_id,
            COALESCE(SUM(p.planned_qty), 0) AS month_plan
        FROM production_plan_entries p
        WHERE p.date >= :start
          AND p.date <  :end
          {stage_join_clause}
        GROUP BY p.item_id
    ),
    roots_union AS (
        SELECT item_id FROM root_products
        UNION
        SELECT DISTINCT b.parent_item_id AS item_id
          FROM bom b
         WHERE b.parent_item_id NOT IN (SELECT child_item_id FROM bom)
        UNION
        SELECT DISTINCT p.item_id
          FROM production_plan_entries p
         WHERE p.date >= :start
           AND p.date <  :end
           {stage_join_clause}
    )
    SELECT
        i.item_id,
        i.item_code,
        i.item_name,
        i.item_article,
        COALESCE(s.month_plan, 0) AS month_plan
    FROM roots_union r
    JOIN items i ON i.item_id = r.item_id
    LEFT JOIN sums s ON s.item_id = i.item_id
    ORDER BY {order_field} {sort_dir}
    LIMIT :limit OFFSET :offset
    """

    # Общее количество — по объединённому множеству изделий
    sql_total = f"""
    SELECT COUNT(1) AS cnt
      FROM (
        SELECT item_id FROM root_products
        UNION
        SELECT DISTINCT b.parent_item_id AS item_id
          FROM bom b
         WHERE b.parent_item_id NOT IN (SELECT child_item_id FROM bom)
        UNION
        SELECT DISTINCT p.item_id
          FROM production_plan_entries p
         WHERE p.date >= :start
           AND p.date <  :end
           {stage_join_clause}
      ) AS roots_union
    """

    with _conn(db_path) as conn:
        page_rows = conn.execute(sql_page, params).fetchall()
        total = int(conn.execute(sql_total, params).fetchone()["cnt"])

        # Fallback: если в окне дат нет ни одной записи плана, показываем корневые изделия (как в Excel)
        if total == 0:
            # total по корневым изделиям
            total_row = conn.execute(
                """
                SELECT COUNT(1) AS cnt
                  FROM root_products rp
                  JOIN items i ON i.item_id = rp.item_id
                """
            ).fetchone()
            total = int(total_row["cnt"]) if total_row and "cnt" in total_row.keys() else 0

            if total > 0:
                page_rows = conn.execute(
                    """
                    SELECT i.item_id, i.item_code, i.item_name, i.item_article, 0.0 AS month_plan
                      FROM root_products rp
                      JOIN items i ON i.item_id = rp.item_id
                     ORDER BY i.item_name
                     LIMIT :limit OFFSET :offset
                    """,
                    {"limit": params["limit"], "offset": params["offset"]},
                ).fetchall()

    # Список дат окна (ISO)
    date_list = [(start + timedelta(days=k)).isoformat() for k in range(horizon_days)]

    if not page_rows:
        return {
            "rows": [],
            "dates": date_list,
            "total": total,
            "page": p,
            "page_size": ps,
        }

    # Собираем item_ids страницы
    item_ids = [int(r["item_id"]) for r in page_rows]

    # Загружаем план по дням только для item_ids страницы
    placeholders = ", ".join(["?"] * len(item_ids))
    params_days: Dict[str, Any] = {"start": start.isoformat(), "end": end.isoformat()}
    stage_where_clause = ""
    if stage_id is not None:
        stage_where_clause = "AND stage_id = :stage_id"
        params_days["stage_id"] = stage_id

    sql_days = f"""
    SELECT item_id, date, COALESCE(SUM(planned_qty), 0) AS qty
      FROM production_plan_entries
     WHERE item_id IN ({placeholders})
       AND date >= :start
       AND date <  :end
       {stage_where_clause}
     GROUP BY item_id, date
    """

    # В sqlite параметров два вида: позиционные ? и именованные :name; смешивать нельзя.
    # Поэтому выполняем через execute с tuple для ? и затем привяжем именованные вручную.
    # Упростим: построим временный SQL с подстановкой ? и выполним через два вызова.
    # 1) Сначала формируем базовый кортеж параметров: (item_ids...,)
    # 2) Затем используем conn.execute с dict-like, но sqlite3 не поддерживает комбинирование.
    # Решение: сформировать весь SQL через ? без именованных для start/end, передав полный кортеж.
    # Для надёжности создадим альтернативную версию запроса с позиционными параметрами.
    params_pos = list(item_ids) + [params_days["start"], params_days["end"]]
    if stage_id is not None:
        sql_days_pos = f"""
        SELECT item_id, date, COALESCE(SUM(planned_qty), 0) AS qty
          FROM production_plan_entries
         WHERE item_id IN ({placeholders})
           AND date >= ?
           AND date <  ?
           AND stage_id = ?
         GROUP BY item_id, date
        """
        params_pos.append(stage_id)
    else:
        sql_days_pos = f"""
        SELECT item_id, date, COALESCE(SUM(planned_qty), 0) AS qty
          FROM production_plan_entries
         WHERE item_id IN ({placeholders})
           AND date >= ?
           AND date <  ?
         GROUP BY item_id, date
        """

    days_map: Dict[int, Dict[str, int]] = {iid: {} for iid in item_ids}
    with _conn(db_path) as conn:
        for r in conn.execute(sql_days_pos, tuple(params_pos)).fetchall():
            iid = int(r["item_id"])
            ds = str(r["date"])
            q = int(round(float(r["qty"] or 0.0)))
            if iid in days_map:
                days_map[iid][ds] = q

    # Собираем результатные строки
    result_rows: List[Dict[str, Any]] = []
    for r in page_rows:
        iid = int(r["item_id"])
        row_days = {d: int(days_map.get(iid, {}).get(d, 0)) for d in date_list}
        result_rows.append({
            "item_id": iid,
            "item_code": str(r["item_code"]),
            "item_name": str(r["item_name"]),
            "item_article": str(r["item_article"]) if r["item_article"] is not None else None,
            "month_plan": float(r["month_plan"] or 0.0),
            "days": row_days,
        })

    return {
        "rows": result_rows,
        "dates": date_list,
        "total": total,
        "page": p,
        "page_size": ps,
    }

# --- Удаление строк плана для изделия в пределах окна дат ---
def delete_plan_rows_for_item(
    start_date_str: str,
    days: int,
    item_id: int,
    stage_id: Optional[int] = None,
    db_path: Optional[str | Path] = None,
) -> int:
    """
    Удаляет записи плана для изделия item_id в интервале [start; start+days).
    Если указан stage_id — удаляет только в рамках этого этапа.
    Возвращает количество удалённых строк.
    """
    try:
        start = date.fromisoformat(start_date_str)
    except Exception:
        start = date.today()
    end = start + timedelta(days=max(1, int(days or 1)))

    with _conn(db_path) as conn:
        if stage_id is None:
            cur = conn.execute(
                """
                DELETE FROM production_plan_entries
                 WHERE item_id = ?
                   AND date >= ?
                   AND date <  ?
                """,
                (int(item_id), start.isoformat(), end.isoformat()),
            )
        else:
            cur = conn.execute(
                """
                DELETE FROM production_plan_entries
                 WHERE item_id = ?
                   AND COALESCE(stage_id, -1) = COALESCE(?, -1)
                   AND date >= ?
                   AND date <  ?
                """,
                (int(item_id), int(stage_id), start.isoformat(), end.isoformat()),
            )
        conn.commit()
        return int(cur.rowcount or 0)
