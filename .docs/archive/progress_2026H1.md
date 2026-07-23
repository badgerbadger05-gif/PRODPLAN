# Дневник прогресса — январь–июнь 2026 (архив)

Вырезано из `.docs/progress.md` 2026-07-23. Записи в исходном порядке (сначала обратная хронология июнь→январь, в конце — ранние блоки февраль–май).

## Последняя сессия

**2026-06-26 — расценка в `Document_СдельныйНаряд` (`PW*`) через регистр цен:**

- После включения автоматического REST-интерфейса 1С в OData появился
  `InformationRegister_ЦеныНоменклатуры`.
- PRODPLAN теперь перед записью/проведением сдельного наряда запрашивает
  `InformationRegister_ЦеныНоменклатуры/SliceLast(...)` по `Операция_Key` и
  `ВидЦен_Key`, записывает `Операции.Расценка` и
  `Операции.Стоимость = КоличествоФакт * Цена`.
- Важно: цену надо искать по операции, потому что операция в этой базе тоже
  `Catalog_Номенклатура`. Для примера `PW001309087` операция возвращает
  `Цена=10`, а изделие по тому же виду цены возвращает другую цену (`116`).
- Проверка: `pytest tests/services/test_one_c_piecework_export.py
  tests/services/test_operations_sync.py -q` — 18 passed.

**2026-06-24 — расследование пустой расценки в `Document_СдельныйНаряд` (`PW*`):**

- Подтверждено, что `PW001309087` создан и проведен через OData, но строка
  `Операции.Расценка` осталась `0`, несмотря на `ВидЦен_Key=Учетная цена`.
- На тот момент регистр цен еще не был опубликован в OData, поэтому рабочим
  обходным источником были исторические штатные строки
  `Document_СдельныйНаряд_Операции` (`ЗСНФ*`). После обновления 2026-06-26
  это только аварийный фолбэк; основной источник описан выше.

**2026-06-11 — журнал закупок + читаемость журнала исполнения (ветка `feature/purchase-journal`):**

- Новая страница «Журнал закупок» `/purchase-control`: строки заказов поставщику из 1С
  + незаказанные MRP-закупки (`to_order`) последнего FIXED_SNAPSHOT-прогона; действия
  «Заказать в 1С» (через `POST /v1/plan/results/{run_id}/purchases/export-to-1c`) и
  «Синхронизировать» (через `/v1/sync/supplier-orders-odata` с сохранённым OData-конфигом).
- Бэк: `purchase_control_journal.py` + роутер `/v1/purchase-control/{orders,orders/{id},filters}`;
  новых таблиц БД нет (read-модель). Тесты: `tests/services/test_purchase_control.py`.
- Журнал исполнения (период-план): новые колонки «Срок» и «Статус» (бэк добавил
  `need_date`/`status` в строки журнала), тултипы на заголовках, строки «покрыто складом»
  скрыты по умолчанию (чекбокс), счётчики сводки кликабельны (применяют фильтр),
  раскрытие work items переделано на подписанные значения; брутто/склад — в подсказке ячейки.
- Deep-link: закупка с заказом 1С ведёт из журнала исполнения в `/purchase-control?search=<номер>`.
- Планы: `.docs/purchase_journal_plan.md`, `.docs/execution_journal_ux_plan.md`.
- CI: 315 pytest, lint 0 ошибок, build ок. Playwright smoke в этом окружении не запускается
  (Chromium не ставится на ubuntu 26.04; порт 8000 занят посторонним контейнером) — проверить вручную.

**2026-06-01 — закрыты найденные разрывы по текущему TODO:**

- `Document_СдельныйНаряд` признан реализованным в документации: сервис
  `one_c_piecework_export.py`, endpoint
  `POST /v1/production-control/manufactures/export-piecework-to-1c`, вызов из
  flow «Произвести».
- Журнал заказов теперь отдаёт в API MRP-поля для period-plan требований:
  `source`, `source_mrp_allocation_key`, `mrp_req_net_qty`,
  `mrp_req_covered_qty`, `mrp_req_remaining_qty`.
- Кнопка «Досоздать» в карточке строки использует
  `/v1/production-control/orders/from-mrp-requirements`, а не legacy
  `/orders/from-mrp`.
- Deep-link из журнала исполнения Period Plan ведёт в журнал заказов или в
  нужную вкладку MRP-результата (`production`, `purchases`, `rework`) с
  подсветкой строки.
- После успешного `Document_СдельныйНаряд` PRODPLAN отправляет родительскому
  `Document_ЗаказНаПроизводство` состояние `Завершен`
  (`СостояниеЗаказа_Key=ad28565a-991b-11eb-e39a-fa163e61326a`) и обновляет
  локальный `ProductionOrder`.
