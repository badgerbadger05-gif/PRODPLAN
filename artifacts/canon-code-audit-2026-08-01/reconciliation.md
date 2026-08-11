# Реестр разночтений «канон → код» — 01.08.2026

**Ревизия 3**. Ревизия 1 (09:42) — исходный аудит; ревизия 2 — сверка после
первой волны исправлений; ревизия 3 фиксирует решения владельца по бывшим
блокерам и их текущее исполнение.

Аудит рабочего дерева `repo/` (ветка `codex/repair-ledger-rebuild`, HEAD `39ce5d63`).
Направление сверки: **канон → код**. Код, противоречащий документации,
квалифицирован как легаси-дефект, а не как замысел.

Источники нормы: `.docs/CANON.md`, `.docs/notes/mrp-decisions-log.md` (§N),
контракты `.docs/*.md`. Исходный аудит — восемь параллельных проверок по доменам.

**Состояние прогона.** Ревизия 1: `1409 passed, 3 skipped, 8 failed`. Ревизия 2:
`1496 passed, 3 skipped, 0 failed` (`.venv/bin/python -m pytest -q -p no:randomly`,
снято с рабочего дерева, не с коммита). Восемь падений ревизии 1 предсуществовали
на HEAD `39ce5d63` и не были внесены волной — проверено прогоном в отдельном
worktree на чистом HEAD. Ревизия 3 после удаления live-custody fallback,
`CapacityScheduler`, сведения fold резервов и исправления surplus выпуска:
`1496 passed, 3 skipped, 0 failed`; JUnit —
`backend-full-after-output-surplus-fix.xml` в этом каталоге. После закрытия
Б-10 результат сохранён: `1496 passed, 3 skipped, 0 failed`; JUnit —
`backend-full-after-b10-fix.xml`. После закрытия Б-1 и Б-3:
`1500 passed, 3 skipped, 0 failed`; JUnit —
`backend-full-after-shelf-queue-fix.xml`. После exact-surplus и усиления
реестра писателей: `1501 passed, 3 skipped, 0 failed`; JUnit —
`backend-full-after-exact-surplus-writer-gate.xml`.
После централизации складского scope: `1503 passed, 3 skipped, 0 failed`;
JUnit — `backend-full-after-stock-owner-centralization.xml`.
После централизации процентов исполнения replenishment: `1504 passed, 3 skipped,
0 failed`; JUnit — `backend-full-after-execution-pct-owner.xml`.
После полного удаления retired `action="refresh"`: `1494 passed, 3 skipped,
0 failed` (10 положительных тестов удалённой модели также удалены); JUnit —
`backend-full-after-refresh-action-removal.xml`.
После переноса material coverage и purchase materialization eligibility из UI
к backend-владельцам: `1495 passed, 3 skipped, 0 failed`; JUnit —
`backend-full-after-frontend-contract-owners.xml`.
После централизации forecast, execution-progress и capacity status:
`1500 passed, 3 skipped, 0 failed`; JUnit —
`backend-full-after-frontend-math-owners.xml`.
После публикации `truth_meta` во frontend и закрытия трёх открытых enum:
`1501 passed, 3 skipped, 0 failed`; JUnit —
`backend-full-after-frontend-truth-enums.xml`. Frontend: lint, build,
`45` файлов / `269` unit-тестов, `3` bundle-budget и `12` Playwright smoke —
все зелёные.
После URL-состояния производственного журнала и строгих OpenAPI-контрактов
employees/operations, workshop-binding review и specification search:
`1504 passed, 3 skipped, 0 failed`; JUnit —
`backend-full-after-url-openapi-slice.xml`. Frontend: lint, build,
`45` файлов / `277` unit-тестов, `3` bundle-budget и `12` Playwright smoke —
все зелёные. Граница UI→services проверена полным гейтом.

---

## 0. Решения владельца по бывшим блокерам

Разрешать молча запрещено (постмортем `.docs/notes/incident-2026-07-canon-context-loss.md`,
вывод 4). Решения Б-1—Б-10 зафиксированы в §§18—25 журнала решений.

