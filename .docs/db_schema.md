# База данных (кратко)

Этот файл — **короткая навигация**, а не попытка полностью описать схему.

Источник правды по таблицам и полям:

- SQLAlchemy модели: `backend/app/models.py`
- Миграции: `backend/alembic/versions/`

## Ключевые сущности

### Справочники

- `items` — номенклатура
- `units` — единицы измерения
- `production_stages` — этапы
- `production_resources` — участки/ресурсы
- `production_kinds` — виды производства (из 1С)
- `resource_production_kinds` — связь «вид → участок» (настраивается вручную)

### Спецификации

- `specifications`
- `spec_components`
- `spec_operations`
- `default_specifications`

### MRP (прогоны и результаты)

- `planning_config_versions` — версии конфигурации
- `planning_run` — прогон
- `planned_order` — производственные заказы
- `planned_order_stage` — этапы заказов (нормо-часы, участок)
- `planned_purchase` — закупки
- `capacity_load` — загрузка мощностей по участкам и датам
- `pegging_link` — трассировка потребностей

### Overwrite/Manual контуры

- `forced_order_request` / `forced_order_result` — принудительные заказы (для выпуска даже при дефиците)

## Важные допущения (актуально)

- Планирование ведётся **в дневном разрезе**. Исторические weekly/bucket_type в основных таблицах удалены.

### Настройки буфера и оптимальных партий

- `production_resources.buffer_days` — количество дней буфера для расчета базового количества запуска на участке
- `items.optimal_batch` — оптимальный размер партии для данного изделия
