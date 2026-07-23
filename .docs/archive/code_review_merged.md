# PRODPLAN — Сводный код-ревью (3 независимых рецензии)

**Дата:** 2026-06-14. **Ветка:** `feature/purchase-journal`.
**Источники:** `code_review_claude.md`, `code_review_codex.md`, `code_review_qwen.md`.
**Метод:** три нейросети ревьюили независимо и параллельно, затем результаты сведены и дедуплицированы; спорные пункты доппроверены чтением кода.

### Легенда
- Авторство: **[C]** Claude, **[X]** Codex, **[Q]** Qwen. Несколько букв = нашли независимо (усиленный сигнал).
- Достоверность: **✅ подтверждено** чтением кода в ходе свода · **🔎 требует проверки** (прогон инструмента/доступ к БД) · без пометки — описано из чтения кода автором.

---

## Самое главное (порядок устранения)

1. **Безопасность** (M-1, M-2): нет auth, секреты в открытом виде и в истории git. Дёшево закрыть, дороже всего стоит ошибка. (M-3 снят — запись в прод штатная.)
2. **Целостность данных** (M-4…M-7): дубли документов в 1С, двойной счёт остатков, неидемпотентный пересчёт, гонки по пулу остатков участка. Прямо влияет на физические действия (перемещения, заказы поставщикам).
3. **Корректность** (быстрые баги): `models.ODataConfig` (краш), `create_all` vs Alembic, reconcile перезаписывает snapshot.
4. **Фронт-контракты и UX-ловушки** (экспорт скрытых строк, залипание loading, отсутствие отката).
5. **Поддерживаемость/инфра** — постепенно по выходным.

---

## 🔴 CRITICAL

### M-1. Нет аутентификации/авторизации на API — [C][X][Q] ✅
`backend/app/main.py:40-50`, все роутеры зависят только от `get_db`. Ни auth-middleware, ни JWT/API-key. Любой в LAN читает всю БД, запускает синк, удаляет планы и **выгружает документы в боевую 1С**. Воркеры (`sync_worker.py:29,34`, `reconcile_worker.py:35,40`) тоже анонимны. CORS по origin защитой не является.
→ Немедленно: basic-auth/mTLS на reverse-proxy. В коде: глобальная зависимость аутентификации + роли; сервисный токен для воркеров; ролевой гейт на side-effecting эндпоинты. Закрыть/удалить debug-эндпоинты (`specification.py /debug`, `sync.py /debug/production-order-states`).

### M-2. Секреты в открытом виде и в истории git — [C][X][Q] ✅
- **Пароль 1С в истории git** [C] ✅: `config/odata_config.json`, коммит `4f511220` → `"password": "Gw6dwAEKmm$o"`, `"username": "odata.user"`; коммит `0a4fec95` лишь затёр. Секрет навсегда в истории (`git log -p -S 'Gw6dwAEKmm'`).
- **`GET /api/odata/config` отдаёт пароль/токен** [C] ✅ (`routers/odata.py:42-45`).
- **`save_odata_config` пишет креды plaintext-JSON** [Q] ✅ (`services/odata_config.py:27-31`).
- **Пароль БД `password`/fallback в compose и `database.py`** [X][Q] ✅ (`docker-compose.yml:20,42`, `docker-compose.test.yml:11` → `..._change_me`) + проброс портов PG на хост (`55432/55433`).
→ Считать учётку `odata.user` скомпрометированной — **сменить пароль в 1С**. Переписать историю (`git filter-repo`)/ротировать. Все секреты — в env/секрет-менеджер. В ответе `/odata/config` маскировать. Не публиковать порт PG наружу; обязательный non-default пароль.

### ~~M-3. Guard записи в боевую 1С обходится на уровне API~~ — СНЯТО (штатное поведение)
Codex [X] отметил `routers/production_control.py:381,431,511,600` (`allow_production=… or not dry_run`) и `:620` (хардкод `True`) как обход `require_demo_base`-барьера. **Это не дефект:** программа уже в продакшене и **должна** писать в боевую 1С — `dry_run=false → запись в прод` есть желаемая семантика. `require_demo_base`/`unf_demo`-guard был защитой времени разработки и больше не является операционной моделью.
- Остаточно (LOW, опц.): флаг `allow_production` и demo-guard теперь фактически мёртвый/вводящий в заблуждение код — при наличии времени убрать или задокументировать, чтобы не путать.
- ⚠️ Важно: раз запись в прод штатная, **вес переносится на M-1 (нет auth)** — без аутентификации любой в LAN триггерит реальные документы в 1С — и на **M-4 (дубли документов)**, которые в проде наносят реальный ущерб.

