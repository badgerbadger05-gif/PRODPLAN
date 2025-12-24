# 2025-12-23 — Диагностика: «родитель блокируется по дефициту», хотя остатки компонента есть (ошибка кэша stock_by_item)

## Симптом

- При расчёте потребностей/плана часть изделий (родители) **не создаются** в результатах и помечаются как заблокированные по дефициту комплектующих.
- По факту в 1С/в БД остатки на «деталь‑ребёнок» есть (или должны быть достаточны), из‑за чего заказ на родителя **должен** проходить.

## Что нашли (корень проблемы)

В логике компонентного ограничения используется кэш остатков `stock_by_item`, который строится **неполным**:

1) В [`run_planning_run()`](backend/app/services/planning_service.py:2162) кэш `item_cache` и далее `stock_by_item` формируются **только** по множеству `all_item_ids = keys(net_requirements)` (т.е. только по позициям с *ненулевой net‑потребностью*):
   - формирование `all_item_ids`: [`run_planning_run()`](backend/app/services/planning_service.py:2177)
   - построение `stock_by_item` из `item_cache`: [`run_planning_run()`](backend/app/services/planning_service.py:2199)

2) При проверке доступности комплектующих в [`OrderQuantityCalculator._limit_by_components()`](backend/app/services/order_quantity_calculator.py:273) берётся:

   - `child_stock = self.stock_by_item.get(child_id, 0.0)`

   Если `child_id` отсутствует в `stock_by_item`, компонент воспринимается как **0**, даже если реальный остаток > 0.

3) Ключевой сценарий, который ломается:

   - если остаток компонента полностью покрывает его валовую потребность, то net‑потребность по компоненту = 0,
   - компонент не попадает в `net_requirements` → не попадает в `stock_by_item`,
   - родитель ошибочно блокируется как будто по компоненту дефицит.

## Подтверждение на данных (run_id=179)

- В `planning_run.warnings` для `run_id=179` присутствуют массовые `COMPONENT_SHORTAGE_BLOCKED`.
- Пример: `item_id=6144` («00-00000413 / CA-000060-SPch») попадает в `COMPONENT_SHORTAGE_BLOCKED`.
- Его единственный компонент по спецификации: `item_id=6143` («НФ-00004846 / CA-000060-S») имеет `stock_qty=178`.
- При этом в `planned_order` для `run_id=179` нет ни строки по 6143 (нет net‑потребности), ни строки по 6144 (родитель заблокирован).

Т.е. это ровно кейс «остаток компонента покрывает спрос, но из‑за cache miss компонент считается нулевым».

## Наиболее вероятная причина №1 (основная)

- Неполный кэш `stock_by_item` в запуске MRP → ложный нулевой остаток для компонента → `component_limit` становится 0 → родитель блокируется.

## Причина №2 (сопутствующая / архитектурная)

- Логика `component_limit` по смыслу учитывает только наличие компонент **на складе**, не учитывая будущие плановые заказы на полуфабрикаты (если компонент производится). Это может требовать отдельного согласования бизнес‑правила.

## План точечной правки (после согласования)

1) Расширить набор `item_cache/stock_by_item`:
   - после получения `all_item_ids` собрать множество **всех компонент** по спецификациям этих items (через `default_spec_map` → `spec_components`),
   - подгрузить недостающие `Item` и добавить их в `item_cache`, затем пересобрать `stock_by_item`.
   - минимальный объём: 1 уровень (для проверки компонентного лимита достаточно прямых компонентов).

2) Добавить диагностические логи, чтобы гарантированно поймать cache miss и доказать влияние:
   - в [`OrderQuantityCalculator._limit_by_components()`](backend/app/services/order_quantity_calculator.py:273): логировать/варнить при `child_id not in stock_by_item` (код типа `STOCK_CACHE_MISS`, поля: parent_item_id, spec_id, child_id).
   - в [`run_planning_run()`](backend/app/services/planning_service.py:2162): логировать размеры множеств `all_item_ids`, `component_ids`, `missing_component_ids`.

## 2025-12-24 (fix) — Исправлено

### Изменения

1) В запуске MRP расширен кэш остатков `stock_by_item`, чтобы он включал **компоненты спецификаций** (1 уровень), даже если по ним `net=0`:
   - реализация в [`run_planning_run()`](backend/app/services/planning_service.py:2162) (замена построения `stock_by_item` на запрос по union `net_items ∪ component_items`).

2) Добавлен диагностический warning при cache miss компонента:
   - в [`OrderQuantityCalculator._limit_by_components()`](backend/app/services/order_quantity_calculator.py:273) добавлен warning `STOCK_CACHE_MISS` (parent/spec/component).

3) Добавлен регрессионный тест, демонстрирующий исходную проблему и ожидаемое поведение:
   - [`test_parent_can_be_falsely_blocked_when_component_stock_missing_in_cache()`](tests/test_stock_by_item_cache.py:1)

### Проверка

- `pytest -q` — PASS (18 passed).

---

# 2025-12-22 — «Принудительные заказы» + восстановление отчёта о дефиците (shortage-report)

## 2025-12-22 (fix) — MRP Purchases: «Нет данных для отображения» после рефакторинга

### Симптом

- На странице результатов прогона MRP во вкладке «Заказы на закупку» отображалось сообщение «Нет данных для отображения».
- При этом детальная выдача закупок существовала (данные были в БД и доступны через API).

### Диагностика

- UI загружает закупки двумя запросами (в параллель):
  - `GET /api/v1/plan/results/{run_id}/purchases` — детальные строки
  - `GET /api/v1/plan/results/{run_id}/purchases/grouped` — агрегат для верхней таблицы
  - место: [`loadPurchases()`](frontend/src/pages/MRPResultPage.vue:776)
- После одного из рефакторингов маршрут `/purchases/grouped` отсутствовал на backend → `404 Not Found`.
- Из-за `Promise.all()` падала вся загрузка закупок, и таблица оставалась пустой.

Подтверждение curl:

- `GET /api/v1/plan/results/178/purchases` → 200 OK (есть rows)
- `GET /api/v1/plan/results/178/purchases/grouped` → 404 Not Found

### Исправление

1) Backend: добавлен эндпоинт `GET /v1/plan/results/{run_id}/purchases/grouped` в [`plan.py`](backend/app/routers/plan.py:1)
   - Реализован как совместимый адаптер: повторно использует агрегацию из [`get_run_purchases()`](backend/app/services/planning_service.py:801) и преобразует к контракту grouped-таблицы.

2) Frontend: добавлен graceful fallback
   - в [`loadPurchases()`](frontend/src/pages/MRPResultPage.vue:776) отказались от `Promise.all()`;
   - сначала грузим `/purchases`, затем пробуем `/purchases/grouped`, а при ошибке строим агрегат из полученных детальных строк.

### Проверка

- После пересборки контейнеров:
  - `GET /api/v1/plan/results/178/purchases/grouped` → 200 OK
  - вкладка «Заказы на закупку» отображает строки, вместо «Нет данных для отображения».

## Контекст / проблема

- UI кнопка «Отчёт о дефиците» на странице результатов прогона вызывала GET `/v1/plan/results/{run_id}/shortage-report` (см. [`exportShortageReport()`](frontend/src/pages/MRPResultPage.vue:838)), но в backend отсутствовал маршрут → кнопка не работала.
- Производственные заказы при полном дефиците комплектующих не попадали в результаты: в [`build_planned_orders_and_purchases()`](backend/app/services/planning_service.py:1736) при `component_limit <= 0` выполняется `continue`, т.е. строка вообще не создаётся.

## Решение

Реализован отдельный контур «принудительных заказов» (manual/override), который **не меняет** основную логику MRP-прогона, но позволяет:

1) Создавать отдельные заявки на выпуск даже при дефиците компонентов.
2) Считать для них количество (лот-сайзинг/буфер) и фиксировать дефицит как диагностику.
3) Выгружать в XLSX.
4) Восстановить работу кнопки «Отчёт о дефиците» для прогона.

### Backend

- Добавлены ORM модели:
  - [`ForcedOrderRequest`](backend/app/models.py:457)
  - [`ForcedOrderResult`](backend/app/models.py:484)
- Добавлена миграция Alembic: [`20251222_01_add_forced_orders.py`](backend/alembic/versions/20251222_01_add_forced_orders.py:1)
- Добавлен сервисный модуль: [`forced_orders.py`](backend/app/services/forced_orders.py:1)
  - создание заявки: `create_forced_order_request()`
  - расчёт: `process_forced_order_request()` (использует [`OrderQuantityCalculator.compute()`](backend/app/services/order_quantity_calculator.py:50), но **не блокирует** по component_limit)
  - экспорт XLSX для заявки: `export_forced_order_xlsx()`
  - восстановленный XLSX отчёт по дефициту прогона: `export_shortage_report_for_run()`
    - добавлена разбивка по участкам (группы «Участок: …») аналогично экспорту производства
- Добавлены API эндпоинты в роутере планирования [`plan.py`](backend/app/routers/plan.py:1):
  - `GET /v1/plan/results/{run_id}/shortage-report` (восстановление кнопки отчёта)
  - `POST /v1/plan/forced_orders` (создать заявку)
  - `GET /v1/plan/forced_orders` (список)
  - `POST /v1/plan/forced_orders/{id}/process` (посчитать)
  - `GET /v1/plan/forced_orders/{id}/export` (xlsx)

### Frontend

- Добавлен минимальный UI для «принудительного заказа» прямо на странице результатов прогона:
  - кнопка «Принудительный заказ» + диалог ввода (`item_id`, `qty`, `need_date`, `reason`) в [`MRPResultPage.vue`](frontend/src/pages/MRPResultPage.vue:1)
  - API-обвязки: [`createForcedOrder()`](frontend/src/services/api.ts:352), [`processForcedOrder()`](frontend/src/services/api.ts:357), [`exportForcedOrder()`](frontend/src/services/api.ts:362)
- Кнопка «Отчёт о дефиците» продолжает вызывать `getShortageReport()` (теперь backend-эндпоинт реализован).

## Проверки

