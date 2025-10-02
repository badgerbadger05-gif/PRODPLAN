# Схема базы данных PRODPLAN

**Версия:** 1.0  
**Дата:** 2025-09-24

## Описание

Этот документ описывает схему базы данных PostgreSQL для проекта PRODPLAN. Система мигрирует с SQLite на PostgreSQL с разделением на бэкенд (FastAPI) и фронтенд (Quasar).

## ER-диаграмма (описание связей)

```
┌─────────────────────┐    ┌─────────────────────┐
│   production_stages │    │        items        │
│                     │    │                     │
│ stage_id (PK)       │◄───┤ stage_id (FK)       │
│ stage_name          │    │ item_id (PK)        │
│ stage_order         │    │ item_code (UNIQUE)  │
└─────────────────────┘    │ item_name           │
                           │ stock_qty           │
                           │ status              │
                           │ created_at          │
                           │ updated_at          │
                           └─────────────────────┘
                                    │
                                    │
                                    ▼
┌─────────────────────┐    ┌─────────────────────┐
│      units          │    │         bom         │
│                     │    │                     │
│ unit_id (PK)        │    │ bom_id (PK)         │
│ unit_ref1c          │◄───┤ parent_item_id (FK) │
│ unit_code           │    │ child_item_id (FK)  │
│ unit_name           │    │ quantity            │
│ unit_full_name      │    │ link_stage_id (FK)  │
│ short_name          │    │ created_at          │
│ iso_code            │    │ updated_at          │
│ base_unit_ref1c (FK)│    └─────────────────────┘
│ ratio               │
│ precision           │
│ created_at          │
│ updated_at          │
└─────────────────────┘

┌─────────────────────┐    ┌─────────────────────┐
│   production_plan_  │    │   stock_history     │
│      entries        │    │                     │
│                     │    │ id (PK)             │
│ id (PK)             │    │ item_code (FK)      │
│ item_id (FK)        │    │ stock_qty           │
│ stage_id (FK)       │    │ recorded_at         │
│ date                │    └─────────────────────┘
│ planned_qty         │
│ completed_qty       │    ┌─────────────────────┐
│ status              │    │  default_specifica- │
│ notes               │    │      tions          │
│ updated_at          │    │                     │
└─────────────────────┘    │ id (PK)             │
                           │ item_ref1c          │
                           │ spec_ref1c          │
                           │ created_at          │
                           │ updated_at          │
                           └─────────────────────┘

┌─────────────────────┐    ┌─────────────────────┐
│   specifications    │    │   spec_operations   │
│                     │    │                     │
│ spec_id (PK)        │    │ spec_op_id (PK)     │
│ spec_code           │    │ spec_id (FK)        │
│ spec_name           │    │ operation_ref1c     │
│ item_ref1c          │    │ stage_id (FK)       │
│ created_at          │    │ time_norm_nh        │
│ updated_at          │    │ created_at          │
│                      │    │ updated_at          │
└─────────────────────┘    └─────────────────────┘

┌─────────────────────┐    ┌─────────────────────┐
│    operations       │    │   spec_components   │
│                     │    │                     │
│ operation_id (PK)   │    │ comp_id (PK)        │
│ operation_ref1c     │    │ spec_id (FK)        │
│ operation_name      │    │ item_ref1c          │
│ created_at          │    │ quantity            │
│ updated_at          │    │ unit_ref1c          │
└─────────────────────┘    │ link_stage_id (FK)  │
                           │ created_at          │
                           │ updated_at          │
                           └─────────────────────┘
```

## Таблицы

### 1. production_stages — Этапы производства
```sql
CREATE TABLE production_stages (
  stage_id     SERIAL PRIMARY KEY,
  stage_name   VARCHAR(255) UNIQUE NOT NULL,
  stage_order  INTEGER
);
```
Словарь возможных этапов производства.

### 2. items — Номенклатура
```sql
CREATE TABLE items (
  item_id          SERIAL PRIMARY KEY,
  item_code        VARCHAR(255) UNIQUE NOT NULL,
  item_name        VARCHAR(500) NOT NULL,
  stage_id         INTEGER,
  stock_qty        DECIMAL(15,3) DEFAULT 0.0,
  status           VARCHAR(50) DEFAULT 'active',
  replenishment_method VARCHAR(50), -- Закупка, Переработка и т.д.
  created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(stage_id) REFERENCES production_stages(stage_id)
);
```
Справочник номенклатуры из 1С.

