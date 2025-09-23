"""
Скрипт для получения базовой информации о структуре базы данных SQLite.

Этот скрипт подключается к базе данных specifications.db и выводит:
- Список всех таблиц в базе данных
- Список колонок в ключевых таблицах:
  * items
  * stock_history
  * production_plan_entries
  * user_orders
- Перечень индексов для ключевых таблиц

Использование:
python scripts/db_info.py
"""

import sqlite3


def print_table_schema(cur: sqlite3.Cursor, table_name: str) -> None:
    cur.execute(f"PRAGMA table_info({table_name});")
    columns = cur.fetchall()
    if not columns:
        print(f"\nTable '{table_name}' not found.")
        return
    print(f"\nColumns in {table_name} table:")
    for col in columns:
        # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
        print(f"  - {col[1]} ({col[2]})")


def print_indices(cur: sqlite3.Cursor, table_name: str) -> None:
    cur.execute(f"PRAGMA index_list('{table_name}');")
    idxs = cur.fetchall()
    if not idxs:
        return
    print(f"\nIndices on {table_name}:")
    for idx in idxs:
        # PRAGMA index_list columns (SQLite >= 3.8.9): seq, name, unique, origin, partial
        unique = "UNIQUE" if (len(idx) > 2 and idx[2]) else "NON-UNIQUE"
        print(f"  - {idx[1]} ({unique})")


def get_database_info():
    """Получает и выводит информацию о структуре базы данных."""
    conn = sqlite3.connect('data/specifications.db')
    cursor = conn.cursor()

    # Получаем список таблиц
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
    tables = cursor.fetchall()
    table_names = [t[0] for t in tables]

    print("Tables in the database:")
    for name in table_names:
        print(f"  - {name}")

    # Печатаем схемы ключевых таблиц, если они существуют
    for tbl in ("items", "stock_history", "production_plan_entries", "user_orders"):
        if tbl in table_names:
            print_table_schema(cursor, tbl)
            print_indices(cursor, tbl)

    conn.close()


if __name__ == "__main__":
    get_database_info()