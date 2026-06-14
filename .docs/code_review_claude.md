# Код-ревью PRODPLAN — рецензия Claude (Opus 4.8)

Дата: 2026-06-14. Ветка: `feature/purchase-journal`.
Метод: 6 параллельных агентов по подсистемам (планирование/MRP, интеграция с 1С, оперативное управление производством, спецификации/номенклатура, фронтенд, инфраструктура/безопасность/данные). Ниже — сведённый результат, отсортированный по приоритету. Это одна из трёх независимых рецензий для последующего слияния.

---

## TL;DR — на что смотреть в первую очередь

1. **Безопасность нулевая.** На всём API нет аутентификации/авторизации, а в git-истории лежит рабочий пароль к 1С. Для ERP с записью в боевую 1С это критично прямо сейчас.
2. **«Двойной счёт остатков» — системный, а не точечный баг.** Тот же класс, что в кейсе PP001308915, воспроизводится минимум в 5 местах в двух подсистемах (планирование и обеспечение). Лечить надо архитектурно: нетировать остаток ровно в одном месте.
3. **Идемпотентность выгрузки в 1С дырявая.** Есть реальное окно, в котором сбой между POST в 1С и локальным `commit` приводит к дублю документа (заказа/перемещения/наряда) при повторном экспорте.
4. **Нет блокировок при конкурентной работе с пулом остатков участка.** Два одновременных пользователя могут заклеймить один и тот же остаток.
5. **Дрейф схемы БД:** `Base.metadata.create_all` работает параллельно с Alembic; тесты идут на SQLite, прод — на PostgreSQL.

---

## CRITICAL

### C1. Нет аутентификации и авторизации на API
`backend/app/main.py:40-50`, все роутеры. Ни одного `Depends(auth)`, ни JWT/API-key/Basic. Любой в локальной сети, достучавшись до `mtzdock.lan:9010`/`:8010`, читает всю производственную БД, запускает синхронизацию и **выгружает документы в боевую 1С** (`/plan/export-to-1c`, `/plan/reconcile`, экспорты обеспечения). Воркеры тоже ходят анонимно (`sync_worker.py:29,34`, `reconcile_worker.py:35,40`).
→ Немедленно: basic-auth/mTLS на reverse-proxy. В коде: глобальная зависимость аутентификации + роли; сервисный токен для воркеров; ролевой гейт на side-effecting эндпоинты (`export-to-1c`, `reconcile`, удаления планов).

### C2. Рабочий пароль 1С в git-истории + утечка через API
- `config/odata_config.json`, коммит `4f511220` добавил `"password": "Gw6dwAEKmm$o"`, `"username": "odata.user"`; коммит `0a4fec95` лишь затёр на `""`. В рабочем дереве пусто и файл в `.gitignore`, но секрет **навсегда в истории** (`git log -p -S 'Gw6dwAEKmm'`).
- `GET /api/odata/config` (`backend/app/routers/odata.py:42-45`) без auth возвращает текущий JSON конфига **вместе с паролем/токеном**.
→ Считать учётку `odata.user` скомпрометированной и **сменить пароль в 1С**. Переписать историю (`git filter-repo`)/ротировать. Перейти на секреты через env. В ответе `/odata/config` маскировать секреты.

