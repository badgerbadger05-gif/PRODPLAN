# Схема базы данных PRODPLAN (Актуальная версия)

**Версия:** 3.0
**Дата:** 2025-11-28

## Описание

Этот документ описывает актуальную схему базы данных PostgreSQL для проекта PRODPLAN, сгенерированную на основе моделей SQLAlchemy (`backend/app/models.py`).

---

## Таблицы

### 1. `production_stages` — Этапы производства
Словарь возможных этапов производства.

| Столбец | Тип | Описание |
|---|---|---|
| `stage_id` | `INTEGER` (PK) | Уникальный идентификатор этапа |
| `stage_name` | `VARCHAR(255)` | Название этапа (уникальное) |
| `stage_order` | `INTEGER` | Порядок сортировки |
| `stage_ref1c` | `VARCHAR(36)` | Ссылка на ID в 1С |
| `created_at` | `TIMESTAMP` | Время создания |
| `updated_at` | `TIMESTAMP` | Время последнего обновления |

### 2. `items` — Номенклатура
Справочник номенклатуры (товары, материалы, компоненты).

| Столбец | Тип | Описание |
|---|---|---|
| `item_id` | `INTEGER` (PK) | Уникальный идентификатор номенклатуры |
| `item_code` | `VARCHAR(50)` | Код номенклатуры (уникальный) |
| `item_name` | `TEXT` | Полное наименование |
| `item_article` | `VARCHAR(100)` | Артикул |
| `item_ref1c` | `VARCHAR(36)` | Ссылка на ID в 1С |
| `replenishment_method` | `VARCHAR(50)` | Способ пополнения (напр. "Закупка") |
| `replenishment_time` | `INTEGER` | Срок пополнения (в днях) |
| `unit` | `VARCHAR(50)` | Базовая единица измерения |
| `stock_qty` | `DECIMAL(10, 3)` | Текущий остаток на складе |
| `optimal_batch` | `DECIMAL(15, 3)` | Оптимальная партия для запуска |
| `status` | `VARCHAR(20)` | Статус ('active', 'inactive') |
| `created_at` | `TIMESTAMP` | Время создания |
| `updated_at` | `TIMESTAMP` | Время последнего обновления |

### 3. `item_categories` — Категории номенклатуры
Иерархический справочник категорий (групп) номенклатуры из 1С.

| Столбец | Тип | Описание |
|---|---|---|
| `category_id` | `INTEGER` (PK) | Уникальный идентификатор категории |
| `category_code` | `VARCHAR(50)` | Код категории |
| `category_name` | `VARCHAR(255)` | Наименование категории |
| `category_ref1c` | `VARCHAR(36)` | Ссылка на ID в 1С (уникальная) |
| `parent_id` | `INTEGER` (FK) | Ссылка на родительскую категорию (`item_categories.category_id`) |
| `is_folder` | `BOOLEAN` | Является ли папкой (группой) |
| `predefined` | `BOOLEAN` | Предопределенная категория |
| `predefined_name` | `VARCHAR(100)`| Имя предопределенной категории |
| `data_version` | `VARCHAR(50)` | Версия данных из 1С |
| `deletion_mark` | `BOOLEAN` | Пометка на удаление |
| `created_at` | `TIMESTAMP` | Время создания |
| `updated_at` | `TIMESTAMP` | Время последнего обновления |

### 4. `units` — Единицы измерения
Справочник единиц измерения из 1С.

| Столбец | Тип | Описание |
|---|---|---|
| `unit_id` | `INTEGER` (PK) | Уникальный идентификатор |
| `unit_ref1c` | `VARCHAR(36)` | Ссылка на ID в 1С (уникальная) |
| `unit_code` | `VARCHAR(50)` | Код ЕИ |
| `unit_name` | `VARCHAR(255)` | Краткое наименование |
| `unit_full_name` | `VARCHAR(255)` | Полное наименование |
| `short_name` | `VARCHAR(50)` | Сокращение |
| `iso_code` | `VARCHAR(50)` | Международный код |
| `base_unit_ref1c`| `VARCHAR(36)` | Ссылка на базовую ЕИ |
| `ratio` | `DECIMAL(18, 6)`| Коэффициент к базовой ЕИ |
| `precision` | `INTEGER` | Точность (кол-во знаков после запятой) |
| `created_at` | `TIMESTAMP` | Время создания |
| `updated_at` | `TIMESTAMP` | Время последнего обновления |