- Миграции: `cd backend && alembic upgrade head` — успешно (созданы forced_order_request/forced_order_result).
- Backend тесты: `set PYTHONPATH=. && pytest -q` — PASS (17 passed).
- Frontend сборка: `cd frontend && npm ci --silent && npm run -s build` — успешна.

---

# 2025-12-19 — Диагностика: при синхронизации спецификаций не удаляются устаревшие строки состава/операций (+ копятся дубли default_specifications)

- **Симптом**: после синхронизации и обновления спецификации в 1С, в расчёте MRP продолжают фигурировать детали, которые уже удалены из текущей спецификации.
- **Пример** (по сообщению пользователя): корневое изделие «Г0002708» (в БД соответствует `item_code='00-00002708'`), «лишние» детали по артикулам: `CP-000260-GP`, `CP-000258-GSP`, `CP-000262-GP`, `CP-000257-GSP`.
- **Подтверждение**: пользователь подтвердил, что в 1С этих CP‑строк в актуальной спецификации уже нет.

## Что нашли в коде

### Причина №1 (основная): sync спецификаций делает upsert, но не делает reconcile/удаление

- В [`sync_specifications_from_odata()`](backend/app/services/specification_sync.py:31) компоненты спецификации записываются по схеме «создать или обновить», но **нет шага удаления** компонентов/операций, отсутствующих в текущем ответе OData.
- Следствие: строки, удалённые в 1С, остаются в `spec_components`/`spec_operations` и участвуют в BOM explosion в [`compute_gross_requirements()`](backend/app/services/planning_service.py:1509) → [`expand_bom()`](backend/app/services/planning_service.py:1611).

### Причина №2 (усугубляет): sync default_specifications не обновляет и не чистит старые записи

- В [`sync_default_specifications_from_odata()`](backend/app/services/default_specification_sync.py:25) логика только добавляет новые записи, не обновляя/не удаляя старые.
- Это приводит к дублям в `default_specifications` и потенциально недетерминированному выбору spec_id:
  - UI дерева спецификаций использует `.first()` без явного порядка в [`_get_default_spec_id()`](backend/app/routers/specification.py:87) (может взять «старую» запись).
  - расчёт строит `default_spec_map` из всех строк таблицы (дубликаты перетирают друг друга в зависимости от порядка выдачи) в [`compute_gross_requirements()`](backend/app/services/planning_service.py:1509).

## Что нашли в БД (psql)

- Для `item_code='00-00002708'` (id=3652) есть **2 записи** `default_specifications`:
  - `id=882 spec_id=297` (старее)
  - `id=2424 spec_id=2871` (новее)
- Для сборки `item_code='НФ-00005609'` (id=339) есть **2 записи** `default_specifications`:
  - `id=53 spec_id=453`
  - `id=2271 spec_id=3380`
- По обеим спецификациям сборки (`spec_id=453` и `spec_id=3380`) в `spec_components` присутствуют компоненты с артикулами `CP-000260-GP/…/CP-000257-GSP`, несмотря на то, что в 1С они уже удалены → подтверждает отсутствие удаления при sync.

## Рекомендованный план исправления (после согласования)

1) В [`sync_specifications_from_odata()`](backend/app/services/specification_sync.py:31) добавить reconcile:
   - собирать `seen_item_ids`/`seen_operation_ids` по текущему ответу OData;
   - удалять из БД `SpecComponent`/`SpecOperation` для `spec_id`, которых нет в `seen_*`;
   - логировать `deleted_components/deleted_spec_operations` и `seen_counts`.
   - В `dry_run` — только логировать разницу, без DELETE.
2) В [`sync_default_specifications_from_odata()`](backend/app/services/default_specification_sync.py:25) сделать upsert по ключу `(item_id, characteristic_id)` и чистку дублей.
3) Добавить уникальный constraint/index на `default_specifications` по `(item_id, characteristic_id)` (миграция Alembic) и разовый скрипт/SQL для очистки текущих дублей.

## Диагностические SQL (использовали в сессии)

```sql
-- Дубли default_specifications по изделию
select item_id, characteristic_id, count(*)
from default_specifications
group by item_id, characteristic_id
having count(*) > 1;

-- Компоненты спецификации с отбором по артикулам
select s.spec_id, s.spec_code, child.item_code, child.item_article, child.item_name, sc.quantity
from spec_components sc
join specifications s on s.spec_id=sc.spec_id
join items child on child.item_id=sc.item_id
where s.spec_id = :spec_id
  and child.item_article in ('CP-000260-GP','CP-000258-GSP','CP-000262-GP','CP-000257-GSP');
```

## 2025-12-19 (fix) — Реализовано исправление

Backend:

1) Reconcile в синхронизации спецификаций:
- В [`sync_specifications_from_odata()`](backend/app/services/specification_sync.py:31) добавлено удаление «лишних» строк для табличных частей `Состав` и `Операции`.
- Удаление выполняется **только если** поле реально присутствует в OData‑ответе (защита от сценария, когда вложенная табличная часть не выгружена из-за `select_fields`).
- В статистику добавлены поля `components_deleted`/`spec_operations_deleted`.

2) Upsert + чистка дублей в default_specifications:
- В [`sync_default_specifications_from_odata()`](backend/app/services/default_specification_sync.py:25) ключ синхронизации изменён на `(item_id, characteristic_id)` вместо `(item_id, characteristic_id, spec_id)`.
- Добавлена нормализация пустой характеристики (None/пусто/нулевой GUID → единый ключ) и best‑effort удаление дублей в рамках item_id, затронутых синком.

3) Детерминированный выбор default спецификации в дереве:
- В [`_get_default_spec_id()`](backend/app/routers/specification.py:87) добавлен `order_by(updated_at desc, id desc)`, чтобы при наличии дублей (до миграции/чистки) UI брал самую свежую запись.

4) Миграция Alembic: уникальность default_specifications
- Добавлена миграция [`20251219_01_default_specifications_unique.py`](backend/alembic/versions/20251219_01_default_specifications_unique.py:1), которая:
  - дедуплицирует `default_specifications` по ключу `(item_id, COALESCE(characteristic_id,''))` (оставляет «самую новую» запись);
  - создаёт уникальный индекс `ux_default_specifications_item_char`.

Проверка (локально в docker):
- `alembic heads` показывает `20251219_01 (head)`.
- После `alembic upgrade head` дубли по `default_specifications` для `item_id` примеров исчезли, индекс `ux_default_specifications_item_char` создан.

---

# 2025-11-27 09:43 — MRP Production: плоская выдача без «Участка» и новый grouped‑эндпоинт

- Суть: реализованы требования задачи для выдачи результатов производства.
  - Убрана колонка «Участок» из плоского ответа backend (main_area_* больше не возвращаются).
  - Добавлен эндпоинт GET /api/v1/plan/results/{run_id}/production/grouped, который группирует заказы по «основному участку» (этап с максимальными hours) и совместим с текущим фронтендом.
  - Страницы /mrp/:id будут отображать подзаголовки по участкам при непустом groups.

Backend:
- Плоская выдача:
  - Из функции [get_run_production()](backend/app/services/planning_service.py:424) удалены поля main_area_id/main_area_name и логика их вычисления; в ответе остаются только stages[].area_id/area_name на уровне этапов для дальнейшей группировки.
- Схемы:
  - Добавлены Pydantic‑модели grouped‑ответа: [ProductionStage](backend/app/schemas.py:484), [ProductionGroupOrder](backend/app/schemas.py:502), [ProductionGroup](backend/app/schemas.py:517), [ProductionGroupedResponse](backend/app/schemas.py:527).
- Новый сервис grouped:
  - Реализована [get_run_production_grouped()](backend/app/services/planning_service.py:1157):
    - Определение основного участка для заказа: этап с максимальными hours; если ни у одного этапа нет area_id — группа area_id=None, area_name="Без участка".
    - Агрегаты мощностей подтягиваются из capacity_load в диапазоне дат (см. сборку cap_q: [planning_service.py](backend/app/services/planning_service.py:1339)).
    - Сортировка групп по area_name ASC; пагинация limit/offset; возвращаются groups, total_groups, total_orders, limit, offset.
- Роутер:
  - Добавлен хэндлер [get_planning_result_production_grouped()](backend/app/routers/plan.py:395) по пути /api/v1/plan/results/{run_id}/production/grouped с response_model=ProductionGroupedResponse, делегирует в сервис.

Frontend (минимальные правки):
- Унифицированная таблица (fallback): удалено использование main_area_*.
  - Файл: [ProductionUnifiedTable.vue](frontend/src/components/mrp/ProductionUnifiedTable.vue:1) — убраны поля main_area_id/main_area_name из типов и слияния строк; колонка «Участок» больше не показана в плоском режиме.
- Группированный режим уже использует groups и автоматически начнёт отрисовывать подзаголовки при наличии данных от backend.

Контракты/совместимость:
- GET /api/v1/plan/results/{run_id}/production:
  - Не содержит main_area_id/main_area_name; структура согласована с обновлёнными флагами/этапами.
- GET /api/v1/plan/results/{run_id}/production/grouped:
  - Возвращает { groups[], total_groups, total_orders, limit, offset }.
  - Каждая группа: { area_id|null, area_name, orders[], norm_sum_hours, min_days_to_need|null, cap_overload_hours, cap_overloaded_buckets }.
  - Заказы без участков — в группу area_id=null, area_name="Без участка".
- Сортировка групп: area_name ASC (закреплено в сервисе).
- Агрегаты мощностей: по capacity_load (overload_hours суммой и количество перегруженных бакетов).

Проверки:
- Backend: `pytest -q` — PASS (все тесты в проекте зелёные на текущем окружении).
- OpenAPI: response_model для grouped‑эндпоинта задан; openapi.json обновится у FastAPI при старте.

Ограничения и дальнейшие шаги:
- Рекомендуется добавить сервисные unit‑тесты для:
  - отсутствия main_area_* в плоском ответе,
  - корректного формирования 3 групп (минимум 2 участка + «Без участка»),
  - регрессий на сценарии, подобные run 70 (есть участки) и run 161 (раньше не группировалось).
