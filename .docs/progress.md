
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

**2026-03-16 — миграции Alembic доведены до актуального head после исправления идентификаторов ревизий и ручного выравнивания состояния БД:**

1) **Исправлены несовместимые идентификаторы ревизий в миграциях:**
   - в [backend/alembic/versions/20260312_01_add_planned_rework.py](backend/alembic/versions/20260312_01_add_planned_rework.py:1) `down_revision` приведён к фактическому id предыдущей миграции `20260226_01`;
   - в [backend/alembic/versions/20260313_01_add_item_category_link.py](backend/alembic/versions/20260313_01_add_item_category_link.py:1) сокращён `revision` до совместимого значения `20260313_01`, чтобы запись помещалась в `alembic_version.version_num`.

2) **Диагностика состояния БД показала частично применённую схему:**
   - таблица `planned_rework` уже существовала в БД, хотя [alembic current](backend/alembic/versions/20260312_01_add_planned_rework.py:13) оставался на ревизии `20260226_01`;
   - поле `items.category_id` ещё отсутствовало, поэтому обычный `upgrade head` падал сначала на повторном создании `planned_rework`, а затем на переполнении `version_num`.

3) **Миграции доведены до актуального состояния:**
   - выполнен `stamp` на ревизию `20260312_01_add_planned_rework` для синхронизации Alembic с уже существующей таблицей `planned_rework`;
   - после исправления revision id успешно выполнен `python -m alembic upgrade head` в каталоге [backend](backend:1);
   - итог: применена миграция добавления `items.category_id` из [backend/alembic/versions/20260313_01_add_item_category_link.py](backend/alembic/versions/20260313_01_add_item_category_link.py:19).

4) **Результат текущей сессии:**
   - цепочка миграций Alembic теперь проходит до актуального `head`;
   - состояние БД соответствует последним backend-изменениям для `rework` и группировки по товарным группам.

**2026-03-16 — завершение refactoring-плана `purchase` / `rework`: закрыт последний regression-пункт по сравнению production orders на контрольном run-е и формально завершена итерация 12:**

1) **Добавлен контрольный regression-test на сравнение production orders «до/после» в одном сценарии с примешанными `purchase` и `rework`:**
   - в [tests/test_stock_by_item_cache.py](tests/test_stock_by_item_cache.py:664) добавлен сценарий `test_control_run_keeps_production_orders_identical_with_purchase_and_rework_flows`;
   - тест строит baseline-run только с `production`, затем mixed-run с тем же `production`-изделием и дополнительными потоками `purchase` / `rework`;
   - сравнение выполняется по проекции строк [PlannedOrder](backend/app/models.py:434): `item_id`, `requested_qty`, `planned_qty`, `qty`, `need_date`, `bucket_date`.

2) **Зафиксирован ожидаемый инвариант последней незакрытой проверки:**
   - наличие потоков `purchase` и `rework` не должно изменять состав и количества production-заказов для того же контрольного спроса;
   - mixed-run дополнительно подтверждает, что параллельно действительно создаются строки [PlannedPurchase](backend/app/models.py:462) и [PlannedRework](backend/app/models.py:479), не влияя на baseline projection production.

3) **Проверка:**
   - выполнен прогон `set "PYTHONPATH=backend" && pytest tests/test_stock_by_item_cache.py -q`;
   - результат: `10 passed`.

4) **План и документация синхронизированы с фактическим завершением:**
   - в [`.docs/rework_refactoring_plan.md`](.docs/rework_refactoring_plan.md) закрыт пункт сравнения production orders до/после;
   - закрыты итоговые чекбоксы по итерации 12, финальной regression-проверке и обновлению документации.

**2026-03-16 — синхронизация плана рефакторинга `rework`: чекбоксы в [`.docs/rework_refactoring_plan.md`](.docs/rework_refactoring_plan.md) приведены в соответствие с уже выполненными backend/frontend/regression-итерациями, незавершённым оставлен только контрольный пункт по сравнению production orders на контрольном run-е:**

1) **План рефакторинга синхронизирован с уже подтверждёнными результатами прошлых итераций:**
   - закрыты верхнеуровневые пункты по выделению отдельного потока `rework`, сохранению production-инвариантов, поддержке спецификаций/дефицита комплектующих и стабилизации групп товаров;
   - закрыты проектные шаги по классификатору пополнения, общей нормализации количества, отдельной сущности [PlannedRework](backend/app/models.py:479), grouped-endpoint'ам, Excel-экспорту и UI-слою результатов MRP;
   - закрыты чекбоксы по unit/API/UI/regression-проверкам, для которых в журнале уже были зафиксированы выполненные тесты и сборки.

2) **Основание для синхронизации чекбоксов — уже выполненные и задокументированные проверки:**
   - regression-наборы по production-инвариантам и summary/warnings, описанные в [`.docs/progress.md`](.docs/progress.md:24), подтверждают сохранение текущих production-контрактов;
   - API-регрессия результатов MRP и XLSX-export'ов подтверждена сценариями в [tests/routers/test_plan_result_endpoints.py](tests/routers/test_plan_result_endpoints.py:65);
   - сценарии full / partial / blocked для `rework` подтверждены тестами в [tests/test_stock_by_item_cache.py](tests/test_stock_by_item_cache.py:435);
   - проверка production-warning contract зафиксирована в [tests/test_planning_service.py](tests/test_planning_service.py:138).

3) **Что осталось незавершённым в плане после синхронизации:**
   - не закрыт только пункт сравнения production orders «до/после» на контрольном run-е в [`.docs/rework_refactoring_plan.md`](.docs/rework_refactoring_plan.md);
   - соответственно, итерация 12 в плане остаётся частично открытой до появления отдельного контрольного сравнения именно по строкам production orders, а не только по summary/API/test-regression.

4) **Граница текущей сессии:**
   - изменения в код backend/frontend не вносились;
   - обновлены только документы [`.docs/rework_refactoring_plan.md`](.docs/rework_refactoring_plan.md) и [`.docs/progress.md`](.docs/progress.md).

**2026-03-16 — production-регрессия по refactoring-потоку `purchase` / `rework`: подтверждена неизменность текущих production-контрактов на контрольном backend-наборе:**

1) **Выполнен сводный regression-прогон backend-тестов для production-инвариантов и result-endpoint'ов:**
   - запущен набор:
     - `set "PYTHONPATH=." && pytest tests/test_stock_by_item_cache.py tests/test_planning_service.py tests/services/test_capacity_scheduler.py tests/services/test_capacity_push_right.py tests/services/test_capacity_fallback.py tests/routers/test_plan_result_endpoints.py -q`;
   - результат: `22 passed`.

