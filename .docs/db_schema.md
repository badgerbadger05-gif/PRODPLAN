# База данных

Источник правды: `backend/app/models.py` (77 таблиц), миграции — `backend/alembic/versions/`.
Любое изменение моделей → миграция Alembic + правка этого файла.

Внимание: на чистой БД схему строит `create_all`, а не миграции (несущий `create_all` — см. `tech-debt.md` B1).

## Справочники и календарь

- `items` — номенклатура (+ `optimal_batch`, `buffer_days` через ресурс)
- `item_categories` — категории номенклатуры 1С
- `units` — единицы измерения
- `employees` — сотрудники 1С (для сдельного наряда)
- `operations` — операции (нормы/расценки)
- `suppliers` — поставщики
- `production_stages` — этапы производства
- `production_resources` — участки/ресурсы
- `resource_stages` — связь «ресурс → этап»
- `production_kinds` / `resource_production_kinds` — виды производства 1С и их привязка к участкам
- `stock_warehouses` — склады 1С
- `item_warehouse_stock` — legacy-остатки по складам (снимок из 1С)
- `ignored_warehouses` — исключённые из подбора склады
- `workshop_warehouse_bindings` — привязка цех ↔ склад
- `root_products` — корневые изделия
- `item_embeddings` — эмбеддинги для поиска номенклатуры
- `work_calendar_day` — производственный календарь

## Спецификации (BOM)

- `specifications` / `spec_components` / `spec_operations` — спецификации, состав, операции
- `default_specifications` — спецификация по умолчанию для изделия

## Ядро планирования (MRP)

- `planning_config_versions` — версии конфигурации планирования
- `production_plan_header` / `production_plan_line` — период-план (шапка/строки)
- `production_plan_entries` — записи плана производства
- `planning_run` — прогон MRP
- `planned_order` / `planned_order_stage` — плановые производственные заказы и их этапы
- `planned_purchase` — плановые закупки
- `planned_rework` — заказы на переработку
- `mrp_requirement` / `mrp_requirement_bucket` — потребности MRP и их разрезы
- `capacity_load` — загрузка мощностей
- `pegging_link` — трассировка потребностей
- `forced_order_request` / `forced_order_result` — принудительные заказы
- `production_day_close` / `production_day_close_item` — закрытие дня и перенос остатков

## MRP freeze / execution / drift

- `mrp_freeze_baseline` / `mrp_freeze_allocation` / `mrp_freeze_component` — заморозка нетто-расчёта (stock-once между планами)
- `mrp_execution_allocation` — привязка факта исполнения к потребностям
- `mrp_drift_event` — события дрейфа (reconcile)
- `mrp_requirement_carry` — МЁРТВАЯ (carry-дизайн отброшен, снести — см. `tech-debt.md`)

## DBR (11 таблиц)

- `dbr_settings` — настройки модуля
- `dbr_assembly_rate` — темпы сборки
- `dbr_category_supply_risk` — риск снабжения по категориям
- `dbr_supermarket_position` — позиции супермаркета (буферы питателей)
- `dbr_feeder_signal` — сигналы питателей
- `dbr_production_program` / `dbr_production_program_item` — производственная программа
- `dbr_drum_schedule` / `dbr_drum_schedule_program` / `dbr_drum_slot` — расписание барабана и слоты
- `dbr_drum_capacity_gap` — дефициты мощности барабана

## Item-ledger (7 таблиц)

- `stock_ledger_entry` — append-only движения (леджер-1)
- `stock_bin` — агрегат остатка по ключу item/склад/характеристика/организация
- `stock_recorder_pull` — очередь pull-by-document (retry/attempts)
- `stock_ledger_anchor` — T0-якорь первичной загрузки
- `reservation_entry` / `reservation_event` / `reservation_coverage` — леджер-2 резервов (резерв, журнал событий, покрытие)

## Paint/weld

- `paint_weld_pairs` / `paint_weld_chain_links` — связка «окраска ↔ сварка» (пары и звенья цепочки)

## 1С-зеркала (документы)

- `production_orders` / `production_products` — заказы на производство 1С и их строки
- `production_order_line_states` — состояния строк (перемещение/выпуск, ref-ы документов)
- `production_material_issues` / `production_material_issue_lines` — заявки на перемещение материалов
- `production_manufactures` / `production_manufacture_operations` — выпуски (СборкаЗапасов) и операции
- `production_components` / `production_operations` — состав/операции заказов 1С
- `supplier_orders` / `supplier_order_items` — заказы поставщику 1С
- `sync_link` — связь локальных сущностей с документами 1С (Ref_Key)