- Проверка: `pytest tests/services/test_production_control.py
  tests/services/test_period_plan_service.py -q` — 42 passed; `npm run lint`
  и `npm run build` во `frontend-erp-shell` — успешно. Дополнительно для
  цепочки «Произвести»:
  `pytest tests/services/test_one_c_piecework_export.py tests/services/test_produce_and_manufacture_export.py tests/services/test_production_control.py -q`
  — 52 passed.

**2026-05-26 — закрыто правило первичных документов из MRP:**

- `Document_ЗаказНаПроизводство`, который PRODPLAN создаёт из MRP, является первичным документом и вводится без 1С-основания.
- `Document_ЗаказПоставщику`, который PRODPLAN создаёт из MRP-потребности, также является первичным документом и вводится без 1С-основания.
- Правило `ДокументОснование` + `ДокументОснование_Type` остаётся обязательным для дочерних документов: перемещений, выпуска/сборки и сдельного наряда.

**2026-05-26 — добавлена синхронизация сотрудников 1С:**

- Новый справочник `employees` хранит `Ref_Key`, код, имя, `DeletionMark` и `DataVersion` из `Catalog_Сотрудники`.
- Добавлены сервис `employee_sync.py`, endpoint `POST /api/v1/sync/employees-odata` и миграция `20260526_01_add_employees`.
- 2026-05-27: синхронизация добавлена в интерфейс `/sync` как кнопка «Сотрудники» и включена в полную синхронизацию.
- Синхронизация сотрудников нужна перед созданием `Document_СдельныйНаряд`, чтобы исполнитель и состав бригады заполнялись локально известными `Ref_Key`.

**2026-05-25 — зафиксировано правило создания документов 1С “на основании”:**

- На демо-базе `unf_demo` подтверждено, что нормальные `Document_СдельныйНаряд` имеют `ДокументОснование_Type=StandardODATA.Document_СборкаЗапасов`.
- Создан probe `PW25052503`, `Ref_Key=724e3322-583a-11f1-91eb-9ee51454587f`, с основанием `Document_СборкаЗапасов`.
- В `one_c_stock_transfer_export.py` и `one_c_manufacture_export.py` добавлено заполнение `ДокументОснование=<Ref_Key заказа на производство>` и `ДокументОснование_Type=StandardODATA.Document_ЗаказНаПроизводство`.
- Для будущего `Document_СдельныйНаряд` правило: создавать на основании `Document_СборкаЗапасов`.

**2026-05-25 — завершена реализация всех групп workplan.md (кроме Document_СдельныйНаряд и formatter policy):**

Группа 3 — Остатки по складам:
- `ItemWarehouseStock` и синхронизация (`odata_stock_sync.py`) уже существовали
- Реализован `_auto_select_source_warehouse()` в `production_control_material_issues.py`: выбирает склад-источник по `ItemWarehouseStock`, исключает `ignored_warehouses` и deselected склады
- При неоднозначности (тай) фронт показывает диалог с radio-кнопками; повторный вызов API с `source_warehouse_ref1c`
- `MaterialIssueCreatePayload` расширен полем `source_warehouse_ref1c`

Группа 4 — Факт поступления заказов поставщику:
- Синхронизация `received_qty` уже была реализована в `supplier_order_sync.py` (поле `КоличествоПоступило`)
- Добавлен `supplier_covered_qty` = `requested_qty - qty` в API `get_run_purchases` (`planning_service.py`)
- В таблице MRP-закупок (`MrpResultPage.tsx`) добавлена колонка «Покрыто» с % и цветовой индикацией

Группа 5 — Мелкие доработки period plan:
- Автогенерация `comment` при создании плана: «МАЙ 2026» / «МАЙ–ИЮНЬ 2026»
- Пояснительный текст в SyncPage для пустого списка групп с сохранённым выбором

Группа 6 — Технический долг:
- Удалён compatibility facade `production_control.py` (нет внешних импортов)
- Матрица CI-команд добавлена в `.docs/README.md`

Проверка: **148 passed**, lint — 0 ошибок, build — успешно.

---

**2026-05-25 — исправлены API-рассогласования фронта с бэкендом в производственном контроле:**

Ревью кодовой базы показало, что все backend write-сервисы (`one_c_production_order_export`, `one_c_stock_transfer_export`, `one_c_manufacture_export`, `one_c_posted_transfer_sync`) уже реализованы. Фронт при этом вызывал несуществующие эндпоинты и отправлял неправильные поля.