| # | Расхождение | Позиции | Статус |
|---|---|---|---|
| Б-1 | `pull_qty`: формула против прозы о перемещении | `shelf_projection_core.py`, §19 | **ЗАКРЫТ 01.08** — уменьшает только `saved_addressed_transfer_qty`, ограниченный свободным остатком другого склада; без сохранённой адресной проекции transfer равен нулю |
| Б-2 | Владелец custody и допустимость live-пути | `production_material_custody_projection.py`, §20 | **ЗАКРЫТ 01.08** — единственный владелец generation-scoped projection; live fallback удалён из чтений и команд |
| Б-3 | Единый источник `period_from` общей очереди | `assembly-queue-and-drum.md`, §21 | **ЗАКРЫТ 01.08** — builder требует зафиксированный период `PlanningRun`, валидирует равенство заголовку и сохраняет его в строке; барабан и FIFO читают сохранённый `sort_key` |
| Б-4 | §18 «Переработка»: канон замораживал flow «до решения владельца» | `replenishment.py:36-37` | **ЗАКРЫТ 01.08** — решение владельца дано в сессии codex; `REWORK_MARKERS` → `unavailable`, legacy-путь отключён. Остаточно: `PlannedRework`, схемы и модель в коде остались |
| Б-5 | Основание `Document_СборкаЗапасов` | `one_c_manufacture_export.py`, §22 | **ЗАКРЫТ 01.08** — обязано содержать `ДокументОснование` и `_Type`; payload и fail-closed guard реализованы |
| Б-6 | Единый номер цепочки против ключа по типу документа | `one_c_document_numbers.py`, §22 | **РЕШЕНИЕ ПРИНЯТО** — собственный детерминированный ключ и префикс для каждого типа; общий номер разных типов не требуется |
| Б-7 | Запись в `Catalog_Спецификации` | `spec_writeback_1c.py`, §22 | **ЗАКРЫТ 01.08** — запись разрешена как часть редактора; точка внесена в матрицу и реестр писателей |
| Б-8 | Операторское закрытие заказа | `one_c_production_order_export.py`, §22 | **ЗАКРЫТ 01.08** — кнопка и санкционированный экспорт реализованы; автоматическое закрытие запрещено |
| Б-9 | Отменённая DBR-модель в progress | `.docs/progress.md`, §24 | **ЗАКРЫТ 01.08** — устаревшее утверждение удалено/переписано |
| Б-10 | Технический `SupplierOrder*.updated_at` использовался как бизнес-cutoff и исключал заказ после обычной повторной синхронизации | `supplier_future_supply.py`, §25 | **ЗАКРЫТ 01.08** — `updated_at` удалён из cutoff-гарда и evidence hash; повторная синхронизация не меняет future supply, бизнес-поля фиксируются новым поколением |

---

## 1. ИСПРАВЛЕНО И ПОДТВЕРЖДЕНО (ревизия 3)

Проверено чтением кода, не по отчёту исполнителя.

### Двойной учёт физического количества — все три пути закрыты

| Было | Стало |
|---|---|
| **A-1 §16 не реализован.** `senior_hold_qty`/`free_s0_qty` отсутствовали целиком; `mrp_freeze.py` исключал из пула статичное `covered_from_stock_at_freeze_qty` → склад старшего плана доставался младшему | `mrp_freeze.py:1159-1168`: `senior_hold_qty = max(reserved_qty − attributed_qty, 0)`, накопление в `retained_stock_by_key` |
| **A-2 §6 адресность не реализована.** `MatchRule = Literal["fifo"]`; идентичность резолвилась и не читалась; отменённая формулировка «provenance only» жила в докстринге владельца и была закреплена тестом | `historical_replay_core.py:17` — `Literal["fifo","pegged"]`; `:112` `_is_addressed_match`; `:207,216` exact-first (`"pegged"`, `is_addressed=True`), остаток → FIFO. Докстринг удалён |
| **A-3 carry-forward тащил чужой cutoff.** Строки `ledger_future_supply` копировались с родительским `capture_cutoff` и `open_qty_at_cutoff` | `future_supply_capture.py:450` штампует `target_cutoff`; валидация равенства `:586` |
| **A-4 custody-фолд невоспроизводим.** Watermark по `id` при фильтре по `effective_at`, немонотонному по построению → молчаливая потеря дельт | `custody_projection.py:205-231`: гард на событие после baseline-watermark + отдельная ветка `effective_at > baseline_cutoff` |

### Молчаливая потеря и подмена факта

