# PRODPLAN: Справочник API

**Версия:** 2.0
**Дата:** 2025-09-24

С переходом на новую архитектуру система управляется через REST API, предоставляемое Backend-сервером (FastAPI). Старый CLI-интерфейс (`main.py`) больше не используется.

Swagger UI (интерактивная документация) доступен по адресу: [http://localhost:8000/docs](http://localhost:8000/docs)

## 🔄 API синхронизации (`/api/v1/sync`)

Эти эндпоинты предназначены для загрузки и обновления данных из 1С OData.

### `POST /sync/nomenclature-odata`
Синхронизирует справочник номенклатуры.

### `POST /sync/units-odata`
Синхронизирует справочник единиц измерения.

### `POST /sync/operations-odata`
Синхронизирует справочник технологических операций.

### `POST /sync/specifications-odata`
Синхронизирует заголовки спецификаций.

### `POST /sync/default-specifications-odata`
Синхронизирует данные о спецификациях по умолчанию для номенклатуры.

**Общий формат запроса для OData-синхронизации:**
```json
{
  "base_url": "http://your-1c-server/odata/standard.odata",
  "entity_name": "Catalog_Номенклатура",
  "username": "user",
  "password": "pass",
  "dry_run": false
}
```

### `GET /sync/progress`
Отслеживает прогресс выполнения запущенной задачи синхронизации.
- **Query-параметр:** `key` (например, `nomenclature`, `units`, `operations`).

## 🌳 API спецификаций (`/api/v1/specification`)

Эндпоинты для работы со структурой спецификаций (BOM).

### `GET /specification/tree`
Возвращает один уровень дочерних узлов для указанного родительского элемента. Используется для "ленивой" загрузки (lazy-load) дерева на клиенте.

**Query-параметры:**
- `item_code` или `item_id`: Идентификатор корневого изделия.
- `parent_id`: ID узла в дереве, для которого нужно загрузить дочерние элементы.
- `root_qty`: Количество корневого изделия для расчета.

### `GET /specification/full`
Возвращает полное, рекурсивно развернутое дерево спецификации для указанного изделия.

**Query-параметры:**
- `item_code`: Код изделия.
- `root_qty`: Количество для расчета.
- `max_depth`: Максимальная глубина рекурсии (по умолчанию 15).

**Формат узла в ответе (для `/tree` и `/full`):**
```json
{
  "id": "item:123:1.0", // Уникальный ID узла
  "type": "item", // 'item' или 'operation'
  "name": "Наименование изделия",
  "article": "Артикул",
  "replenishmentMethod": "Производство",
  "stage": { "id": "1", "name": "Сборка" },
  "qtyPerParent": 1.0, // Количество в родительской спецификации
  "unit": "шт",
  "computed": {
    "treeQty": 1.0 // Итоговое количество в дереве
  },
  "hasChildren": true, // Есть ли дочерние узлы
  "warnings": [] // Массив предупреждений (NO_STAGE, CYCLE_DETECTED)
}
```

## 📊 Планирование: агрегированные эндпоинты (backend-first, без ломающих изменений)

Ниже перечислены новые серверные эндпоинты для «верхних» агрегатов (группировка по видам/участкам, сводка мощностей, дневная повестка). Существующие /results/* эндпоинты не изменялись. Новые эндпоинты реализованы в: [python.def get_run_production_grouped()](backend/app/services/planning_service.py:2508), [python.def get_run_production_agenda_day()](backend/app/services/planning_service.py:2717), [python.def get_run_purchases_grouped()](backend/app/services/planning_service.py:2436), [python.def get_capacity_summary()](backend/app/services/planning_service.py:2386) и опубликованы в роутере: [python.router /v1/plan](backend/app/routers/plan.py:834).

1) GET /api/v1/plan/results/{run_id}/production/grouped
- Описание: Группировка производственных заказов по «виду/участку» (dominant area), с агрегацией по (item_id, unit). Индикаторы мощности подтягиваются из сводки.
- Query-параметры:
  - bucket_type: 'daily' | 'weekly' (опц.)
  - date_from: 'YYYY-MM-DD' (опц.)
  - date_to: 'YYYY-MM-DD' (опц.)
  - area_id: number (опц.)
  - limit, offset (опц., по умолчанию limit=1000)
  - sort_by: 'item_name' | 'item_article' | 'qty' | 'need_date' | 'bucket_date' | 'priority_index' (опц.)
  - sort_dir: 'asc' | 'desc' (опц.)
- Ответ:
  {
    "groups": [
      {
        "area_id": 10,
        "area_name": "Сварка",
        "orders": [
          {
            "agg_key": "123|шт",
            "item_id": 123,
            "item_name": "Деталь А",
            "item_article": "ART-001",
            "unit": "шт",
            "qty": 250.0,
            "norm_hours_total": 120.5,
            "norm_hours_per_unit": 0.482
          }
        ],
        "norm_sum_hours": 120.5,
        "min_days_to_need": 3,
        "cap_overload_hours": 6.0,
        "cap_overloaded_buckets": 1
      }
    ],
    "total_groups": 5,
    "total_orders": 187,
    "limit": 1000,
    "offset": 0
  }

2) GET /api/v1/plan/results/{run_id}/production/agenda_day
- ⚠️ **УДАЛЕНО**: Функциональность "Задание на день" больше не поддерживается.
- Вместо этого используйте фильтрацию по конкретному дню через параметры date_from и date_to в эндпоинтах production и production/grouped.

3) GET /api/v1/plan/results/{run_id}/purchases/grouped
- Описание: Сводная группировка заявок на закупку по (item_id, unit).
- Query-параметры:
  - bucket_type: 'daily' | 'weekly' (опц.)
  - date_from: 'YYYY-MM-DD' (опц.)
  - date_to: 'YYYY-MM-DD' (опц.)
  - limit, offset (опц.)
- Ответ:
  {
    "rows": [
      {
        "agg_key": "345|шт",
        "item_id": 345,
        "item_name": "Крепёж Х",
        "item_article": "BOLT-8",
        "unit": "шт",
        "qty": 500.0
      }
    ],
    "total": 7,
    "limit": 1000,
    "offset": 0
  }

4) GET /api/v1/plan/results/{run_id}/capacity/summary
- Описание: Сводка по мощностям за период (часы/перегрузы) по видам/участкам. Для недельных бакетов используется пятница ISO-недели (см. [python.def _iso_week_friday()](backend/app/services/planning_service.py:731)).
- Query-параметры:
  - bucket_type: 'daily' | 'weekly' (опц.)
  - date_from: 'YYYY-MM-DD' (опц.)
  - date_to: 'YYYY-MM-DD' (опц.)
- Ответ:
  {
    "map": {
      "10": {
        "hours_planned": 320.0,
        "hours_available": 300.0,
        "overload_hours": 20.0,
        "overloaded_buckets": 3
      }
    },
    "total_rows": 42
  }

Примечания:
- Политика дат и недель:
  - Сервер нормализует даты бакетов и фильтры по date_from/date_to. Недельный бакет — пятница ISO-недели.
  - Фронтенд не выполняет дополнительную переклассификацию дат/недель в верхних агрегатах.
- Обратная совместимость:
  - Эндпоинты /api/v1/plan/results/{run_id}/production, /purchases, /capacity, /pegging — без изменений.
- Реализация:
  - Маршруты объявлены в [python.router plan.py](backend/app/routers/plan.py:834), сервисные функции — в [python.planning_service](backend/app/services/planning_service.py:2386).