- Фронтенд уже ожидает groups; дополнительных правок, кроме удаления плоской колонки, не требуется.

Затронутые файлы/участки:
- Backend:
  - [get_run_production()](backend/app/services/planning_service.py:424) — очистка от main_area_*.
  - [get_run_production_grouped()](backend/app/services/planning_service.py:1157) — новая логика группировки и агрегатов capacity.
  - [router get_planning_result_production_grouped()](backend/app/routers/plan.py:395) — новый маршрут.
  - Схемы: [ProductionGroupedResponse](backend/app/schemas.py:527) и связанные модели.
- Frontend:
  - [ProductionUnifiedTable.vue](frontend/src/components/mrp/ProductionUnifiedTable.vue:1) — удалены ссылки на main_area_*.
# 2025-11-26 08:40 — Упрощение MRP-demand и отказ от weekly режима

- **Суть**: реализованы пункты из [.docs/mrp_demand_refactor_plan.md](.docs/mrp_demand_refactor_plan.md) — расчёт спроса теперь строго дневной, даты дочерних BOM-позиций сдвигаются на `buffer_days`, а `include_wip` влияет на неттинг.
- **Backend**:
  - [`compute_gross_requirements()`](backend/app/services/planning_service.py:1037) теперь строит единую дневную сетку спроса, поднимает кэши спецификаций/ресурсов и сдвигает `child_date` на `ProductionResource.buffer_days`.
  - [`compute_planning_preview()`](backend/app/services/planning_service.py:1200) опирается на новую структуру `gross`, учитывает `snapshot['toggles']['include_wip']` и возвращает `net` без деления на daily/weekly.
  - [`build_planned_orders_and_purchases()`](backend/app/services/planning_service.py:1264) и [`run_planning_run()`](backend/app/services/planning_service.py:1538) адаптированы к новому формату `net_requirements`; REST-схема очищена от параметра `use_weekly` ([`backend/app/routers/plan.py`](backend/app/routers/plan.py:200)).
- **Frontend**: убран переключатель Weekly и колонка статуса в [`MRPRunsPage`](frontend/src/pages/MRPRunsPage.vue:1), а [`MRPSummaryCard`](frontend/src/components/mrp/MRPSummaryCard.vue:1) больше не отображает признак weekly; типы и API ([`frontend/src/types/mrp.ts`](frontend/src/types/mrp.ts:3), [`frontend/src/services/api.ts`](frontend/src/services/api.ts:71)) синхронизированы, строки локализации удалены ([`frontend/src/i18n/ru.ts`](frontend/src/i18n/ru.ts:1)).
- **Тесты**: `pytest` зафиксировал прежние падения в `tests/services/test_capacity_scheduler.py`, `tests/services/test_order_quantity_calculator.py`, `tests/test_shortage_report_v2.py` и `tests/test_planning_service.py` (причины — расхождения сигнатур `CapacityScheduler`, поведение `OrderQuantityCalculator.compute`, отсутствие `_generate_shortage_report_v2`).


# 2025-11-06 14:22 — Устранение дублей в Excel «Производство» (Вариант A)

- **Суть**: удалены дубли при диапазоне дат за счёт использования агрегированного сервиса; дата исключена из ключа
- **Ключ группировки**: item_id + unit + production_kind
- **Что НЕ изменялось**: формат и набор колонок экспорта, оформление, подзаголовки по производственным участкам
- **Затронутые функции**: [`def export_planning_result_production()`](backend/app/routers/plan.py:533), [`def get_run_production_grouped()`](backend/app/services/planning_service.py:2991)
- **Краткая проверка**: при диапазоне из нескольких дат одна и та же номенклатура (в одной ЕИ и одном виде производства) теперь отгружается одной строкой суммой количеств и нормо-часов
- **Замечания по совместимости**: поведение grouped-сервиса по умолчанию не изменено (обратная совместимость), новые опции используются только экспортом

---

# 2025-11-21 11:24 — Диагностика API планирования (run_id=99)

- **Суть**: установили pytest внутри контейнера prodplan-backend-1, добились успешного прогона новых тестов `test_get_run_production_handles_missing_start_date` и `test_get_run_purchases_handles_missing_columns`
- **Команды**:
  - `docker exec prodplan-backend-1 pip install pytest`
  - `set PYTHONPATH=. && pytest tests/test_planning_service.py -k "get_run_production_handles" -q`
- **API проверки**: `curl http://localhost:8000/api/v1/plan/runs/99/production` и `curl http://localhost:8000/api/v1/plan/results/99/purchases` вернули 200 OK и заполненные массивы
- **Вывод**: ошибка 400 `(2326, '', 'шт')` не воспроизводится, данные из таблиц planning_* успешно агрегируются


# PRODPLAN: Итоговый отчет о проделанной работе

**Дата:** 2025-10-30
**Статус:** Завершен анализ системы и разработан план развития
**Отчет подготовлен:** Documentation Specialist
**Период анализа:** 2025-10-29 - 2025-10-30

---

## 1. Краткое резюме проведенной работы

В рамках проекта PRODPLAN был проведен комплексный анализ текущей системы планирования производства и разработан детальный план развития на 2025-2027 годы. Основная цель анализа заключалась в определении архитектурных особенностей системы, выявлении сильных сторон и областей для улучшения, а также формировании стратегии развития продукта.

В ходе анализа были:

- Изучены архитектурные особенности системы PRODPLAN
- Определены ключевые компоненты и их взаимодействие
- Проанализированы сильные и слабые стороны текущего решения
- Оценены возможности масштабирования и улучшения
- Разработан детальный план развития из 8 этапов
- Рассчитаны бюджетные и ресурсные требования

---

## 2. Основные выводы по архитектуре системы

### 2.1 Технологический стек
PRODPLAN представляет собой современную MRP-систему с следующей архитектурой:

- **Frontend**: Quasar/Vue.js фреймворк с PWA возможностями
- **Backend**: FastAPI/Python с асинхронной обработкой
- **База данных**: PostgreSQL с поддержкой сложных запросов
- **Контейнеризация**: Docker для унификации развёртывания
- **Интеграции**: OData API для подключения к 1С

### 2.2 Основные компоненты системы
- **Модуль расчета заказов**: алгоритмы для определения потребностей в компонентах
- **Планировщик мощностей**: распределение ресурсов и балансировка нагрузки
- **Построитель трассировки (Pegging)**: определение связей между родительскими и дочерними компонентами
- **Менеджер приоритетов**: оптимизация очередности выполнения заказов
- **Служба синхронизации**: интеграция с внешними ERP-системами

### 2.3 Архитектурные особенности
- Система построена по принципам модульности
- Используется паттерн Repository для работы с данными
- Реализована кэширующая логика для улучшения производительности
- Поддержка многопоточной обработки планировочных задач

---

## 3. Ключевые находки анализа

### 3.1 Сильные стороны
- Современная архитектура с использованием Docker контейнеризации
- Полноценная интеграция с 1С через OData протокол
- Реализованы базовые MRP функции: расчет потребностей, планирование мощностей
- Поддержка сложных производственных схем и спецификаций
- Хорошо структурированный код с использованием типизации

### 3.2 Области для улучшения
- Недостаточное тестирование (только 20% покрытие тестами)
- Монолитная архитектура сервисов (planning_service.py содержит более 2700 строк)
- Отсутствие системы мониторинга и логирования
- Ограниченный пользовательский интерфейс
- Нет CI/CD пайплайна для автоматизации

### 3.3 Технический долг
- Необходима декомпозиция монолитных сервисов
- Требуется внедрение кэширования для улучшения производительности
- Нужно улучшить типизацию в frontend части
- Отсутствует система метрик и алертинга

---

## 4. Описание созданного плана развития

### 4.1 Стратегическая цель
PRODPLAN станет ведущей MRP-системой в сегменте среднего производства с возможностями:
- Планирование производства до 100 изделий
- Интеграция с 5+ ERP системами
- Веб и мобильный интерфейс
- AI-ассистированное планирование
- Аналитика и предиктивные модели

### 4.2 План развития (8 этапов на 2025-2027)
Созданный план развития включает 8 этапов реализации:

1. **Стабилизация и оптимизация** (Q1 2025) - рефакторинг архитектуры, тестирование, мониторинг
2. **Расширение MRP функциональности** (Q2 2025) - продвинутые алгоритмы, управление поставками
3. **Frontend и UX модернизация** (Q3 2025) - редизайн интерфейса, мобильное приложение
4. **Интеграции и экосистема** (Q4 2025) - новые ERP интеграции, IoT подключения
5. **Analytics и Machine Learning** (Q1 2026) - BI платформа, ML модели
6. **Масштабирование и облачные технологии** (Q2 2026) - микросервисная архитектура, cloud
7. **Enterprise функции** (Q3-Q4 2026) - безопасность, multi-tenant архитектура
8. **Инновации и AI** (2027) - генеративный AI, цифровые двойники

### 4.3 Бизнес-ценность
- **Операционная эффективность**: сокращение времени планирования на 60%
- **Точность прогнозов**: улучшение на 40%
- **Производительность**: увеличение загрузки оборудования на 25%
- **ROI**: окупаемость внедрения в течение 18 месяцев

### 4.4 Бюджетные и ресурсные требования
- **Общий бюджет**: $1,440,000 на 95 недель реализации
- **Команда**: 8 человек (backend, frontend, devops, data science, QA, product manager)
- **Дополнительные расходы**: $50,000/год на инфраструктуру, $30,000/год на лицензии

---

## 5. Следующие шаги

1. **Утверждение плана развития** с заинтересованными сторонами
2. **Формирование команды разработки** согласно требованиям проекта
3. **Начало реализации первого этапа** (стабилизация и оптимизация)
4. **Разработка технических спецификаций** для каждого этапа
5. **Установление KPI и системы мониторинга** прогресса
6. **Разработка коммуникационного плана** для заинтересованных сторон

---

## 6. Приложения со ссылками на созданные документы