Исправлено в `ProductionControlPage.tsx` и `ProductionCommandBar.tsx`:
- `start-in-1c` → удалена дублирующая кнопка; единая «Запустить в 1С» теперь вызывает правильный `POST /orders/export-to-1c` с `order_ids` (ранее слал `product_ids`)
- `sync-from-1c` → `POST /sync-posted-transfers` (правильный эндпоинт для `К перемещению → Собран`)
- `produce-to-1c` → 2-шаговый flow: `POST /orders/{id}/produce` + `POST /manufactures/export-to-1c`

Добавлено:
- `order_id`, `order_source`, `order_ref1c` в тип `OrderRow` (бэкенд уже возвращал их)
- Функция `update_product_quantity()` в `production_control_journal.py`
- Эндпоинт `PATCH /orders/{product_id}/quantity` (использует `ProductionDetailPane`)
- Lint-фикс `PeriodPlanPage.tsx` (unused `wi` parameter)

Проверка: **147 passed**, lint — 0 ошибок, build — успешно.

**2026-05-25 — закрыты три накопившихся разрыва в реализации period-plan → production-control:**

### 1. Исправлен разрыв ORM / миграция в `ProductionProduct`

Миграция `20260522_06` добавила в `production_products` два столбца —
`source_mrp_requirement_id` (FK → `mrp_requirement.id ON DELETE SET NULL`) и
`source_mrp_allocation_key` — но ORM-класс `ProductionProduct` в `models.py`
этих полей не содержал. Это приводило к `AttributeError` при обращении к
`ProductionProduct.source_mrp_requirement_id.in_(...)` в execution journal.

Фикс: оба столбца добавлены в класс `ProductionProduct` (`models.py`).

### 2. Функция `create_production_orders_from_mrp_requirements`

Файл: `backend/app/services/production_control_journal.py`

Прежде журнал производственного контроля умел создавать заказы только из
`PlannedOrder` (legacy MRP). Period-plan MRP создаёт `MrpRequirement`-строки,
и до этой сессии пути от «MRP-снимок period-плана» к «производственный заказ»
не существовало.

Новая функция:
- принимает список `requirement_ids`;
- пропускает строки с `flow != production` и с `remaining_qty ≈ 0`;
- идемпотентна через `ProductionProduct.source_mrp_requirement_id`;
- создаёт `ProductionOrder(source='mrp')` + `ProductionProduct` +
  `ProductionOrderLineState(status='shortage')`;
- обновляет `MrpRequirement.covered_qty` / `remaining_qty`;
- возвращает `{status, created, reused, skipped, errors}`.

Эндпоинт: `POST /v1/production-control/orders/from-mrp-requirements`
(файл `backend/app/routers/production_control.py`, payload
`OrdersFromMrpRequirementsPayload`).

### 3. Исправлен lookup `work_items` в `get_period_plan_execution_journal`

Файл: `backend/app/services/period_plan_service.py`

Ранее запрос закупок (PlannedPurchase) был переписан на двойной словарь:
- `purchases_by_req_id` — строки с `source_mrp_requirement_id` (точный lookup);
- `purchases_by_item_fallback` — строки без него (legacy).

Но строка, которая назначает `work_items` в цикле по строкам журнала, по-прежнему
обращалась к несуществующей переменной `purchases_by_item` → `NameError` в
production. Исправлено:
```python
work_items = purchases_by_req_id.get(req_id, []) or purchases_by_item_fallback.get(int(req.item_id), [])
```

### 4. Frontend: кнопка «Создать заказы производства»

- `frontend-erp-shell/src/services/periodPlan.ts` — добавлена функция
  `createProductionOrdersFromRequirements(requirementIds, initiatedBy)`.
- `frontend-erp-shell/src/ui/pages/PeriodPlanPage.tsx` — добавлен обработчик
  `handleCreateProductionOrders()` и кнопка «Создать заказы производства» в
  commandBar вкладки «Журнал исполнения». Кнопка активна только если в журнале
  есть production-строки с `remaining_qty > 0`. После успеха показывает счётчик
  created/reused/skipped и перезагружает журнал.

### 5. Тесты и коммиты

- `tests/services/test_period_plan_service.py` (15 тестов) — покрытие логики
  аллокации `PlannedPurchase` с нетингом заказов поставщику.
- Вся цепочка: 146 passed, TypeScript — 0 ошибок.
- Коммиты:
  - `6602840` — feat: net PlannedPurchase against active supplier orders
    (миграция `20260522_08`, хелпер `_load_purchase_supplier_remaining`,
    нетинг в `create_mrp_snapshot_from_period_plan`)
  - `f3a039a` — feat: materialize MRP requirements into production orders +
    fix ORM/journal gaps (три разрыва выше + frontend)

---

**2026-05-25 — Period Plan в ERP-shell доведён до целевой логики:**

