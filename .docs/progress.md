
# Текущее состояние проекта (коротко)

Этот файл должен быть **коротким** (ориентир: до ~200 строк). Исторические дневники сюда не возвращаем.

## Инварианты / решения

1) MRP работает **в дневном режиме**. Исторические weekly/bucket_type очищены.
2) Любые изменения моделей БД → миграция Alembic.
3) Контракты API стараемся не ломать.

## Что важно помнить (контекст для ИИ)

- Был кейс «ложная блокировка по комплектующим из-за неполного кэша остатков» → важно следить за полнотой кэшей stock.
- Есть контур «принудительных заказов» (manual override) + экспорт.
- В синхронизации спецификаций важен reconcile (удаление устаревших строк) и дедупликация `default_specifications`.

## Открытые вопросы

- Бизнес-правило учёта компонентов: учитывать только склад или также будущие плановые заказы на полуфабрикаты.

## Последняя сессия

2026-01-28 — ревизия `.docs/` (сжатие контекста, удаление повторений, устаревшего и лишнего).

2026-01-27 — изменение семантики буфера: «буфер = только временной сдвиг, без добавки к количеству»:
- В [`OrderQuantityCalculator.compute()`](backend/app/services/order_quantity_calculator.py:50) отключено формирование `buffer_qty` как добавочного количества: буфер больше не увеличивает размер заказа.
- Буфер остаётся в системе как **временной сдвиг** (lead-time/start earlier) на этапе net-first BOM explosion через [`resolve_buffer_days()`](backend/app/services/planning_service.py:1580).
- Обновлены тесты, фиксирующие, что `buffer_days` не раздувает количества (см. [`tests/services/test_order_quantity_calculator.py`](tests/services/test_order_quantity_calculator.py:47)).

2026-01-27 — анализ проблемы с формированием дополнительных количеств деталей при заказе:
- Проведен анализ влияния буфера на расчет потребностей
- Создан документ [.docs/buffer_analysis.md](.docs/buffer_analysis.md) с описанием проблемы и рекомендациями
- Выявлено, что текущая реализация буфера может приводить к избыточному производству, особенно если сдвиг начала производства уже учитывает необходимость наличия запасов
- Определены возможные пути решения: временно отключить буфер, пересмотреть логику учета буфера или изменить интерпретацию буфера

2026-01-27 — разработка рекомендаций по настройке буфера:
- Создан документ [.docs/buffer_recommendations.md](.docs/buffer_recommendations.md) с практическими рекомендациями по настройке системы
- Предложены варианты настройки: временное отключение буфера, уменьшение значений, использование оптимального размера партии
- Рассмотрены рекомендации по использованию и проверке эффективности настроек

2026-01-20 — уточнены границы интеграции и обновлена документация по учёту активных заказов 1С:
- обновлён документ [`\.docs/production_orders_check.md`](.docs/production_orders_check.md:1): явно зафиксировано, что **заказы PRODPLAN и заказы 1С — разные сущности**, связь между ними отсутствует и **мы не синхронизируем заказы между системами**; данные 1С используются только для **корректировки потребности** (A — как уже запланированный выпуск, B — как занятие компонентов) и расчёта `remaining_qty`.

2026-01-20 — перепроверка текущей реализации MRP vs требования «учёт активных заказов 1С» + план изменений:

### Наблюдения по текущему коду (расхождения с целевыми правилами)

1) **Фильтрация активных заказов 1С сейчас неверная/не реализована**
   - Синхронизация заказов 1С в [`sync_production_orders_from_odata()`](backend/app/services/production_order_sync.py:31) тянет только `Posted` → пишет в `ProductionOrder.is_posted`.
   - По постановке: «закрыт» = `СостояниеЗаказа == "Завершен"`, и `Posted` **не используется** ([`.docs/production_orders_check.md`](.docs/production_orders_check.md:19)).
   - В модели [`ProductionOrder`](backend/app/models.py:145) отсутствуют `order_state_key` / `deletion_mark`, поэтому фильтра `DeletionMark == false` и `СостояниеЗаказа_Key != DONE_STATE_KEY` в БД сделать невозможно.

