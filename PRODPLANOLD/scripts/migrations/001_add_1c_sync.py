#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Миграция БД (SQLite) для синхронизации с 1С согласно актуализированной документации.

Назначение:
- Добавить GUID 1С и дополнительные поля в items
- Ввести справочник складов и детальные остатки по складам
- Добавить сущности спецификаций (шапка/состав/операции)
- Ввести структуры для заказов на производство и поставщикам
- Зафиксировать соответствия этапов производства с GUID 1С

Особенности SQLite:
- ALTER TABLE ADD COLUMN допускается, но нет IF NOT EXISTS; используем PRAGMA table_info для проверки
- Ограничения UNIQUE на новые колонки обеспечиваем через CREATE UNIQUE INDEX IF NOT EXISTS

Использование:
  python scripts/migrations/001_add_1c_sync.py --db data/specifications.db
  python scripts/migrations/001_add_1c_sync.py            # по умолчанию data/specifications.db

Скрипт идемпотентный: повторный запуск не вызывает ошибок и не дублирует объекты.

Внимание:
- Скрипт только готовит схему. Заполнение данными выполняется отдельными процедурами импорта.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Iterable, Tuple, List


def _col_exists(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    cols = cur.execute(f"PRAGMA table_info({table});").fetchall()
    names = {str(c[1]) for c in cols}
    return column in names


def _table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    row = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?;",
        (table,),
    ).fetchone()
    return bool(row)


def _exec(cur: sqlite3.Cursor, sql: str, params: Tuple = ()) -> None:
    cur.execute(sql, params)


def _exec_script(cur: sqlite3.Cursor, sql: str) -> None:
    cur.executescript(sql)


