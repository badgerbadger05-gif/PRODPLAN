# PRODPLAN: Архитектура системы

<<<<<<< HEAD
**Версия:** 2.0  
**Дата:** 2025-09-24

## 🏗️ Архитектурная схема

PRODPLAN использует современную микросервисную архитектуру, где каждый компонент работает в своем Docker-контейнере.

```
┌─────────────────┐   ┌──────────────────┐   ┌──────────────────┐   ┌────────────────┐
│   Пользователи  │──▶│     Frontend     │──▶│      Backend     │──▶│   База данных  │
│ (Веб-браузер)   │   │ (Quasar, Vue.js) │   │ (FastAPI, Python)│   │  (PostgreSQL)  │
└─────────────────┘   └──────────────────┘   └──────────────────┘   └────────────────┘
         ▲                     │                      ▲
         │                     ▼                      │
         └─────────────────────┴──────────────────────┘
                                │
                         ┌──────────────┐
                         │  1С система  │
                         │ (OData API)  │
                         └──────────────┘
```

- **Frontend**: Одностраничное приложение (SPA) на Quasar (Vue.js + TypeScript), которое отвечает за пользовательский интерфейс. Запускается в контейнере, доступно на порту **9000**.
- **Backend**: API-сервер на FastAPI (Python), который обрабатывает всю бизнес-логику, запросы к базе данных и интеграцию с 1С. Запускается в контейнере, доступен на порту **8000**.
- **База данных**: PostgreSQL, хранящая все данные системы (номенклатура, спецификации, планы). Запускается в отдельном контейнере.
- **1С система**: Внешний источник данных, с которым Backend общается по OData API для синхронизации.

## 📊 Схема базы данных PostgreSQL

Схема данных была мигрирована с SQLite на PostgreSQL для улучшения масштабируемости и надежности.

### Основные таблицы

- **`items`**: Номенклатура (товары, материалы, компоненты).
- **`specifications`**: Заголовки спецификаций.
- **`specification_components`**: Состав спецификаций (компоненты).
- **`specification_operations`**: Операции в спецификациях.
- **`operations`**: Справочник технологических операций.
- **`production_stages`**: Производственные этапы.
- **`units`**: Единицы измерения.
- **`default_specifications`**: Спецификации по умолчанию для изделий.
- **`plan_entries`**: Записи плана производства.

Полное описание полей и связей доступно в документе [План_миграции_на_Quasar_FastAPI_PostgreSQL.md](План_миграции_на_Quasar_FastAPI_PostgreSQL.md).

## 🔧 Модули системы

### Backend (`backend/app/`)

| Директория | Назначение |
|------------|------------|
| `main.py`  | Точка входа FastAPI приложения, регистрация роутеров. |
| `database.py` | Настройка подключения к PostgreSQL. |
| `models.py` | Модели таблиц SQLAlchemy. |
| `schemas.py`| Схемы данных Pydantic для валидации в API. |
| `routers/` | API-эндпоинты (например, `plan.py`, `sync.py`, `specification.py`). |
| `services/`| Сервисный слой с бизнес-логикой (например, `plan_service.py`, `odata_client.py`). |

### Frontend (`frontend/src/`)

| Директория | Назначение |
|------------|------------|
| `main.ts` | Точка входа приложения Quasar. |
| `App.vue` | Корневой Vue компонент. |
| `pages/` | Компоненты страниц (например, `PlanPage.vue`, `SyncPage.vue`). |
| `components/`| Переиспользуемые UI-компоненты. |
| `layouts/` | Основной шаблон приложения (`MainLayout.vue`). |
| `services/`| Клиентские сервисы для взаимодействия с Backend API. |
| `stores/` | Управление состоянием приложения с помощью Pinia. |
=======
**Версия:** 1.6  
**Дата:** 2025-09-16