### M-4. Дубли документов в 1С при экспорте — [C][X] ✅(C)
Механизм идемпотентности (`sync_link`) **существует, но негерметичен** (важно: Qwen [Q] записал его в «сделано хорошо» — это верно лишь частично):
- **Окно POST↔commit** [C] ✅: `one_c_export_common.py:201-253` — `client.post()` создаёт документ в 1С, `upsert_link(status="success")` ставится в памяти, но `db.commit()` только в конце цикла. Сбой между POST и commit → документ в 1С есть, линка нет → повтор создаёт **дубль** (заказ/перемещение/наряд).
- **Дубли заказа поставщику при частичном экспорте** [X]: `one_c_purchase_order_export.py:75,100,148,240` — группы строятся из всех `PlannedPurchase` без исключения уже связанных `SyncLink`; reuse по номеру+поставщик+комментарий, а не по `purchase_id`/`target_ref_key`.
- **`upsert_link` без `ON CONFLICT`** [C]: `one_c_export_common.py:104-150` — гонка двух экспортов → `IntegrityError` после возможного POST.
- **Legacy `export_issue_to_1c` без проверки линка** [C]: `production_control_material_issues.py:1335-1384` — повтор создаёт второе перемещение (эндпоинт активен).
- **POST/PATCH/post_operation без ретраев** [C]: `odata_client.py:131-241` — таймаут на POST особенно опасен (документ мог создаться).
→ Коммитить линк `planned` с заранее сгенерированным `Ref_Key` **до** POST (1С принимает Ref_Key в payload) либо per-entry commit сразу после POST; `INSERT … ON CONFLICT DO UPDATE` + advisory-lock по `source_id`; исключать уже связанные строки до группировки; убрать/защитить legacy-эндпоинт; ретраить PATCH/Post.

**Статус M-4 (фиксы test-first):** ✅ per-entry commit в `post_export_entries`; ✅ legacy `export_issue_to_1c` теперь не повторяет POST при уже выставленном `exported_ref1c` (idempotency-guard); ✅ частичный экспорт закупок исключает строки с успешным `SyncLink` до группировки (нет дубля заказа поставщику). Остаточно (требует прод-валидации): pre-generated `Ref_Key` до POST для устранения окна на один документ; `ON CONFLICT` в `upsert_sync_link`; ретраи PATCH/Post.

### M-5. Двойной счёт остатков — системный класс (кейс PP001308915) — [C] ✅
Нетто-потребность вычитает остаток/WIP на каждом уровне разузлования, затем те же остатки учитываются повторно (≥5 мест):
- `planning_service.py:3968-4004` ⊕ `order_quantity_calculator.py:51-113` — preview уже `net`, но в калькулятор идёт свежий `Item.stock_qty`, и `_limit_by_components` снова лимитирует родителя наличием уже «потраченных» компонентов.
- `planning_service.py:3894-3905` vs `_build_component_reservations_from_active_1c` — компоненты активных заказов защищены дважды.
- ~~`mrp_stock_helpers.py:133-167` — `outerjoin` множит `remaining_qty`~~ — ✅ **ложная тревога** (проверено): на `ProductionOrderLineState` есть `UniqueConstraint("product_id")`, строк состояния на продукт ≤1, размножения нет. Остаётся лишь нюанс семантики `planned_finish_date IS NULL` = «доступно немедленно» (by design).
- (обеспечение) `production_control_material_availability.py:36-65` vs `production_control_reservations.py:236-238` — потребность от `remaining_qty`, потребление от `produced_qty`; после частичного выпуска базы расходятся → ложный `excess` и снятие нужного резерва.
→ Нетировать остаток **ровно в одном месте**: кормить калькулятор остаточным запасом либо передавать gross и нетировать единожды. Агрегировать состояние до одной строки на продукт.