2) **Подтверждены ключевые инварианты production-потока после выделения `rework` и доработок `purchase`:**
   - сохраняется корректность production-расчёта по контрольным сценариям склада, резервов компонентов активных заказов 1С и capacity scheduling;
   - сохраняется контракт summary/warnings в [`get_run_summary()`](backend/app/services/planning_service.py:358);
   - сохраняются production-result endpoint'ы списка, grouped-представления и XLSX-экспорта через [`plan.py`](backend/app/routers/plan.py:1).

3) **Зафиксированы только некритичные предупреждения окружения, без падения тестов:**
   - `MovedIn20Warning` для [`declarative_base()`](backend/app/database.py:15);
   - `PydanticDeprecatedSince20` для class-based `Config` в [`backend/app/schemas.py`](backend/app/schemas.py:28);
   - `DeprecationWarning` на использование `datetime.utcnow()` в тестах, например в [`tests/test_stock_by_item_cache.py`](tests/test_stock_by_item_cache.py:38).

4) **Граница текущей сессии:**
   - изменения в коде не вносились;
   - обновлён только журнал состояния в [`.docs/progress.md`](.docs/progress.md).

**2026-03-16 — frontend-итерация UI-экспорта для `rework`: добавлены кнопки выгрузки на странице результатов MRP и подключён frontend API для нового export-endpoint'а:**

1) **Страница результатов MRP расширена действиями экспорта для верхнего блока `rework`:**
   - в [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:118) в фильтрах вкладки `rework` добавлены кнопки `CSV` и `XLSX` рядом с уже существующими действиями production/purchase;
   - в [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:1084) добавлен handler `exportRework(...)`, который скачивает либо текстовый CSV, либо base64-XLSX через уже используемые browser-side helper'ы загрузки файлов;
   - это закрывает недостающий UI-слой над уже существующим backend-endpoint'ом `GET /v1/plan/results/{run_id}/rework/export`.

2) **Frontend API синхронизирован с backend-контрактом экспорта переработки:**
   - в [frontend/src/services/api.ts](frontend/src/services/api.ts:254) добавлена обёртка `exportPlanningResultRework(...)` для вызова [`/v1/plan/results/{run_id}/rework/export`](backend/app/routers/plan.py:1194);
   - сигнатура повторяет существующий стиль export-обёрток для production/purchases и поддерживает оба формата: `csv` / `xlsx`.

3) **Локализация вкладок `rework` переведена на i18n-ключи вместо захардкоженных подписей:**
   - в [frontend/src/i18n/ru.ts](frontend/src/i18n/ru.ts:9) добавлены ключи `mrp.tabs.rework` и `mrp.tabs.reworkDetail`;
   - в [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:31) и [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:172) обе вкладки `rework` теперь используют общую локализацию, чтобы новый UI-экспорт не добавлял ещё одну точку расхождения текста.

4) **Проверка:**
   - выполнен прогон `npm run build` в каталоге [frontend](frontend/package.json:1);
   - результат: сборка успешна, критичных ошибок TypeScript/Vue после добавления `rework`-экспорта нет.

**2026-03-16 — backend-итерация API-регрессии для результатов `purchase` / `rework`: добавлены endpoint-тесты и синхронизированы чекбоксы плана:**

1) **Добавлены интеграционные API-тесты для результатов MRP через роутер [`plan`](backend/app/routers/plan.py):**
   - новый файл [tests/routers/test_plan_result_endpoints.py](tests/routers/test_plan_result_endpoints.py) поднимает тестовое FastAPI-приложение с подключением [router](backend/app/routers/plan.py:62) и override для [get_db](backend/app/database.py:24);
   - покрыты endpoint'ы списка `rework`, grouped-by-category для закупки и переработки, а также XLSX-export для обоих потоков.

2) **Зафиксированы проверяемые контракты API для новых потоков:**
   - проверяется, что `GET /api/v1/plan/results/{run_id}/rework` возвращает поля `spec_code`, `requested_qty`, `planned_qty`, `component_partial`, `shortage`;
   - проверяется, что grouped-endpoint'ы возвращают корректные суммы и счётчики (`sum_qty`, `sum_requested_qty`, `sum_planned_qty`, `partial_orders`, fallback `Без товарной группы`);
   - проверяется, что export-endpoint'ы возвращают `xlsx` payload с корректными листами, group-title и содержимым строк.

3) **Чекбоксы API-тестов в плане рефакторинга закрыты по факту выполнения:**
   - в [`.docs/rework_refactoring_plan.md`](.docs/rework_refactoring_plan.md) закрыты пункты раздела `6.2. API-тесты backend` для списка `rework`, grouped endpoint'ов, Excel-export и полей агрегации.

4) **Проверка:**
   - выполнен прогон:
     - `set "PYTHONPATH=backend" && pytest tests/routers/test_plan_result_endpoints.py tests/services/test_mrp_result_exports.py tests/services/test_grouped_by_category_results.py tests/services/test_rework_results.py -q`
   - результат: `11 passed`.

5) **Добавлена точечная регрессия на формат production-warning'ов в summary:**
   - в [tests/test_planning_service.py](tests/test_planning_service.py) добавлен сценарий на [get_run_summary()](backend/app/services/planning_service.py:358), который проверяет сохранение production-контракта по `warnings`, `kindIssues` и `componentShortages`;
   - отдельно зафиксировано legacy-преобразование `PRODUCTION_KIND_NOT_FOUND -> NO_PRODUCTION_KIND`, чтобы фронтенд продолжал получать прежний код предупреждения.

6) **Дополнительная проверка после расширения регрессии:**
   - выполнен прогон:
     - `set "PYTHONPATH=." && pytest tests/test_planning_service.py tests/routers/test_plan_result_endpoints.py tests/services/test_mrp_result_exports.py tests/services/test_grouped_by_category_results.py tests/services/test_rework_results.py -q`
   - результат: `14 passed`.

7) **Прогнаны текущие production-regression тесты без изменения бизнес-логики расчёта:**
   - дополнительно выполнен прогон:
     - `set "PYTHONPATH=." && pytest tests/test_stock_by_item_cache.py tests/test_planning_service.py tests/services/test_capacity_scheduler.py tests/services/test_capacity_push_right.py tests/services/test_capacity_fallback.py -q`
   - результат: `18 passed`;
   - по итогам в [`.docs/rework_refactoring_plan.md`](.docs/rework_refactoring_plan.md) закрыт чекбокс `Прогнать тесты, покрывающие текущий расчёт production`.