1) Бэкенд (`backend/app/routers/plan.py`, `backend/app/services/period_plan_service.py`):
   - `DELETE /v1/plan/period-plans/{id}` (нельзя если есть SUCCESS MRP-прогоны по плану),
   - `DELETE /v1/plan/period-plans/{id}/items/{item_id}` (удаление строки матрицы; блокируется при locked-ячейках),
   - `POST /v1/plan/period-plans/{id}/unarchive`, `PATCH /v1/plan/period-plans/{id}` (rename / период / comment — только draft),
   - `GET /v1/plan/period-plans/{id}/runs` — история MRP-прогонов по `source_plan_id`,
   - `GET /v1/plan/period-plans` — фильтры `status`/`period_from`/`period_to`/`created_by`, сортировка `sort_by`/`sort_dir`.

2) Фронтенд (`frontend-erp-shell/src/ui/pages/PeriodPlanPage.tsx`):
   - Полноценное окно вместо split/модалки — клик по строке плана заменяет список на детальное окно (паттерн `MrpRunsPage → MrpResultPage`).
   - Список: фильтры по статусу/периоду/автору, сортировка по колонкам, дополнительные колонки `comment` / `created_by` / `created_at` / `line_count`, архивные планы тусклые.
   - Создание плана: поле `comment`, валидация `period_to ≥ period_from`, подсказка о пятницах, автофокус на названии.
   - Подбор номенклатуры в матрице — search-as-you-type через `/v1/nomenclature/search`, выпадающий список с подсветкой, навигация `↑/↓`, выбор по `Enter`, автофокус на инпуте при открытии черновика.
   - Матрица: Tab/Enter/стрелки навигация между ячейками, двойной клик копирует значение по строке, кнопка `≡` равномерно разносит итого по доступным неделям, итоги по колонкам в `<tfoot>`, locked-ячейки помечены штриховкой, dirty-ячейки подсвечены жёлтым.
   - Журнал: дропдаун выбора прогона (MRP-runs), фильтры по `BOM level` / покрытию, сортируемые колонки, экспорт в CSV.
   - Шапка плана редактируется в режиме «Изменить шапку» (для draft).
   - Хоткеи: Esc возвращает к списку, F5 обновляет, Enter в списке открывает выбранный план.

3) Инфраструктура:
   - `frontend-erp-shell/nginx.conf` — `proxy_pass http://backend:8000;` без trailing slash (бэкенд монтирует `/api` префикс).
   - `frontend-erp-shell/Dockerfile` и `nginx.conf` — UTF-8 без BOM, иначе nginx падает на парсинге.
   - `backend/.dockerignore` — `!requirements.txt` исключение из общего `*.txt`.
   - Бэкенд-контейнер вручную подключён к двум сетям (`prodplan-erp_default` и legacy `prodplan_default`), чтобы достучаться до populated DB `prodplan-db-1`. Скрипт миграции — `scripts/migrate_legacy_db.sh`, комментарий с инструкциями — в `docker-compose.yml`. После миграции legacy-стек можно потушить.

4) Контракт фичи зафиксирован в [`.docs/period_plan_target.md`](.docs/period_plan_target.md).

5) Проверка: `npm run build` во `frontend-erp-shell` — успешно; ручной smoke в браузере на `http://localhost:9000` — список открывается, фильтры работают, создание плана с автооткрытием, search-as-you-type, добавление номенклатуры, удаление строки, фиксация, MRP-снимок, журнал с фильтрами и экспортом.

**2026-05-19 — исправлена выгрузка заказов поставщикам и добавлены галочки выбора строк для выгрузок MRP:**

1) Закупки:
   - API закупок теперь возвращает `source_purchase_ids` для агрегированных строк;
   - `POST /api/v1/plan/results/{run_id}/purchases/export-to-1c` принимает `purchase_ids` и выгружает в 1С только выбранные строки;
   - во вкладке закупок добавлены чекбоксы, кнопка `В 1С` показывает количество выбранных строк и не запускается без выбора.
   - исправлена запись строк заказа в 1С: табличная часть `Запасы` теперь передается вложенной коллекцией документа/через `PATCH` существующего пустого документа, так как прямой POST в `Document_ЗаказПоставщику_Запасы` 1С не поддерживает;
   - пустой GUID поставщика `00000000-0000-0000-0000-000000000000` считается отсутствующим поставщиком и попадает в `skipped_rows`;
   - фронт показывает текст ошибок 1С по проблемным заказам, а не только общие счетчики.

2) Производство:
   - API production-результатов и grouped-production теперь возвращают `source_order_ids`;
   - в верхних production-таблицах добавлены чекбоксы и счетчик выбранных заказов, чтобы будущая выгрузка/журнал заказов на производство использовали тот же механизм выбора.