2) **Невозможно корректно посчитать `remaining_qty` по строкам заказа 1С**
   - В [`ProductionProduct`](backend/app/models.py:157) нет `line_number` и `characteristic_ref1c`.
   - В синке продукции ключ обновления сейчас `(order_id, item_id)` ([`sync_production_orders_from_odata()`](backend/app/services/production_order_sync.py:182)) ⇒ если в 1С несколько строк одного изделия (или разрез по характеристикам), они будут схлопнуты, и `ordered_qty` станет некорректным.
   - Факта выпуска (`produced_qty`) нет: нигде не используется `Document_СборкаЗапасов` / `Document_СборкаЗапасов_Продукция` (контекст в [`.docs/production_orders_analysis.md`](.docs/production_orders_analysis.md:25)).

3) **Логика A/B в MRP сейчас отсутствует**
   - A (уменьшение потребности на выпуск) не применяется: [`build_planned_orders_and_purchases()`](backend/app/services/planning_service.py:1736) использует `requested_qty` напрямую.
   - B (занятие компонентов активными заказами) не применяется: `OrderQuantityCalculator` ограничивает по `stock_by_item + wip_by_item` ([`OrderQuantityCalculator._limit_by_components()`](backend/app/services/order_quantity_calculator.py:273)), но `stock_by_item` не корректируется на резерв под активные заказы 1С.
   - `wip_by_item` сейчас задан как `defaultdict(float)` без наполнения ([`run_planning_run()`](backend/app/services/planning_service.py:2162)), т.е. «открытые заказы 1С» не участвуют ни как WIP, ни как резерв.

### Куда и как вносить изменения (целевой дизайн, без прямого доступа калькулятора к БД)

0) Инвариант: калькулятор остаётся stateless относительно БД, поэтому вся интеграция делается на этапе подготовки кэшей в [`run_planning_run()`](backend/app/services/planning_service.py:2162) и/или перед вызовом [`build_planned_orders_and_purchases()`](backend/app/services/planning_service.py:1736).

1) Миграции/модели (кэш 1С для расчёта)
   - `production_orders`:
     - добавить `order_state_key` (строка GUID из `СостояниеЗаказа_Key`),
     - добавить `order_state_name` (опционально; из `$expand=СостояниеЗаказа`),
     - добавить `deletion_mark` (bool из `DeletionMark`).
   - `production_products`:
     - добавить `line_number` (int из `LineNumber`),
     - добавить `characteristic_ref1c` (GUID из `Характеристика_Key`, nullable),
     - добавить `produced_qty` и `remaining_qty` (DECIMAL),
     - добавить уникальность `(order_id, line_number)`.

2) Синхронизация заказов 1С (заголовок + строки продукции)
   - В OData-запрос к `Document_ЗаказНаПроизводство` добавить: `СостояниеЗаказа_Key`, `DeletionMark`, опционально `$expand=СостояниеЗаказа`.
   - В синке продукции `Document_ЗаказНаПроизводство_Продукция` писать `line_number` и `characteristic_ref1c`; ключ upsert сделать по `(order_id, line_number)`.

3) Сервис факта выпуска и расчёт `remaining_qty`
   - Источник факта: `Document_СборкаЗапасов` связанные через `ЗаказНаПроизводство_Key` + табличная часть `Document_СборкаЗапасов_Продукция` (см. [`.docs/production_orders_odata_queries.md`](.docs/production_orders_odata_queries.md:65)).
   - Бизнес-правило для MVP: учитывать только `Posted == true` и `DeletionMark == false` у сборок.
   - Агрегация `produced_qty` предпочтительно по `(order_ref1c, line_number)` с проверкой `item_ref1c/characteristic_ref1c` (см. [`.docs/production_orders_check.md`](.docs/production_orders_check.md:204)).
   - `remaining_qty = max(ordered_qty - produced_qty, 0)` (см. [`.docs/production_orders_check.md`](.docs/production_orders_check.md:200)).