**Статус B3 (исследовано 2026-06-14, фиксы test-first):**
- ✅ Уточнение: в `OrderQuantityCalculator.compute` `_limit_by_components` считается **только для диагностики** (`final_qty=min(requested,horizon)`); реальный гейтинг по компонентам — в `build_planned_orders_and_purchases` на **свежем** `effective_stock_by_item`.
- ✅ Сделано: удалён мёртвый первый цикл в `_limit_by_components` (`order_quantity_calculator.py:328-335`, результаты отбрасывались) — без изменения поведения, 323 теста зелёные.
- ⚠️ Суть double-count подтверждена воспроизведением, но это **семантика/политика гейтинга, а не однозначный баг**: `compute_planning_preview` при netting *расходует* остаток компонента (мутирует `avail_stock`, в т.ч. на собственный/прямой спрос компонента), а `build` гейтит родителя по *валовому* свежему остатку компонента, не вычитая уже распределённое. Пример: C stock=10, прямой спрос C=10 и потребность родителя P=10×C → preview отдаёт C net=10, но build разрешает P=10 по «свежим» 10 ед. C (которые на деле уйдут на прямой спрос C). Гейтинг оптимистичный. Делать его пессимистичным — **продуктовое решение** (может занижать план родителей, чьи компоненты будут произведены по плану). Менять ядро MRP вслепую нельзя: движок плотно покрыт тестами с зафиксированной семантикой; нужна валидация на прод-данных. → отдельная задача с участием заказчика, не автоправка.

### M-6. Неидемпотентный пересчёт прогона + commit при FAILURE — [C] ✅
`planning_service.py:537-578` — `_get_or_create_run` при переданном `run_id` не удаляет старые `PlannedOrder/Stage/Purchase/…`, фазы делают `db.add` заново → удвоение заказов/нагрузки. `finally: db.commit()` (`:4136-4143`) коммитит **и на ветке FAILURE** → проваленный прогон фиксируется частично.
→ В начале `try` при заданном `run_id` чистить дочерние строки; на исключении `rollback()` + отдельной транзакцией писать только заголовок FAILURE.

### M-7. Гонка по пулу остатков участка без блокировок — [C]
`production_control_material_issues.py:662-945` — `create_material_issues` читает `_free_destination_stock` и тут же клеймит `in_place` без блокировки строк/документов. Два параллельных запроса с общим компонентом на одном участке заклеймят один остаток дважды (PP001308915). Защита `consumed_destination_stock` работает только внутри одного вызова.
→ Advisory-lock на `(warehouse_ref1c, item_id)` или `SELECT … FOR UPDATE`; повторная проверка резерва в той же транзакции с retry.

---

## 🟠 HIGH