3) Проверка:
   - `PYTHONPATH=backend pytest tests/services/test_one_c_purchase_order_export.py tests/routers/test_plan_result_endpoints.py -q` — 9 passed;
   - `npm run build` в `frontend` — успешно.

**2026-05-19 — добавлена выгрузка MRP-закупок в заказы поставщикам 1С:**

1) Номенклатура расширена поставщиком:
   - в `items` добавлено поле `supplier_ref1c` и миграция `20260519_01_add_item_supplier_ref1c.py`;
   - синхронизация `Catalog_Номенклатура` теперь читает `Поставщик_Key`;
   - при расчете `planned_purchase` существующая логика `supplier_ref1c=getattr(item, "supplier_ref1c", None)` получает реальное поле.

2) Добавлен backend-экспорт закупок в 1С:
   - новый endpoint `POST /api/v1/plan/results/{run_id}/purchases/export-to-1c`;
   - строки `planned_purchase` группируются по поставщику, на каждого поставщика создается отдельный `Document_ЗаказПоставщику`;
   - детали пишутся в `Document_ЗаказПоставщику_Запасы`;
   - строки без поставщика или `item_ref1c` не выгружаются и возвращаются в `skipped_rows`;
   - номер документа детерминированный (`PP...`) и перед созданием проверяется в 1С по `Number`, чтобы повторный клик не создавал дубль.

3) Frontend:
   - на вкладке закупок MRP добавлена кнопка `В 1С`;
   - кнопка вызывает новый endpoint с текущим диапазоном дат и показывает итог: создано, уже существовало, пропущено.

4) Проверка:
   - `PYTHONPATH=backend pytest tests/services/test_one_c_purchase_order_export.py tests/services/test_mrp_result_exports.py tests/routers/test_plan_result_endpoints.py -q` — 8 passed;
   - `npm run build` в `frontend` — успешно.

**2026-05-08 — уточнено правило статусов заказов поставщику для будущего учета в MRP:**

1) В [`.docs/supplier_orders_check.md`](.docs/supplier_orders_check.md) зафиксировано подтвержденное правило:
   - статус `"Новый заказ"` не считается заказанным и не уменьшает потребность;
   - статус `"Отменен"` не считается заказанным и не уменьшает потребность;
   - статус `"Завершен"` не считается заказанным и не уменьшает потребность;
   - удаленные заказы (`DeletionMark = true`) не учитываются;
   - остальные неудаленные статусы считаются заказанными и должны уменьшать будущие `planned_purchase` на остаток к поступлению.

2) Открытыми оставлены вопросы факта поступления, строк без даты, характеристик и точных OData-полей/Guid статусов.

**2026-05-08 — реализован первичный учет заказов поставщику в MRP:**

1) Расширены модели и синхронизация заказов поставщику:
   - добавлены состояние, `DeletionMark`, номер строки, характеристика, `received_qty`, `remaining_qty`;
   - исправлено использование `SupplierOrderItem.item_id`: теперь это id строки, а номенклатура хранится в `item_id_ref`;
   - синхронизация выставляет поставщика после `flush()` и корректно создает/обновляет строки.

2) В MRP добавлен учет уже размещенных заказов поставщику:
   - `_get_active_supplier_remaining_by_item_date()` выбирает неудаленные заказы со статусом не `"Новый заказ"`, не `"Отменен"` и не `"Завершен"`;
   - `build_planned_orders_and_purchases()` уменьшает только закупочные потребности (`planned_purchase`) на строки с `delivery_date <= need_date`;
   - покрытие списывается локально, чтобы один заказ поставщику не использовался дважды.

3) Добавлена миграция `20260508_01_extend_supplier_orders.py` и тесты:
   - `tests/test_stock_by_item_cache.py`;
   - `tests/services/test_supplier_order_sync.py`;
   - проверка: `pytest tests/services/test_supplier_order_sync.py tests/test_stock_by_item_cache.py tests/services/test_replenishment.py -q` — 18 passed.

**2026-05-07 — подготовлена документация для будущего учета заказов поставщику 1С в MRP:**

1) Добавлен документ [`.docs/supplier_orders_check.md`](.docs/supplier_orders_check.md):
   - зафиксирована аналогия с учетом активных заказов на производство;
   - для заказов поставщику описан только эффект "уже заказано, но еще не поступило" как уменьшение будущих `planned_purchase`;
   - отдельно отмечено, что компонентный резерв по BOM для заказов поставщику не нужен;
   - открытыми оставлены правила статусов, факта поступления, `DeletionMark`, `Posted`, строк без даты и характеристик.