4) Интеграция A (уменьшение потребности на выпуск изделия)
   - Внутри [`build_planned_orders_and_purchases()`](backend/app/services/planning_service.py:1736) перед вызовом [`OrderQuantityCalculator.compute()`](backend/app/services/order_quantity_calculator.py:50):
     - получить `active_remaining_qty(item_id)` как сумму `production_products.remaining_qty` только по активным заказам 1С,
     - пересчитать `requested_qty_adj = max(requested_qty - active_remaining_qty, 0)`,
     - дальше работать от `requested_qty_adj`.

5) Интеграция B (резерв компонентов активными заказами 1С)
   - В [`run_planning_run()`](backend/app/services/planning_service.py:2162) сразу после построения `stock_by_item` сформировать `effective_stock_by_item`:
     - взять активные строки 1С с `remaining_qty > 0`,
     - по спецификации изделия развернуть потребность в компонентах и накопить `reserved_by_component[child_id]`,
     - `effective_stock_by_item = max(stock_by_item - reserved_by_component, 0)`,
     - передавать в [`OrderQuantityCalculator`](backend/app/services/order_quantity_calculator.py:8) уже `effective_stock_by_item`.

### MVP (минимальный инкремент, который можно принять)

MVP-1 (обязательный):
- корректно кэшируем состояние/удаление заказов 1С (`order_state_key`, `deletion_mark`),
- нормализуем строки продукции по `line_number`,
- считаем `produced_qty/remaining_qty` по сборкам (с фильтром `Posted==true`, `DeletionMark==false`),
- внедряем A в [`build_planned_orders_and_purchases()`](backend/app/services/planning_service.py:1736).

MVP-2:
- внедряем B через `effective_stock_by_item` перед созданием [`OrderQuantityCalculator`](backend/app/services/order_quantity_calculator.py:8).

### Набор тест-кейсов/проверок (добавить в автотесты)

1) Активный заказ 1С уменьшает потребность на выпуск (A):
   - `requested_qty=10`, `active_remaining_qty=7` ⇒ планируем не больше 3.
2) Завершённый (`order_state_key == DONE_STATE_KEY`) не влияет.
3) `DeletionMark==true` не влияет.
4) Частичное выполнение учитывается:
   - `ordered_qty=10`, `produced_qty=4` ⇒ `remaining_qty=6`.
5) Дубли строк (одинаковый `item`, разные `line_number`) не схлопываются; суммарный `active_remaining_qty` корректный.
6) B: активные заказы 1С занимают компоненты и уменьшают доступный `stock_by_item` так, что лимит по комплектующим становится меньше (частичный план или блокировка).

2026-01-16 — уточнение по причине «остатки не обнуляются при синхронизации»:
- В текущей реализации синхронизации остатков [`sync_stock_from_odata()`](backend/app/services/odata_stock_sync.py:59) **не обязан** проставлять 0 для позиций, которых нет в ответе OData.
- Логика обновления такая:
  - если позиция нашлась в OData (по GUID `item_ref1c` или по нормализованному `item_code`) — пишем `new_qty`;
  - если позиция **не нашлась** в OData — то при `zero_missing=false` оставляем старое значение `stock_qty` (строка [`sync_stock_from_odata()`](backend/app/services/odata_stock_sync.py:215)); при `zero_missing=true` — обнуляем.
- В UI синхронизации остатков сейчас запрос всегда отправляется с `zero_missing: false` ([`syncStock()`](frontend/src/pages/SyncPage.vue:561)), поэтому если OData Balance не возвращает строки с нулевыми остатками (частый кейс), то «нули» просто отсутствуют в ответе и в БД **не перезапишутся** — останутся старые положительные остатки.
# 2026-02-06 — Недельный отчёт о выпуске техники: закрытие дня + перенос остатков

Реализовано по ТЗ из [`weekly_production_report_code_change_plan.md`](.docs/weekly_production_report_code_change_plan.md:1) и решениям из [`weekly_production_report_decisions.md`](.docs/weekly_production_report_decisions.md:1).

## Backend

- Добавлены таблицы (миграция Alembic) для механизма закрытия дня и переносов:
  - `work_calendar_day`
  - `production_day_close`
  - `production_day_close_item`
  См. [`backend/alembic/versions/20260206_01_add_production_day_close_tables.py`](backend/alembic/versions/20260206_01_add_production_day_close_tables.py:1).