- **`models.ODataConfig` → краш** — [Q] ✅: класса нет в `models.py`, но `routers/sync.py:308` делает `db.query(models.ODataConfig)` → `AttributeError` при вызове `/debug/production-order-states`.
- **`create_all` vs Alembic** — [C][X][Q] ✅, но рекомендация уточнена по факту прод-проверки (2026-06-14): `main.py:23` `create_all` **несущий**, а НЕ просто «дрейф». Проверено на чистой БД: `alembic upgrade head` падает на root-миграции `20250925_01` с `relation "items" does not exist` — ни одна миграция не создаёт базовые таблицы (`items` и др.), alembic внедрён на уже существующую БД и покрывает только инкременты. Прод стоит на `stamp` head (`alembic_version=20250611_01`, 61 таблица), схему строит `create_all`. ⚠️ **Просто убрать `create_all` нельзя** — сломает чистый деплой. Правильный путь (отдельная задача, делать с деплоем): (1) сгенерировать baseline-миграцию (down_revision=None) с полной текущей схемой и перецепить `20250925_01` на неё; (2) проверить `alembic upgrade head` на чистой PG (тест-контейнер уже настроен); (3) только потом убрать `create_all` и завести `alembic upgrade head` в entrypoint.
- **Reconcile перезаписывает «замороженный» snapshot текущим BOM** — [X]: `mrp_reconciliation.py:393,408,455,623` — root-потребности из snapshot взрываются текущими `DefaultSpecification`/`SpecComponent`; недостижимые получают gross 0. Определить семантику (frozen или явная «актуализация по BOM»).
- **Недопланирование общих подсборок (конвергентный BOM)** — [C]: `period_plan_service.py:599-601,540-541` — `exploded_parents` разузловывает узел один раз за BFS; дети общей подсборки при втором заходе не разворачиваются.
- **WIP-завышение** — [C]: `forced_orders.py:105-110` (`SUM(quantity)` без фильтров активности vs канонический `remaining_qty`).
- **Лимит по мощности не работает** — [C]: `capacity_scheduler.py:125-162` — свободные часы суммируются по всем участкам × всем дням окна в один пул.
- **Fallback-резолвинг спецификации по имени** — [C]: `specification.py:110-118` — `spec_code==item_code OR spec_name==item_name`, берётся последняя по id → молчаливая привязка к чужому BOM.
- **N+1 по дереву спецификаций** — [C][Q]: `specification.py:416-484` — `_resolve_spec_id_for_item_id`+`_has_children`+повторный `db.query(Item)` на каждый узел.
- **Обход дерева схлопывает diamond как цикл** — [C]: `specification.py:847-926` — глобальный `item_id`-set, легитимное переиспользование = `CYCLE_DETECTED` → недосчёт потребности.
- **`return_leftover_components` пере-выдаёт возврат** — [C]: `production_control_production_flow.py:367-578` — leftover от полного `issued` без вычитания проведённых `return`.
- **`posted`-резерв по `required_qty`, не `issued_qty`** — [C]: `production_control_reservations.py:209-218` — при частичном проведении в 1С завышенный резерв → ложный дефицит для других.
- **`update_line_state` ломает инварианты резервов** — [C]: `production_control_journal.py:1219-1263` — ручной `status="cancelled"`/`issue_status` не освобождает резервы → висящие claim'ы держат остаток участка.
- **Тесты на SQLite, прод на PostgreSQL** — [C]: `tests/conftest.py:16-18` — `DECIMAL` как float, `server_default`, частичные индексы, сырой `ORDER BY` не валидируются.
- **Фронт — экспорт «невидимых» строк** — [X]: `PurchaseControlPage.tsx:48,93,154` — `selectedPurchaseIds` не пересекается с `rows` при смене фильтра/страницы → в 1С уходят скрытые позиции. Аналогично период-план [X] `PeriodPlanPage.tsx:742,942,1540` — «Создать заказы» берёт raw `journal.rows`, игнорируя видимые фильтры.
- **Фронт — залипание loading** — [C]: `PurchaseControlPage.tsx:149-185` — `setLoading(false)` только в `catch`, нет `finally`.
- **Фронт — нет отмены запросов** — [C]: `lib/api.ts:1-25` — без `AbortSignal`; гонки «последним выиграл» (эталон с `loadSeq` только в `MrpResultPage`).
- **Фронт — `submitProduce` без отката** — [C][Q]: `ProductionControlPage.tsx:548-727` — многошаговая распределённая транзакция; при сбое на шагах 3-6 material issues не откатываются → документы-сироты. Плюс ответы через нетипизированный `Record<string, unknown>` с эвристикой `result.created ?? …`.
- **Фронт — неуникальные `row_key`** — [C][X]: `purchase_control_journal.py:197` `line:{item_id}` не уникален при нескольких строках одной номенклатуры → неверное выделение/detail pane.

---

## 🟡 MEDIUM