2) В [`.docs/README.md`](.docs/README.md) добавлена ссылка на новую документацию.

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

2026-05-22 — React ERP-shell для переноса фронта

- Добавлен новый фронтенд-контур `frontend-erp-shell` от свежей `main` без удаления старого Quasar-фронта.
- Перенесены живые рабочие экраны: главная, журнал заказов, квартальный план выпуска, недельный отчет выпуска, MRP прогоны, результат MRP, синхронизация, ресурсы, распределение этапов и спецификации.
- В MRP результате добавлены реальные действия по выбранным строкам: создание заказов в журнале и выгрузка закупок в 1С.
- В журнал заказов добавлены фильтр по участку, настройки складов, выдача материалов, материалы, печать маршрутных и запуск в 1С.
- В ресурсах добавлены создание/сохранение участка и привязки видов производства; опасное удаление участков намеренно не перенесено.
- Проверка: `npm run build` в `frontend-erp-shell` — успешно; browser smoke-test ключевых экранов — без console errors.

---

## 2026-05-23 — hard split cleanup (next-erp)

- Legacy Quasar frontend (`frontend/`) полностью удален из ветки `next-erp`.
- Docker frontend service переведен на `frontend-erp-shell`.
- Добавлены `frontend-erp-shell/Dockerfile`, `frontend-erp-shell/nginx.conf`, `frontend-erp-shell/.dockerignore`.
- Обновлены базовые документы архитектуры и правил: `.docs/ai.md`, `.docs/architecture.md`, `.docs/frontend_erp_shell_migration.md`.
- Выполнена зачистка tracked временных артефактов и кэш-файлов (`.tmp/*`, `tmp/*`, `__pycache__`, root debug/log/json snapshots).
- Усилен `.gitignore` для предотвращения повторного засорения репозитория.
- Проверка: `npm run build` в `frontend-erp-shell` проходит успешно.

## 2026-05-23 — local test workflow and refactor cleanup

- Installed local Python 3.11 and created project `.venv` workflow.
- Added `pytest.ini` so the canonical backend suite is `tests/`, avoiding root diagnostic artifacts.
- Added `httpx` to backend test/runtime dependencies because FastAPI `TestClient` requires it.
- Added `scripts/test.ps1` and `scripts/test.sh` as repeatable backend/frontend verification entrypoints.
- Moved ad-hoc OData diagnostics from repo root and `backend/` into `tools/diagnostics/`; moved generated reports into `docs/reports/`.
- Removed hard-coded OData credentials from tracked config and added `config/odata_config.example.json`.
- Centralized OData config loading/saving in `backend/app/services/odata_config.py`.
- Migrated SQLAlchemy declarative base import and Pydantic schema config to current APIs, removing deprecation warning noise from the backend suite.
- Extracted common 1C export helpers (`payload_hash`, date formatting, demo URL guard, empty Ref_Key cleanup) into `backend/app/services/one_c_export_common.py`.
- Centralized 1C export `SyncLink` lookup/upsert helpers and OData client construction while keeping local module monkeypatch points for tests.
- Aligned `frontend-erp-shell/package.json` with the actual npm/package-lock workflow (`packageManager: npm@10.5.1`).
- Updated Docker Compose test ergonomics:
  - PostgreSQL host port defaults to `55432`.
  - backend container mounts `tests/` and `pytest.ini` for `docker compose run --rm --no-deps backend pytest -q`.
- Verification:
  - local `scripts/test.ps1` — 131 passed + frontend build passed;
  - `docker compose run --rm --no-deps backend pytest -q` — 131 passed;
  - direct `npm run build` in `frontend-erp-shell` — passed.

## 2026-05-24 — dependency split and frontend lint baseline

- Split backend Python dependencies:
  - `backend/requirements.txt` now contains runtime dependencies only;
  - `backend/requirements-dev.txt` includes runtime + pytest/httpx test dependencies.
- Updated `backend/Dockerfile` with `INSTALL_DEV` build arg; local `docker-compose.yml` enables it for backend test ergonomics.
- Updated `backend/.dockerignore` so both requirements files are included in backend image builds.
- Added frontend ESLint flat config in `frontend-erp-shell/eslint.config.js`.
- Added `npm run lint` to `frontend-erp-shell/package.json` and committed the npm lockfile changes.
- Updated `scripts/test.ps1` and `scripts/test.sh` to run backend pytest, frontend lint, and frontend production build.
- Cleaned one real lint issue: removed unused `dateRu` import from `ProductionPlanQuarterPage`.
- Verification:
  - local `.venv` pytest — 131 passed;
  - `npm run lint` — passes with 10 React Hooks `exhaustive-deps` warnings;
  - `npm run build` — passed;
  - `docker compose build backend` — passed;
  - `docker compose run --rm --no-deps backend pytest -q` — 131 passed;
  - `scripts/test.ps1` — passed end-to-end.