8) **Добавлена безопасная API-регрессия для production-result endpoints:**
   - в [tests/routers/test_plan_result_endpoints.py](tests/routers/test_plan_result_endpoints.py) добавлен сценарий на `production`-контракты:
     - `GET /api/v1/plan/results/{run_id}/production`;
     - `GET /api/v1/plan/results/{run_id}/production/grouped`;
     - `GET /api/v1/plan/results/{run_id}/production/export?format=xlsx`;
   - тест фиксирует, что сохраняются ключевые поля и флаги production-выдачи: `norm_hours_total`, `norm_hours_per_unit`, `flags.componentPartial`, `flags.capacityShiftDays`, группировка по участку и XLSX-структура выгрузки.

9) **Дополнительная проверка после расширения production-regression API:**
   - выполнен прогон:
     - `set "PYTHONPATH=." && pytest tests/routers/test_plan_result_endpoints.py tests/test_planning_service.py tests/test_stock_by_item_cache.py -q`
   - результат: `16 passed`.

10) **План синхронизирован с фактически добавленными API-тестами:**
   - в [`.docs/rework_refactoring_plan.md`](.docs/rework_refactoring_plan.md) закрыт пункт `тесты API для результатов MRP` в списке обязательной проверки файлов/направлений.

**2026-03-16 — backend-регрессия по потокам пополнения: синхронизированы тесты классификатора с фактическим включением `rework` и добавлен контрольный сценарий для `production`:**

1) **Актуализирован тест классификации способа пополнения под уже внедрённый поток `rework`:**
   - в [tests/services/test_replenishment.py](tests/services/test_replenishment.py) legacy-ожидание для `Переработка -> production` заменено на актуальное `Переработка -> rework`;
   - это устраняет ложную регрессию после фактического включения [classify_replenishment_flow()](backend/app/services/replenishment.py:19) для маркеров `переработ` / `rework`.

2) **Добавлен отдельный regression-test на неизменность обычного production-потока:**
   - в [tests/services/test_replenishment.py](tests/services/test_replenishment.py) добавлен сценарий, подтверждающий, что позиция с методом `Производство` по-прежнему создаёт запись в [PlannedOrder](backend/app/models.py:425) и не утекает в [PlannedPurchase](backend/app/models.py:462) или [PlannedRework](backend/app/models.py:479);
   - это фиксирует один из базовых инвариантов перед дальнейшей production-регрессией из плана рефакторинга.

3) **Проверка набора backend-тестов для потоков `production` / `purchase` / `rework`:**
   - выполнен прогон:
     - `set "PYTHONPATH=backend" && pytest tests/test_stock_by_item_cache.py tests/services/test_rework_results.py tests/services/test_grouped_by_category_results.py tests/services/test_order_quantity_calculator.py tests/services/test_replenishment.py -q`
   - результат: `25 passed`.

**2026-03-16 — backend-итерация Excel-экспорта для потоков `purchase` и `rework`: добавлены отдельные XLSX-выгрузки с группировкой по товарным группам:**

1) **Выделен отдельный backend-service для экспорта результатов MRP, не затрагивающий production-экспорт:**
   - добавлен файл [backend/app/services/mrp_result_export.py](backend/app/services/mrp_result_export.py), где собраны общие helper'ы для формирования workbook, авто-ширины колонок, base64-кодирования и подзаголовков товарных групп;
   - это изолирует новую Excel-логику закупки/переработки от существующего [export_production_orders_xlsx()](backend/app/services/production_order_export.py:30) и от production-export в [backend/app/routers/plan.py](backend/app/routers/plan.py:918).

2) **XLSX-экспорт закупок переведён на backend-группировку по товарным группам:**
   - в [export_purchases_results_xlsx()](backend/app/services/mrp_result_export.py:114) используется [get_run_purchases_grouped_by_category()](backend/app/services/planning_service.py:1202);
   - секции Excel теперь строятся как «Товарная группа → заголовок колонок → строки заказов»;
   - в выгрузку включены поля: наименование, артикул, количество, ЕИ, дата потребности, дата заказа, срок пополнения и `supplier_ref1c`.

3) **Добавлен отдельный XLSX-экспорт переработки с диагностикой комплектующих:**
   - в [export_rework_results_xlsx()](backend/app/services/mrp_result_export.py:167) используется [get_run_rework_grouped_by_category()](backend/app/services/planning_service.py:1482);
   - в Excel для `rework` выводятся `requested_qty`, `planned_qty`, спецификация, лимит по комплектующим и человекочитаемый статус ограничений (`Без ограничений` / `Частично ограничен` / `Заблокирован`);
   - fallback-группа `Без товарной группы` сохраняется и в Excel.

4) **API результатов MRP расширен новым endpoint'ом экспорта `rework`, а экспорт закупок переподключён на новый service:**
   - в [backend/app/routers/plan.py](backend/app/routers/plan.py) `GET /v1/plan/results/{run_id}/purchases/export?format=xlsx` теперь делегирует в [export_purchases_results_xlsx()](backend/app/services/mrp_result_export.py:114);
   - добавлен новый endpoint `GET /v1/plan/results/{run_id}/rework/export`, реализованный в [export_planning_result_rework()](backend/app/routers/plan.py:1203);
   - CSV-контракты оставлены совместимыми, а новая XLSX-логика добавлена рядом с существующей без изменения production API.

5) **Добавлены тесты на Excel-экспорт новых потоков:**
   - новый файл [tests/services/test_mrp_result_exports.py](tests/services/test_mrp_result_exports.py) проверяет:
     - группировку закупок по товарным группам в XLSX;
     - наличие fallback-секции `Без товарной группы`;
     - экспорт `rework` со спецификацией и статусами ограничений по комплектующим.

6) **Проверка:**
   - выполнен прогон:
     - `set "PYTHONPATH=backend" && pytest tests/services/test_mrp_result_exports.py tests/services/test_grouped_by_category_results.py tests/services/test_rework_results.py -q`
   - результат: `8 passed`.

**2026-03-13 — frontend-итерация рефакторинга потоков пополнения: восстановлена гидрация выбора групп и добавлен UI для `rework`/группировки закупок по товарным группам:**

1) **Страница синхронизации групп теперь поднимает состояние при открытии и после выгрузки групп:**
   - в [frontend/src/pages/SyncPage.vue](frontend/src/pages/SyncPage.vue) добавлен вызов загрузки списка групп и сохранённого выбора на `mounted`;
   - после выгрузки групп из OData страница сразу перечитывает список и сохранённый набор `selectedIds`, поэтому повторное открытие больше не требует ручного обновления;
   - загрузка сохранённого выбора вынесена в отдельный helper, чтобы повторно использовать его без лишнего копирования логики.