### 5. `specifications` — Спецификации
Заголовки спецификаций на производство.

| Столбец | Тип | Описание |
|---|---|---|
| `spec_id` | `INTEGER` (PK) | Уникальный идентификатор спецификации |
| `spec_code` | `VARCHAR(50)` | Код спецификации |
| `spec_name` | `TEXT` | Наименование спецификации |
| `spec_ref1c` | `VARCHAR(36)` | Ссылка на ID в 1С (уникальная) |
| `production_kind_id`| `INTEGER` (FK) | Связь с видом производства (`production_kinds.id`) |
| `created_at` | `TIMESTAMP` | Время создания |
| `updated_at` | `TIMESTAMP` | Время последнего обновления |

### 6. `spec_components` — Компоненты спецификаций
Состав спецификации (материалы и комплектующие).

| Столбец | Тип | Описание |
|---|---|---|
| `component_id` | `INTEGER` (PK) | Уникальный идентификатор |
| `spec_id` | `INTEGER` (FK) | Ссылка на спецификацию (`specifications.spec_id`) |
| `item_id` | `INTEGER` (FK) | Ссылка на номенклатуру (`items.item_id`) |
| `quantity` | `DECIMAL(10, 3)` | Количество |
| `stage_id` | `INTEGER` (FK) | Ссылка на этап производства (`production_stages.stage_id`) |
| `component_type` | `VARCHAR(50)` | Тип компонента ('Материал', 'Сборка') |
| `created_at` | `TIMESTAMP` | Время создания |
| `updated_at` | `TIMESTAMP` | Время последнего обновления |

### 7. `operations` — Технологические операции
Справочник технологических операций.

| Столбец | Тип | Описание |
|---|---|---|
| `operation_id` | `INTEGER` (PK) | Уникальный идентификатор |
| `operation_ref1c` | `VARCHAR(36)` | Ссылка на ID в 1С (уникальная) |
| `operation_name` | `VARCHAR(255)` | Наименование операции |
| `time_norm` | `DECIMAL(10, 4)` | Норма времени |
| `created_at` | `TIMESTAMP` | Время создания |
| `updated_at` | `TIMESTAMP` | Время последнего обновления |

### 8. `spec_operations` — Операции в спецификациях
Технологические операции, привязанные к спецификациям.

| Столбец | Тип | Описание |
|---|---|---|
| `spec_operation_id` | `INTEGER` (PK) | Уникальный идентификатор |
| `spec_id` | `INTEGER` (FK) | Ссылка на спецификацию (`specifications.spec_id`) |
| `operation_id` | `INTEGER` (FK) | Ссылка на операцию (`operations.operation_id`) |
| `stage_id` | `INTEGER` (FK) | Ссылка на этап (`production_stages.stage_id`) |
| `time_norm` | `DECIMAL(10, 4)` | Норма времени |
| `created_at` | `TIMESTAMP` | Время создания |
| `updated_at` | `TIMESTAMP` | Время последнего обновления |

### 9. `default_specifications` — Спецификации по умолчанию
Связь номенклатуры с основной спецификацией.

| Столбец | Тип | Описание |
|---|---|---|
| `id` | `INTEGER` (PK) | Уникальный идентификатор |
| `item_id` | `INTEGER` (FK) | Ссылка на номенклатуру (`items.item_id`) |
| `characteristic_id`| `VARCHAR(36)` | ID характеристики (если используется) |
| `spec_id` | `INTEGER` (FK) | Ссылка на спецификацию (`specifications.spec_id`) |
| `created_at` | `TIMESTAMP` | Время создания |
| `updated_at` | `TIMESTAMP` | Время последнего обновления |

### 10. `production_resources` — Производственные участки
Справочник производственных мощностей (участков).