## 2026-05-24 — clean frontend lint baseline

- Stabilized React ERP-shell page data loaders with `useCallback` and explicit loader parameters.
- Preserved manual-load UX for filter/date fields by avoiding dependency arrays that auto-fetch on every input edit.
- Cleaned React Hooks `exhaustive-deps` warnings in:
  - `MrpResultPage`;
  - `MrpRunsPage`;
  - `PeriodPlanPage`;
  - `ProductionControlPage`;
  - `ProductionPlanQuarterPage`;
  - `ProductionReportWeekPage`;
  - `ResourcesPage`.
- Verification:
  - `npm run lint` — passed with zero warnings;
  - `npm run build` — passed;
  - local `.venv` pytest — 131 passed;
  - `scripts/test.ps1` — passed end-to-end.

## 2026-05-24 — 1C export write-loop consolidation

- Added `post_export_entries` to `backend/app/services/one_c_export_common.py`.
- Centralized the repeated real-write protocol for production order, material issue, and manufacture exports:
  - write planned `sync_link`;
  - POST to 1C;
  - validate returned `Ref_Key`;
  - write success/error `sync_link`;
  - commit once after the batch.
- Kept exporter-specific domain callbacks local:
  - production orders still stamp `production_orders.order_ref1c`;
  - material issues still update issue/export state and line state;
  - manufacture exports still update manufacture status/ref/error fields.
- Verification:
  - targeted 1C export tests — 23 passed;
  - local `.venv` pytest — 131 passed;
  - `scripts/test.ps1` — passed end-to-end.

## 2026-05-24 — React ERP-shell browser smoke

- Added Playwright test runner to `frontend-erp-shell`.
- Added `frontend-erp-shell/playwright.config.ts` with Chromium project and Vite webServer integration.
- Added `frontend-erp-shell/tests/smoke/app-smoke.spec.ts`.
- Smoke scenario checks:
  - backend health endpoint is available at `http://127.0.0.1:8000/health`;
  - shell opens on `http://127.0.0.1:9300`;
  - critical sections render via real sidebar navigation;
  - console errors and page errors fail the test.
- Added `npm run smoke`.
- Added opt-in smoke mode to project test scripts:
  - PowerShell: `scripts/test.ps1 -Smoke`;
  - Bash: `scripts/test.sh --smoke`.
- Ignored Playwright generated artifacts (`test-results/`, `playwright-report/`).
- Verification:
  - `npm run smoke` — 1 passed;
  - frontend `npm run lint` + `npm run smoke` — passed;
  - backend pytest — 131 passed.

## 2026-05-24 - production-control settings split

- Extracted warehouse settings service logic from `backend/app/services/production_control.py` into `backend/app/services/production_control_settings.py`.
- Kept backwards-compatible re-exports from `production_control.py` for existing tests/imports while moving new direct consumers to the dedicated module.
- Split settings HTTP endpoints from the large production-control router into `backend/app/routers/production_control_settings.py` and included it from the existing router to preserve all API paths.
- Reduced current production-control module sizes:
  - router: 309 lines;
  - service: 1387 lines;
  - new settings service: 114 lines;
  - new settings router: 63 lines.
- Verification:
  - `pytest tests/services/test_production_control.py -q` - 16 passed;
  - `.venv\Scripts\python.exe -m pytest -q` - 131 passed;
  - `scripts/test.ps1` - backend pytest, frontend lint, and frontend build passed;
  - direct router import confirmed all `/v1/production-control/settings*` routes are still registered.
- Note: importing `app.main` directly still depends on the active local Postgres credentials because `Base.metadata.create_all(bind=engine)` runs at import time.

## 2026-05-24 - production-control common and route-sheet split

- Extracted pure production-control helpers into `backend/app/services/production_control_common.py`:
  - GUID normalization/checking;
  - safe float conversion;
  - date parsing/ISO formatting;
  - production line number fallback.
- Extracted route-sheet printing/rendering into `backend/app/services/production_control_route_sheets.py`.
- Updated the production-control router to import route-sheet handlers from the dedicated module while keeping lazy compatibility wrappers in `production_control.py`.
- Reduced current production-control service size from 1389 to 1241 lines.
- Current split module sizes:
  - common helpers: 47 lines;
  - route sheets: 130 lines;
  - settings service: 117 lines;
  - settings router: 63 lines.