### C3. Окно дублирования документов в 1С при экспорте
`one_c_export_common.py:201-253` (`post_export_entries`): для новой записи `client.post(...)` создаёт документ в 1С, затем выставляется `target_ref_key` и `upsert_link(status="success")`, но `db.commit()` — только в конце цикла. Если процесс упадёт/контейнер рестартует/оборвётся соединение с БД **между POST в 1С и commit** — документ в 1С есть, а sync_link не зафиксирован. Повторный экспорт не найдёт линк и **создаст второй документ** (заказ/перемещение/наряд). Защита по поиску `Number` есть только у purchase-экспорта.
Связанные: `one_c_production_order_export.py:672-683` и `one_c_piecework_export.py:735-749` — `_post_document_operational`/PATCH закрытия заказа неатомарны с записью линка; при падении проведения статус становится `error`, хотя документ уже создан. Гонка двух параллельных экспортов одного документа (`upsert_sync_link` делает `find`→`add` без `ON CONFLICT`, `one_c_export_common.py:104-150`) даёт `IntegrityError` после возможного POST.
→ Коммитить линк со статусом `planned` и заранее сгенерированным `Ref_Key` **до** POST (1С OData принимает Ref_Key в payload), либо per-entry commit сразу после POST; на запись `upsert` использовать PostgreSQL `INSERT … ON CONFLICT DO UPDATE` + advisory-lock по `source_id`; PATCH/Post делать ретраебельными.

### C4. Двойной счёт остатков в MRP-движке (класс PP001308915)
Нетто-потребность вычитает остатки/WIP на каждом уровне разузлования, а затем те же остатки учитываются ещё раз:
- `planning_service.py:3968-4004` ⊕ `order_quantity_calculator.py:51-113` — preview уже возвращает `net`, но в калькулятор передаётся свежий `Item.stock_qty`, и `_limit_by_components` снова ограничивает родителя наличием компонентов, уже «потраченных» при нетировании.
- `planning_service.py:3894-3905` vs `_build_component_reservations_from_active_1c` — компоненты активных заказов защищаются дважды: и через WIP-нетирование родителя, и через карту резерваций.
- `mrp_stock_helpers.py:133-167` (`active_wip_eta_by_item`) — `outerjoin` к `ProductionOrderLineState` по `product_id` множит `remaining_qty` при нескольких строках состояния; `planned_finish_date IS NULL` трактуется как «доступно немедленно».
→ Нетировать остаток **ровно в одном месте**: либо кормить калькулятор остаточным (после нетирования) запасом, либо передавать gross и нетировать единожды. Агрегировать состояние до одной строки на продукт.

### C5. Неидемпотентный пересчёт прогона + commit при FAILURE
`planning_service.py:537-578` (`_get_or_create_run`) при переданном `run_id` возвращает существующий прогон **без удаления** его `PlannedOrder/Stage/Purchase/Rework/CapacityLoad/PeggingLink`, фазы делают `db.add` заново → удвоение всех заказов и нагрузки участков. `finally: db.commit()` (`:4136-4143`) коммитит **и на ветке FAILURE** после `raise` → проваленный прогон фиксируется с частичными строками, которые отчёты читают как валидные. (Сейчас HTTP всегда шлёт `run_id=None`, но контракт сломан для ретраев/сверки.)
→ В начале `try` при заданном `run_id` удалять дочерние строки прогона; на исключении — `db.rollback()` и отдельной короткой транзакцией писать только заголовок со статусом FAILURE.

### C6. Конкурентный доступ к пулу остатков участка без блокировок
`production_control_material_issues.py:662-945` (`create_material_issues`) читает `_free_destination_stock` (свободный остаток минус резервы) и тут же создаёт `in_place`-claim **без блокировки** строк/документов. Два параллельных HTTP-запроса на разные `product_id` с общим компонентом на одном участке оба увидят один и тот же свободный остаток и оба его заклеймят → двойной счёт остатка участка (ровно PP001308915). `consumed_destination_stock` защищает только внутри одного вызова.
→ Advisory-lock на `(warehouse_ref1c, item_id)` или `SELECT … FOR UPDATE` по участвующим строкам перед расчётом свободного остатка; повторная проверка резерва в той же транзакции с retry на конфликте.