def migrate(db_path: Path) -> List[str]:
    applied: List[str] = []
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON;")

        # 1) items: добавить поля item_ref1c, replenishment_method, replenishment_time
        if _table_exists(cur, "items"):
            if not _col_exists(cur, "items", "item_ref1c"):
                _exec(cur, "ALTER TABLE items ADD COLUMN item_ref1c TEXT")
                applied.append("items.ADD COLUMN item_ref1c TEXT")
                # Индекс для быстрого поиска/уникальности GUID 1С (через UNIQUE INDEX)
                _exec(cur, "CREATE UNIQUE INDEX IF NOT EXISTS ux_items_ref1c ON items(item_ref1c)")
                applied.append("CREATE UNIQUE INDEX ux_items_ref1c ON items(item_ref1c)")
            if not _col_exists(cur, "items", "replenishment_method"):
                _exec(cur, "ALTER TABLE items ADD COLUMN replenishment_method TEXT")
                applied.append("items.ADD COLUMN replenishment_method TEXT")
            if not _col_exists(cur, "items", "replenishment_time"):
                _exec(cur, "ALTER TABLE items ADD COLUMN replenishment_time INTEGER")
                applied.append("items.ADD COLUMN replenishment_time INTEGER")

        # 2) Справочник складов и остатки по складам
        if not _table_exists(cur, "warehouses"):
            _exec_script(
                cur,
                """
                CREATE TABLE IF NOT EXISTS warehouses (
                  warehouse_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                  warehouse_ref1c  TEXT UNIQUE NOT NULL,
                  warehouse_code   TEXT,
                  warehouse_name   TEXT
                );
                """,
            )
            applied.append("CREATE TABLE warehouses")
        else:
            # На будущее: добавить недостающие колонки, если понадобится
            pass

        if not _table_exists(cur, "stock"):
            _exec_script(
                cur,
                """
                CREATE TABLE IF NOT EXISTS stock (
                  item_id      INTEGER NOT NULL,
                  warehouse_id INTEGER NOT NULL,
                  quantity     REAL NOT NULL DEFAULT 0.0,
                  PRIMARY KEY (item_id, warehouse_id),
                  FOREIGN KEY(item_id)      REFERENCES items(item_id)        ON DELETE CASCADE,
                  FOREIGN KEY(warehouse_id) REFERENCES warehouses(warehouse_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS ix_stock_item ON stock(item_id);
                CREATE INDEX IF NOT EXISTS ix_stock_wh   ON stock(warehouse_id);
                """,
            )
            applied.append("CREATE TABLE stock (+indexes)")
        # else: схема уже есть

        # 3) Спецификации (шапка, состав, операции)
        if not _table_exists(cur, "specifications"):
            _exec_script(
                cur,
                """
                CREATE TABLE IF NOT EXISTS specifications (
                  spec_ref1c    TEXT PRIMARY KEY,  -- Ref_Key 1С (GUID)
                  spec_code     TEXT,
                  spec_name     TEXT,
                  owner_item_id INTEGER,
                  FOREIGN KEY(owner_item_id) REFERENCES items(item_id) ON DELETE SET NULL
                );
                """,
            )
            applied.append("CREATE TABLE specifications")

        # production_stages: добавить соответствие GUID 1С
        if _table_exists(cur, "production_stages"):
            if not _col_exists(cur, "production_stages", "stage_ref1c"):
                _exec(cur, "ALTER TABLE production_stages ADD COLUMN stage_ref1c TEXT")
                applied.append("production_stages.ADD COLUMN stage_ref1c TEXT")
                _exec(
                    cur,
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_stage_ref1c ON production_stages(stage_ref1c)",
                )
                applied.append("CREATE UNIQUE INDEX ux_stage_ref1c ON production_stages(stage_ref1c)")

        if not _table_exists(cur, "spec_components"):
            _exec_script(
                cur,
                """
                CREATE TABLE IF NOT EXISTS spec_components (
                  id                INTEGER PRIMARY KEY AUTOINCREMENT,
                  spec_ref1c        TEXT NOT NULL,
                  parent_item_id    INTEGER,
                  component_item_id INTEGER NOT NULL,
                  quantity          REAL NOT NULL,
                  stage_ref1c       TEXT,
                  stage_id          INTEGER,
                  component_type    TEXT,
                  FOREIGN KEY(spec_ref1c)        REFERENCES specifications(spec_ref1c) ON DELETE CASCADE,
                  FOREIGN KEY(parent_item_id)    REFERENCES items(item_id)            ON DELETE SET NULL,
                  FOREIGN KEY(component_item_id) REFERENCES items(item_id)            ON DELETE CASCADE,
                  FOREIGN KEY(stage_id)          REFERENCES production_stages(stage_id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS ix_spec_components_spec ON spec_components(spec_ref1c);
                CREATE INDEX IF NOT EXISTS ix_spec_components_parent ON spec_components(parent_item_id);
                CREATE INDEX IF NOT EXISTS ix_spec_components_component ON spec_components(component_item_id);
                """,
            )
            applied.append("CREATE TABLE spec_components (+indexes)")

        if not _table_exists(cur, "spec_operations"):
            _exec_script(
                cur,
                """
                CREATE TABLE IF NOT EXISTS spec_operations (
                  id              INTEGER PRIMARY KEY AUTOINCREMENT,
                  spec_ref1c      TEXT NOT NULL,
                  operation_ref1c TEXT,
                  time_norm       REAL,
                  stage_ref1c     TEXT,
                  stage_id        INTEGER,
                  FOREIGN KEY(spec_ref1c) REFERENCES specifications(spec_ref1c) ON DELETE CASCADE,
                  FOREIGN KEY(stage_id)   REFERENCES production_stages(stage_id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS ix_spec_operations_spec ON spec_operations(spec_ref1c);
                """,
            )
            applied.append("CREATE TABLE spec_operations (+indexes)")

        # 4) Заказы на производство
        if not _table_exists(cur, "production_orders"):
            _exec_script(
                cur,
                """
                CREATE TABLE IF NOT EXISTS production_orders (
                  order_ref1c  TEXT PRIMARY KEY,   -- Ref_Key 1С
                  order_number TEXT,
                  order_date   TEXT,
                  is_posted    INTEGER
                );
                """,
            )
            applied.append("CREATE TABLE production_orders")

        if not _table_exists(cur, "production_products"):
            _exec_script(
                cur,
                """
                CREATE TABLE IF NOT EXISTS production_products (
                  id           INTEGER PRIMARY KEY AUTOINCREMENT,
                  order_ref1c  TEXT NOT NULL,
                  item_id      INTEGER NOT NULL,
                  quantity     REAL NOT NULL,
                  spec_ref1c   TEXT,
                  stage_ref1c  TEXT,
                  stage_id     INTEGER,
                  FOREIGN KEY(order_ref1c) REFERENCES production_orders(order_ref1c) ON DELETE CASCADE,
                  FOREIGN KEY(item_id)     REFERENCES items(item_id)                ON DELETE CASCADE,
                  FOREIGN KEY(spec_ref1c)  REFERENCES specifications(spec_ref1c)    ON DELETE SET NULL,
                  FOREIGN KEY(stage_id)    REFERENCES production_stages(stage_id)   ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS ix_prod_products_order ON production_products(order_ref1c);
                """,
            )
            applied.append("CREATE TABLE production_products (+indexes)")

        if not _table_exists(cur, "production_components"):
            _exec_script(
                cur,
                """
                CREATE TABLE IF NOT EXISTS production_components (
                  id           INTEGER PRIMARY KEY AUTOINCREMENT,
                  order_ref1c  TEXT NOT NULL,
                  item_id      INTEGER NOT NULL,
                  quantity     REAL NOT NULL,
                  spec_ref1c   TEXT,
                  stage_ref1c  TEXT,
                  stage_id     INTEGER,
                  FOREIGN KEY(order_ref1c) REFERENCES production_orders(order_ref1c) ON DELETE CASCADE,
                  FOREIGN KEY(item_id)     REFERENCES items(item_id)                ON DELETE CASCADE,
                  FOREIGN KEY(spec_ref1c)  REFERENCES specifications(spec_ref1c)    ON DELETE SET NULL,
                  FOREIGN KEY(stage_id)    REFERENCES production_stages(stage_id)   ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS ix_prod_components_order ON production_components(order_ref1c);
                """,
            )
            applied.append("CREATE TABLE production_components (+indexes)")

        if not _table_exists(cur, "production_operations"):
            _exec_script(
                cur,
                """
                CREATE TABLE IF NOT EXISTS production_operations (
                  id               INTEGER PRIMARY KEY AUTOINCREMENT,
                  order_ref1c      TEXT NOT NULL,
                  operation_ref1c  TEXT,
                  planned_quantity REAL,
                  time_norm        REAL,
                  standard_hours   REAL,
                  stage_ref1c      TEXT,
                  stage_id         INTEGER,
                  FOREIGN KEY(order_ref1c) REFERENCES production_orders(order_ref1c) ON DELETE CASCADE,
                  FOREIGN KEY(stage_id)    REFERENCES production_stages(stage_id)   ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS ix_prod_operations_order ON production_operations(order_ref1c);
                """,
            )
            applied.append("CREATE TABLE production_operations (+indexes)")

        # 5) Заказы поставщикам
        if not _table_exists(cur, "supplier_orders"):
            _exec_script(
                cur,
                """
                CREATE TABLE IF NOT EXISTS supplier_orders (
                  order_ref1c     TEXT PRIMARY KEY,
                  order_number    TEXT,
                  order_date      TEXT,
                  is_posted       INTEGER,
                  supplier_ref1c  TEXT,
                  document_amount REAL
                );
                """,
            )
            applied.append("CREATE TABLE supplier_orders")

        if not _table_exists(cur, "supplier_order_items"):
            _exec_script(
                cur,
                """
                CREATE TABLE IF NOT EXISTS supplier_order_items (
                  id            INTEGER PRIMARY KEY AUTOINCREMENT,
                  order_ref1c   TEXT NOT NULL,
                  item_id       INTEGER NOT NULL,
                  quantity      REAL NOT NULL,
                  price         REAL,
                  amount        REAL,
                  delivery_date TEXT,
                  FOREIGN KEY(order_ref1c) REFERENCES supplier_orders(order_ref1c) ON DELETE CASCADE,
                  FOREIGN KEY(item_id)     REFERENCES items(item_id)              ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS ix_supplier_items_order ON supplier_order_items(order_ref1c);
                """,
            )
            applied.append("CREATE TABLE supplier_order_items (+indexes)")

        # 6) Спецификации по умолчанию для номенклатуры
        if not _table_exists(cur, "default_specifications"):
            _exec_script(
                cur,
                """
                CREATE TABLE IF NOT EXISTS default_specifications (
                  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                  item_id              INTEGER NOT NULL,
                  characteristic_ref1c TEXT,
                  spec_ref1c           TEXT NOT NULL,
                  FOREIGN KEY(item_id)    REFERENCES items(item_id)           ON DELETE CASCADE,
                  FOREIGN KEY(spec_ref1c) REFERENCES specifications(spec_ref1c) ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_defspec_item_char
                  ON default_specifications(item_id, characteristic_ref1c);
                """,
            )
            applied.append("CREATE TABLE default_specifications (+unique index)")

        conn.commit()
        return applied
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Миграция схемы БД для синхронизации с 1С (SQLite, идемпотентно).")
    parser.add_argument("--db", type=str, default="data/specifications.db", help="Путь к SQLite БД")
    args = parser.parse_args()
    db_path = Path(args.db)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    steps = migrate(db_path)
    print("Applied steps:")
    for s in steps:
        print(f"  - {s}")
    if not steps:
        print("Схема уже соответствует требуемой: изменений не требуется.")


if __name__ == "__main__":
    main()