### 3. units — Единицы измерения
```sql
CREATE TABLE units (
  unit_id          SERIAL PRIMARY KEY,
  unit_ref1c       VARCHAR(255) UNIQUE NOT NULL,
  unit_code        VARCHAR(50),
  unit_name        VARCHAR(255),
  unit_full_name   VARCHAR(500),
  short_name       VARCHAR(50),
  iso_code         VARCHAR(20),
  base_unit_ref1c  VARCHAR(255),
  ratio            DECIMAL(10,6),
  precision        INTEGER,
  created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(base_unit_ref1c) REFERENCES units(unit_ref1c)
);
```
Справочник единиц измерения из 1С.

### 4. bom — Спецификации (Bill of Materials)
```sql
CREATE TABLE bom (
  bom_id          SERIAL PRIMARY KEY,
  parent_item_id INTEGER NOT NULL,
  child_item_id   INTEGER NOT NULL,
  quantity        DECIMAL(15,3) NOT NULL,
  link_stage_id   INTEGER,
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(parent_item_id) REFERENCES items(item_id),
  FOREIGN KEY(child_item_id)  REFERENCES items(item_id),
  FOREIGN KEY(link_stage_id)  REFERENCES production_stages(stage_id)
);
```
Структура спецификаций (состав изделий).

### 5. specifications — Спецификации (из 1С)
```sql
CREATE TABLE specifications (
  spec_id         SERIAL PRIMARY KEY,
 spec_code       VARCHAR(255),
  spec_name       VARCHAR(500),
  item_ref1c      VARCHAR(255),  -- Ссылка на изделие в 1С
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```
Спецификации из 1С (Catalog_Спецификации).

### 6. spec_components — Компоненты спецификаций
```sql
CREATE TABLE spec_components (
  comp_id         SERIAL PRIMARY KEY,
  spec_id         INTEGER NOT NULL,
  item_ref1c      VARCHAR(255) NOT NULL,  -- Ссылка на компонент в 1С
  quantity        DECIMAL(15,3) NOT NULL,
  unit_ref1c      VARCHAR(255),  -- Ссылка на единицу измерения
  link_stage_id   INTEGER,       -- Этап производства
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(spec_id) REFERENCES specifications(spec_id),
  FOREIGN KEY(link_stage_id) REFERENCES production_stages(stage_id),
  FOREIGN KEY(unit_ref1c) REFERENCES units(unit_ref1c)
);
```
Компоненты спецификаций (Catalog_Спецификации_Состав).

### 7. operations — Операции (из 1С)
```sql
CREATE TABLE operations (
  operation_id     SERIAL PRIMARY KEY,
  operation_ref1c  VARCHAR(255) UNIQUE NOT NULL,
  operation_name   VARCHAR(500),  -- Наименование операции
  created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```
Справочник операций (получаемый через навигацию из спецификаций).

### 8. spec_operations — Операции в спецификациях
```sql
CREATE TABLE spec_operations (
  spec_op_id      SERIAL PRIMARY KEY,
 spec_id         INTEGER NOT NULL,
  operation_ref1c VARCHAR(255) NOT NULL,
  stage_id        INTEGER,       -- Этап производства
  time_norm_nh    DECIMAL(10,2), -- Норма времени в человеко-часах
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(spec_id) REFERENCES specifications(spec_id),
  FOREIGN KEY(stage_id) REFERENCES production_stages(stage_id)
);
```
Операции в спецификациях (Catalog_Спецификации_Операции).

### 9. default_specifications — Спецификации по умолчанию
```sql
CREATE TABLE default_specifications (
  id              SERIAL PRIMARY KEY,
  item_ref1c      VARCHAR(255) NOT NULL,  -- Ссылка на изделие в 1С
  spec_ref1c      VARCHAR(255) NOT NULL, -- Ссылка на спецификацию в 1С
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```
Связь изделий с их спецификациями по умолчанию (InformationRegister_СпецификацииПоУмолчанию).

### 10. production_plan_entries — Планы производства
```sql
CREATE TABLE production_plan_entries (
  id              SERIAL PRIMARY KEY,
  item_id         INTEGER NOT NULL,
  stage_id        INTEGER,
  date            DATE NOT NULL,
  planned_qty     DECIMAL(15,3) NOT NULL DEFAULT 0.0,
  completed_qty   DECIMAL(15,3) NOT NULL DEFAULT 0.0,
  status          VARCHAR(20) NOT NULL DEFAULT 'GREEN',
  notes           TEXT,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(item_id) REFERENCES items(item_id),
  FOREIGN KEY(stage_id) REFERENCES production_stages(stage_id)
);
```
Планы производства по изделиям и этапам.