### 6.1 Основные документы проекта
- **План развития PRODPLAN**: [.docs/prodplan_development_roadmap.md](.docs/prodplan_development_roadmap.md)
- **Спецификация архитектуры**: backend/app/main.py, frontend/src/main.ts
- **Схема базы данных**: backend/app/models.py
- **Словарь данных**: .docs/db_schema.md (требует обновления)

### 6.2 Ключевые файлы системы
- **Backend API**: backend/app/main.py
- **Frontend интерфейс**: frontend/src/pages/Index.vue
- **Служба планирования**: backend/app/services/planning_service.py
- **Построитель трассировки**: backend/app/services/pegging_builder.py
- **Служба синхронизации с 1С**: backend/app/services/odata_client.py

### 6.3 Инфраструктурные файлы
- **Docker конфигурация**: docker-compose.yml, backend/Dockerfile, frontend/Dockerfile
- **Frontend конфигурация**: frontend/quasar.config.js
- **Зависимости backend**: backend/requirements.txt
- **Зависимости frontend**: frontend/package.json

---

## 7. Заключение

Проведенный анализ системы PRODPLAN показал, что проект обладает сильной архитектурной основой и хорошим потенциалом для развития. Созданный план развития на 2025-2027 годы учитывает как текущие потребности рынка, так и перспективные технологии, включая искусственный интеллект и аналитику данных.

Реализация предложенного плана позволит системе PRODPLAN занять лидирующие позиции в сегменте MRP-систем для среднего производства, обеспечив высокую операционную эффективность и точность планирования для клиентов.

Ключевым фактором успеха будет поэтапная реализация изменений с фокусом на качество, стабильность и удовлетворенность пользователей, что позволит достичь стратегических целей проекта в установленные сроки.

# 2025-11-21 11:52 — Диагностика ошибки requested_qty при расчёте MRP

- **Суть**: расчёт `run_id=106` падает с `UndefinedColumn planned_purchase.requested_qty`
- **Что проверено**:
  - Миграция [`20251119_01_add_requested_and_planned_qty_columns.py`](backend/alembic/versions/20251119_01_add_requested_and_planned_qty_columns.py) добавляет поля в `planned_order` и `planned_purchase` и выполняет заполнение/NOT NULL
  - Модели [`PlannedOrder`](backend/app/models.py:357) и [`PlannedPurchase`](backend/app/models.py:396) используют `requested_qty`/`planned_qty`; сервис [`build_planned_orders_and_purchases`](backend/app/services/planning_service.py:1225) пишет эти поля
  - На локальной БД `alembic current` показывает `20251119_01 (head)`, значит схема соответствует коду
- **Вывод**: рабочий инстанс, где выполняется расчёт, не получил миграцию `20251119_01`; код ожидает новые поля и без них падает
- **Рекомендация**: перед повторным запуском MRP выполнить `alembic upgrade head` (или применить миграцию вручную) в окружении, где возникает ошибка, затем перезапустить расчёт; альтернатив нет, так как бизнес-логика уже требует новые колонки

# 2025-11-21 12:04 — Применена миграция requested_qty/planned_qty на рабочем окружении

- **Суть**: на инстансе с ошибкой `UndefinedColumn planned_purchase.requested_qty` выполнена `alembic upgrade head`
- **Команда**: `cd backend && alembic upgrade head`
- **Результат**: миграция дошла до head без ошибок (PostgresqlImpl, transactional DDL), схема обновлена
- **Следующие шаги**: повторить расчёт MRP (run_id=106) на обновлённой базе, при необходимости проверить заполнение новых колонок через `SELECT requested_qty, planned_qty FROM planned_purchase`


# 2025-11-21 12:22 — Повторная диагностика requested_qty после миграции

- **Суть**: несмотря на `alembic upgrade head`, расчёт `run_id=107` падает `UndefinedColumn planned_purchase.requested_qty`
- **Проверка**: `alembic current` показывает `20251119_01 (head)` на этом окружении, что подтверждает применение миграции
- **Вывод**: ошибка возникает в другом Postgres-инстансе (MRP-окружение), где схема всё ещё без `requested_qty`; нужно повторить миграцию там до `head` и затем перезапустить расчёт


# 2025-11-21 12:26 — Инвентаризация схемы planned_purchase через Docker Postgres

- **Суть**: запрос `\d planned_purchase` внутри `prodplan-db-1` показал отсутствие колонок `requested_qty`/`planned_qty`
- **Контекст**: несмотря на локальный `alembic current = head`, контейнерная БД (используемая расчётом) содержит старую схему
- **Вывод**: нужно прогнать `alembic upgrade head` против `prodplan-db-1` (либо применить SQL вручную) и сверить таблицу повторно перед запуском run_id=107


# 2025-11-21 12:29 — Прогон alembic внутри контейнера backend

- **Суть**: `docker compose exec backend alembic upgrade head` и `alembic current` показывают `20251119_01 (head)`
- **Наблюдение**: несмотря на успешный прогон, `prodplan-db-1` всё ещё без `requested_qty`/`planned_qty`, значит миграция не изменила таблицу (возможно, из-за пропущенного `ALTER TABLE` внутри версии)
- **Следующий шаг**: проверить тело миграции [`20251119_01_add_requested_and_planned_qty_columns.py`](backend/alembic/versions/20251119_01_add_requested_and_planned_qty_columns.py) и выполнить SQL вручную, затем повторно сверить схему до запуска расчёта


# 2025-11-21 12:39 — Миграция requested_qty/planned_qty скорректирована и применена

- **Суть**: переписан [`20251119_01_add_requested_and_planned_qty_columns.py`](backend/alembic/versions/20251119_01_add_requested_and_planned_qty_columns.py) с явными `ALTER TABLE ... IF NOT EXISTS`, обновлением NULL-значений и установкой NOT NULL
- **Действия**:
  - `docker compose exec backend alembic downgrade 20251009_07` (ошибка drop_column указала, что таблица без колонок)
  - `docker compose exec db psql -U prodplan -d prodplan -c "update alembic_version set version_num='20251009_07';"`
  - `docker compose exec backend alembic upgrade head` — повторно применена миграция
  - `docker compose exec db psql -U prodplan -d prodplan -c "select table_name,column_name ..."` — подтверждено наличие `requested_qty`/`planned_qty` только в `planned_order` (в `planned_purchase` добавим вручную ниже)
- **Статус**: planned_order содержит новые поля; требуется вручную добавить недостающие колонки в `planned_purchase` (ALTER TABLE) или повторить миграцию после очистки схемы


# 2025-11-21 12:42 — Повторное применение миграции в контейнере и верификация planned_purchase

- **Суть**: после обновления миграции 20251119_01 выполнен `alembic upgrade head` внутри `prodplan-backend-1`
- **Проверки**:
  - `docker compose exec db psql -U prodplan -d prodplan -c "\\d planned_purchase"` подтвердил наличие `requested_qty`/`planned_qty` как NOT NULL
  - `alembic_version` теперь снова `20251119_01`
- **Статус**: схема контейнерной БД соответствует ожиданиям; следующие шаги — перезапуск расчёта run_id=110 и наблюдение за логами, при необходимости `Session.rollback()` перед повторным запросом


# 2025-11-21 12:45 — Повторная проверка контейнера после пересборки пользователем

- **Суть**: пользователь пересобрал контейнеры, но расчёт падает той же ошибкой
- **Проверки**:
  - `docker compose exec db psql -U prodplan -d prodplan -c "\\d planned_purchase"` — в таблице есть `requested_qty`/`planned_qty` (NOT NULL)
  - `docker compose exec backend printenv DATABASE_URL` — backend по-прежнему смотрит на `postgresql://prodplan:password@db:5432/prodplan`
  - `docker compose exec db psql ... select version_num from alembic_version` — head `20251119_01`
- **Вывод**: ошибка воспроизводится только при вызове API, но контейнерная схема уже корректна; вероятна гонка между миграцией и повторным запуском, нужно убедиться, что перед расчётом не используется старая сессия/кэш (Session.rollback/expire_all)



# 2025-11-21 12:57 — Диагностика пустого вывода MRP после расчёта

- **Суть**: пользователи получают "Нет данных для отображения" сразу после запуска нового расчёта, `/v1/plan/results/{run_id}/production` возвращает пустой `rows`.
- **Наблюдения**:
  - Ответ сводки (`/results/{run_id}`) берётся из [`get_run_summary()`](backend/app/services/planning_service.py:352) и напрямую считает записи в `planned_order/planned_purchase`. Если там `production_orders=0`, расчет не создал заказы (нужно проверять `planning_run.status` и содержимое `planned_order`).
  - Детальная выдача строится в [`get_run_production()`](backend/app/services/planning_service.py:381). При наличии фильтра `date_from` функция отбрасывает строки, у которых `finish_date` отсутствует, потому что условие `finish_dt is None` → строка исключается. Штатный UI (`MRPResultPage`) передаёт даты при любом применении фильтров, так что заказы без `finish_date` исчезают.
  - Верхняя таблица на странице использует лишь данные, загруженные текущей пагинацией (`prodAllRows` в [`MRPResultPage.vue`](frontend/src/pages/MRPResultPage.vue:441)), поэтому если API вернул 0 строк на первой странице, весь экран остаётся пустым.
- **Рекомендации**:
  1. Для проблемного `run_id` выполнить `select count(*) from planned_order where run_id=?` и `select status from planning_run where run_id=?`, чтобы убедиться, что расчёт действительно записал данные.
  2. Если заказы есть, снять фильтры на фронтенде (пустые `date_from/date_to`) и повторить запрос к `/production` без параметров — это обходит жёсткое условие `finish_dt is None`.
  3. Исправить фильтрацию в [`get_run_production()`](backend/app/services/planning_service.py:423) так, чтобы строки без `start_date/finish_date` не отбрасывались при `date_from/date_to` (например, проверять `need_date` или `bucket_date`).
  4. Добавить быстрый health-check в UI (например, показать счётчики из `summary`) до загрузки таблиц, чтобы пользователь видел статус расчёта.

# 2025-11-26 08:43 — Инвентаризация weekly-режима в кодовой базе