2) **Frontend API и типы MRP расширены под новые backend-контракты `grouped-by-category` и `rework`:**
   - в [frontend/src/services/api.ts](frontend/src/services/api.ts) добавлены обёртки для `/purchases/grouped-by-category`, `/rework` и `/rework/grouped-by-category`;
   - в [frontend/src/types/mrp.ts](frontend/src/types/mrp.ts) добавлены типы `PurchaseCategoryGroupedResponse`, `ReworkRow`, `ReworkGroup`, а также счётчик `rework_requests` в summary.

3) **Страница результатов MRP получила отдельный верхний и детальный блок `rework`:**
   - в [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue) добавлены вкладки `rework` в верхней секции и в секции деталей;
   - загрузка `rework` теперь идёт через отдельное состояние страницы с фильтрами, пагинацией и grouped-представлением по товарным группам;
   - для `rework` показаны суммы `requested_qty` / `planned_qty` и индикаторы `partial` / `blocked` на уровне группы.

4) **Закупки в верхнем блоке MRP переведены на backend-группировку по товарным группам:**
   - вместо старой агрегированной таблицы `item_id + unit` верхняя вкладка закупок теперь использует grouped endpoint по товарным группам;
   - при недоступности grouped endpoint сохранён безопасный fallback в группу `Без товарной группы`, собранную из плоских строк `/purchases`.

5) **Техническая зачистка страницы результатов:**
   - удалён временный diagnostic-banner с `runId/params`;
   - наблюдатели и первичная загрузка расширены так, чтобы `rework` поднимался вместе с production/purchases и корректно подгружался при переключении вкладок.

6) **Проверка:**
   - выполнен фронтенд-билд:
     - `npm run build` (в каталоге `frontend`)
   - результат: сборка успешна.

7) **План рефакторинга синхронизирован с фактически выполненной frontend-итерацией:**
   - в `.docs/rework_refactoring_plan.md` отмечены выполненные пункты по анализу frontend-точек входа, итерациям 10/11, UI-части `rework` и стабилизации страницы выбора групп;
   - незакрытыми оставлены только шаги, для которых ещё нет отдельной UI-проверки через браузер, Excel-экспорта и production-регрессии.

**2026-03-13 — итерация 6 рефакторинга потоков пополнения: добавлена backend-группировка закупки и `rework` по товарным группам:**

1) **В номенклатуре введена явная связь изделия с товарной группой:**
   - в [`Item`](backend/app/models.py) добавлено поле `category_id` и связь с [`ItemCategory`](backend/app/models.py);
   - в [`ItemCategory`](backend/app/models.py) добавлены обратные связи `items`, `parent`/`children` приведены к согласованному виду;
   - для БД добавлена миграция [`20260313_01_add_item_category_link.py`](backend/alembic/versions/20260313_01_add_item_category_link.py).

2) **Синхронизация номенклатуры начала сохранять товарную группу прямо в `items`:**
   - в [`sync_nomenclature_from_odata()`](backend/app/services/nomenclature_sync.py) после разрешения `КатегорияНоменклатуры_Key` запись [`Item`](backend/app/models.py) теперь получает связь `category`/`category_id`;
   - это касается как обычного upsert по `item_ref1c`, так и fallback-сценария добивки отсутствующих `item_code`.

3) **Добавлены backend-service методы grouped-выдачи по товарным группам:**
   - [`_load_item_category_meta()`](backend/app/services/planning_service.py) загружает метаданные товарной группы для набора изделий;
   - [`get_run_purchases_grouped_by_category()`](backend/app/services/planning_service.py) группирует закупки по `Item.category_id`;
   - [`get_run_rework_grouped_by_category()`](backend/app/services/planning_service.py) группирует заказы на переработку по той же схеме.

4) **Добавлены новые API-endpoint'ы grouped-представления по товарным группам:**
   - [`GET /v1/plan/results/{run_id}/purchases/grouped-by-category`](backend/app/routers/plan.py);
   - [`GET /v1/plan/results/{run_id}/rework/grouped-by-category`](backend/app/routers/plan.py);
   - для них добавлены схемы [`PurchaseCategoryGroupedResponse`](backend/app/schemas.py), [`PurchaseCategoryGroup`](backend/app/schemas.py), [`PurchaseCategoryGroupOrder`](backend/app/schemas.py).

5) **Покрытие тестами:**
   - новый файл [`tests/services/test_grouped_by_category_results.py`](tests/services/test_grouped_by_category_results.py) проверяет:
     - сохранение `item.category_id` при синхронизации номенклатуры;
     - группировку закупок по товарным группам;
     - группировку `rework` по товарным группам с корректным подсчётом `partial_orders` / `blocked_orders`.

6) **Проверка:**
   - выполнен прогон:
     - `set "PYTHONPATH=backend" && pytest tests/services/test_grouped_by_category_results.py tests/test_stock_by_item_cache.py tests/services/test_rework_results.py -q`
   - результат: `15 passed`.

**2026-03-12 — итерация 5 рефакторинга потоков пополнения: добавлены backend-endpoint'ы чтения `rework` и базовая grouped-выдача:**

1) **Расширены backend-результаты прогона для отдельного потока `rework`:**
   - в [`get_run_summary()`](backend/app/services/planning_service.py:357) добавлен счётчик `rework_requests` рядом с существующими `production_orders` и `purchase_requests`;
   - это позволяет UI и последующим API-итерациям видеть отдельный объём заявок на переработку без смешивания с закупкой.

2) **Добавлены сервисные выборки для `planned_rework`:**
   - в [`_query_run_rework_rows()`](backend/app/services/planning_service.py:1169) собрана единая денормализованная выборка строк `planned_rework` с номенклатурой, ЕИ и спецификацией;
   - в [`get_run_rework()`](backend/app/services/planning_service.py:1243) добавлены фильтрация, сортировка и пагинация для плоского списка заказов на переработку;
   - в [`get_run_rework_grouped()`](backend/app/services/planning_service.py:1305) добавлена grouped-выдача.

3) **Добавлены отдельные API-endpoint'ы для `rework`:**
   - [`GET /v1/plan/results/{run_id}/rework`](backend/app/routers/plan.py:638) возвращает список строк переработки;
   - [`GET /v1/plan/results/{run_id}/rework/grouped`](backend/app/routers/plan.py:668) возвращает grouped-представление.

4) **Зафиксирован временный fallback по товарным группам:**
   - текущая модель [`Item`](backend/app/models.py:36) пока не содержит явной связи с `item_categories`, достаточной для надёжной backend-группировки результатов по товарной группе;
   - поэтому [`get_run_rework_grouped()`](backend/app/services/planning_service.py:1305) на этой итерации возвращает один fallback-блок `Без товарной группы`, сохраняя уже полезный grouped-контракт для фронтенда и следующей итерации;
   - это сознательно не меняет существующие контракты `production` и `purchase`, а лишь добавляет новый безопасный контракт для `rework`.