| Столбец | Тип | Описание |
|---|---|---|
| `resource_id` | `INTEGER` (PK) | Уникальный идентификатор |
| `resource_name` | `VARCHAR(255)` | Название участка |
| `shift_offset` | `INTEGER` | Сдвиг планирования (в днях) |
| `planning_range` | `INTEGER` | Горизонт планирования (в днях) |
| `capacity` | `DECIMAL(10, 2)` | Мощность |
| `work_schedule` | `VARCHAR(100)` | График работы (напр. '5/2') |
| `daily_work_hours`| `DECIMAL(4, 2)` | Рабочее время в часах за сутки |
| `buffer_days` | `INTEGER` | Буфер (в днях) для расчета запуска |
| `created_at` | `TIMESTAMP` | Время создания |
| `updated_at` | `TIMESTAMP` | Время последнего обновления |

### 11. `resource_stages` — Этапы на участках
Привязка этапов производства к производственным участкам.

| Столбец | Тип | Описание |
|---|---|---|
| `id` | `INTEGER` (PK) | Уникальный идентификатор |
| `resource_id` | `INTEGER` (FK) | Ссылка на участок (`production_resources.resource_id`) |
| `stage_id` | `INTEGER` (FK) | Ссылка на этап (`production_stages.stage_id`) |
| `created_at` | `TIMESTAMP` | Время создания |
| `updated_at` | `TIMESTAMP` | Время последнего обновления |

### 12. `production_kinds` — Виды производства
Справочник видов производства из 1С.

| Столбец | Тип | Описание |
|---|---|---|
| `id` | `INTEGER` (PK) | Уникальный идентификатор |
| `ref_1c` | `VARCHAR(255)` | Ссылка на ID в 1С (уникальная) |
| `name` | `VARCHAR(255)` | Наименование вида производства |
| `created_at` | `TIMESTAMP` | Время создания |
| `updated_at` | `TIMESTAMP` | Время последнего обновления |

### 13. `resource_production_kinds` — Виды производств на участках
Привязка видов производств к участкам.

| Столбец | Тип | Описание |
|---|---|---|
| `id` | `INTEGER` (PK) | Уникальный идентификатор |
| `resource_id` | `INTEGER` (FK) | Ссылка на участок (`production_resources.resource_id`) |
| `production_kind_id`| `INTEGER` (FK) | Ссылка на вид производства (`production_kinds.id`) |
| `created_at` | `TIMESTAMP` | Время создания |
| `updated_at` | `TIMESTAMP` | Время последнего обновления |

---

## Таблицы MRP (Планирование потребностей)

### 14. `planning_config_versions` — Версии конфигураций планирования
Хранит версии настроек (конфигураций) для запуска MRP.

| Столбец | Тип | Описание |
|---|---|---|
| `id` | `INTEGER` (PK) | Уникальный идентификатор |
| `version` | `INTEGER` | Номер версии |
| `is_active` | `BOOLEAN` | Является ли текущей активной конфигурацией |
| `config` | `JSONB` | Тело конфигурации в формате JSON |
| `comment` | `TEXT` | Комментарий к версии |
| `created_by` | `VARCHAR(100)` | Автор версии |
| `created_at` | `TIMESTAMP` | Время создания |

### 15. `planning_run` — Прогоны планирования
Запись о каждом запуске (прогоне) MRP.

| Столбец | Тип | Описание |
|---|---|---|
| `run_id` | `INTEGER` (PK) | Уникальный идентификатор прогона |
| `started_at` | `TIMESTAMP` | Время начала |
| `finished_at`| `TIMESTAMP` | Время завершения |
| `status` | `VARCHAR(20)` | Статус ('PENDING', 'SUCCESS', 'ERROR') |
| `started_by` | `VARCHAR(100)` | Инициатор запуска |
| `horizon_days`| `INTEGER` | Горизонт планирования (в днях) |
| `pinned` | `BOOLEAN` | Флаг закрепления (защита от удаления) |
| `config_version_id`| `INTEGER` (FK) | Ссылка на версию конфигурации (`planning_config_versions.id`) |
| `config_snapshot`| `JSONB` | Снимок конфигурации на момент запуска |
| `warnings` | `JSONB` | Предупреждения, возникшие в ходе расчета |
| `kpi` | `JSONB` | Ключевые показатели эффективности прогона |