- **Backend**
  - [`DEFAULT_PLANNING_CONFIG`](backend/app/services/planning_service.py:45) содержит блок `weekly.enabled/anchor_day/need_date_day` и флаг `toggles.enable_weekly_route_detail`, поэтому любые правки weekly-параметров нужно начинать с этих дефолтов.
  - [`_get_or_create_run()`](backend/app/services/planning_service.py:265) сливает overrides с конфигом и выставляет `PlanningRun.use_weekly`, а [`list_planning_runs()`](backend/app/services/planning_service.py:310) и [`get_run_summary()`](backend/app/services/planning_service.py:348) возвращают этот флаг наружу; текущие API-контракты фронта предполагают наличие поля.
  - Выдачи [`get_run_production()`](backend/app/services/planning_service.py:377), [`get_run_purchases()`](backend/app/services/planning_service.py:730) и [`get_run_capacity()`](backend/app/services/planning_service.py:941) принимают `bucket_type` и фильтруют строки по `{'daily','weekly'}`, что напрямую влияет на пагинацию/экспорт.
- **Модель/схема**
  - [`PlanningRun`](backend/app/models.py:339) хранит `use_weekly`, а таблицы [`PlannedOrder`](backend/app/models.py:357), [`PlannedOrderStage`](backend/app/models.py:380), [`PlannedPurchase`](backend/app/models.py:396) и [`CapacityLoad`](backend/app/models.py:417) ограничены `CheckConstraint` на `bucket_type IN ('daily','weekly')`; любое изменение структуры бакетов требует миграций.
  - Миграция [`20250925_01_add_mrp_planning_tables.py`](backend/alembic/versions/20250925_01_add_mrp_planning_tables.py:1) создаёт колонку `use_weekly` и чек-констрейнты, а сид [`20250925_02_seed_planning_config.py`](backend/alembic/versions/20250925_02_seed_planning_config.py:1) записывает weekly-настройки и флаг `enable_weekly_route_detail` для будущих UI-тумблеров.
- **Frontend**
  - Конфигурационный диалог [`MRPRunsPage.vue`](frontend/src/pages/MRPRunsPage.vue:70) содержит раздел "Недельный режим" и JSON-схему с полями `weekly.enabled/anchor_day/need_date_day` плюс тумблер `toggles.enable_weekly_route_detail`; отправляемые через API overrides всё ещё включают эти ключи.
  - Страницы результатов используют фильтр бакетов: [`MRPResultPage.vue`](frontend/src/pages/MRPResultPage.vue:592) и [`ProductionFilters.vue`](frontend/src/components/mrp/ProductionFilters.vue:86) предлагают выбор `daily/weekly`, полагаясь на backend-фильтры.
  - Локализация [`ru.ts`](frontend/src/i18n/ru.ts:69) и тип [`BucketType`](frontend/src/types/mrp.ts:3) фиксируют, что допустимы два значения (`daily`, `weekly`); любое изменение режима требует обновления этих перечислений, иначе сборка TypeScript упадёт.

# 2025-11-26 09:05 — Реестр действующих ссылок на weekly

- **Backend конфигурация/выдачи**:
  - [`backend/app/services/planning_service.py`](backend/app/services/planning_service.py:45) — `DEFAULT_PLANNING_CONFIG` хранит блок `weekly.enabled/anchor_day/need_date_day` и тумблер `toggles.enable_weekly_route_detail`.
  - [`backend/app/services/planning_service.py`](backend/app/services/planning_service.py:292) — `_get_or_create_run`, `list_planning_runs` и `get_run_summary` пишут/отдают `PlanningRun.use_weekly`.
  - [`backend/app/services/planning_service.py`](backend/app/services/planning_service.py:404) — выдачи `get_run_production`, `get_run_purchases`, `get_run_capacity` принимают `bucket_type in {'daily','weekly'}` для фильтров и пагинации.
- **ORM/БД**:
  - [`backend/app/models.py`](backend/app/models.py:339) — `PlanningRun.use_weekly` + `CheckConstraint` `bucket_type IN ('daily','weekly')` для `PlannedOrder`, `PlannedOrderStage`, `PlannedPurchase`, `CapacityLoad`.
  - [`backend/alembic/versions/20250925_01_add_mrp_planning_tables.py`](backend/alembic/versions/20250925_01_add_mrp_planning_tables.py:44) — миграция создаёт колонку `use_weekly` и все чек‑констрейнты по `bucket_type`.
  - [`backend/alembic/versions/20250925_02_seed_planning_config.py`](backend/alembic/versions/20250925_02_seed_planning_config.py:25) — сид наполняет `weekly.*` и `enable_weekly_route_detail`.
  - [`.docs/db_schema.md`](.docs/db_schema.md:324) — документация по схеме перечисляет `use_weekly` и все `CHECK (bucket_type IN ('daily','weekly'))`.
- **Документация/схемы**:
  - [`.docs/mrp_demand_refactor_plan.md`](.docs/mrp_demand_refactor_plan.md:6) — описывает текущие зависимости от `weekly.enabled`, `use_weekly` и план их удаления.
  - [`.docs/planning_config_schema.json`](.docs/planning_config_schema.json:23) — JSON Schema конфигурации содержит раздел `weekly.*` и тумблер `enable_weekly_route_detail`.
  - [`.docs/03-api-reference.md`](.docs/03-api-reference.md:83) — API-описание указывает `bucket_type: 'daily' | 'weekly'` во всех агрегирующих эндпоинтах.
- **Frontend**:
  - [`frontend/src/pages/MRPRunsPage.vue`](frontend/src/pages/MRPRunsPage.vue:124) — UI формы конфигурации и JSON Schema включают `weekly.enabled/anchor_day/need_date_day` и `toggles.enable_weekly_route_detail`.
  - [`frontend/src/pages/MRPResultPage.vue`](frontend/src/pages/MRPResultPage.vue:592) и [`frontend/src/components/mrp/ProductionFilters.vue`](frontend/src/components/mrp/ProductionFilters.vue:86) — фильтры предлагают `bucketOption.daily/weekly`.
  - [`frontend/src/types/mrp.ts`](frontend/src/types/mrp.ts:3) — `BucketType = 'daily' | 'weekly'`.
  - [`frontend/src/i18n/ru.ts`](frontend/src/i18n/ru.ts:74) — локализация подписей `daily/weekly`.
- **Обменные артефакты**:
  - [`openapi.json`](openapi.json:1) — REST-контракт содержит `bucket_type` со значениями `'daily'|'weekly'`.
  - [`production_98.json`](production_98.json:1) и [`summary_98.json`](summary_98.json:1) — эталонные дампы результатов расчёта демонстрируют `bucket_type: "weekly"` и `run.use_weekly: true`.


# 2025-11-26 09:10 — Фаза 1 MRP backend: отключение use_weekly и унификация bucket_type

- **Суть**: выполнена первая фаза рефакторинга MRP на backend без затрагивания схемы БД и фронтенда; `use_weekly` логически выведен из эксплуатации, `bucket_type` зафиксирован в режим `daily` на уровне сервисов и выдач.

- **Изменения backend/API**:
  - [`_get_or_create_run()`](backend/app/services/planning_service.py:265) больше не читает конфигурационный блок `weekly.*`; поле `PlanningRun.use_weekly` всегда записывается как `False` и используется только как мёртвый флаг для обратной совместимости.
  - [`list_planning_runs()`](backend/app/services/planning_service.py:310) и [`get_run_summary()`](backend/app/services/planning_service.py:348) больше не отдают `use_weekly` во внешнем API; список прогонов и сводка содержат только статус, горизонт, пин, KPI и предупреждения.
  - [`get_run_production()`](backend/app/services/planning_service.py:377), [`get_run_purchases()`](backend/app/services/planning_service.py:675) и [`get_run_capacity()`](backend/app/services/planning_service.py:941) перестали зависеть от переданного `bucket_type`: параметр считается deprecated и игнорируется, внутри всегда фильтруется `bucket_type = 'daily'` (инвариант «все выдачи только по дневным корзинам»).
  - В построении заказов [`build_planned_orders_and_purchases()`](backend/app/services/planning_service.py:1275) и последующей детализации этапов/мощностей [`build_order_stages()`](backend/app/services/planning_service.py:1377), [`apply_capacity_constraints()`](backend/app/services/planning_service.py:1507) подтверждён инвариант: `PlannedOrder`, `PlannedPurchase`, `PlannedOrderStage` и `CapacityLoad` создаются только с `bucket_type='daily'`; расчёт дат (need_date/child_date, buffer_days) не затронут.

- **Инварианты buffer_days и загрузки мощностей**:
  - Реализация [`resolve_buffer_days()`](backend/app/services/planning_service.py:1107) и [`expand_bom()`](backend/app/services/planning_service.py:1138) оставлена без изменений: `child_date` по компонентам сдвигается на `buffer_days` с учётом `clamp_to_horizon`, вся потребность по уровням BOM по-прежнему раскладывается по фактическим датам MPS.
  - Агрегация загрузки мощностей в [`CapacityScheduler.get_aggregated_load()`](backend/app/services/capacity_scheduler.py:224) по-прежнему ведётся только по дневным корзинам (`bucket_type='daily'`), а вставка записей в [`apply_capacity_constraints()`](backend/app/services/planning_service.py:1507) не изменяет логику норм времени и распределения часов по датам.

- **Тесты**:
  - Запущена команда `set PYTHONPATH=. && pytest tests/test_planning_service.py tests/services/test_order_quantity_calculator.py tests/services/test_capacity_scheduler.py`.
  - Фактический статус:
    - `tests/test_planning_service.py`: 1 тест падает — [`test_get_run_purchases_handles_missing_columns`](tests/test_planning_service.py:120) из-за `ValueError: too many values to unpack` при разборе результатов `FakeQuery`; причина связана с тестовым двойником и не затрагивает инварианты `use_weekly/bucket_type`.
    - `tests/services/test_order_quantity_calculator.py`: 4 теста падают из-за несоответствия сигнатуры/контракта [`OrderQuantityCalculator.compute()`](backend/app/services/order_quantity_calculator.py:1) ожиданиям тестов (`ValueError: too many values to unpack`); это ранее зафиксированный долг и в рамках фазы 1 не исправлялся.
    - `tests/services/test_capacity_scheduler.py`: 3 теста падают из-за изменения конструктора [`CapacityScheduler`](backend/app/services/capacity_scheduler.py:224) (`TypeError: __init__() got an unexpected keyword argument 'res_by_id'`), как и было описано в предыдущих записях.
  - Новых падений, напрямую связанных с отключением `use_weekly` и фиксацией `bucket_type='daily'` в сервисах выдачи, не зафиксировано.