5) **Добавлены backend-схемы ответа и тестовое покрытие:**
   - в [`backend/app/schemas.py`](backend/app/schemas.py:540) добавлены `ReworkGroupOrder`, `ReworkGroup`, `ReworkGroupedResponse`;
   - в [`tests/test_stock_by_item_cache.py`](tests/test_stock_by_item_cache.py:432) добавлены сценарии full / partial / blocked для создания `rework`-заказов;
   - новый файл [`tests/services/test_rework_results.py`](tests/services/test_rework_results.py:1) покрывает список `rework`, grouped-fallback и счётчик `rework_requests` в summary.

6) **Проверка:**
   - выполнен прогон:
     - `set "PYTHONPATH=backend" && pytest tests/test_stock_by_item_cache.py tests/services/test_rework_results.py -q`
   - результат: `12 passed`.

**2026-03-12 — итерация 4 рефакторинга потоков пополнения: введена backend-основа для отдельного потока `rework`:**

1) **Активирован отдельный поток классификации `rework`:**
   - в [`classify_replenishment_flow()`](backend/app/services/replenishment.py:19) маркеры `переработ` / `rework` теперь возвращают поток `rework`;
   - поток `purchase` остался отдельным, а все прочие значения по-прежнему попадают в `production`.

2) **Добавлена отдельная сущность результатов MRP для переработки:**
   - в [`PlannedRework`](backend/app/models.py:479) введена новая таблица `planned_rework`;
   - сохраняются поля количества и сроков (`requested_qty`, `planned_qty`, `qty`, `need_date`, `order_date`, `lead_time_days`), ссылка на спецификацию `spec_id`, а также диагностика дефицита комплектующих (`component_limit`, `component_blocked`, `component_partial`, `shortage`).

3) **Добавлена миграция и API-schema основа:**
   - миграция [`20260312_01_add_planned_rework.py`](backend/alembic/versions/20260312_01_add_planned_rework.py) создаёт таблицу `planned_rework` и индексы под будущие backend/API-выборки;
   - в [`backend/app/schemas.py`](backend/app/schemas.py:538) добавлены Pydantic-схемы `PlannedReworkBase` / `PlannedRework` для следующих итераций API.

4) **Поток `rework` интегрирован в расчёт без UI/API-расширений:**
   - в [`build_planned_orders_and_purchases()`](backend/app/services/planning_service.py:2102) ветка `rework` выделена отдельно от `production` и `purchase`;
   - `rework` использует существующий [`OrderQuantityCalculator.compute()`](backend/app/services/order_quantity_calculator.py:51), то есть уже наследует поддержку спецификаций и ограничений по комплектующим;
   - для результата `rework` сохраняются случаи:
     - полная блокировка по комплектующим → `planned_qty = 0` + warning `REWORK_COMPONENT_SHORTAGE_BLOCKED`;
     - частичное ограничение → частичный `planned_qty` + warning `REWORK_COMPONENT_SHORTAGE_PARTIAL`;
     - отсутствие дефицита → нормализованное количество через общий helper [`normalize_qty_for_item()`](backend/app/services/order_quantity_calculator.py:120).

5) **Границы текущей итерации зафиксированы:**
   - production-ветка и purchase-ветка не переводились на новые API-контракты;
   - UI, grouped-endpoint'ы и Excel-экспорт для `rework` пока не добавлялись;
   - следующий целевой шаг — отдельные backend-endpoint'ы чтения `planned_rework` и тесты на сценарии blocked/partial/full.

6) **Проверка:**
   - выполнен синтаксический прогон:
     - `python -m py_compile backend/app/models.py backend/app/schemas.py backend/app/services/replenishment.py backend/app/services/planning_service.py backend/alembic/versions/20260312_01_add_planned_rework.py`
   - результат: успешно.

**2026-03-12 — итерация 3 рефакторинга потоков пополнения: исправлен закупочный поток для дискретных ЕИ:**

1) **Поток закупки в [`build_planned_orders_and_purchases()`](backend/app/services/planning_service.py:2102) переведён на общий helper нормализации количества:**
   - ветка `purchase` больше не пишет `planned_qty` напрямую из сырого `requested_qty_raw`;
   - теперь используется [`normalize_qty_for_item()`](backend/app/services/order_quantity_calculator.py:120);
   - для дискретных ЕИ дробная часть отбрасывается, для недискретных ЕИ дробное значение сохраняется.

2) **Зафиксировано новое поведение сохранения закупки:**
   - в [`planned_purchase`](backend/app/models.py:462) поля `requested_qty`, `planned_qty`, `qty` сохраняются уже в нормализованном бизнес-количестве;
   - это устраняет генерацию дробных закупок для позиций в `шт` без изменения production-ветки.

3) **Добавлены интеграционные тесты на purchase-flow в [`tests/test_stock_by_item_cache.py`](tests/test_stock_by_item_cache.py:1):**
   - дискретная закупка (`шт`) с потребностью `7.9` сохраняется как `7.0`;
   - недискретная закупка (`кг`) с потребностью `7.9` сохраняется как `7.9`.

4) **Инвариант после итерации:**
   - production остаётся на уже вынесенном shared-normalization без изменения своей логики;
   - закупка теперь использует тот же слой нормализации количества, но без production-специфичных ограничений по комплектующим и мощностям;
   - следующий целевой шаг может безопасно переходить к выделению отдельной сущности/потока `rework`.

**2026-03-12 — итерация 1 рефакторинга потоков пополнения: вынесена классификация без изменения поведения:**

1) **Вынесен единый helper классификации потока пополнения:**
   - Добавлен модуль [`backend/app/services/replenishment.py`](backend/app/services/replenishment.py) с константами потоков и функциями:
     - `normalize_replenishment_method(...)`
     - `classify_replenishment_flow(...)`
     - `is_purchase_replenishment(...)`
   - Важно: на этой итерации сохранено прежнее поведение MRP:
     - маркеры закупки (`покуп`, `закуп`, `purchase`, `buy`) → поток `purchase`;
     - все остальные значения, включая `переработка`, пока остаются в потоке `production`.

2) **Планирование переведено на shared-classifier без смены бизнес-результата:**
   - В [`build_planned_orders_and_purchases()`](backend/app/services/planning_service.py:2098) удалена локальная inline-логика разбора `items.replenishment_method`.
   - Вместо неё используется общий классификатор из [`replenishment.py`](backend/app/services/replenishment.py).
   - Это подготавливает безопасную основу для следующей итерации, где `rework` будет выделяться в отдельный поток.

3) **Добавлены тесты на классификацию и интеграцию с planning:**
   - Новый файл [`tests/services/test_replenishment.py`](tests/services/test_replenishment.py) покрывает:
     - распознавание синонимов закупки;
     - сохранение legacy-default поведения для `production`;
     - интеграцию классификатора с [`build_planned_orders_and_purchases()`](backend/app/services/planning_service.py:2098).

