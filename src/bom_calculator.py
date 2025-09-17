from __future__ import annotations

import sqlite3
from typing import Iterable, List, Sequence, Tuple

import pandas as pd


def explode_bom_for_root(
    conn: sqlite3.Connection,
    root_code: str,
    order_qty: float,
    max_depth: int = 15,
) -> pd.DataFrame:
    """
    Развёртка BOM для одного корневого изделия.

    Returns DataFrame:
      columns = ['item_code', 'item_name', 'stage_name', 'required_qty', 'min_depth']
      - required_qty: суммарное количество компонента под корневое изделие
      - min_depth: минимальная глубина появления компонента (для справки/сортировки)
    """
    query = f"""
    WITH RECURSIVE bom_explosion AS (
        -- Уровень 1: дети корня
        SELECT
            b.child_item_id AS item_id,
            ({order_qty}) * b.quantity AS total_qty,
            1 AS depth
        FROM items i
        JOIN bom b ON b.parent_item_id = i.item_id
        WHERE i.item_code = ?

        UNION ALL

        -- Глубже
        SELECT
            b.child_item_id AS item_id,
            e.total_qty * b.quantity AS total_qty,
            e.depth + 1 AS depth
        FROM bom_explosion e
        JOIN bom b ON b.parent_item_id = e.item_id
        WHERE e.depth < {max_depth}
    )
    SELECT
        i.item_code,
        i.item_name,
        COALESCE(ps.stage_name, 'Не указан') AS stage_name,
        SUM(e.total_qty) AS required_qty,
        MIN(e.depth) AS min_depth
    FROM bom_explosion e
    JOIN items i ON i.item_id = e.item_id
    LEFT JOIN production_stages ps ON ps.stage_id = i.stage_id
    GROUP BY i.item_code, i.item_name, ps.stage_name
    ORDER BY stage_name, i.item_code;
    """
    return pd.read_sql_query(query, conn, params=[root_code])


def calculate_component_needs(
    conn: sqlite3.Connection,
    orders: Sequence[Tuple[str, float]],
    max_depth: int = 15,
) -> pd.DataFrame:
    """
    SQL-based расчёт потребностей в компонентах по множеству изделий.

    Args:
        conn: подключение к SQLite
        orders: последовательность (item_code, qty) для корневых изделий
        max_depth: максимальная глубина рекурсии (защита от циклов)

    Returns:
        DataFrame c колонками:
          ['item_code', 'item_name', 'stage_name', 'total_quantity_needed']
    """
    # Заполнить временную таблицу заказов
    orders_df = pd.DataFrame(orders, columns=["item_code", "qty"])
    orders_df.to_sql("temp_orders", conn, if_exists="replace", index=False)

    query = f"""
    WITH RECURSIVE bom_explosion AS (
        -- Начальные изделия из заказов (уровень 0)
        SELECT
            i.item_id,
            i.item_code,
            i.item_name,
            ps.stage_name,
            o.qty AS total_qty,
            0 AS depth
        FROM temp_orders o
        JOIN items i ON i.item_code = o.item_code
        LEFT JOIN production_stages ps ON ps.stage_id = i.stage_id

        UNION ALL

        -- Рекурсивное развертывание компонентов
        SELECT
            child.item_id,
            child.item_code,
            child.item_name,
            ps.stage_name,
            parent.total_qty * b.quantity AS total_qty,
            parent.depth + 1 AS depth
        FROM bom_explosion parent
        JOIN bom b ON b.parent_item_id = parent.item_id
        JOIN items child ON child.item_id = b.child_item_id
        LEFT JOIN production_stages ps ON ps.stage_id = child.stage_id
        WHERE parent.depth < {max_depth}
    )
    SELECT
        item_code,
        item_name,
        COALESCE(stage_name, 'Не указан') AS stage_name,
        SUM(total_qty) AS total_quantity_needed
    FROM bom_explosion
    WHERE depth > 0  -- исключаем корневые изделия
    GROUP BY item_code, item_name, stage_name
    ORDER BY stage_name, item_code;
    """
    try:
        result = pd.read_sql_query(query, conn)
    finally:
        try:
            conn.execute("DROP TABLE IF EXISTS temp_orders")
        except Exception:
            pass
    return result


def where_used(
    conn: sqlite3.Connection,
    child_code: str,
    max_depth: int = 15,
) -> pd.DataFrame:
    """
    Запрос «Где используется» (Where-Used) для указанного компонента.

    Returns DataFrame:
      columns = ['item_code', 'item_name']
      - уникальный список родителей (включая предков, дедов и т.д.)
    """
    query = f"""
    WITH RECURSIVE start_part AS (
        SELECT item_id FROM items WHERE item_code = ?
    ),
    ancestors AS (
        SELECT
            b.parent_item_id AS item_id,
            1 AS depth
        FROM bom b
        JOIN start_part s ON b.child_item_id = s.item_id

        UNION ALL

        SELECT
            b.parent_item_id AS item_id,
            a.depth + 1 AS depth
        FROM ancestors a
        JOIN bom b ON b.child_item_id = a.item_id
        WHERE a.depth < {max_depth}
    )
    SELECT DISTINCT i.item_code, i.item_name
    FROM ancestors a
    JOIN items i ON i.item_id = a.item_id
    ORDER BY i.item_code;
    """
    return pd.read_sql_query(query, conn)


def get_root_products(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Получить корневые изделия.

    Приоритет:
      1) Если существует и непуста таблица root_products — используем её.
      2) Иначе — эвристика: элементы, которые не являются чьими‑то детьми.
    """
    try:
        exists_df = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='root_products';",
            conn,
        )
        if not exists_df.empty:
            rp_df = pd.read_sql_query(
                """
                SELECT i.item_code, i.item_name, ps.stage_name
                FROM root_products rp
                JOIN items i ON i.item_id = rp.item_id
                LEFT JOIN production_stages ps ON ps.stage_id = i.stage_id
                ORDER BY i.item_code;
                """,
                conn,
            )
            if not rp_df.empty:
                return rp_df
    except Exception:
        pass

    query = """
    SELECT i.item_code, i.item_name, ps.stage_name
    FROM items i
    LEFT JOIN production_stages ps ON ps.stage_id = i.stage_id
    WHERE i.item_id NOT IN (SELECT DISTINCT child_item_id FROM bom)
    ORDER BY i.item_code;
    """
    return pd.read_sql_query(query, conn)