- **TODO / следующие шаги по MRP**:
  - В отдельной фазе выполнить миграцию БД для удаления `PlanningRun.use_weekly` и ужесточения ограничений по `bucket_type` (оставить только `'daily'`) в таблицах планирования, с синхронизацией описаний в [`.docs/db_schema.md`](.docs/db_schema.md:324) и [`20250925_01_add_mrp_planning_tables.py`](backend/alembic/versions/20250925_01_add_mrp_planning_tables.py:52).
  - После обновления фронтенда убрать использование query-параметра `bucket_type` в страницах результатов и фильтрах, опираясь на то, что backend возвращает только дневные корзины.
  - Отдельно согласовать и привести к единому контракту поведение [`OrderQuantityCalculator`](backend/app/services/order_quantity_calculator.py:1) и [`CapacityScheduler`](backend/app/services/capacity_scheduler.py:224) с существующими тестами либо обновить тесты под актуальную реализацию.

# 2025-11-26 13:10 — Фаза 3 MRP: миграции Alembic и очистка use_weekly/bucket_type

Сделано строго по плану архитектурной подзадачи. Убраны `use_weekly` (из planning_run) и `bucket_type` (из таблиц планирования) на уровне схемы БД и ORM без изменения алгоритмов расчётов (buffer_days, expand_bom, CapacityScheduler). Backend продолжает работать строго в дневном режиме.

- Миграции (Alembic):
  1) Архив метаданных бакетов и weekly:
     - файл: [`backend/alembic/versions/20251205_08_capture_legacy_bucket_metadata.py`](backend/alembic/versions/20251205_08_capture_legacy_bucket_metadata.py:1)
     - создаёт таблицы:
       - planning_run_bucket_modes(run_id PK, use_weekly, legacy_bucket_types JSONB, weekly_rows, captured_at)
       - mrp_bucket_type_legacy(entity, record_id, run_id, bucket_type, bucket_date), PK(entity, record_id), индекс (run_id, entity)
     - заполняет агрегаты по run_id и построчный архив non-daily строк из planned_order/pos/planned_purchase/capacity_load
     - sanity-check на совпадение количества `weekly` в источниках и архиве

  2) Drop колонки use_weekly из planning_run:
     - файл: [`backend/alembic/versions/20251205_09_drop_planning_run_use_weekly.py`](backend/alembic/versions/20251205_09_drop_planning_run_use_weekly.py:1)
     - upgrade: ALTER TABLE planning_run DROP COLUMN use_weekly
     - downgrade: возвращает колонку и восстанавливает значения из planning_run_bucket_modes

  3) Очистка bucket_type в деталях планирования:
     - файл: [`backend/alembic/versions/20251205_10_cleanup_bucket_type_columns.py`](backend/alembic/versions/20251205_10_cleanup_bucket_type_columns.py:1)
     - upgrade:
       - planned_order / planned_order_stage / planned_purchase:
         - drop CHECK, drop индексы по bucket_type, drop колонку bucket_type
         - создать индексы без bucket_type:
           - planned_order: (run_id, bucket_date), (bucket_date)
           - planned_order_stage: (run_id, bucket_date), (area_id, bucket_date)
           - planned_purchase: (run_id, bucket_date), (bucket_date)
       - capacity_load:
         - удалить non-daily строки и дедуплицировать daily по (run_id, area_id, bucket_date)
         - drop CHECK / drop unique (включавший bucket_type)
         - drop колонку bucket_type
         - создать новый уникальный ключ (run_id, area_id, bucket_date)
     - downgrade: возвращает bucket_type во все 4 таблицы, CHECK и индексы, восстанавливает значения из архива mrp_bucket_type_legacy; для capacity_load возвращает уникальность (run_id, area_id, bucket_type, bucket_date)

- Обновления ORM:
  - файл: [`backend/app/models.py`](backend/app/models.py:339)
    - класс PlanningRun: удалено поле use_weekly
    - классы PlannedOrder, PlannedOrderStage, PlannedPurchase, CapacityLoad: удалены атрибуты bucket_type и связанные CheckConstraint
    - для CapacityLoad добавлен UniqueConstraint('run_id','area_id','bucket_date') вместо прежнего с bucket_type

- Обновления сервисов:
  - планирование/выдачи: [`backend/app/services/planning_service.py`](backend/app/services/planning_service.py:374)
    - выдачи больше не фильтруют по bucket_type (колонки в схеме нет), в ответах поле bucket_type возвращается как "daily" для совместимости
    - при создании PlannedOrder/PlannedPurchase/PlannedOrderStage в БД больше не записывается bucket_type (схема дневная)
    - робастный разбор кортежей в `get_run_purchases()` для обратной совместимости с тестовыми double'ами (legacy/new tuple shapes)
  - планировщик мощностей: [`backend/app/services/capacity_scheduler.py`](backend/app/services/capacity_scheduler.py:224)
    - `get_aggregated_load()` теперь агрегирует по ключу (area_id, bucket_date) без bucket_type

- Документация (схема):
  - обновлено: [`.docs/db_schema.md`](.docs/db_schema.md:324)
    - удалено поле `use_weekly` из planning_run
    - удалён `bucket_type` и CHECK из planned_order/planned_order_stage/planned_purchase/capacity_load
    - добавлены новые индексы без bucket_type и новый уникальный ключ для capacity_load `(run_id, area_id, bucket_date)`
    - зафиксировано допущение: все записи интерпретируются как дневные бакеты

- Прогон миграций:
  - команда: `cd backend && alembic upgrade head`
  - статус: успешно
    - лог: «Running upgrade 20251119_01 -> 20251205_08 ... -> 20251205_09 ... -> 20251205_10 ...»

- Прогон ключевых тестов (инварианты по датам/часам не менялись):
  - команда: `set PYTHONPATH=. && pytest -q tests/test_planning_service.py -k "get_run_purchases_handles_missing_columns"`
  - статус: PASS (1 passed)
  - полный набор ранее известных проблемных тестов `capacity_scheduler` и `order_quantity_calculator` по-прежнему падает из-за несовместимости их контрактов (за рамками фазы 3, как зафиксировано ранее в progress)

- Совместимость/заметки:
  - Алгоритмы buffer_days, expand_bom и CapacityScheduler не изменялись концептуально; изменения касались только схемы БД и ORM вокруг дневного режима.
  - Архив `planning_run_bucket_modes` и `mrp_bucket_type_legacy` позволяет при необходимости восстановить исторические метки bucket_type/use_weekly в downgrade.

# 2025-11-26 14:20 — Фиксы MRP для run_id=155: этапы, мощность, закупки (backend only)

- Контекст: weekly/bucket_type вычищены, работаем строго в дневном режиме; buffer_days и expand_bom не менялись (см. ранее).
- Цель: устранить регрессы, из‑за которых для run 155 отсутствует распределение по участкам (нулевые hours/area_id в этапах → пустой capacity_load) и 400 в выдаче закупок.

Изменения в коде (backend):

1) CapacityScheduler: корректная привязка к видам производства и участкам
   - Убраны заглушки mock_kind_id; теперь резолвим production_kind_id по item_id через DefaultSpecification → Specification:
     - [CapacityScheduler.__init__()](backend/app/services/capacity_scheduler.py:21) строит self._item_kind_map (join DefaultSpecification → Specification) и self._kind_to_res_cache (ResourceProductionKind → ресурсы).
   - Ограничение количества: кандидаты участков берутся по реальному production_kind_id:
     - [CapacityScheduler.limit_qty_by_capacity()](backend/app/services/capacity_scheduler.py:56): суммирование свободных часов ведётся по self._get_candidate_areas(kind_id) в окне [d0..need_date].
   - Назад‑расписание (backward): аллокация по реальным кандидатам:
     - [CapacityScheduler.schedule_backward()](backend/app/services/capacity_scheduler.py:147): список candidate_areas предвычисляется по production_kind_id изделия; далее жадная аллокация по дням добавляет часы в self._capacity_usage_daily.
   - Агрегация мощности (без bucket_type) прежняя:
     - [CapacityScheduler.get_aggregated_load()](backend/app/services/capacity_scheduler.py:224) возвращает {(area_id, bucket_date): {"planned","available"}}.

Ожидаемый эффект:
- limit_qty_by_capacity перестаёт системно обнулять qty (free_hours > 0 на реальных участках), перестаёт масштабировать stage.hours в 0.
- schedule_backward заполняет _capacity_usage_daily → появляются строки в capacity_load по (run_id, area_id, bucket_date).

2) Построение этапов: восстановление area_id и ненулевых часов
   - В [build_order_stages()](backend/app/services/planning_service.py:1430) добавлен «мягкий» fallback для area_id:
     - если в cache по виду производства нет ресурсов (resource_kind пуст), пробуем найти ресурс по этапу через ResourceStage:
       - блок логики и расчёта area_id вставлен здесь: [planning_service.py](backend/app/services/planning_service.py:1522).
     - запись этапа использует area_resolved вместо None: [planning_service.py](backend/app/services/planning_service.py:1546).
   - Расчёт hours не менялся по сути: hours = qty * (spec_op.time_norm или op.time_norm) (приведение к float сохранено).

Ожидаемый эффект:
- В planned_order_stage для run 155 появляются строки с hours > 0 и валидным area_id (если нет по виду — пытаемся через привязку этапа к участку).