4) **Проверки:**
   - Выполнен прогон:
     - `set "PYTHONPATH=backend" && pytest tests/test_stock_by_item_cache.py tests/services/test_replenishment.py -q`
   - Результат: **7 passed**.

5) **Зафиксирован текущий инвариант для следующих итераций:**
    - поток `production` пока остаётся эталонным и не должен менять результат из-за выноса классификатора;
    - отдельная активация `rework` как нового потока должна делаться только в следующей целевой итерации с отдельными тестами и API/DB-изменениями.

**2026-03-12 — итерация 2 рефакторинга потоков пополнения: вынесена общая нормализация количества без смены бизнес-поведения:**

1) **В [`OrderQuantityCalculator`](backend/app/services/order_quantity_calculator.py:8) добавлен общий слой нормализации количества:**
   - добавлены helper-методы:
     - `is_discrete_item(...)`
     - `normalize_qty_for_item(...)`
   - зафиксировано текущее правило итерации:
     - для дискретных ЕИ количество приводится вниз к целому;
     - для недискретных ЕИ дробная часть сохраняется.

2) **Production-ветка [`build_planned_orders_and_purchases()`](backend/app/services/planning_service.py:2102) переведена на shared-normalization helper:**
   - удалена локальная дублирующая эвристика дискретности из [`planning_service.py`](backend/app/services/planning_service.py:2102);
   - нормализация `requested_qty` и итогового `planned_qty` теперь делается через общий helper калькулятора;
   - бизнес-поведение production сохранено: изменён только источник общей логики, а не сами правила округления.

3) **Добавлены первые тесты на общий слой нормализации в [`tests/services/test_order_quantity_calculator.py`](tests/services/test_order_quantity_calculator.py:1):**
   - проверка, что дискретная ЕИ (`шт`) отбрасывает дробную часть;
   - проверка, что недискретная ЕИ (`кг`) сохраняет дробные значения;
   - дополнительно закреплено поведение [`OrderQuantityCalculator.compute()`](backend/app/services/order_quantity_calculator.py:51) для дробного горизонта спроса у недискретных ЕИ.

4) **Инвариант после итерации:**
   - закупка всё ещё не переведена на общий helper и пока сохраняет legacy-поведение с `requested_qty_raw`;
   - это сделано намеренно, чтобы следующая итерация отдельно исправила закупочный поток и доказала изменение тестами без риска регрессии production.

**2026-02-27 — точечные фиксы по ревью main (без изменения API-контрактов):**

1) **Безопасность (секреты):**
   - Удалены захардкоженные креды из исследовательских скриптов:
     - `test_order_zsnf-000943.py`
     - `backend/test_order_zsnf-000943.py`
     - `test_compare.py`
     - `backend/test_compare.py`
     - дополнительно: `backend/test_register_balance.py`, `backend/test_register_single.py`.
   - Скрипты переведены на переменные окружения `ODATA_USERNAME` / `ODATA_PASSWORD`.
   - В `order_zsnf-000943_report.md` реальные учётные данные заменены на безопасный плейсхолдер.

2) **Синхронизация факта выпуска (`sync_production_fact_from_odata`):**
   - Убрано тихое отсечение данных:
     - удалён лимит `order_keys[:200]`;
     - удалён лимит `assembly_keys[:50]`.
   - Сохранена устойчивость через поэтапную обработку и прогресс-логирование.
   - Контракт возвращаемой статистики сохранён.

3) **Фикс регрессии в `planning_service.py`:**
   - Восстановлена корректная реализация `_read_last_stock_sync_at()` для чтения `config/last_sync_time.json`.
   - Удалён недостижимый фрагмент кода после `return dict(reserved_by_component), warnings`.

4) **Фильтрация заказов (`Posted`):**
   - В `sync_production_orders_from_odata()` поле `Posted` добавлено в обязательные поля `effective_select_fields`.

5) **Проверки:**
   - `python -m pytest tests/test_stock_by_item_cache.py -q` → `4 passed`.
   - Быстрая проверка синтаксиса:
     - `python -m py_compile backend/app/services/production_order_sync.py backend/app/services/planning_service.py` → успешно.

**2026-02-27 — внедрён учёт активных заказов 1С в MRP (A+B, рекурсивный резерв компонентов):**

Реализованы изменения в расчёте планирования с защитой от перепроизводства:

1) **Учёт A (как уже запланированный выпуск):**
   - В [`build_planned_orders_and_purchases()`](backend/app/services/planning_service.py:2070) добавлен вычет активного `remaining_qty` заказов 1С из производственной потребности по `item_id`:
     - `requested_qty_adj = max(requested_qty - active_remaining_qty, 0)`
   - Если после вычета потребность `<= 0`, заказ на производство не создаётся.

2) **Учёт B (занятие компонентов активными заказами 1С):**
   - В [`run_planning_run()`](backend/app/services/planning_service.py:2560) добавлен расчёт `reserved_by_component` через рекурсивный взрыв BOM от `remaining_qty` активных заказов 1С.
   - Перед созданием [`OrderQuantityCalculator`](backend/app/services/order_quantity_calculator.py:8) строится `effective_stock_by_item`:
     - `effective_stock = max(stock - reserved, 0)`
   - В калькулятор передаётся уже скорректированный склад (`stock_by_item=effective_stock_by_item`).

3) **Фильтр активных заказов 1С в расчёте A/B:**
   - Используется правило:
     - `production_orders.deletion_mark = false`
     - `lower(order_state_key) != DONE_STATE_KEY`
     - `production_products.remaining_qty > 0`
   - Константа `DONE_STATE_KEY` зафиксирована в [`planning_service.py`](backend/app/services/planning_service.py:72).

4) **Безопасность рекурсии и защита от циклов BOM:**
   - Добавлена функция [`_build_component_reservations_from_active_1c()`](backend/app/services/planning_service.py:1554) с:
     - ограничением глубины (`planning.limits.max_bom_depth`, fallback 200),
     - защитой от циклов по пути обхода,
     - предупреждением `ACTIVE_1C_BOM_CYCLE_SKIPPED` при пропуске циклического ребра.

5) **Тесты:**
   - Расширен файл [`tests/test_stock_by_item_cache.py`](tests/test_stock_by_item_cache.py:1):
     - проверка вычета A из потребности;
     - проверка фильтра активных заказов 1С для агрегата `remaining_qty`;
     - проверка рекурсивного резерва B и защиты от циклов;
     - сохранён существующий кейс про `stock_by_item` и блокировку по компонентам.
   - Прогон: `python -m pytest tests/test_stock_by_item_cache.py -q` → **4 passed**.

