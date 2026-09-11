# R1 — инвентарь идентичностей и контракт данных

Статус: инвентарь и контракт зафиксированы локально, 2026-09-10.

Этот документ — технический инвентарь R1. Предметные правила принадлежат
[журналу решений](../.docs/notes/mrp-decisions-log.md); здесь нет второй копии
формул. `docs/current-execution-roadmap.md` задаёт порядок волн и не является
источником предметной истины.

## 1. Граница инвентаризации

Поиск выполнен по ORM-моделям, Alembic, backend services/routers, exporters,
jobs/tools, raw SQL, tests и frontend transport/domain. В текущем checkout
`parent_generation_id` не является колонкой: он передаётся API/worker-ами и
хранится в JSON `ledger_generation.source_watermarks`. `snapshot_id` — FK
дочерних строк и часть transport/export payload; `ledger_generation_id` — FK
технической публикационной привязки. Ни один из трёх идентификаторов сам по
себе не является устойчивой бизнес-идентичностью движения, резерва, заказа или
плана.

## 2. Идентичности и полные ключи

### 2.1 Устойчивые идентичности

| Объект | Устойчивая идентичность | Техническая привязка, не часть identity |
|---|---|---|
| Принятое движение | нормализованный регистратор 1С + строка + версия/замена источника; локальный `StockLedgerEntry.id` | `ledger_generation_id`, `physical_import_batch_id` |
| Факт-замена | `StockLedgerFactSupersession.old_sle_id` → `new_sle_id` + import batch | поколение публикации |
| План и строка плана | `ProductionPlanHeader.id` и `ProductionPlanLine.id` | generation/cutoff снимка |
| MRP | `PlanningRun.run_id`, `source_plan_id`, lineage `prior_run_id`; один живой run на план | поколение, в котором был опубликован read-model |
| Резерв | `ReservationEntry.id`/`requirement_id` в пределах MRP | `ledger_generation_id` |
| Исполнение факта | `ReservationConsumptionAllocation` по `(sle_id, reservation_id)` и idempotency key | поколение приёма |
| Исполнительная строка | локальный ID + подтверждённая внешняя ссылка/строка 1С | snapshot/generation export batch |
| Read-model | `(consumer, snapshot_key, ledger_generation_id)`; строки `(snapshot_id, row_key)` | JSON candidate metrics |

Новый persistence не может использовать `generation_id` или `snapshot_id` как
замену этим ключам. Такой технический ID **не является бизнес-идентичностью**.
Исторический `ledger_generation_id` сохраняет provenance и
границу воспроизводимости, но не разрешает копировать неизменённые
обязательства.

### 2.2 Полный физический и распределительный ключ

Физический ключ: `(item_id, characteristic_ref, organization_ref,
warehouse_ref1c)`. Ключ распределения резерва дополнительно включает
`planning_stock_pool`; режим `make/buy/rework` выбирает журнал, но не создаёт
вторую математику. Точные связи допускаются только при полном подтверждении
идентичности; неизвестная/неоднозначная связь переходит в общий FIFO и не
выбрасывает факт.

Количества движения, норм, резервов и назначений хранятся как `Decimal`
`DECIMAL(15,3)` (или эквивалентный доменный `Decimal` до persistence). Нельзя
округлять промежуточное назначение или заменять `NULL`/`unavailable` нулём.
Корневой выпуск и плитка барабана штучные и целые; дробная корневая
потребность — ошибка вышестоящего контура, а нормы компонентов могут быть
дробными.

FIFO сортирует по полной предметной области: дата периода/срок, дата
периода плана, `run_id`, `requirement_id`, дата/номер bucket (если есть),
затем устойчивый локальный ID. При полном равенстве используется строковый
ключ движения/резерва. Порядок фактов детерминирован по `(posting_at,
fact_id)`. Возврат поставщику следует решению §15: исходное поступление
точно, затем строка заказа newest-first, затем глобально newest-first.

### 2.3 Граница MRP и исполнения

После фиксации frozen-план, BOM, полный резерв, покрытие на cutoff и исходная
потребность не меняются. Принятый факт меняет только текущий итог исполнения,
сохранённый остаток и действующие основания. При смене спецификации старый MRP
закрывается, его живые резервы освобождаются, а новый MRP того же плана
получает сохранённый остаток корней и отдельный журнал исполнения; correction
сравнивает сохранённый `old_remaining` с `new_required` по детали. Нулевой
остаток корней закрывает старый MRP без successor. Это граница предметного
решения §§3, 7 и 9 журнала.

Закрытие плана — бизнес-команда с сохранённым audit payload. Факт после
закрытия не исчезает и не переоткрывает план автоматически; корректировка
фиксируется отдельным основанием. Поступление, выпуск, расход удержанного
материала и закрытие заказа остаются разными событиями.

## 3. Реестр generation/snapshot-полей

### 3.1 Таблицы и FK