- **B-1** `historical_replay_persistence.py:298` — `warehouse_policy_missing` и
  `warehouse_ref_missing` теперь `raise ValueError`, а не тихий выброс факта мимо
  проверки сохранения количества.
- **B-3** гейт `ambiguous_identity_facts`, ронявший всё поколение вместо FIFO,
  из `generation_lifecycle.py` убран.
- **B-4** `frontend-erp-shell/src/lib/format.ts` — `qty()` возвращает `'—'` при
  `null`/`''`/не-числе вместо `Number(value || 0)`.
- Бейдж «Потребность закрыта полностью» (`ProductionDetailPane.tsx:210`) — под
  `mrpRemaining != null &&`; остаток при `null` рендерится `'—'`.

### Обход канонического запрета

- **C-2** `production_order_sync.py:53` — `last_result_truncated` наконец читается;
  закрытие локальных заказов по обрезанной выгрузке невозможно.

### Нереализованные разделы канона

- **§14** `supplier_receipt_allocation.py:263-284` — `PurchaseExportObligationAllocation`
  и `allocated_qty` читаются аллокатором.
- **§15.2** там же `:366,458` — `active_by_order_line` читается с `reversed()`
  (newest-first внутри строки заказа), больше не мёртвая структура.
- **§18** `replenishment.py:32-40` — пустой/неизвестный маршрут → `unavailable`.
- **§8** `assembly_output_core.py:109-131` — при нескольких exact-строках **одного**
  плана идёт `take(row, "exact")` в oldest-first порядке, `decision_status="allocatable"`.
  `ambiguous` остался только для строк из **разных** планов — канон этот случай не
  покрывает, вердикт принят как разумный.
- **Полка BOM-уровня ≥ 2** — join `parent_item_id == DrumSlot.item_id` устранён.
- **`covered_from_stock_at_freeze_qty` включал WIP** — `wip_order` из нетто убран.

### Вторые движки и хранилища

- **Д-1** `items.stock_qty` больше не пишется `odata_stock_sync` и не отдаётся
  роутером спецификаций. Колонка в модели осталась.
- **Д-4** канонический владелец `freeze_reservation_amounts` перестал быть мёртвым —
  вызывается из живого пути `reservation_ledger.py:208`.
- **Д-7** мёртвый параллельный планировщик: `build_planned_orders_and_purchases` —
  **0 ссылок**, удалён.
- **Д-9** `_journal_coverage_status` переписан: остались только
  `material_coverage_status` → `line_status`; ветки живого `issue_status`
  (`posted`/`requested`/`issued`/`exported`) удалены. Операционное состояние больше
  не вмораживается в снимок как обеспеченность.

### Гейты и гигиена

- `.github/workflows/canon.yml`: добавлен сервис `postgres:15` +
  `PRODPLAN_TEST_PG_URL` / `PRODPLAN_PG_CHECK_DSN` — три PG-теста
  (`test_material_issue_locking`, `test_pg_rebuild_check`,
  `test_reservation_replenishment_core_migration`) перестали молча пропускаться в CI.
  Это закрытие корневой причины №4 постмортема.
- Туда же добавлен `npm run smoke`; job `canon-invariants` расширен пятью suites
  (`test_generation_lifecycle`, `test_obligation_refresh_validation`,
  `test_assembly_output_persistence`, `test_mrp_freeze`, `test_purchase_control_snapshot`).
- Абсолютные машинные пути в `.docs/prodplan-shadow-deploy.md` — **0**.
- Мёртвая ссылка на `.docs/production_orders_odata_queries.md` в живом
  `production_order_sync.py` — **0**.
- Тесты, закреплявшие антиканоническое поведение
  (`test_historical_replay_core.py`, `test_assembly_output_core.py`,
  `test_physical_refresh_future_supply.py`), переписаны.

---

## 2. ОСТАЁТСЯ

### 2.1. Закрытое нарушение канона (P0)

**C-1. Количество материализованной строки переписывалось через 1С-синк.**
Старый путь безусловно выполнял `existing_product.quantity = quantity` после
сопоставления по `Ref_Key`.

Канон (`unified_production_journal.md`) запрещает прямое перезаписывание количества
исполнительной строки; ради этого удалён `dedupe_mrp_production_orders.py`, и
канон-тест механически проверяет отсутствие ручек правки. Публичного API
действительно нет — но ночной синк обходит запрет: MRP-заказ после экспорта получает
`order_ref1c`, синк находит его по `Ref_Key` и переписывает обязательство без нового
согласованного поколения.