**Совместимость/корректность**
- `.dict()` вместо `.model_dump()` (Pydantic v2) — [Q] ✅: `routers/odata.py:51,58,83,132`, `services/item_service.py:22,32`.
- `datetime.utcnow()` deprecated — [Q] ✅: 13 файлов (`planning_service.py:574,4140`, `one_c_export_common.py:124`, …). → `datetime.now(timezone.utc)`.
- `include_wip` по умолчанию `True` вопреки `DEFAULT_PLANNING_CONFIG=False` — [C]: `planning_service.py:2908`.
- Синтетические id через `hash(str)%10**10` (соль per-process, коллизии) — [C]: `planning_service.py:1049,1533`.
- `clamp_to_today` схлопывает цепочку lead-time — [C]: `period_plan_service.py:632-634`.
- `posted_transfer` синк перезаписывает `required/issued_qty` из 1С без сверки по строке — [C]: `one_c_posted_transfer_sync.py:147-204`.
- balance-guard без разреза ячейки vs cell-aware transfer — [C]: `one_c_manufacture_export.py:467-540`.
- `default_specification_sync` без reconcile удалённых — [C]: устаревшая «основная спецификация» продолжает резолвиться.
- `nomenclature_sync.py:135` — фильтр `IsFolder` заявлен, но не выполняется → папки как `Item`. `:360-371` — `IntegrityError` проглатывается, но возвращается «успех».
- `workshop_resolution.py:106-123` — при >1 участке на вид производства молча `min(id)` без диагноза.
- сырой `ORDER BY` через f-string (спасает whitelist) — [C][Q]: `plan_service.py:72-74`; OData filter без санитизации — [Q]: `odata_client.py get_all`.

**Безопасность/инфра**
- `document.write()` серверным HTML — XSS-вектор — [Q]: `ProductionControlPage.tsx renderRouteSheets()`.
- Контейнер backend от root (нет `USER`) — [Q] ✅: `backend/Dockerfile`.
- Хардкод GUID организации/подразделения 1С — [Q]: `one_c_export_common.py:12-14`.
- Нет конфигурации пула соединений — [Q] ✅: `database.py:12` только `pool_pre_ping`, без `pool_size/max_overflow/pool_recycle`.
- Нет rate-limit на sync-эндпоинты (риск перегрузить 1С) — [Q].
- `sync_orchestrator.py:231-271` — `threading.Lock` бесполезен при `--workers 4`.
- OData-пароль гоняется через браузер — [Q]: `SyncPage.tsx`/`domain/sync.ts`.
- `460× except Exception` (часть — silent `pass`) — [Q].
- `print()` вместо `logging` (~57) — [Q][C]: `odata_client.py` печатает полный URL с `$filter`.

**Модель данных / производительность**
- FK без `index=True` на горячих join (`SpecComponent`, `SpecOperation`, `ProductionProduct`, `ProductionComponent`, `OrderOperation`, `supplier_order_items`) — [C]: `models.py`.
- Дочерние FK заказа без `ondelete` (непоследовательно) — [C]: `models.py:245,502,515,556`.
- `list_journal` slow-path грузит весь журнал в память — [C]: `production_control_journal.py:837-1185`.
- Полные сканы `Supplier`/`SyncLink` на каждый запрос журнала закупок — [C]: `purchase_control_journal.py:124-143,240`.
- N+1 спецификации/состояния на продукт заказа — [C]: `one_c_production_order_export.py:416-427`.
- резолв номенклатуры/складов из 1С почанково на каждом стоке — [C]: `odata_client.py:255-328,800-848`.
- Экспортные эндпоинты грузят до 100000 строк в память — [Q]: `plan.py`.
- Незакреплённые версии зависимостей (`sqlalchemy>=1.4`→2.x, `pydantic>=1.8`→v2) — [C][Q]: `requirements.txt`.

**Фронт**
- Нет catch-all маршрута и ErrorBoundary — [C][Q]: `App.tsx:57-72`.
- `HashRouter` вместо `BrowserRouter` при наличии бэкенда — [Q] ✅: `main.tsx:3,9`.
- CSV-экспорт без экранирования переводов строк — [Q]: `PurchaseControlPage.tsx`.
- UTC/локальные даты смешаны (`nextFriday`) — [C]: `PeriodPlanPage.tsx:50-55`.
- Дробный ввод vs целочисленная раздача количеств — [C]: `PeriodPlanPage.tsx:795-865`.
- deep-link `?search=` применяется только на mount — [X]: `PurchaseControlPage.tsx:43,58,108`.
- specification_sync теряет многострочные составы по ключу `(spec_id,item_id)` — [C]: `specification_sync.py:220-235,340-353`.

---

## 🟢 LOW / поддерживаемость