### 16. `planned_order` — Плановые заказы на производство
Результат MRP: заказы на производство продукции.

| Столбец | Тип | Описание |
|---|---|---|
| `order_id` | `INTEGER` (PK) | Уникальный идентификатор заказа |
| `run_id` | `INTEGER` (FK) | Ссылка на прогон (`planning_run.run_id`) |
| `item_id` | `INTEGER` (FK) | Ссылка на номенклатуру (`items.item_id`) |
| `requested_qty`| `DECIMAL(15, 3)`| Исходная потребность |
| `planned_qty`| `DECIMAL(15, 3)`| Скорректированная потребность |
| `qty` | `DECIMAL(15, 3)` | Количество к производству (с учетом лот-сайзинга) |
| `need_date` | `DATE` | Дата потребности |
| `start_date` | `DATE` | Расчетная дата начала производства |
| `finish_date`| `DATE` | Расчетная дата окончания производства |
| `bucket_date`| `DATE` | Дата, в которую попадает заказ (для группировки) |
| `demand_ref` | `TEXT` | Ссылка на источник потребности |

### 17. `planned_purchase` — Плановые заказы на закупку
Результат MRP: заказы на закупку материалов.

| Столбец | Тип | Описание |
|---|---|---|
| `purchase_id`| `INTEGER` (PK) | Уникальный идентификатор |
| `run_id` | `INTEGER` (FK) | Ссылка на прогон (`planning_run.run_id`) |
| `item_id` | `INTEGER` (FK) | Ссылка на номенклатуру (`items.item_id`) |
| `requested_qty`| `DECIMAL(15, 3)`| Исходная потребность |
| `planned_qty`| `DECIMAL(15, 3)`| Скорректированная потребность |
| `qty` | `DECIMAL(15, 3)` | Количество к закупке |
| `need_date` | `DATE` | Дата, когда материал должен быть на складе |
| `order_date` | `DATE` | Расчетная дата размещения заказа поставщику |
| `lead_time_days`| `INTEGER` | Срок поставки (в днях) |
| `bucket_date`| `DATE` | Дата, в которую попадает заказ (для группировки) |

### 18. `capacity_load` — Загрузка мощностей
Результат MRP: почасовая загрузка производственных участков.

| Столбец | Тип | Описание |
|---|---|---|
| `id` | `INTEGER` (PK) | Уникальный идентификатор |
| `run_id` | `INTEGER` (FK) | Ссылка на прогон (`planning_run.run_id`) |
| `area_id` | `INTEGER` (FK) | Ссылка на участок (`production_resources.resource_id`) |
| `bucket_date`| `DATE` | Дата |
| `hours_planned`| `DECIMAL(12, 3)`| Запланировано часов |
| `hours_available`| `DECIMAL(12, 3)`| Доступно часов |
| `overload_hours`| `DECIMAL(12, 3)`| Перегрузка в часах |

### 19. `pegging_link` — Связи потребностей (Pegging)
Traceability: отслеживание связей между производными и первичными потребностями.

| Столбец | Тип | Описание |
|---|---|---|
| `id` | `INTEGER` (PK) | Уникальный идентификатор |
| `run_id` | `INTEGER` (FK) | Ссылка на прогон (`planning_run.run_id`) |
| `child_item_id`| `INTEGER` (FK) | ID дочерней номенклатуры (`items.item_id`) |
| `parent_item_id`| `INTEGER` (FK) | ID родительской номенклатуры (`items.item_id`) |
| `demand_ref` | `TEXT` | Ссылка на первичную потребность (напр. заказ клиента) |
| `qty_contribution`| `DECIMAL(15, 3)`| Количество, которое внесла эта связь |
| `need_date` | `DATE` | Дата потребности дочернего элемента |
| `parent_need_date`| `DATE` | Дата потребности родительского элемента |

---
*Документ сгенерирован автоматически на основе `backend/app/models.py`.*