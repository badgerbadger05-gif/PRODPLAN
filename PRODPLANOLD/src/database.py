from __future__ import annotations
import sqlite3
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path("data/specifications.db")
DATA_DIR = DEFAULT_DB_PATH.parent

PRAGMAS = (
    "PRAGMA foreign_keys = ON;",
    "PRAGMA journal_mode = WAL;",
    "PRAGMA synchronous = NORMAL;",
    "PRAGMA temp_store = MEMORY;",
    "PRAGMA cache_size = -64000;"
)

def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """
    Получить подключение к SQLite с PRAGMA.
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    for pragma in PRAGMAS:
        conn.execute(pragma)
    return conn

def init_database(db_path: Optional[Path] = None) -> None:
    """
    Инициализация схемы БД (идемпотентно).
    """
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        # Миграции схемы: добавить недостающие колонки в items (идемпотентно)
        try:
            cols = conn.execute("PRAGMA table_info(items)").fetchall()
            col_names = {str(c[1]) for c in cols}
            if "stock_qty" not in col_names:
                conn.execute("ALTER TABLE items ADD COLUMN stock_qty REAL DEFAULT 0.0")
            if "item_article" not in col_names:
                conn.execute("ALTER TABLE items ADD COLUMN item_article TEXT")
        except Exception:
            # Мягкий фоллбек: не роняем инициализацию, если ALTER недоступен (старые SQLite и пр.)
            pass

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS production_stages (
  stage_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  stage_name   TEXT UNIQUE NOT NULL,
  stage_order  INTEGER
);

CREATE TABLE IF NOT EXISTS items (
  item_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  item_code        TEXT UNIQUE NOT NULL,
  item_name        TEXT NOT NULL,
  stage_id         INTEGER,
  item_description TEXT,
  unit             TEXT,
  stock_qty        REAL DEFAULT 0.0,
  status           TEXT DEFAULT 'active',
  created_at       TEXT DEFAULT (datetime('now')),
  updated_at       TEXT DEFAULT (datetime('now')),
  FOREIGN KEY(stage_id) REFERENCES production_stages(stage_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS bom (
  bom_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_item_id  INTEGER NOT NULL,
  child_item_id   INTEGER NOT NULL,
  quantity        REAL NOT NULL,
  link_stage_id   INTEGER,
  created_at      TEXT DEFAULT (datetime('now')),
  updated_at      TEXT DEFAULT (datetime('now')),
  FOREIGN KEY(parent_item_id) REFERENCES items(item_id) ON DELETE CASCADE,
  FOREIGN KEY(child_item_id)  REFERENCES items(item_id) ON DELETE CASCADE,
  FOREIGN KEY(link_stage_id)  REFERENCES production_stages(stage_id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_bom_parent_child
  ON bom(parent_item_id, child_item_id);

CREATE INDEX IF NOT EXISTS ix_bom_parent ON bom(parent_item_id);
CREATE INDEX IF NOT EXISTS ix_bom_child  ON bom(child_item_id);

CREATE TABLE IF NOT EXISTS import_batches (
  batch_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  source_dir   TEXT NOT NULL,
  hash_algo    TEXT,
  content_hash TEXT,
  started_at   TEXT DEFAULT (datetime('now')),
  completed_at TEXT,
  notes        TEXT
);

-- Корневые изделия (определяются парсером спецификаций)
CREATE TABLE IF NOT EXISTS root_products (
  item_id INTEGER PRIMARY KEY,
  FOREIGN KEY(item_id) REFERENCES items(item_id) ON DELETE CASCADE
);

-- История остатков (централизовано)
CREATE TABLE IF NOT EXISTS stock_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_code TEXT NOT NULL,
  stock_qty REAL NOT NULL,
  recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(item_code) REFERENCES items(item_code) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_stock_history_item_date ON stock_history(item_code, recorded_at);

-- Пользовательские/плановые записи плана производства
CREATE TABLE IF NOT EXISTS production_plan_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL,
  stage_id INTEGER,
  date TEXT NOT NULL,
  planned_qty REAL NOT NULL DEFAULT 0.0,
  completed_qty REAL NOT NULL DEFAULT 0.0,
  status TEXT NOT NULL DEFAULT 'GREEN',
  notes TEXT,
  updated_at TEXT DEFAULT (datetime('now')),
  FOREIGN KEY(item_id) REFERENCES items(item_id) ON DELETE CASCADE,
  FOREIGN KEY(stage_id) REFERENCES production_stages(stage_id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_plan_item_stage_date
  ON production_plan_entries(item_id, stage_id, date);
CREATE INDEX IF NOT EXISTS ix_plan_stage_date ON production_plan_entries(stage_id, date);

-- Пользовательские заказы (на закупку/производство)
CREATE TABLE IF NOT EXISTS user_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL,
  order_type TEXT NOT NULL, -- 'production' | 'purchase'
  due_date TEXT,
  quantity REAL NOT NULL DEFAULT 0.0,
  status TEXT DEFAULT 'NEW',
  notes TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now')),
  FOREIGN KEY(item_id) REFERENCES items(item_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_user_orders_item_type ON user_orders(item_id, order_type);

-- Триггеры автообновления updated_at
CREATE TRIGGER IF NOT EXISTS trg_items_updated_at
AFTER UPDATE ON items
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE items SET updated_at = datetime('now') WHERE item_id = OLD.item_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_bom_updated_at
AFTER UPDATE ON bom
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE bom SET updated_at = datetime('now') WHERE bom_id = OLD.bom_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_plan_entries_updated_at
AFTER UPDATE ON production_plan_entries
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE production_plan_entries SET updated_at = datetime('now') WHERE id = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_user_orders_updated_at
AFTER UPDATE ON user_orders
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE user_orders SET updated_at = datetime('now') WHERE id = OLD.id;
END;

-- Производственные участки (ресурсы)
CREATE TABLE IF NOT EXISTS production_areas (
  area_id INTEGER PRIMARY KEY AUTOINCREMENT,
  area_name TEXT UNIQUE NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,                      -- Флаг участия участка в расчёте
  planning_offset_days INTEGER NOT NULL DEFAULT 0,        -- Сдвиг планирования (дни)
  planning_range_days INTEGER NOT NULL DEFAULT 30,        -- Диапазон планирования (дни)
  capacity_per_day REAL NOT NULL DEFAULT 0.0,             -- Мощность, ед./день
  days_per_week INTEGER NOT NULL DEFAULT 5,               -- Рабочих дней в неделю
  hours_per_day REAL NOT NULL DEFAULT 8.0,                -- Рабочих часов в день
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS area_stage_map (
  area_id INTEGER NOT NULL,
  stage_id INTEGER NOT NULL,
  PRIMARY KEY (area_id, stage_id),
  FOREIGN KEY(area_id) REFERENCES production_areas(area_id) ON DELETE CASCADE,
  FOREIGN KEY(stage_id) REFERENCES production_stages(stage_id) ON DELETE CASCADE
);

CREATE TRIGGER IF NOT EXISTS trg_production_areas_updated_at
AFTER UPDATE ON production_areas
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE production_areas SET updated_at = datetime('now') WHERE area_id = OLD.area_id;
END;
"""

if __name__ == "__main__":
    init_database()