## 🏗️ Архитектурная схема

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   1С система    │────│  PRODPLAN Core   │────│  Пользователи   │
│                 │    │                  │    │                 │
│ • OData API     │    │ • SQLite БД      │    │ • Quasar UI     │
│ • Excel остатки │────│ • Python модули  │────│ • Excel отчеты  │
│ • Спецификации  │    │ • CLI команды    │    │ • Batch скрипты │
└─────────────────┘    └──────────────────┘    └─────────────────┘
```

## 📊 Схема базы данных SQLite

### Основные таблицы

#### `production_stages` — Этапы производства
```sql
CREATE TABLE production_stages (
  stage_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  stage_name   TEXT UNIQUE NOT NULL,
  stage_order  INTEGER
);
```

#### `items` — Номенклатура
```sql
CREATE TABLE items (
  item_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  item_code        TEXT UNIQUE NOT NULL,
  item_name        TEXT NOT NULL,
  stage_id         INTEGER,
  stock_qty        REAL DEFAULT 0.0,
  status           TEXT DEFAULT 'active',
  created_at       TEXT DEFAULT (datetime('now')),
  updated_at       TEXT DEFAULT (datetime('now')),
  FOREIGN KEY(stage_id) REFERENCES production_stages(stage_id)
);
```

#### `bom` — Спецификации (Bill of Materials)
```sql
CREATE TABLE bom (
  bom_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_item_id  INTEGER NOT NULL,
  child_item_id   INTEGER NOT NULL,
  quantity        REAL NOT NULL,
  link_stage_id   INTEGER,
  created_at      TEXT DEFAULT (datetime('now')),
  updated_at      TEXT DEFAULT (datetime('now')),
  FOREIGN KEY(parent_item_id) REFERENCES items(item_id),
  FOREIGN KEY(child_item_id)  REFERENCES items(item_id),
  FOREIGN KEY(link_stage_id)  REFERENCES production_stages(stage_id)
);
```

### Пользовательские данные

#### `production_plan_entries` — Планы производства
```sql
CREATE TABLE production_plan_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL,
  stage_id INTEGER,
  date TEXT NOT NULL,
  planned_qty REAL NOT NULL DEFAULT 0.0,
  completed_qty REAL NOT NULL DEFAULT 0.0,
  status TEXT NOT NULL DEFAULT 'GREEN',
  notes TEXT,
  updated_at TEXT DEFAULT (datetime('now')),
  FOREIGN KEY(item_id) REFERENCES items(item_id),
  FOREIGN KEY(stage_id) REFERENCES production_stages(stage_id)
);
```

#### `stock_history` — История остатков (30 дней)
```sql  
CREATE TABLE stock_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_code TEXT NOT NULL,
  stock_qty REAL NOT NULL,
  recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(item_code) REFERENCES items(item_code)
);
```

### Служебные таблицы
- `root_products` — Корневые изделия (определяются автоматически)
- `user_orders` — Пользовательские заказы  
- `import_batches` — История импорта данных

## 🔧 Модули системы

### 📈 Основные модули (`src/`)

| Модуль | Назначение | Ключевые функции |
|--------|------------|------------------|
| `database.py` | Управление SQLite БД | `get_connection()`, `init_database()` |
| `ui.py` | Quasar веб-интерфейс | `main()`, `_save_plan_to_db()` |
| `planner.py` | Генерация Excel планов | `generate_production_plan()` |
| `order_calculator.py` | Расчет заказов | `calculate_component_needs()` |
| `bom_calculator.py` | SQL расчеты BOM | `explode_bom_for_root()` |

### 📥 Синхронизация данных

| Модуль | Назначение | Источник данных |
|--------|------------|-----------------|
| `stock_sync.py` | Остатки из Excel | `ostatki/*.xlsx` |
| `odata_stock_sync.py` | Остатки из 1С OData | API 1С |
| `spec_importer.py` | Спецификации | `specs/*.xlsx` |
| `stock_history.py` | История остатков | Автоматически |

## 🔄 Потоки данных

### Источники истины:
1. **Спецификации**: `specs/` (Excel) → `items`, `bom`, `production_stages`
2. **Остатки**: 1С OData/Excel → `items.stock_qty`, `stock_history`  
3. **Пользовательские планы**: Quasar UI → `production_plan_entries`

### Алгоритмы:

#### Развертка BOM (рекурсивные CTE)
```sql
WITH RECURSIVE bom_explosion AS (
  SELECT item_id, 1 as quantity, 0 as level
  FROM root_products
  UNION ALL
  SELECT b.child_item_id, be.quantity * b.quantity, be.level + 1
  FROM bom_explosion be
  JOIN bom b ON be.item_id = b.parent_item_id
  WHERE be.level < 15  -- защита от циклов
)
```

#### Нормализация кодов товаров
- Удаление пробелов: `"  ABC123  "` → `"ABC123"`
- Приведение регистра: `"abc123"` → `"ABC123"`  
- Числовые форматы: `"123.0"` → `"123"`

#### Статусы заказов
- **🟢 GREEN**: Материалы в наличии, срок выполнения соблюден
- **🟡 BLUE**: Материалы в наличии, срок просрочен  
- **🔴 RED**: Дефицит материалов, выполнение невозможно
>>>>>>> d7084eacd68e37db18f44b56b8270808ee4aa212

## 📁 Структура проекта

```
prodplan/
<<<<<<< HEAD
├── backend/                 # FastAPI сервер (Python)
│   ├── app/
│   └── Dockerfile
├── frontend/                # Quasar приложение (Vue.js)
│   ├── src/
│   └── Dockerfile
├── .docs/                   # Документация проекта
├── config/                  # Конфигурационные файлы
├── docker-compose.yml       # Оркестрация Docker-контейнеров
├── start.bat                # Скрипт запуска для Windows
└── start.sh                 # Скрипт запуска для Linux/macOS
```
=======
├── src/                    # Основные модули Python
├── data/                   # Файлы баз данных  
│   └── specifications.db   # SQLite БД (главная)
├── specs/                  # Excel спецификации (источник)
├── ostatki/                # Excel остатки из 1С
├── output/                 # Генерируемые отчеты
├── docs/                   # Документация проекта
├── *.bat                   # Batch скрипты Windows
├── main.py                 # CLI точка входа
└── requirements.txt        # Python зависимости
```

## 🔐 Настройки производительности

### SQLite оптимизация:
```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
```

### Индексы для быстрых запросов:
```sql  
CREATE UNIQUE INDEX ux_bom_parent_child ON bom(parent_item_id, child_item_id);
CREATE INDEX ix_plan_stage_date ON production_plan_entries(stage_id, date);
CREATE INDEX idx_stock_history_item_date ON stock_history(item_code, recorded_at);
```

### Времена выполнения этапов (дни):
- **Механообработка**: 3
- **Сборка**: 2  
- **Закупка**: 7
- **Покраска**: 2
- **Фрезеровка**: 3
- **Гибка**: 2
- **Сверловка**: 2
- **Зенковка**: 1
>>>>>>> d7084eacd68e37db18f44b56b8270808ee4aa212
