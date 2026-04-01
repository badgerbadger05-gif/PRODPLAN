# База данных

Краткая навигация по схеме.

Источник правды:
- SQLAlchemy модели: `backend/app/models.py`
- Миграции: `backend/alembic/versions/`

## Ключевые сущности

### Справочники
- `items` — номенклатура
- `units` — единицы измерения
- `production_stages` — этапы
- `production_resources` — участки/ресурсы
- `production_kinds` — виды производства (из 1С)
- `resource_production_kinds` — связь «вид → участок»

### Спецификации
- `specifications`
- `spec_components`
- `spec_operations`
- `default_specifications`

### MRP
- `planning_config_versions` — версии конфигурации
- `planning_run` — прогон
- `planned_order` — производственные заказы
- `planned_order_stage` — этапы заказов
- `planned_purchase` — закупки
- `planned_rework` — заказы на переработку (отдельный поток MRP с диагностикой дефицита комплектующих)
- `capacity_load` — загрузка мощностей
- `pegging_link` — трассировка потребностей

### Принудительные заказы
- `forced_order_request` / `forced_order_result` — принудительные заказы

## Особенности
- Планирование ведётся **в дневном разрезе**
- `production_resources.buffer_days` — дни буфера для расчета запуска
- `items.optimal_batch` — оптимальный размер партии