- Добавлены модели:
  - [`WorkCalendarDay`](backend/app/models.py:318)
  - [`ProductionDayClose`](backend/app/models.py:327)
  - [`ProductionDayCloseItem`](backend/app/models.py:344)

- Сервис глобального календаря рабочих дней:
  - [`is_workday()`](backend/app/services/work_calendar_service.py:11)
  - [`previous_workday()`](backend/app/services/work_calendar_service.py:32)
  - [`next_workday()`](backend/app/services/work_calendar_service.py:45)

- Сервис недельного отчёта и закрытия дня:
  - [`get_week_report()`](backend/app/services/production_report_service.py:44)
  - [`bulk_upsert_fact()`](backend/app/services/production_report_service.py:149) — запрещает запись факта в закрытые дни (read-only)
  - [`close_previous_workday()`](backend/app/services/production_report_service.py:215) — поддерживает re-run с откатом прошлого переноса, переносит остаток на `next_workday(next_workday(today))`, пропуски рабочих дней запрещены

- API (добавлено в [`plan.py`](backend/app/routers/plan.py:1)):
  - `POST /api/v1/plan/production_report/week`
  - `POST /api/v1/plan/production_report/fact/bulk_upsert`
  - `POST /api/v1/plan/production_report/day/close`

## Tests

Добавлены тесты закрытия дня, переносов, re-run и read-only факта:
[`tests/services/test_production_report_day_close.py`](tests/services/test_production_report_day_close.py:1)

## Frontend

- Добавлена страница «Отчёт о выпуске техники недельный»:
  [`frontend/src/pages/ProductionReportWeekPage.vue`](frontend/src/pages/ProductionReportWeekPage.vue:1)
  - 7 колонок (Пн–Вс), ввод факта, подсветка «перевыпуск», итоги недели
  - кнопки «Сохранить факт» (bulk upsert) и «Закрыть день»
  - закрытые дни становятся read-only (по `close_status` из API)

## Fixes

- Исправлено: в недельном отчёте факт за «закрываемый день» (из `close_hint.close_date`) должен оставаться редактируемым даже если у даты уже `close_status='CLOSED'`.
  Это нужно для сценария **re-run**: пользователь корректирует факт и нажимает «Закрыть день» повторно, а бэкенд пересчитывает перенос.
  Реализация: UI делает исключение в [`isDateClosed()`](frontend/src/pages/ProductionReportWeekPage.vue:206) и разрешает ввод для `close_hint.close_date`.

- Исправлено UX: при открытии отчёта в понедельник `close_hint.close_date` часто попадает в предыдущую неделю (прошлая пятница). Теперь UI автоматически переключает отображаемую неделю на неделю `close_hint.close_date`, если закрываемая дата не входит в текущий набор `days`.
  Реализация: логика в [`load()`](frontend/src/pages/ProductionReportWeekPage.vue:233) переякоривает `anyDate` на `close_hint.close_date` и перезагружает данные.

- Убрано авто-смещение окна в «План выпуска техники квартальный»: ранее страница якорила матрицу на `today` и скрывала прошедшие недели/дни (визуально создавая эффект «план сдвинулся на сегодня» без изменения данных в БД). Теперь квартальный план грузится за фиксированный диапазон квартала и показывает все недели в хронологическом порядке.
  Реализация: [`loadPlanData()`](frontend/src/pages/PlanQuarterlyPage.vue:554) использует `getQuarterBounds()` как `start_date/days`, а [`getVisibleWeekDays()`](frontend/src/pages/PlanQuarterlyPage.vue:368) всегда возвращает все 7 дней.

- Добавлен роут:
  [`frontend/src/router/index.ts`](frontend/src/router/index.ts:1)
  `'/plan/production-report/week'`

- Добавлен пункт меню в [`MainLayout`](frontend/src/layouts/MainLayout.vue:1).

- Добавлены API-обёртки на фронте:
  [`frontend/src/services/api.ts`](frontend/src/services/api.ts:340)

Примечание: в проекте отсутствует ESLint-конфиг (npm script `lint` падает по этой причине), поэтому автопроверка фронта ограничена.