6) **План валидации на реальных данных (контроль против перепроизводства):**
   - Выполнить baseline-прогон MRP до включения изменений и сохранить:
     - `planned_order` по изделиям,
     - предупреждения по компонентам.
   - После изменений повторить прогон на тех же входных данных и сравнить:
     - для изделий с активными заказами 1С: снижение/обнуление `requested_qty` в пределах суммы `remaining_qty`;
     - для компонент: рост предупреждений дефицита только там, где действительно есть резерв под активные заказы 1С;
     - отсутствие строк с `qty <= 0`.
   - Отдельно проверить контрольный кейс с частично выполненным заказом 1С (`remaining_qty > 0`) и убедиться, что объём нового выпуска в PRODPLAN не дублирует уже покрываемый объём 1С.

**2026-02-26 — учёт выполнения заказов 1С (Сборка запасов) + улучшенный Excel-экспорт:**

Реализовано полное отслеживание выполнения деталей по заказам на производство:

1) **Модель БД** ([`models.py`](backend/app/models.py:168)):
   - Добавлены поля в `ProductionProduct`:
     - `produced_qty` — фактически выпущенное количество
     - `remaining_qty` — остаток к выпуску (`quantity - produced_qty`)

2) **Миграция Alembic** ([`20260226_01_add_produced_and_remaining_qty.py`](backend/alembic/versions/20260226_01_add_produced_and_remaining_qty.py)):
   - Добавляет два поля в `production_products`
   - Инициализирует `remaining_qty = quantity` (т.к. `produced_qty = 0`)

3) **Синхронизация факта выпуска** ([`production_order_sync.py`](backend/app/services/production_order_sync.py:489)):
   - Новая функция `sync_production_fact_from_odata()`
   - Загружает `Document_СборкаЗапасов` и `Document_СборкаЗапасов_Продукция`
   - Агрегирует `produced_qty` по `(order_ref1c, line_number, item_id, characteristic)`
   - Обновляет `produced_qty` и `remaining_qty` в БД
   - Фильтр: только `Posted == true` и `DeletionMark == false`
   - Автоматически вызывается после `sync_production_orders_from_odata()`

4) **API для факта** ([`sync.py`](backend/app/routers/sync.py:181)):
   - Новый эндпоинт `POST /v1/sync/production-orders-fact-odata`
   - Отдельный запуск синхронизации факта (опционально)

5) **Excel-экспорт** ([`production_order_export.py`](backend/app/services/production_order_export.py:31)):
   - **Группировка по заказам**: заказ = подзаголовок (синий фон, белый текст)
   - **Детали заказа**: строки с чередованием цветов (zebra striping)
   - **Колонки**: Номенклатура, Артикул, Характеристика, ЕИ, Заказано, Выполнено, Осталось
   - Форматирование: границы, авто-ширина, слияние ячеек для подзаголовка

6) **Исправления** (дополнительно):
   - **Улучшен маппинг строк**: поиск `ProductionProduct` по приоритетам:
     1) `(order_id, line_number)` — строгий матч
     2) `(order_id, item_id, characteristic_ref1c)` — fallback
     3) `(order_id, item_id)` — последний fallback
   - **Правило "отсутствует = закрыт"**: заказы, которых нет в загрузке 1С, помечаются `deletion_mark = True`
   - **Логирование**: детальные логи для отладки маппинга строк продукции

**Результат:**
- При синхронизации заказов автоматически загружается факт выполнения из 1С
- Excel-файл показывает актуальное состояние: сколько заказано, выполнено, осталось
- Визуально заказы сгруппированы с выделением цветом
- Заказы, удалённые из 1С, автоматически закрываются в PRODPLAN

---

2026-02-26 — точечный фикс импорта производственных заказов 1С: устранена инверсия фильтра «активных» заказов в [`sync_production_orders_from_odata()`](backend/app/services/production_order_sync.py:86):

- В OData-фильтр зафиксировано правило активного заказа: `DeletionMark eq false and (СостояниеЗаказа_Key ne guid'<DONE_STATE_KEY>')`.
- Усилена защита от некорректного `select_fields`: в запрос принудительно добавляются поля `DeletionMark` и `СостояниеЗаказа_Key`, даже если пришёл кастомный набор полей.
- Сохранён второй слой защиты на уровне приложения (post-filter) перед записью в БД.
- Добавлена отдельная диагностика для записей без критичных полей фильтрации (`missing filter fields`) — такие записи исключаются из обработки.
- Локальная проверка синтаксиса: `python -m py_compile backend/app/services/production_order_sync.py` — успешно.

2026-02-26 — дополнительный фикс после диагностики «загружено 93, отфильтровано DeletionMark=true: 93»:

- По логам backend выявлено, что поле `DeletionMark` может приходить в нестандартном скалярном формате (не только `bool`/`str`).
- В [`_parse_1c_bool()`](backend/app/services/production_order_sync.py:49) добавлена безопасная обработка:
  - словарных обёрток (`{"value": ...}`, `{"Value": ...}` и синонимы);
  - числовых значений `0/1` как bool;
  - строгий fallback в `default` вместо `bool(val)`, чтобы избежать ложных `True`.
- Цель: исключить ложную интерпретацию `DeletionMark` как `true` и восстановить корректную загрузку активных заказов.
- Локальная проверка синтаксиса: `python -m py_compile backend/app/services/production_order_sync.py` — успешно.

2026-02-19 — исправлена фильтрация «активных» заказов на производство 1С (удалённые/завершённые больше не должны попадать в синх и экспорт):

- В [`sync_production_orders_from_odata()`](backend/app/services/production_order_sync.py:60):
  - добавлена нормализация GUID состояния и защита от разных форматов (`guid'...'`, `{...}`, разный регистр);
  - расширена обработка bool для `DeletionMark` (в т.ч. «истина/ложь», «да/нет»);
  - добавлен второй слой фильтрации на уровне приложения: исключаем `DeletionMark==true` и `state==DONE_STATE_KEY` даже если 1С/прокси некорректно обработали `$filter`.
- В [`export_production_orders_xlsx()`](backend/app/services/production_order_export.py:18) добавлен фильтр исключения `order_state_key == DONE_STATE_KEY` поверх `deletion_mark == false`, чтобы из БД гарантированно выгружались только «не завершённые».

2026-02-13 — синхронизация производственных заказов 1С: ручной запуск + отчёт для доверия пользователей:

- Запросы к 1С по OData выполняются **только по явному действию пользователя** (никакого авто-обновления внутри MRP).
  Точка входа backend: `POST /api/v1/sync/production-orders-odata` через [`sync_production_orders_odata()`](backend/app/routers/sync.py:131), который вызывает загрузку OData в [`sync_production_orders_from_odata()`](backend/app/services/production_order_sync.py:31).