### C7. Недопланирование общих подсборок (конвергентный BOM)
`period_plan_service.py:599-601, 540-541` — guard `iid in exploded_parents` разузловывает каждый узел лишь один раз за весь BFS, но `gross_map`/`net_map` аккумулируются при каждом посещении. Если подузел встречается на двух разных глубинах BOM (общая подсборка — частый случай), его дети при втором заходе **не разузловываются** → системный дефицит закупок/производства для общих компонентов.
→ Guard по стеку текущего пути (защита от цикла), а не one-shot; либо сначала полный gross, затем один топологический проход нетирования.

---

## HIGH

**Планирование**
- `forced_orders.py:105-110` — WIP как `SUM(ProductionProduct.quantity)` без фильтров активности/удаления/DONE (vs канонический `remaining_qty`) → завышенный WIP, заниженный план.
- `order_quantity_calculator.py:337-372` — мёртвый/сломанный первый цикл в `_limit_by_components`; `STOCK_CACHE_MISS` трактует отсутствие компонента в кэше как 0 → ложный дефицит.
- `capacity_scheduler.py:125-162` — свободные часы суммируются по всем участкам-кандидатам × всем дням окна в один пул → ёмкость окна многократно переоценена, лимит по мощности фактически не работает.
- `plan.py` (повсеместно, напр. 451, 633, 686) — `except Exception: raise HTTPException(400, str(e))`: «не найдено» отдаётся как 400, внутренние ошибки как 400, текст исключения утекает клиенту; ряд мутирующих эндпоинтов без явного commit/rollback.

**Интеграция с 1С**
- `odata_client.py:131-241` — POST/PATCH/post_operation без ретраев (в отличие от GET); таймаут на POST особенно опасен (документ мог создаться) → провоцирует дубль.
- `one_c_export_common.py:57-81` — demo-guard определяет демо-базу по подстроке `"unf_demo"`; единственный барьер от записи в прод — флаг `allow_production`, без allowlist прод-URL и подтверждения.
- `odata_client.py` — `print()` полного URL с `$filter` (бизнес-данные) вместо управляемого `logging`.

**Оперативное управление производством**
- `production_control_material_availability.py:36-65` vs `production_control_reservations.py:236-238` — потребность считается от `remaining_qty`, потребление — от `produced_qty`; после частичного выпуска базы расходятся → возможен ложный `excess` и снятие нужного резерва с открытых перемещений.
- `production_control_production_flow.py:367-578` (`return_leftover_components`) — leftover от полного `issued` без вычитания уже проведённых `return` → пере-выдача возврата при повторном вызове.
- `production_control_production_flow.py:174-196` — overproduction наращивает `quantity`, но не резерв; guard либо падает, либо поведение несогласованно/недокументировано.
- `production_control_reservations.py:209-218` — `posted`-доставка резервирует `required_qty`, а не `issued_qty`; при частичном проведении в 1С → завышенный резерв и ложный дефицит для других заказов.
- `production_control_journal.py:1219-1263` (`update_line_state`) — произвольная установка `status="cancelled"`/`issue_status` через PATCH не освобождает резервы и не удаляет issue-документы → висящие резервы держат остаток участка (утечка).

**Спецификации / номенклатура**
- `specification.py:110-118` — fallback-резолвинг спецификации по `spec_code==item_code OR spec_name==item_name` (берётся последняя по id) → молчаливая привязка к чужому BOM; в `workshop_resolution` fallback не используется — логики расходятся.
- `specification.py:416-484, 213-225` — N+1 по дереву: `_resolve_spec_id_for_item_id` + `_has_children` + повторный `db.query(Item)` на каждый узел/операцию → тысячи запросов на дерево.
- `specification.py:847-926` — обход по глобальному `item_id`-множеству: легитимный diamond (повторное использование) помечается `CYCLE_DETECTED` и не разворачивается; реальный цикл и переиспользование неразличимы → недосчёт потребности.