| Таблица/поле | Семантический класс | Основные писатели | Основные читатели |
|---|---|---|---|
| `ledger_generation.id`, `generation_key`, `source_watermarks` | факт публикации + техническая lineage JSON | `generation_lifecycle.py`, `physical_refresh_*`, `obligation_generation.py`, `obligation_refresh_*`, bootstrap/admin | `planning_truth.py`, `live_plan_scope.py`, все generation-bound projections |
| `ledger_build_batch.ledger_generation_id` | техническая стадия/метрика | lifecycle/build workers | publish gates, diagnostics, SQL verify |
| `planning_truth_state.current_generation_id` | указатель принятой истины | `planning_truth.py`, accepted publish | физическая visibility, routers/services как fail-closed gate |
| `planning_read_snapshot.ledger_generation_id` | immutable read-model truth | `mrp_result_projection.py`, `*_snapshot.py` | routers, exports, materialization, frontend DTO |
| `planning_read_row.snapshot_id`, `planning_read_root_member.snapshot_id` | дочерняя принадлежность snapshot | snapshot builders | snapshot readers/filter/export |
| `closed_plan_snapshot.ledger_generation_id` | audit факта закрытия | period-plan close/rebase | resume/rebase validation |
| `planning_run.ledger_generation_id` | происхождение frozen obligation | run candidate/obligation refresh | `live_plan_scope.py`, MRP snapshot and guards |
| `planned_order`, `planned_purchase`, `planned_rework.ledger_generation_id` | происхождение obligation row | MRP freeze/obligation refresh | journals, exporters, coverage |
| `production_products`, `production_material_issues.ledger_generation_id` | generation-bound operational projection | MRP/material issue services | production control/read-model |
| custody manifest/projection `ledger_generation_id`, `baseline_generation_id` | frozen custody projection + base | custody projection worker | material availability/read-model |
| supplier/future/export tables (`ledger_future_supply`, `purchase_export_line_allocation`, `purchase_export_batch.ledger_generation_id`, `planning_read_snapshot_id`, `purchase_export_obligation_allocation`, `sync_link`) | evidence/export idempotency | capture/materialization/one_c exporters | export retry/read-back, purchase journals |
| reservation tables (`reservation_entry`, `reservation_event`, `reservation_consumption_allocation`, `replenishment_work_item`) | generation-bound reserve/event projection | reservation ledger/replay/work-item builder | FIFO, coverage, make/buy journal |
| assembly/shelf (`assembly_queue_line`, `assembly_readiness`, `drum_schedule`, `shelf_projection`) | saved current read-model projection | queue/readiness/drum/shelf workers | routers/UI/export |
| supplier/output provenance (`stock_ledger_supplier_receipt_provenance`, `assembly_output_fact_decision`, `assembly_output_allocation`, `stock_bin`) | fact provenance and fold cache | replay/ingest/output allocators | physical visibility, audit/read-model |
| `assembly_output_repair_job.source_generation_id`, `phase1_generation_id`; target `published_generation_id` | repair audit state | output repair workflow | repair gate/audit |

Все перечисленные `ledger_generation_id` имеют FK на
`ledger_generation.id` с `RESTRICT`, кроме audit-полей repair job, которые
сейчас являются typed technical references без ORM FK. Это различие должно быть
сохранено в R3 mapping, а не замаскировано копированием строк.

### 3.2 `parent_generation_id` и JSON

`source_watermarks` допускает только проверенный manifest:

- `generation_kind=physical_refresh` — новый факт/физика, обязательства не
  переанкериваются;
- `generation_kind=obligation_refresh` + `parent_generation_id` — запечатанный
  fork обязательств, со snapshot IDs candidate-метрик до атомарной публикации;
- `replay_from`, cutoff и watermarks — техническая граница полноты источника.

Читать sealed-цепочку можно только в `item_ledger/live_plan_scope.py`.
`planning_run.ledger_generation_id == current_generation_id` — запрещённый
shortcut; факт-ветка и obligation-ветка расходятся. JSON `payload`, `metrics`,
`result` и transport DTO могут повторять `snapshot_id`/generation только как
provenance, никогда как business identity.

### 3.3 Readers/writers вне ORM

* **Workers/jobs:** `sync_orchestrator.py`, physical/historical import and
  rebuild tools создают/принимают generation; obligation refresh, MRP freeze,
  queue/readiness/drum/shelf and materialization workers создают projection.
* **Exporters:** `one_c_production_order_export.py`,
  `one_c_purchase_order_export.py`, `one_c_stock_transfer_export.py` и
  `spec_writeback_1c.py` используют generation/snapshot только для доказательства
  основания и идемпотентности; единственные writers в 1С перечислены в CANON.
* **Routers/API:** item-ledger admin принимает expected parent; plan,
  production-control и purchase-control читают/pin-ят `snapshot_id` и
  передают generation/cutoff/truth status. GET не строит новый снимок.
* **Raw SQL/Alembic:** lineage FK/unique/index определены в миграциях
  `20260723_*`–`20260904_*`; operational verify/cleanup —
  `tools/sql/verify_ledger_rebuild.sql`, `clear_*_ledger_projections.sql`.
  Эти скрипты не являются runtime-писателями и не могут быть fallback.