**ЗАКРЫТ 01.08.** Синк обновляет количество только для `order.source != "mrp"`;
материализованное MRP-обязательство из 1С не перезаписывается.

### 2.2. Вторые движки

| # | Что | Где |
|---|---|---|
| Д-3 | **ЗАКРЫТ.** `_fold_entry` делегирует единственному `fold_reservation_entry`, который фильтрует события по поколению | `reservation.py`, `reservation_ledger.py` |
| Д-5 | Второй аллокатор фактов пока остаётся compatibility-проекцией кэша `ProductionProduct`; переполнение FIFO и exact-link закрыто: строка ограничена `quantity`, остаток явно попадает в `surplus_qty` | `production_fact_projection.py` |
| Д-7 | **ЗАКРЫТ.** `CapacityScheduler`, его тест и живой вызов удалены; фиксация не читает wall clock и отдельный календарь | удалено |
| Д-11 | **ЗАКРЫТ.** Live-пересбор маршрутного листа удалён; чтение только из сохранённого payload | `production_control_printing.py` |
| Д-2 | **ЗАКРЫТ.** Очередь сохраняет единый период и `sort_key`; расхождение run/header fail-closed; барабан и FIFO выпуска читают сохранённую очередь | три файла |
| Д-6 | **ЗАКРЫТ для MRP/reservation.** Scope склада и сумма `StockBin` централизованы в `mrp_stock_helpers.planning_stock_by_item`; фиксация, периодный план и резерв используют одного владельца, включая fail-closed при политике без выбранных складов и фильтр организации. Операционные складские выборки перемещений остаются отдельным доменом | `mrp_stock_helpers.py` |
| Д-8 | Писатели через `getattr(client, ...)` и `post_operation` теперь входят в точный AST-реестр; `Catalog_Спецификации` санкционирован решением владельца; recovery заказа поставщику читает до двух строк и падает при неоднозначности | `test_canon_invariants.py` |
| Д-12 | **ЗАКРЫТ для replenishment execution.** Все проценты исполнения периодного плана делегируют `reservation.replenishment_execution_pct`; сервис оставляет только округление до одного знака. Zero-base → `None`, переполнение клампится до 100 у владельца | `period_plan_service.py`, `reservation.py` |
| Д-13 | **ЗАКРЫТ.** Retired `action="refresh"` удалён из validator, publisher, snapshot и replay; orphan `create_candidate_run` и закреплявшие старую модель тесты удалены. Допустимы только `retain`, `retire`, `add` | пять сервисов + канон-гейт |

### 2.3. Фронтенд

Починен слой форматирования (`qty()`, бейдж остатка). Остальное не тронуто:

- предметная математика перечисленного аудита закрыта. Порог прогноза `days > 5`:
  один backend-владелец `forecast.py` отдаёт `forecast_status`, три UI-копии
  заменены одним presentation-компонентом без пороговой формулы. Выбор строк закупки закрыт:
  backend отдаёт `can_materialize` и `materialize_disabled_reason`, UI больше
  не восстанавливает право из `line_status`/`row_generator`/quantity.
  Классификация обеспеченности из `missing_qty` также закрыта:
  карточка использует backend `coverage_status`/`coverage_label`, неизвестное
  значение показывает как `Недоступно`;
- `pct >= 100 → ready` заменён backend `execution_progress_status`, который
  учитывает zero-base и lower-bound при частично доступной истине;
- `overload_hours > 0` заменён backend `capacity_status`; UI только отображает
  `overloaded` / `within_capacity` и fail-closed неизвестное значение;
- три открытых enum `Known | string` закрыты: статус закупочного факта и
  severity спецификации стали закрытыми union, а `employee_type` берётся из
  сгенерированной OpenAPI-схемы `employee | brigade`; неизвестная severity
  отображается fail-closed;
- транспорт всё ещё преимущественно нетипизирован: после текущего среза
  **125 из 175** операций в `docs/api/openapi.json` возвращают `{}` /
  `additionalProperties: true`. Контракты сотрудников и операций строки
  производственного заказа, а также оба read-only endpoint разбора привязок
  участков, поиск номенклатуры редактора спецификаций и текстовый поиск
  номенклатуры периодного плана и список складов настройки синхронизации уже
  строгие и frontend берёт их из сгенерированной схемы; основная масса ручных
  DTO остаётся следствием backend-контрактов;