### 11. stock_history — История остатков
```sql
CREATE TABLE stock_history (
  id              SERIAL PRIMARY KEY,
  item_code       VARCHAR(255) NOT NULL,
  stock_qty       DECIMAL(15,3) NOT NULL,
  recorded_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(item_code) REFERENCES items(item_code)
);
```
История изменения остатков по номенклатуре (хранится 30 дней).

## Индексы для производительности

```sql
-- Индексы для быстрых запросов
CREATE UNIQUE INDEX idx_bom_parent_child ON bom(parent_item_id, child_item_id);
CREATE INDEX idx_plan_stage_date ON production_plan_entries(stage_id, date);
CREATE INDEX idx_stock_history_item_date ON stock_history(item_code, recorded_at);
CREATE INDEX idx_items_code ON items(item_code);
CREATE INDEX idx_items_stage ON items(stage_id);
CREATE INDEX idx_specifications_code ON specifications(spec_code);
```

## Служебные таблицы

- `root_products` — Корневые изделия (определяются автоматно)
- `user_orders` — Пользовательские заказы
- `import_batches` — История импорта данных

### 12. production_resources — Производственные участки
```sql
CREATE TABLE production_resources (
  resource_id     SERIAL PRIMARY KEY,
  resource_name   VARCHAR(255) NOT NULL,
  shift_offset    INTEGER DEFAULT 0,      -- Сдвиг планирования
  planning_range  INTEGER DEFAULT 30,     -- Диапазон планирования в днях
  capacity        DECIMAL(10,3) DEFAULT 0.0, -- Мощность
  work_schedule   VARCHAR(50) DEFAULT '5/2', -- График работы
  daily_work_hours DECIMAL(4,2) DEFAULT 8.0, -- Рабочее время в часах в сутки
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 13. resource_stages — Привязка этапов к участкам
```sql
CREATE TABLE resource_stages (
  id              SERIAL PRIMARY KEY,
  resource_id     INTEGER NOT NULL,
  stage_id        INTEGER NOT NULL,
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(resource_id) REFERENCES production_resources(resource_id),
  FOREIGN KEY(stage_id) REFERENCES production_stages(stage_id)
);
```

## Таблицы MRP (из 2025-09-25)

### 14. planning_config_versions
```sql
CREATE TABLE planning_config_versions (
  id              SERIAL PRIMARY KEY,
  version         INTEGER NOT NULL,
  is_active       BOOLEAN NOT NULL DEFAULT FALSE,
  config          JSONB NOT NULL,
  comment         TEXT,
  created_by      VARCHAR(100),
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- unique(version)
CREATE UNIQUE INDEX ux_planning_config_version ON planning_config_versions(version);
-- unique active config (partial)
CREATE UNIQUE INDEX ux_planning_config_active ON planning_config_versions(is_active) WHERE is_active = TRUE;
CREATE INDEX idx_planning_config_created_at ON planning_config_versions(created_at);
```

### 15. planning_run
```sql
CREATE TABLE planning_run (
  run_id          SERIAL PRIMARY KEY,
  started_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at     TIMESTAMP,
  status          VARCHAR(20) NOT NULL DEFAULT 'PENDING',
  started_by      VARCHAR(100),
  horizon_days    INTEGER,
  use_weekly      BOOLEAN NOT NULL DEFAULT TRUE,
  config_version_id INTEGER,
  config_snapshot JSONB NOT NULL,
  warnings        JSONB,
  kpi             JSONB,
  pinned          BOOLEAN DEFAULT FALSE,
  FOREIGN KEY(config_version_id) REFERENCES planning_config_versions(id)
);
CREATE INDEX idx_planning_run_status ON planning_run(status);
CREATE INDEX idx_planning_run_started_at ON planning_run(started_at);
-- GIN indexes
CREATE INDEX idx_planning_run_kpi_gin ON planning_run USING GIN (kpi);
CREATE INDEX idx_planning_run_warn_gin ON planning_run USING GIN (warnings);
```

### 16. planned_order
```sql
CREATE TABLE planned_order (
  order_id        SERIAL PRIMARY KEY,
  run_id          INTEGER NOT NULL,
  item_id         INTEGER NOT NULL,
  qty             DECIMAL(15,3) NOT NULL,
  need_date       DATE NOT NULL,
  start_date      DATE,
  finish_date     DATE,
  route_ref       VARCHAR(255),
  priority_index  DECIMAL(10,4),
  bucket_type     VARCHAR(10) NOT NULL,
  bucket_date     DATE NOT NULL,
  demand_ref      TEXT,
  demand_date     DATE,
  CHECK (bucket_type IN ('daily','weekly')),
  FOREIGN KEY(run_id) REFERENCES planning_run(run_id) ON DELETE CASCADE,
  FOREIGN KEY(item_id) REFERENCES items(item_id)
);
CREATE INDEX idx_planned_order_run ON planned_order(run_id);
CREATE INDEX idx_planned_order_item ON planned_order(item_id);
CREATE INDEX idx_planned_order_need_date ON planned_order(need_date);
CREATE INDEX idx_planned_order_bucket ON planned_order(bucket_type, bucket_date);
CREATE INDEX idx_planned_order_priority ON planned_order(priority_index);
CREATE INDEX idx_planned_order_dates ON planned_order(start_date, finish_date);
CREATE INDEX idx_planned_order_run_item ON planned_order(run_id, item_id);
```

### 17. planned_order_stage
```sql
CREATE TABLE planned_order_stage (
  id              SERIAL PRIMARY KEY,
  run_id          INTEGER NOT NULL,
  order_id        INTEGER NOT NULL,
  stage_id        INTEGER NOT NULL,
  area_id         INTEGER,
  bucket_type     VARCHAR(10) NOT NULL,
  bucket_date     DATE NOT NULL,
  hours           DECIMAL(12,3) NOT NULL DEFAULT 0.0,
  CHECK (bucket_type IN ('daily','weekly')),
  FOREIGN KEY(run_id) REFERENCES planning_run(run_id) ON DELETE CASCADE,
  FOREIGN KEY(order_id) REFERENCES planned_order(order_id) ON DELETE CASCADE,
  FOREIGN KEY(stage_id) REFERENCES production_stages(stage_id),
  FOREIGN KEY(area_id) REFERENCES production_resources(resource_id)
);
CREATE INDEX idx_pos_run_order ON planned_order_stage(run_id, order_id);
CREATE INDEX idx_pos_stage_area ON planned_order_stage(stage_id, area_id);
CREATE INDEX idx_pos_bucket ON planned_order_stage(bucket_type, bucket_date);
CREATE INDEX idx_pos_area_bucket ON planned_order_stage(area_id, bucket_type, bucket_date);
CREATE INDEX idx_pos_run_stage ON planned_order_stage(run_id, stage_id);
```

### 18. planned_purchase
```sql
CREATE TABLE planned_purchase (
  purchase_id     SERIAL PRIMARY KEY,
 run_id          INTEGER NOT NULL,
  item_id         INTEGER NOT NULL,
  qty             DECIMAL(15,3) NOT NULL,
  need_date       DATE NOT NULL,
  order_date      DATE NOT NULL,
  lead_time_days  INTEGER NOT NULL,
  priority_index  DECIMAL(10,4),
  bucket_type     VARCHAR(10) NOT NULL,
  bucket_date     DATE NOT NULL,
  supplier_ref1c  VARCHAR(255),
  CHECK (bucket_type IN ('daily','weekly')),
  FOREIGN KEY(run_id) REFERENCES planning_run(run_id) ON DELETE CASCADE,
  FOREIGN KEY(item_id) REFERENCES items(item_id)
);
CREATE INDEX idx_planned_purchase_run ON planned_purchase(run_id);
CREATE INDEX idx_planned_purchase_item ON planned_purchase(item_id);
CREATE INDEX idx_planned_purchase_need ON planned_purchase(need_date);
CREATE INDEX idx_planned_purchase_order ON planned_purchase(order_date);
CREATE INDEX idx_planned_purchase_bucket ON planned_purchase(bucket_type, bucket_date);
CREATE INDEX idx_pp_item_need ON planned_purchase(item_id, need_date);
CREATE INDEX idx_pp_item_order ON planned_purchase(item_id, order_date);
```

### 19. capacity_load
```sql
CREATE TABLE capacity_load (
  id              SERIAL PRIMARY KEY,
  run_id          INTEGER NOT NULL,
  area_id         INTEGER NOT NULL,
  bucket_type     VARCHAR(10) NOT NULL,
  bucket_date     DATE NOT NULL,
  hours_planned   DECIMAL(12,3) NOT NULL DEFAULT 0.0,
  hours_available DECIMAL(12,3) NOT NULL DEFAULT 0.0,
  overload_hours  DECIMAL(12,3) NOT NULL DEFAULT 0.0,
  CHECK (bucket_type IN ('daily','weekly')),
  FOREIGN KEY(run_id) REFERENCES planning_run(run_id) ON DELETE CASCADE,
  FOREIGN KEY(area_id) REFERENCES production_resources(resource_id)
);
CREATE UNIQUE INDEX ux_capacity_load ON capacity_load(run_id, area_id, bucket_type, bucket_date);
CREATE INDEX idx_capacity_load_over ON capacity_load(overload_hours);
```

### 20. pegging_link
```sql
CREATE TABLE pegging_link (
  id                  SERIAL PRIMARY KEY,
  run_id              INTEGER NOT NULL,
  child_item_id       INTEGER NOT NULL,
  parent_item_id      INTEGER,
  demand_ref          TEXT,
  qty_contribution    DECIMAL(15,3) NOT NULL,
  need_date           DATE,
  parent_need_date    DATE,
  FOREIGN KEY(run_id) REFERENCES planning_run(run_id) ON DELETE CASCADE,
  FOREIGN KEY(child_item_id) REFERENCES items(item_id),
  FOREIGN KEY(parent_item_id) REFERENCES items(item_id)
);
CREATE INDEX idx_pegging_run_child ON pegging_link(run_id, child_item_id);
CREATE INDEX idx_pegging_run_parent ON pegging_link(run_id, parent_item_id);
```

## Таблицы для видов производства (из 2025-10-02)

### 21. production_kinds
```sql
CREATE TABLE production_kinds (
  id              SERIAL PRIMARY KEY,
  ref_1c          VARCHAR(255) NOT NULL UNIQUE,
  name            VARCHAR(255) NOT NULL,
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
);
CREATE INDEX ix_production_kinds_ref_1c ON production_kinds(ref_1c);
CREATE INDEX ix_production_kinds_name ON production_kinds(name);
```
Справочник видов производства из 1С (Catalog_ВидыПроизводства).

### 22. resource_production_kinds
```sql
CREATE TABLE resource_production_kinds (
  id                  SERIAL PRIMARY KEY,
  resource_id         INTEGER NOT NULL,
  production_kind_id  INTEGER NOT NULL,
  created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
  updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
  FOREIGN KEY(resource_id) REFERENCES production_resources(resource_id),
  FOREIGN KEY(production_kind_id) REFERENCES production_kinds(id),
  UNIQUE(resource_id, production_kind_id),
  UNIQUE(production_kind_id) -- Глобальная уникальность: вид производства может принадлежать только одному участку
);
CREATE INDEX ix_resource_production_kinds_resource ON resource_production_kinds(resource_id);
CREATE INDEX ix_resource_production_kinds_kind ON resource_production_kinds(production_kind_id);
```
Связь производственных участков и видов производства (заполняется вручную в интерфейсе).

### 23. specifications (изменения)
```sql
ALTER TABLE specifications ADD COLUMN production_kind_id INTEGER;
ALTER TABLE specifications ADD CONSTRAINT fk_specifications_production_kind FOREIGN KEY (production_kind_id) REFERENCES production_kinds(id);
CREATE INDEX ix_specifications_production_kind_id ON specifications(production_kind_id);
```
Добавлено поле для связи спецификации с видом производства.

## Индексы для производительности

```sql
-- Индексы для быстрых запросов
CREATE UNIQUE INDEX idx_bom_parent_child ON bom(parent_item_id, child_item_id);
CREATE INDEX idx_plan_stage_date ON production_plan_entries(stage_id, date);
CREATE INDEX idx_stock_history_item_date ON stock_history(item_code, recorded_at);
CREATE INDEX idx_items_code ON items(item_code);
CREATE INDEX idx_items_stage ON items(stage_id);
CREATE INDEX idx_specifications_code ON specifications(spec_code);
CREATE INDEX idx_resource_stages_resource ON resource_stages(resource_id);
CREATE INDEX idx_resource_stages_stage ON resource_stages(stage_id);
CREATE INDEX idx_specifications_production_kind_id ON specifications(production_kind_id);
CREATE INDEX idx_resource_production_kinds_resource ON resource_production_kinds(resource_id);
CREATE INDEX idx_resource_production_kinds_kind ON resource_production_kinds(production_kind_id);
CREATE INDEX ix_production_kinds_ref_1c ON production_kinds(ref_1c);
CREATE INDEX ix_production_kinds_name ON production_kinds(name);
CREATE INDEX idx_specifications_production_kind_id ON specifications(production_kind_id);
CREATE INDEX idx_resource_production_kinds_resource ON resource_production_kinds(resource_id);
CREATE INDEX idx_resource_production_kinds_kind ON resource_production_kinds(production_kind_id);
CREATE INDEX ix_production_kinds_ref_1c ON production_kinds(ref_1c);
CREATE INDEX ix_production_kinds_name ON production_kinds(name);
```

---