**Инфраструктура**
- `backend/app/main.py:23` — `Base.metadata.create_all` параллельно с Alembic → дрейф схемы (create_all создаёт таблицы в обход истории/частичных индексов).
- `tests/conftest.py:16-18` — тесты на SQLite, прод на PostgreSQL: семантика `DECIMAL` (деньги/количества как float), `server_default`, `JSON`, частичные индексы и сырой `ORDER BY` не валидируются.

**Фронтенд**
- `PurchaseControlPage.tsx:149-185` (`orderTo1C`/`syncFrom1C`) — `setLoading(false)` только в `catch`, нет `finally` → после успешной выгрузки в 1С страница залипает в loading навсегда.
- `lib/api.ts:1-25` — нет `AbortSignal`/таймаута; почти все страницы не отменяют запросы и не проверяют свежесть ответа → «последним выиграл» при быстром переключении (эталон с `loadSeq` есть только в `MrpResultPage`).
- `ProductionControlPage.tsx:548-727` (`submitProduce`) — god-функция критичного процесса (правка кол-ва → перемещение → экспорт → проведение → выпуск → наряд → ручной rollback) на нетипизированных `Record<string, unknown>`.
- Повсеместный `Record<string, unknown>` для ответов мутаций с эвристикой `result.created ?? result.created_count ?? …` → при переименовании поля бэка молча «0 создано» вместо ошибки.

---

## MEDIUM (сжато)

- `planning_service.py:2908` — `include_wip` по умолчанию `True` вопреки `DEFAULT_PLANNING_CONFIG=False`.
- `planning_service.py:1049,1533` — синтетические id через `hash(str)%10**10`: соль per-process (PYTHONHASHSEED), id меняются между воркерами, возможны коллизии.
- `planning_service.py:4010, 581-606` — квадратичный `spec_cache` (есть готовый `spec_by_id`), 3 COUNT на строку в `list_planning_runs`, пагинация в Python.
- `period_plan_service.py:632-634` — многоуровневый `clamp_to_today` схлопывает цепочку lead-time в ближнем горизонте.
- `one_c_manufacture_export.py:467-540` — balance-guard суммирует без разреза ячейки, а transfer-экспорт учитывает ячейки → расхождение cell-aware/cell-blind.
- `one_c_posted_transfer_sync.py:147-204` — синк проведённых перемещений перезаписывает `required_qty`/`issued_qty` из 1С без сверки по строке → тихое изменение потребности.
- `one_c_production_order_export.py:416-427` — N+1 запрос спецификации/состояния на каждый продукт заказа.
- `odata_client.py:255-328, 800-848` — резолв номенклатуры/складов чанками по 20 GUID напрямую из 1С на каждом стоке (сотни запросов) вместо локального кэша.
- `sync_orchestrator.py:231-271` — `threading.Lock` только внутри процесса; при `--workers 4` бесполезен, ручной sync/экспорт не сериализованы.
- `production_control_material_issues.py:1335-1384` (`export_issue_to_1c`) — legacy single-export без проверки sync_link → дубль перемещения в 1С (эндпоинт активен).
- `production_control_journal.py:837-1185` (`list_journal`) — slow-path при фильтре по участку/coverage грузит весь журнал в память и фильтрует в Python.
- `production_control_journal.py:1230-1247` — ручное `completed` не закрывает `in_place`/`posted` claim'ы → висящий резерв на участке.
- `purchase_control_journal.py:124-143,240` — два полных скана `Supplier` + полный скан `SyncLink` на каждый запрос журнала; `row_key=line:{item_id}` не уникален.
- `specification_sync.py:220-235, 340-353` — reconcile компонентов/операций по `(spec_id,item_id)`/`(spec_id,operation_id)` теряет многострочные составы (есть готовый `comp_ref_key`).
- `default_specification_sync.py` — нет reconcile удалённых записей → устаревшая «основная спецификация» продолжает резолвиться.
- `nomenclature_sync.py:360-371` — `IntegrityError` проглатывается с откатом всей транзакции, но возвращается статистика «успех».
- `nomenclature_sync.py:135` — фильтрация папок `IsFolder` заявлена в комментарии, но в цикле не выполняется → папки создаются как `Item`.
- `workshop_resolution.py:106-123` — при нескольких участках на один вид производства берётся `min(id)` молча, без диагноза `KIND_AMBIGUOUS`.
- `specification.py:454-460,1233` — INNER JOIN на `Operation`: осиротевшие `SpecOperation` исчезают из дерева/quality без warning.
- `models.py` — FK без `index=True` на горячих join/where (`SpecComponent`, `SpecOperation`, `ProductionProduct`, `ProductionComponent`, `OrderOperation`, `supplier_order_items`); PostgreSQL не индексирует FK автоматически.
- `models.py:245,502,515,556` — дочерние FK заказа без `ondelete` (непоследовательно с остальными).
- `requirements.txt` — незакреплённые версии (`sqlalchemy>=1.4` втянет 2.x, `pydantic>=1.8` — v2): сборки невоспроизводимы.
- `docker-compose*.yml` — слабые дефолтные пароли БД (`password`, `..._change_me`) + проброшенные порты PG на хост (`55432/55433`).
- `plan_service.py:72-74` — сырой `ORDER BY` через f-string (сейчас спасает whitelist, но хрупко).
- Фронтенд: нет catch-all маршрута и ErrorBoundary (`App.tsx:57-72`); UTC/локальные даты смешаны (`PeriodPlanPage.tsx:50-55 nextFriday`); дробный ручной ввод vs целочисленная раздача количеств (`PeriodPlanPage.tsx:795-865`); индексные ключи списков + небезопасные касты.