- **God-файлы** — [C][Q]: `planning_service.py` (4143), `period_plan_service.py` (1804), `plan.py` (1692), `production_control_journal.py` (1407), `production_control_material_issues.py` (1384), `specification.py` (1246), `models.py` (1032), `schemas.py` (647); фронт `PeriodPlanPage.tsx` (1721), `ProductionControlPage.tsx` (1113); `styles.css` (1736, без скоупа).
- **Дублирование** — [C][Q]: `_to_float` (6+ файлов), `_date_to_iso`, `_prodplan_order_display_number`, `_material_issue_has_1c_link`, `_warehouse_name*`; компонент `ForecastShift` ×3; XLSX-boilerplate; `rootOptions`-загрузка ×3 на фронте.
- **Мёртвый/вводящий в заблуждение код**:
  - `iter_by_guid` — антипаттерн (module-level `def` с `self` + monkey-patch `odata_client.py:851`). **Работает** (НЕ краш). → перенести в класс. *(Снимает ложную тревогу [Q] о `NameError`.)*
  - `nomenclature_search._generate_embedding` — MD5-«эмбеддинг», семантики нет (комментарий «временное решение») — [C][Q].
  - `_short_*_number`, `_next_issue_number` (`count()+1`, гоночный) — [C].
- `item.replenishment_time or 30`/`or 0` — falsy-`or` подменяет легитимный `0` — [C]: `planning_service`, `mrp_reconciliation.py:684`.
- `get_db` без `rollback()` в except; commit'ы разбросаны по сервисам/роутерам — [C][Q]: `database.py:17-23`.
- CORS `allow_credentials=True` + `["*"]` методы/заголовки — [C]: `main.py:31-37`.

---

## Конфликты и ложные тревоги (разрешено сводом)

| Пункт | Заявка | Вердикт |
|------|--------|---------|
| `iter_by_guid` | [Q] CRITICAL: падает с `NameError` | ✅ **Ложная тревога.** Monkey-patch `odata_client.py:851` биндит функцию к классу — работает. Реальная проблема — антипаттерн (LOW). |
| Идемпотентность `sync_link` | [Q] «сделано хорошо» | Частично верно: механизм есть, но **негерметичен** (M-4). В своде — как риск, не как достижение. |
| Два alembic head | первичный regex-чек | ✅ **Ложная тревога** [X]: реальный `alembic heads` = один head `20260611_01`. |

---

## Что сделано хорошо (консенсус)
Деньги/количества — `DECIMAL`, не float (`models.py`); 315 pytest-тестов на бизнес-логику; Alembic-версионирование (48 миграций, один head); TypeScript strict (0 ошибок сборки); ESLint flat config; чистое разделение `domain/`/`services/` на фронте; multi-stage Dockerfile фронта; лёгкие background-воркеры на stdlib; объёмная документация в `.docs/`.

## Верификация (выполнил Codex)
`npm run build` ✅ · `npm run lint` ✅ (1 warning) · targeted pytest 11 passed · `alembic heads` = 1 · `compileall` ✅. `alembic current` не проверен (нет коннекта к БД).

## Пробелы в тестах
Нет HTTP/router-тестов для purchase-control и 9 из 11 роутеров; нет теста на guard записи в прод (M-3); нет теста идемпотентности частичного экспорта (M-4); нет unit-тестов фронта (1 Playwright smoke).

---

## Предлагаемый план (фазы)

**Фаза 1 — Безопасность (1-2 дня):** M-1 (auth + закрыть debug), M-2 (ротация пароля 1С, секреты в env, маскировать `/odata/config`, не публиковать порт PG), `USER nonroot` в Dockerfile. (M-3 снят.)

**Фаза 2 — Целостность данных:** M-4 (идемпотентность экспорта), M-6 (пересчёт прогона), M-7 (блокировки), затем M-5 (рефакторинг нетирования остатков — крупнее, планировать отдельно).

**Фаза 3 — Быстрые баги:** `models.ODataConfig`, `create_all`→Alembic, reconcile snapshot, `.dict()`→`.model_dump()`, `utcnow()`, фронт (loading-leak, экспорт скрытых строк, submitProduce-rollback).

**Фаза 4 — Качество (постепенно):** разбить god-файлы, убрать дублирование, `print`→`logging`, ErrorBoundary, FK-индексы, пул соединений, rate-limit, покрытие роутеров тестами, запиннить зависимости, тесты на PostgreSQL.