- живое несовпадение `/v1/odata/groups` закрыто: backend больше не отдаёт raw
  `{value}`, а публикует строгий UI-view `{items, selected_ids}`. Кэш групп 1С
  и ручной выбор остаются независимыми; отсутствующий/битый кэш не стирает и
  не скрывает сохранённые Ref_Key;
- Обобщённый `AsyncState` намеренно не угадывает бизнес-статус из сетевой
  ошибки. Производственный журнал различает `stale`/`unavailable` только из
  структурированного `503 planning_truth_unavailable`; обычная сеть или чужой
  `503` показывают нейтральное «Статус истины не получен». Успешный
  `truth_meta` показывает status/generation/cutoff, неизвестный контракт
  трактуется fail-closed;
- фильтры, сортировка, страница и активный документ производственного журнала
  отражаются в URL через локальный типизированный адаптер. Внешние
  `product_id`/`order_id` сохраняются как ограничения запроса; transient
  multi-select для команд намеренно не сериализуется;
- backend CANON-гейт запрещает повторное открытие этих трёх enum и возврат
  fallback-подмены неизвестного `employee_type` на `employee`.

Из семи механических запретов `frontend-framework.md:269-277` обеспечены полтора:
граница HTTP (честный TS-AST) и `any` (неявно, через пресет `tseslint`).
Без проверки: дублирование DTO и третья копия таблицы/диалога. Закрытые enum и
конкретные устранённые копии предметной математики теперь удерживаются
CANON-гейтами; общего AST-гейта для любой новой предметной формулы пока нет.

### 2.4. Покрытие восьми архитектурных инвариантов `CANON.md:184-192`

| # | Инвариант | Ревизия 1 | Ревизия 2 |
|---|---|---|---|
| 1 | фикс. список писателей `ReservationEvent` | ДА, высокая | без изменений |
| 2 | единственные точки записи в 1С | частично | усилено: точный AST по `services/` ловит прямые `post/put/patch/delete`, alias через `getattr` и `post_operation`; санкционированные точки перечислены явно |
| 3 | нет второго хранилища остатков | **НЕТ**, нарушен | нарушение снято (Д-1), **проверки по-прежнему нет** |
| 4 | нет второго вычислителя reserve/replenishment/execution | ДА, низкая | владелец ожил (Д-4), но эвристика гейта прежняя; Д-3, Д-6, Д-12 не ловятся |
| 5 | нет ручных DBR-программ и `program_id` | частично | без изменений |
| 6 | нет предметной математики и прямого HTTP во frontend UI | HTTP да, математика нет | без изменений |
| 7 | один `planning_run` на фиксированный план | НЕТ в канон-гейте | БД-индекс + PG в CI усиливают, в канон-гейте по-прежнему нет |
| 8 | нет ссылок на несуществующую документацию | НЕТ по существу | висячие ссылки устранены, гейт остался black-list'ом из 10 имён |

Надёжно покрыто: **2 из 8**.

---

## 3. СТАТУС ФИНАЛЬНОЙ ПРОВЕРКИ

1. Frontend проверен 02.08: lint — green; Vitest `267 passed`; bundle-budget
   `3 passed`; production build — green; Playwright smoke `12 passed`.
   `apiTypes.ts` регенерирован из текущего OpenAPI и повторная генерация
   идемпотентна по SHA-256. `api:types:check` остаётся красным только потому, что
   его реализация сравнивает ремонтное рабочее дерево с прежним `HEAD`; после
   фиксации сгенерированного файла этот CI-гейт станет содержательной проверкой.
   Playwright после контрактных исправлений: `12 passed`; обновлён ровно один
   production-control baseline (`На складе` → backend label `Собрано`).
2. Поведение миграций `20260731_01..07` на реальном PostgreSQL локально не
   проверено (нет `PRODPLAN_TEST_PG_URL`); в CI теперь проверится.
3. Прогон снят с **рабочего дерева**, не с финального коммита волны. Канон
   (`CANON.md:167-168`) требует цифру с финального коммита — она не снята.
4. 12 изменённых visual-baseline не сверены.
5. Расхождение трёх построителей очереди (Д-2) установлено по коду, численно на
   реальных данных не измерено.