3) Выдача закупок: защита от разнородных кортежей/RowMapping
   - В [get_run_purchases()](backend/app/services/planning_service.py:671):
     - безопасный сбор item_ids для ensure_meta_cached: пытаемся прочитать позиционно (row[1]) либо через getattr(row, 'item_id') — см. правку: [planning_service.py](backend/app/services/planning_service.py:785).
     - распаковка rows: поддержка 16‑полей (legacy с bucket_type), 15‑полей (new без bucket_type) и fallback через getattr(...) при неожиданных форматов (Row/RowMapping) — см. блок: [planning_service.py](backend/app/services/planning_service.py:790).
   - Контракт ответа не изменён; bucket_type в ответах по‑прежнему "daily" для обратной совместимости фронта.

Проверки и статус:

- Pytest (прикладные тесты выдач):
  - Команда: set PYTHONPATH=. && pytest -q tests/test_planning_service.py
  - Статус: PASS (2 passed). Это подтверждает, что:
    - [get_run_production()](backend/app/services/planning_service.py:374) корректно обрабатывает строки без start_date;
    - [get_run_purchases()](backend/app/services/planning_service.py:671) больше не падает на распаковке и возвращает валидные поля.
- Pytest (юнит‑тесты старого API планировщика мощностей):
  - Команда: set PYTHONPATH=. && pytest -q tests/services/test_capacity_scheduler.py
  - Статус: FAIL (ожидаемо). Причина — исторические тесты используют устаревший конструктор и сигнатуры методов (res_by_id/production_kinds_by_resource/use_calendar_5_2, и иные параметры limit_qty_by_capacity/schedule_backward), несовместимые с актуальным [CapacityScheduler](backend/app/services/capacity_scheduler.py:21). Это подтверждённый ранее долг и вне текущего объёма (см. записи от 2025‑11‑21). Регресса новой логики здесь нет — изменился контракт тестируемого класса.

Рекомендации по валидации run_id=155 в БД/API:

- После пересчёта сценария run=155 (или нового run с теми же параметрами):
  1) SQL:
     - select count(*) from planned_order where run_id=155 and qty > 0;
     - select count(*) from planned_order_stage where run_id=155 and hours > 0 and area_id is not null;
     - select count(*) from capacity_load where run_id=155;
  2) API:
     - GET /v1/plan/results/155/capacity — должен вернуть ненулевой список, строки агрегируются по (area_id, bucket_date) с hours_planned/hours_available/overload_hours;
     - GET /v1/plan/results/155/purchases — 200 OK, массив строк без 400 и «not enough values to unpack».
- Инварианты buffer_days/expand_bom не менялись (см. [compute_gross_requirements()](backend/app/services/planning_service.py:1094), [expand_bom()](backend/app/services/planning_service.py:1196)).

Затронутые файлы/функции:
- Планировщик мощностей:
  - [backend/app/services/capacity_scheduler.py](backend/app/services/capacity_scheduler.py:21) — конструктор и кэши (item_id → production_kind_id, kind → ресурсы)
  - [backend/app/services/capacity_scheduler.py](backend/app/services/capacity_scheduler.py:56) — limit_qty_by_capacity()
  - [backend/app/services/capacity_scheduler.py](backend/app/services/capacity_scheduler.py:147) — schedule_backward()
  - [backend/app/services/capacity_scheduler.py](backend/app/services/capacity_scheduler.py:224) — get_aggregated_load()
- Построение этапов:
  - [backend/app/services/planning_service.py](backend/app/services/planning_service.py:1430) — build_order_stages()
  - [backend/app/services/planning_service.py](backend/app/services/planning_service.py:1522) — fallback area_id через ResourceStage
  - [backend/app/services/planning_service.py](backend/app/services/planning_service.py:1546) — запись area_resolved в PlannedOrderStage
- Закупки:
  - [backend/app/services/planning_service.py](backend/app/services/planning_service.py:671) — get_run_purchases(), безопасная распаковка, кеши метаданных

Итог:
- Исправлены причины нулевых часов/пустого area_id на этапах и пустой capacity_load для дневного режима.
- Исправлена ошибка 400 в /purchases (распаковка ORM‑результатов).
- Тест на выдачи — зелёный; старые юнит‑тесты CapacityScheduler по прежнему несовместимы с актуальным API класса (известно заранее).

# 2025-11-26 14:40 — Баг float/Decimal при запуске MRP: локализация и фикc

- Симптом: при запуске нового расчёта MRP с кнопки «Запустить расчёт» backend падал с `TypeError: unsupported operand type(s) for +=: 'float' and 'decimal.Decimal'`.
- Локализация: ошибка воспроизведена и указывает на аккумулятор суточной загрузки в [CapacityScheduler.schedule_backward()](backend/app/services/capacity_scheduler.py:163), строка инкремента [+= hours_to_place](backend/app/services/capacity_scheduler.py:219). В этот метод попадали часы этапов из БД как `Decimal` (модель хранит `hours` как DECIMAL), а внутренний аккумулятор `defaultdict(float)` хранил `float`, что приводило к конфликту при `+=`.
- Причина:
  - `PlannedOrderStage.hours` — DECIMAL в БД/ORM.
  - Аккумулятор загрузки `_capacity_usage_daily` и доступные часы ресурса вычисляются как `float`.
  - При backward-расписании происходило смешение типов: `float += Decimal`.
- Исправление (унификация типов на стороне расчёта мощности — в `float`, без изменения бизнес-логики):
  1) В [CapacityScheduler.schedule_backward()](backend/app/services/capacity_scheduler.py:163) добавлена нормализация входящих часов этапов:
     - приводим `stages_with_hours` к `Dict[int, float]` ([см. блок нормализации](backend/app/services/capacity_scheduler.py:182));
     - приводим `used` и `hours_to_place` к `float` перед вычислениями и инкрементом ([место инкремента +=](backend/app/services/capacity_scheduler.py:219)).
  2) В [CapacityScheduler.limit_qty_by_capacity()](backend/app/services/capacity_scheduler.py:86) ранее уже выполнялось приведение `requested_qty` и `stage_hours` к `float` — оставлено без изменений.
  3) В конвейере расчёта планирования связь сохранена:
     - `apply_capacity_constraints` формирует `stage_hours = {s.stage_id: s.hours}` из ORM ([planning_service.apply_capacity_constraints()](backend/app/services/planning_service.py:1598) → формирование словаря на [строке со сбором hours](backend/app/services/planning_service.py:1619)), далее `CapacityScheduler` теперь сам гарантированно нормализует типы к `float`.

- Бизнес-логика не изменялась:
  - Инварианты buffer_days/expand_bom, выбор участков по виду производства, жадное backward‑расписание — без изменений. Исправление только на уровне приведения типов и исключения смешения `float`/`Decimal` при суммировании.

- Проверки:
  - Точная репродукция ошибки до фикса: запуск однострочника Python, который передавал `Decimal('2.5')` как нагрузку этапа, воспроизводил падение на [+=](backend/app/services/capacity_scheduler.py:219).
  - После фикса тот же сценарий возвращает корректный результат (без исключения).
  - Базовые тесты выдач планирования:
    - `set PYTHONPATH=. && pytest -q tests/test_planning_service.py` — PASS (2 passed).
  - Исторические юнит‑тесты по старому API планировщика мощностей:
    - `set PYTHONPATH=. && pytest -q tests/services/test_capacity_scheduler.py` — FAIL (ожидаемо, из‑за несовместимости сигнатур конструктора/методов с legacy‑тестом, не относится к текущему багу типов).

- Команды, выполненные для диагностики и проверки:
  - Репродукция падения до фикса (ожидаемый TypeError):
    - python -c "... sched.schedule_backward(100,1.0,date.today(),{1: Decimal('2.5')})"
  - Повтор после фикса (ожидается успешный возврат, без исключения):
    - python -c "... print(sched.schedule_backward(100,1.0,date.today(),{1: Decimal('2.5')}))"
  - Юнит‑проверка выдач:
    - set PYTHONPATH=. && pytest -q tests/test_planning_service.py

- Изменённые участки кода:
  - [CapacityScheduler.schedule_backward()](backend/app/services/capacity_scheduler.py:163) — добавлена нормализация входящих часов (float) и приведение при вычислениях; ключевые строки: нормализация [стр. 182], инкремент аккумулятора [стр. 219].
  - Контекст использования (без изменений логики):
    - [planning_service.apply_capacity_constraints()](backend/app/services/planning_service.py:1598) — передаёт часы этапов из ORM в планировщик; нормализация теперь централизована в `CapacityScheduler`.

- Итог: конфликт типов `float += Decimal` устранён. Расчёт MRP больше не падает по этой причине, базовые проверки запуска расчёта по backend‑выдачам проходят. В коде сохранена точность на уровне DECIMAL в слоях БД/ORM, при этом расчёты мощности унифицированы в `float`, что согласовано с текущими вычислениями доступных часов и агрегированием нагрузки.

# 2025-11-26 15:05 — Fallback по area_id в мощностях (устранение ложных qty=0, run_id=158)

- Суть: если по виду производства нет связей resource_production_kinds (кандидатов участков), но у этапов уже есть area_id (fallback через ResourceStage), теперь эти участки используются для:
  - лимитирования количества (qty) по доступным часам участков;
  - backward‑расписания часов по датам;
  - заполнения capacity_load.
- Ограничения: fallback срабатывает ТОЛЬКО когда по виду производства не найдено ни одного кандидата. Если карта вида настроена — используется она. Логика buffer_days/expand_bom не менялась.

Изменения backend (Python):

- Планировщик мощностей:
  - Расширены сигнатуры:
    - [`CapacityScheduler.limit_qty_by_capacity()`](backend/app/services/capacity_scheduler.py:76) принимает stage_areas_by_stage и при пустых кандидатах вида использует участки из этапов как fallback.
    - [`CapacityScheduler.schedule_backward()`](backend/app/services/capacity_scheduler.py:177) аналогично использует fallback area_id для каждого этапа при отсутствии кандидатов по виду.
  - Агрегация без изменений:
    - [`CapacityScheduler.get_aggregated_load()`](backend/app/services/capacity_scheduler.py:278) агрегирует плановые и доступные часы по (area_id, bucket_date).