* **Frontend:** `src/services/{planning,purchaseControl,productionControl}.ts`
  передают transport IDs; `src/domain` отображает готовый backend metadata.
  UI не вычисляет и не сохраняет generation/snapshot.

### R4 current replenishment writer boundary

* `current_replenishment.py` is the sole current supplier-receipt assignment
  writer. Its accepted physical publication caller is
  `physical_refresh_orchestrator`; the call remains inside the caller-owned
  transaction.
* `ReservationConsumptionAllocation(is_current=true)` is the current basis
  with stable `(sle_id, reservation_id)` identity. `ledger_generation_id` is
  provenance only. `CurrentReplenishmentState` stores canonical distribution
  scope, source stream, revision and checksum; `CurrentReplenishmentAudit`
  stores changed pairs only.
* `supplier_receipt_allocation.py`/`ReservationEvent` remain historical
  generation-build provenance and are guarded after a completed current marker;
  they do not own current supplier receipt execution. Assembly-out material
  consumption is a separate semantic path and is not folded into this writer.

## 4. Классы данных и допустимые писатели

| Класс | Примеры | Допустимый писатель | Запрещённый shortcut |
|---|---|---|---|
| Факт | accepted Item Ledger, supersession, receipt/output/return | ingest/replay canonical services | статус заказа, `received_qty`, legacy aggregate |
| Frozen-обязательство | plan line, MRP requirement, reservation baseline | fixation/MRP owner до публикации | факт, GET, frontend, refresh текущего итога |
| Текущий итог | saved accepted output, folded coverage, remaining, queue/readiness | canonical persistence projection worker | второй calculator, прямой UI/ручная правка |
| Основание | fact→reservation/plan allocation, correction/return provenance | address/FIFO owner и audit writer | повторная запись unchanged pairs |
| Техническая метрика | build batch, task/debug status, algorithm/version | соответствующий worker | предметная отчётность или fallback |
| История бизнес-команды | close/rebase/export command + reason/actor | explicit command service | автоматическое закрытие/скрипт |

Допустимые runtime-writers ограничены: ingest/physical visibility; reservation
ledger and allocation; production output truth; plan/rebase/close command;
future-supply/custody capture; queue/readiness/drum/shelf projection;
sanctioned one-C exporters. Public GET, frontend, ad-hoc SQL, status sync и
обычный background tick не являются писателями предметных итогов.

## 5. Ручная семантическая матрица R1

Fixtures: `tests/contracts/test_r1_semantic_contract.py`. Они намеренно не
проверяют создание generation/snapshot и могут исполняться на pure functions.

| Сценарий | Вход | Ожидаемый результат | Что остаётся неизменным |
|---|---|---|---|
| Адресный + FIFO | факт `8.125`, exact reserve `2.125`, старший FIFO reserve `10` | `2.125` pegged, `6` FIFO, conservation | frozen reserve/base, IDs |
| Исправление | receipt-a `4` заменён на `1`, receipt-b не меняется | update только пары receipt-a; audit причины | receipt-b и reserve identity |
| Возврат | два поступления `3+2`, возврат `-2` | newest assignment unwinds first, без overrelease | исходные факты и frozen denominator |
| Закрытие | explicit close после accepted output | сохраняется closure payload и audit; поздний факт не reopens автоматически | план, факт, внешний документ |
| Смена MRP | план `10`, принято `4`, новая BOM | successor получает ровно корневой остаток `6`, старый MRP закрыт | план и принятые `4`, чужие планы |

Последние два сценария дополнительно покрываются существующими focused tests
`test_close_fixed_plan_resume.py` и `test_specification_mrp_rebase.py`;
R1 не добавляет второй implementation helper.

## 6. Решённое время и открытые вопросы, не блокирующие R1

1. **Две временные оси (решено):** `posting_at` означает когда движение
   произошло в источнике, `known_at` — когда PRODPLAN принял факт или
   исправление. Сохраняются обе даты. Режим `as_occurred` строится по
   `posting_at`, режим `as_known` — по `known_at`; режим отчёта выбирается
   явно, без молчаливого default/fallback.
2. **Backdate и область FIFO:** R1 фиксирует полный зависимый scope и
   детерминированный tie-break; точный минимальный набор для incremental
   persistence подтверждается в R5.
3. **Коррекция факта после закрытия:** R1 фиксирует сохранение факта и
   бизнес-закрытия без silent reopen; UI/API сценарий дорабатывается в R9.
4. **Legacy поколений:** до R3–R10 старые generation-bound persistence и
   readers не удаляются. Их допустимый вывод определяется mapping/preflight,
   а не feature flag и не произвольным выбором «последнего» snapshot.

Эти вопросы не блокируют документный контракт и pure fixtures, но блокируют
зависимую persistence/migration работу. R2 должен отдельно доказать локальную
БД и отсутствие внешних соединений.
