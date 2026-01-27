
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
 
2025-12-29 — чистка `.docs/` (удаление неактуальной документации, минимальная база знаний).

2026-01-16 — уточнено требование «учет активных заказов на производство при расчете потребностей»:
- обновлен документ [`\.docs/production_orders_check.md`](.docs/production_orders_check.md:1): добавлены разделы по терминам/статусам, области учета (A — как выпуск, B — как потребление компонентов), уточнён алгоритм, добавлены тест-кейсы.
- зафиксирована неоднозначность: поле `is_posted` в документе трактуется как «закрыт», требуется подтвердить соответствие термину «Закрыт» из 1С.
- подтверждено для вашей УНФ: «закрыт» = статус/состояние заказа **«Завершен»**; признак проведения (posted / `is_posted`) для данной фильтрации не используется.

2026-01-19 — подготовлен подробный план внедрения учёта активных заказов на производство:
- обновлён документ [`\.docs/production_orders_check.md`](.docs/production_orders_check.md:1): добавлен пошаговый план изменений (БД/синхронизация/планирование), отдельный блок с канонической логикой расчёта `remaining_qty`, уточнены места в коде (упор на [`build_planned_orders_and_purchases()`](backend/app/services/planning_service.py:1736) и подготовку `stock_by_item`).

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

2026-01-16 — разбор кейса «CP-000683-G попадает в заказ при отсутствии материалов» (пример, MRP run_id=181):
- В UI это **артикул**. В БД соответствует изделию `items.item_id=2699`, `item_code=НФ-00007350`, `item_article=CP-000683-G`.
- В прогоне `run_id=181` создан производственный заказ `planned_order.order_id=266527` на `qty=31` шт (need_date=2026-01-16).
- Для изделия есть спецификация по умолчанию `default_specifications.spec_id=1058`.
- BOM по этой спецификации содержит **единственный** компонент: `items.item_id=2700`, `item_code=НФ-00007428`, `item_article=CP-000683`, количество `1.0` на изделие.
- По кэшу остатков `items.stock_qty` компонента `2700` = `60`, поэтому лимит по комплектующим = `60` шт и ограничение по материалам **не блокирует** выпуск (предупреждений `COMPONENT_SHORTAGE_*` в `planning_run.warnings` для этого изделия нет).

Вывод: с точки зрения текущей логики MRP в [`build_planned_orders_and_purchases()`](backend/app/services/planning_service.py:1736) «материал» для CP-000683-G = компонент CP-000683 (есть на складе), поэтому заказ создаётся. Если в реальности должны учитываться другие материалы (лист/метизы и т.п.), то проблема в данных спецификации (неполный состав / неверная спецификация по умолчанию / компонент CP-000683 должен раскрываться до материалов, но сейчас не раскрывается).

2026-01-16 — уточнение по причине «остатки не обнуляются при синхронизации»:
- В текущей реализации синхронизации остатков [`sync_stock_from_odata()`](backend/app/services/odata_stock_sync.py:59) **не обязан** проставлять 0 для позиций, которых нет в ответе OData.
- Логика обновления такая:
  - если позиция нашлась в OData (по GUID `item_ref1c` или по нормализованному `item_code`) — пишем `new_qty`;
  - если позиция **не нашлась** в OData — то при `zero_missing=false` оставляем старое значение `stock_qty` (строка [`sync_stock_from_odata()`](backend/app/services/odata_stock_sync.py:215)); при `zero_missing=true` — обнуляем.
- В UI синхронизации остатков сейчас запрос всегда отправляется с `zero_missing: false` ([`syncStock()`](frontend/src/pages/SyncPage.vue:561)), поэтому если OData Balance не возвращает строки с нулевыми остатками (частый кейс), то «нули» просто отсутствуют в ответе и в БД **не перезапишутся** — останутся старые положительные остатки.

2026-01-27 — анализ проблемы с формированием дополнительных количеств деталей при заказе:
- Проведен анализ влияния буфера на расчет потребностей
- Создан документ [.docs/buffer_analysis.md](.docs/buffer_analysis.md) с описанием проблемы и рекомендациями
- Выявлено, что текущая реализация буфера может приводить к избыточному производству, особенно если сдвиг начала производства уже учитывает необходимость наличия запасов
- Определены возможные пути решения: временно отключить буфер, пересмотреть логику учета буфера или изменить интерпретацию буфера

  2026-01-27 — разработка рекомендаций по настройке буфера:
 - Создан документ [.docs/buffer_recommendations.md](.docs/buffer_recommendations.md) с практическими рекомендациями по настройке системы
 - Предложены варианты настройки: временное отключение буфера, уменьшение значений, использование оптимального размера партии
 - Рассмотрены рекомендации по использованию и проверке эффективности настроек

2026-01-27 — изменение семантики буфера: «буфер = только временной сдвиг, без добавки к количеству»:
- В [`OrderQuantityCalculator.compute()`](backend/app/services/order_quantity_calculator.py:50) отключено формирование `buffer_qty` как добавочного количества: буфер больше не увеличивает размер заказа.
- Буфер остаётся в системе как **временной сдвиг** (lead-time/start earlier) на этапе net-first BOM explosion через [`resolve_buffer_days()`](backend/app/services/planning_service.py:1580).
- Обновлены тесты, фиксирующие, что `buffer_days` не раздувает количества (см. [`tests/services/test_order_quantity_calculator.py`](tests/services/test_order_quantity_calculator.py:47)).

Следствие: для актуализации «везде 0» нужно запускать синхронизацию с `zero_missing=true` (либо доработать UI/бек, чтобы это было настраиваемо и явно включалось при необходимости).