- Verification:
  - `pytest tests/services/test_production_control.py -q` - 16 passed;
  - `.venv\\Scripts\\python.exe -m pytest -q` - 131 passed;
  - direct router import confirmed `/v1/production-control/route-sheets/print` remains registered.

## 2026-05-24 - production-control printing rename and material-issue split

- Renamed the narrow route-sheet module concept to `backend/app/services/production_control_printing.py` so the layer can host future production-control print views, not only route sheets.
- Removed the old `production_control_route_sheets.py` module name and updated router/service imports to `production_control_printing`.
- Extracted material issue create/read/legacy export helpers into `backend/app/services/production_control_material_issues.py`.
- Updated the production-control router to import material issue handlers from the dedicated module while keeping lazy compatibility wrappers in `production_control.py`.
- Cleaned unused imports left by the split (`Any`/`Dict` in the router, `ProductionResource`/`WorkshopWarehouseBinding` and unused private aliases in the service).
- Normalized encoding in touched production-control files after PowerShell editing exposed mojibake/BOM artifacts.
- Reduced current production-control service size to 1014 lines.
- Verification:
  - `pytest tests/services/test_production_control.py tests/services/test_produce_and_manufacture_export.py tests/services/test_return_leftover_components.py -q` - 34 passed;
  - targeted 1C/export tests - 32 passed;
  - `.venv\\Scripts\\python.exe -m pytest -q` - 131 passed.

## 2026-05-24 - production-control material availability split

- Extracted shared production-control domain helpers into `backend/app/services/production_control_domain.py`:
  - `unit_display`;
  - `ensure_state`;
  - `default_spec_id`;
  - `latest_run_id`.
- Extracted material availability and coverage logic into `backend/app/services/production_control_material_availability.py`:
  - BOM component expansion;
  - stock availability excluding ignored warehouses;
  - active material issue reservations;
  - supplier/planned ETA aggregation;
  - component/order coverage labels;
  - `preview_materials`.
- Updated router, printing, and material-issue modules to use the dedicated availability/domain modules instead of importing through the large `production_control.py` service.
- Kept lazy compatibility wrappers in `production_control.py` for existing imports of `_components_for_product` and `preview_materials`.
- Cleaned stale imports left by the split.
- Reduced current production-control service size to 669 lines.
- Verification:
  - `pytest tests/services/test_production_control.py tests/services/test_produce_and_manufacture_export.py tests/services/test_return_leftover_components.py -q` - 34 passed;
  - `.venv\\Scripts\\python.exe -m pytest -q` - 131 passed.

## 2026-05-24 - production-control production flow split

- Extracted local production flow into `backend/app/services/production_control_production_flow.py`:
  - `produce_line`;
  - outgoing material issue lookup for returns;
  - return document numbering;
  - `return_leftover_components`.
- Updated the production-control router to import production flow handlers from the dedicated module.
- Kept lazy compatibility wrappers in `production_control.py` for old imports used by existing tests and possible external callers.
- Cleaned stale imports and wrapper-section comments after the move.
- Reduced current production-control service size to 418 lines.
- Verification:
  - `pytest tests/services/test_production_control.py tests/services/test_produce_and_manufacture_export.py tests/services/test_return_leftover_components.py -q` - 34 passed;
  - `.venv\\Scripts\\python.exe -m pytest -q` - 131 passed.

## 2026-05-25 - production-control journal split and facade cleanup

- Extracted journal/order materialization into `backend/app/services/production_control_journal.py`:
  - MRP planned-order materialization;
  - journal listing/filtering;
  - line state patching;
  - journal-local status constants and workshop inference helpers.
- Updated the production-control router to import journal handlers from the dedicated module.
- Migrated tests away from the old `app.services.production_control` imports to focused modules.
- Reduced `backend/app/services/production_control.py` to a small compatibility facade with explicit re-exports only.
- Removed stale imports and BOM artifacts from touched production-control files.
- Verification:
  - targeted production-control/produce/return tests - 34 passed;
  - compatibility facade smoke import points to focused modules;
  - `.venv\\Scripts\\python.exe -m pytest -q` - 131 passed.
## 2026-05-26 - журнал заявок на перемещение и кнопка "Собрано"

- `Запустить в 1С` в журнале заказов теперь идет через цепочку: создать/переиспользовать заявку на перемещение, выгрузить и оперативно провести `Document_ЗаказНаПроизводство`, затем создать непроведённый `Document_ПеремещениеЗапасов`.
- Добавлена страница фронта `Заявки перемещений`: список заявок слева, детали комплектующих к сборке справа.
- Добавлена команда `Собрано`: проводит перемещение в 1С через `Unpost` + `Post?PostingModeOperational=true`, локально переводит заявку в `posted`, а обеспечение строки заказа в `Собрано`.