---

## LOW / поддерживаемость

- God-файлы: `planning_service.py` (4143 стр.), `period_plan_service.py` (1804), `production_control_journal.py` (1407), `production_control_material_issues.py` (1384), `plan.py` (1692), `specification.py` (1246); фронт — `PeriodPlanPage.tsx` (1721), `ProductionControlPage.tsx` (1113).
- Дублирование helper'ов между god-файлами: `_prodplan_order_display_number`, `_material_issue_has_1c_link`, `_warehouse_name*`, `_unit_display_by_raw` (journal/printing/material_issues); XLSX-boilerplate (`mrp_result_export`/`forced_orders`); `rootOptions`-загрузка на фронте (3 места).
- Мёртвый/вводящий в заблуждение код: `_short_*_number` в экспортах, `_next_issue_number` (`count()+1`, гоночный), `nomenclature_search._generate_embedding` (MD5-«эмбеддинг», семантики нет), `odata_client.iter_by_guid` (monkey-patch с `self` вне класса).
- `item.replenishment_time or 30` / `or 0` (`planning_service`, `mrp_reconciliation.py:684`) — falsy-`or` подменяет легитимный `0`.
- `database.py:17-23` — `get_db` без `rollback()` в except; commit'ы разбросаны по сервисам, нет единого unit-of-work.
- CORS `allow_credentials=True` + `["*"]` методы/заголовки (`main.py:31-37`) — избыточно широко.

---

## Заметки о достоверности

- C4/C5 (двойной счёт, неидемпотентность/commit-on-failure) и C3 (окно POST↔commit) подтверждены чтением кода.
- Не подтверждены как баги (требуют прогона инструментов): «бесконечные ре-рендеры» в `ResourcesPage`/`WorkshopBindingReviewPage` и циклическая зависимость в `PurchaseControlPage` — там корректные `useCallback` deps, нужен ESLint react-hooks.
- Костыль маршрутизации по этапу **действительно удалён** из routing; `ResourceStage`/`suggest_workshops_by_stage` оставлены осознанно как рекомендательный слой для страницы «Разбор привязок» — это не мёртвый код.
- `production_orders.order_date`/`created_at` — наивные `DateTime` без `timezone=True` при стеке в `Europe/Moscow`: проверить, осознанно ли.