- UX на странице «Синхронизация»: требуется две кнопки:
  1) «Синхронизировать» — выполняет OData → БД.
  2) «Синхронизировать и скачать отчёт» — выполняет OData → БД и затем формирует Excel (XLSX) как документирование выгрузки.
- Формат отчёта (Excel): **один лист**, строка = «заказ + позиция (деталь)». Для каждой позиции нужно выводить минимум:
  - реквизиты заказа (номер/дата/состояние),
  - номенклатура и (если есть) характеристика,
  - `ordered_qty`, `produced_qty`, `remaining_qty` (для частично выполненных), где `remaining_qty = max(ordered_qty - produced_qty, 0)`.

2026-02-09 — диагностика и исправление переносов при re-run «закрытия дня» в недельном отчёте:
- Расширена таблица `production_day_close_item` полями для детерминированного отката переноса (снапшот состояния плана на целевую дату):
  - `original_planned_qty_before_carry`
  - `planned_qty_after_carry`
  - `carry_status`
  Миграция: [`backend/alembic/versions/20260209_01_add_fields_to_production_day_close_item_for_carry_tracking.py`](backend/alembic/versions/20260209_01_add_fields_to_production_day_close_item_for_carry_tracking.py).
- В [`close_previous_workday()`](backend/app/services/production_report_service.py:278) изменён re-run rollback: вместо эвристики «planned_qty - carry» используется `original_planned_qty_before_carry` из `ProductionDayCloseItem`.
- В [`get_week_report()`](backend/app/services/production_report_service.py:50) добавлены диагностические поля для UI:
  - `days[].closed_planned/closed_fact/carry_qty` (агрегация по дню закрытия)
  - `rows[].carry_by_day/closed_plan_by_day/closed_fact_by_day` (по изделию и дню)
- UI недельного отчёта дополнен отображением диагностических данных в шапке и ячейках: [`ProductionReportWeekPage.vue`](frontend/src/pages/ProductionReportWeekPage.vue:1).
- Тесты фиксируют идемпотентность re-run переноса: [`tests/services/test_production_report_day_close.py`](tests/services/test_production_report_day_close.py:1).

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

2026-02-12 — «окно плана от первого не закрытого дня» (якорь по закрытиям дня)

- Добавлен расчёт якорной даты окна плана (первый **не закрытый** рабочий день) в backend:
  - [`get_planning_anchor_date()`](backend/app/services/production_report_service.py:21)
  - API: `GET /api/v1/plan/anchor` ([`get_planning_anchor()`](backend/app/routers/plan.py:268))
  - Семантика: `anchor_date = next_workday(max_closed_date)`; если закрытий нет → `anchor_date = previous_workday(today)`.

- Квартальный план теперь якорится на `anchor_date` и показывает только диапазон от `anchor_date` до конца квартала:
  - загрузка якоря: [`getPlanningAnchor()`](frontend/src/services/api.ts:401)
  - применение в UI: [`loadPlanData()`](frontend/src/pages/PlanQuarterlyPage.vue:549)
  - удаление строки плана (`delete_row`) выполняется в пределах текущего отображаемого окна.

- Добавлены unit-тесты на вычисление якоря:
  - [`test_planning_anchor.py`](tests/services/test_planning_anchor.py:1)

- Примечание по тестам в docker: в backend image ранее не было pytest; добавлен в зависимости:
  - [`backend/requirements.txt`](backend/requirements.txt:1)

- Добавлен роут:
  [`frontend/src/router/index.ts`](frontend/src/router/index.ts:1)
  `'/plan/production-report/week'`

- Добавлен пункт меню в [`MainLayout`](frontend/src/layouts/MainLayout.vue:1).

- Добавлены API-обёртки на фронте:
  [`frontend/src/services/api.ts`](frontend/src/services/api.ts:340)

Примечание: в проекте отсутствует ESLint-конфиг (npm script `lint` падает по этой причине), поэтому автопроверка фронта ограничена.

2026-02-25 — недельный отчёт: выбор закрываемой даты, навигация по неделям, подсветка закрытых дат

- Backend/API:
  - В запрос закрытия дня добавлен параметр `close_date` в [`ProductionReportDayCloseRequest`](backend/app/routers/plan.py:131).
  - В [`close_production_report_day()`](backend/app/routers/plan.py:248) добавлен разбор `close_date` и передача в сервис.
  - В bulk-upsert факта добавлен `rerun_editable_date` в [`ProductionReportFactBulkUpsertRequest`](backend/app/routers/plan.py:127).
  - В [`bulk_upsert_production_report_fact()`](backend/app/routers/plan.py:223) добавана передача `rerun_editable_date` в сервис.
  - В [`bulk_upsert_fact()`](backend/app/services/production_report_service.py:277) добавлен параметр `rerun_editable_date`, чтобы разрешать re-run только для явно выбранной даты.
  - В [`close_previous_workday()`](backend/app/services/production_report_service.py:350) добавлен параметр `close_date_override`:
    - запрещено закрытие нерабочего дня;
    - запрещено закрытие даты позже допустимой (`previous_workday(today)`), чтобы не закрывать «будущие» дни;
    - сохранено правило последовательности закрытий без пропуска рабочих дней.
  - Семантика `target_date` сохранена прежней: `next_workday(next_workday(today))`.

- Frontend:
  - В [`ProductionReportWeekPage.vue`](frontend/src/pages/ProductionReportWeekPage.vue:1):
    - добавлено поле «Закрываемая дата» (`selectedCloseDate`);
    - кнопка «Загрузить» и навигация `Пред./След. неделя` работают без принудительного автопрыжка на неделю `close_hint`;
    - при сохранении факта передаётся `rerun_editable_date`;
    - при закрытии дня в API передаётся выбранная `close_date`;
    - re-run редактирование разрешено только для выбранной даты;
    - добавлена визуальная подсветка закрытых дней в заголовках и ячейках (`closed-day-header`, `closed-day-cell`).
  - В [`api.ts`](frontend/src/services/api.ts:385) расширены типы запросов:
    - `bulkUpsertProductionReportFact(..., rerun_editable_date?)`;
    - `closeProductionReportDay(..., close_date?)`.

- Тесты:
  - Расширен набор в [`test_production_report_day_close.py`](tests/services/test_production_report_day_close.py:1):
    - разрешение сохранения факта в closed day только при совпадении с `rerun_editable_date`;
    - закрытие по явно выбранной дате;
    - запрет закрытия нерабочей даты.
  - Прогон: `set "PYTHONPATH=." && pytest tests/services/test_production_report_day_close.py` — 8 passed.