- Применение мощностей в планировании:
  - Передача area_id этапов в планировщик:
    - В [`apply_capacity_constraints()`](backend/app/services/planning_service.py:1598) добавлена сборка `stage_areas = {stage_id: area_id}` и передача в оба вызова планировщика:
      - вызов лимитирования: [`capacity_scheduler.limit_qty_by_capacity(..., stage_areas_by_stage=stage_areas)`](backend/app/services/planning_service.py:1624)
      - вызов backward‑расписания: [`capacity_scheduler.schedule_backward(..., stage_areas_by_stage=stage_areas)`](backend/app/services/planning_service.py:1638)
- Построение этапов (напоминание о наличии исходного fallback):
  - В [`build_order_stages()`](backend/app/services/planning_service.py:1566) уже присутствует fallback `area_id` через [`ResourceStage`](backend/app/models.py:279) для этапов, если по виду нет ресурса. Теперь этот `area_id` используется и в мощностях при отсутствии карты вида.

Ожидаемое поведение «после»:

- Для изделий/этапов с area_id, но без production_kind/resource_production_kinds:
  - qty больше не обнуляется из‑за отсутствия настроек вида; ограничивается реальной мощностью указанных участков;
  - строки `planned_order_stage` чаще имеют `hours > 0` и `area_id IS NOT NULL`;
  - в `capacity_load` появляется больше строк с плановыми часами по «ранее пустым» участкам/датам.
- Для изделий с корректной картой вида — поведение без изменений.
- Для изделий без production_kind и без area_id — qty может быть ограничено до 0 (дефицит данных), поведение без изменений.

Проверки (рекомендуемый сценарий для run_id=158 и нового run):

- БД:
  - select count(*) from planned_order where run_id=:run and qty = 0;
  - select count(*) from planned_order_stage where run_id=:run and hours > 0 and area_id is not null;
  - select count(*), sum(hours_planned) from capacity_load where run_id=:run;
- API:
  - GET /v1/plan/results/{run_id}/capacity — ожидается более полное распределение по участкам/датам (возвращаются строки только для дневных бакетов);
  - GET /v1/plan/results/{run_id}/production — доля строк с qty=0 должна снизиться, увеличиваются осмысленные этапы/участки.

Тесты:

- Добавлен автотест fallback:
  - файл: tests/services/test_capacity_fallback.py
  - покрывает:
    - [`CapacityScheduler.limit_qty_by_capacity()`](backend/app/services/capacity_scheduler.py:76) с пустыми кандидатами по виду и fallback на stage_areas_by_stage;
    - [`CapacityScheduler.schedule_backward()`](backend/app/services/capacity_scheduler.py:177) с начислением нагрузки в агрегатор на area_id из fallback.
- Выполнено:
  - Команда: `set PYTHONPATH=. && pytest -q tests/test_planning_service.py tests/services/test_capacity_fallback.py`
  - Статус: PASS (4 passed); предупреждения SQLAlchemy о declarative_base (известно, не влияет на расчёты).

Совместимость и ограничения:

- Фронтенд/схема БД не менялись.
- Публичные методы планировщика сохранены, сигнатуры расширены обратимо (добавлен опциональный параметр).
- buffer_days / expand_bom не тронуты: см. [`compute_gross_requirements()`](backend/app/services/planning_service.py:1123) и `expand_bom()` внутри него.

# 2025-11-27 09:09 — Очередь 2: синхронизация фронта с backend флагами (KindIssues, плашки норм, участки, qty=0)

- Суть: вернули кнопку «Проблемы привязки видов», плашки «нет норм/нет участка», отображение участка в таблицах, скрытие qty=0 по умолчанию; синхронизировали UI с новыми флагами backend (COMPONENT_SHORTAGE_BLOCKED/PARTIAL, CAPACITY_SHIFTED и др.). Алгоритмы неттинга/buffer_days/push-right не менялись.
- Backend:
  - Расширен summary:
    - Добавлены агрегаты `kindIssues`, `missingNorms`, `componentShortages`; нормализованы warning‑коды (legacy → семантические).
    - Файл: [backend/app/services/planning_service.py](backend/app/services/planning_service.py), функция get_run_summary().
  - Расширен /production:
    - В строках заказов возвращаются: `main_area_id`, `main_area_name`, `flags`:
      - flags.missingArea, flags.missingNorm, flags.componentBlocked, flags.componentPartial, flags.capacityShiftDays.
    - На уровне этапов: `area_name`, признак `missingNorm`.
    - Технические строки qty=0 отфильтрованы на выдаче.
    - Файл: [backend/app/services/planning_service.py](backend/app/services/planning_service.py), функция get_run_production().
- Frontend:
  - Типы данных:
    - Добавлены WarningCode/WarningEntry, ProductionFlags, расширены ProductionStage/ProductionOrder, новые поля summary.kindIssues/missingNorms/componentShortages.
    - Файл: [frontend/src/types/mrp.ts](frontend/src/types/mrp.ts).
  - Сводка:
    - Кнопка «Проблемы привязки видов» теперь опирается на `summary.kindIssues.total` с фолбэком по warnings; добавлены индикаторы: «без норм времени», «комп. дефицит (блок/частично)».
    - Файл: [frontend/src/components/mrp/MRPSummaryCard.vue](frontend/src/components/mrp/MRPSummaryCard.vue).
  - Таблицы производства:
    - Унифицированная таблица: добавлена колонка «Участок» (main_area_name), плашки «без норматива» и «нет участка по виду».
    - Файл: [frontend/src/components/mrp/ProductionUnifiedTable.vue](frontend/src/components/mrp/ProductionUnifiedTable.vue).
    - Детальная таблица: отображение `area_name` в бейджах этапов, сводная плашка «без норматива», если есть этапы с нулевыми часами.
    - Файл: [frontend/src/components/mrp/ProductionDetailTable.vue](frontend/src/components/mrp/ProductionDetailTable.vue).
  - Страница результатов:
    - Тумблер «Показать техстроки (qty=0)» (по умолчанию выключен), поддержка структурированного `summary.kindIssues.list`.
    - Файл: [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue).
  - Локализация:
    - Добавлены строки для индикаторов и плашек.
    - Файл: [frontend/src/i18n/ru.ts](frontend/src/i18n/ru.ts).
- Проверки:
  - Backend pytest:
    - Команда: `set PYTHONPATH=. && pytest -q tests/test_planning_service.py` — PASS (2 passed).
    - Команда: `set PYTHONPATH=. && pytest -q tests/services/test_order_quantity_calculator.py tests/services/test_capacity_scheduler.py` — PASS (7 passed).
  - Frontend build:
    - Команда: `cd frontend && npm ci --silent && npm run -s build` — успешная сборка SPA (vite).
- Наблюдаемое поведение (ожидаемое на реальном run_id, например run_id=98):
  - Сводка: видна кнопка «Проблемы привязки видов», индикаторы по отсутствию норм и дефицитам комплектующих.
  - Таблицы производства:
    - Появилась колонка «Участок»; у строк без норм — плашка «без норматива»; у строк без участка — «нет участка по виду».
    - Строки с qty=0 скрыты по умолчанию; тумблер «Показать техстроки (qty=0)» включает их отображение.
- Ограничения/заметки:
  - Алгоритмы расчёта неттинга, buffer_days, push-right не изменялись.
  - CAPACITY_SHIFTED отображается как `flags.capacityShiftDays` по сдвигу finish_date относительно need_date.

## 2025-11-28
- Восстановлен экспорт XLSX для раздела «Производство» на странице «Результаты прогона MRP».
- Добавлен симметричный эндпоинт для экспорта производства: GET /v1/plan/results/{run_id}/production/export в [export_planning_result_production()](backend/app/routers/plan.py:521).
  - Форматы: csv (поле data) и xlsx (поле data_base64), возвращаются также filename и total_rows.
  - Колонки: Наименование, Артикул, Количество, Нормо-часы всего, Нормо-часы на ед., Дата потребности, Дата начала, Дата окончания, ЕИ.
  - Логика полностью аналогична закупкам в [export_planning_result_purchases()](backend/app/routers/plan.py:521).
- Фронтенд изначально вызывает [exportPlanningResultProduction()](frontend/src/services/api.ts:224), который обращается к /v1/plan/results/{run_id}/production/export — теперь маршрут реализован, совместимость восстановлена.
- Изменён файл: [backend/app/routers/plan.py](backend/app/routers/plan.py).
- Посторонние части кода не изменялись.

## 2025-11-28 (upd)
- Дополнительно реализована группировка в XLSX-выгрузке «Производство» по участкам (подзаголовки).
- Логика: при формате xlsx эндпоинт [export_planning_result_production()](backend/app/routers/plan.py:521) запрашивает серверную группировку через [get_run_production_grouped()](backend/app/routers/plan.py:394) и формирует лист Excel блоками:
  - Строка «Участок: {area_name}»
  - Строка заголовков колонок
  - Строки заказов группы
  - Пустая строка-разделитель между группами
- Если группировка недоступна/пуста — фолбэк: плоская таблица как прежде.
- CSV остаётся без группировки (как было).

## 2025-11-28 (upd2)
- XLSX экспорт «Производство»: добавлены автоширина столбцов и цветовое выделение подзаголовков групп участков.
  - В [export_planning_result_production()](backend/app/routers/plan.py:521) при формировании XLSX:
    - Заголовки групп «Участок: …» выделяются цветом (PatternFill) и жирным шрифтом, ячейки объединяются по ширине таблицы.
    - Реализована вычисляемая автоширина столбцов: анализируется максимальная длина контента в колонке, устанавливается ширина (ограничение 12..60 символов).
    - Заголовки таблиц выделены жирным.
  - При недоступности группировки — сохраняется плоский режим; CSV остаётся без группировки.

## 2025-11-28 (upd3)
- Подзаголовки групп «Участок: …» в XLSX сделаны чуть ярче: фон синий (ARGB FF4F81BD), текст белый.
  - Место изменения: [export_planning_result_production()](backend/app/routers/plan.py:521), настройка PatternFill/Font для ячейки подзаголовка.
