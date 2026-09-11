# Current-execution release report

Дата среза: 2026-09-11. Область: локальные волны R1–R11; R12 не разрешена. Продовые БД, SSH,
OData, боевые workers, deploy и push не использовались.

Follow-up R10 C evidence (2026-09-11): local-only `tools/r10_storage_rehearsal.py`
was exercised against the disposable-schema PostgreSQL DSN. The rehearsal
verified custom-format `pg_dump`/`pg_restore`, SHA-256 and `pg_restore --list`,
stable current IDs/subject values across `20260911_03`, and non-zero physical
`pg_total_relation_size` reclaim after dropping the three legacy relations.
No public schema or production system was changed.

## Сводка волн

| Волна | Статус на срезе | Доказательство/граница |
|---|---|---|
| R1 — контракт данных, границы и предметные решения | принято локально | test-first `fd57f8fd`, документный gate `48cbd509`, implementation/docs `299ae84b` + follow-up решения; focused gate ниже |
| R2 — локальный PostgreSQL и baseline | принято локально | WSL PostgreSQL 16 runtime, migration/rollback/API baseline/full gate зелёные; Docker остаётся необязательным альтернативным runtime |
| R3 — устойчивые идентичности и принятие физики | принято локально | runtime writers, completeness/publish guards, live-MRP pointer, successor/frozen provenance и migration mapping проверены; focused PG и full gate зелёные |
| R4 — транзакционные основания и текущее исполнение | принято локально | current writer, typed provenance, role separation, rebuild closure, PG atomicity и полный gate зелёные |
| R5 — исправления, отмены и backdate | принято локально | signed replay, explicit history mode, mixed provenance, correction audit, migration and full local gate зелёные |
| R6 — физический Ledger и custody | принято локально | compact StockBin/current custody, publication boundary, role-separated holds, PG MVCC и full gate зелёные |
| R7 — выпуск плана, MRP и будущие поставки | принято локально (после correction gate) | test-first `acc7a505`, `f784882f`, `d82853a4`, `340c143d`; implementation `8580aa92`, `91491516`, `b4524df2`; focused/PG/migration/full gates зелёные |
| R8 — барабан, полки и мехцех | принято локально | stable current owner, manifest/readiness handshake, invalidation hooks, PG/MVCC и full gate зелёные |
| R9 — API, UI и обменные ссылки | принято локально | current-only readers/actions, transport anchors, fail-closed and local API/UI gates зелёные; completion evidence приведено ниже |
| R10 — миграция и удаление старого контура | принято локально | migrations 20260911_01/02/03, migration/PG/storage/cleanup/full gates зелёные; completion evidence приведено ниже |
| R11 — полная локальная приёмка | принято локально | generative/no-op/fault, migration/storage, API/UI и полный локальный gate зелёные; evidence приведено ниже |

`принято локально` выставляется только после полного exit gate соответствующей
волны. R1–R11 приняты локально после соответствующих локальных exit gates.

## R1 evidence

### Commits

* `fd57f8fd` — test-first semantic fixtures: адресный/FIFO, Decimal
  conservation, correction и supplier return.
* `48cbd509` — test-first document gate для явного исторического времени.
* `299ae84b` — implementation/docs: inventory и согласование CANON,
  planning-truth, frontend framework и decisions log.
* Follow-up после gate: решение временных осей, release report и минимальная
  правка doc-gate assertion.

### Files and inventory

* `tests/contracts/test_r1_semantic_contract.py` — ручные входы/выходы,
  не зависящие от generation/snapshot persistence.
* `docs/r1-data-contract-inventory.md` — таблицы, FK, JSON watermarks,
  readers/writers, exporters/jobs/raw SQL, identity/full keys, Decimal scale,
  tie-break, facts/frozen/current/audit/technical classes и MRP boundaries.
* `.docs/CANON.md`, `.docs/planning-truth-contract.md`,
  `.docs/frontend-framework.md`, `.docs/notes/mrp-decisions-log.md` — единый
  нормативный контракт без дублирования предметных формул.

### Historical time decision

Журнал решений §32 теперь нормативно различает `posting_at` (когда движение
произошло) и `known_at` (когда PRODPLAN принял факт или correction). Оба поля
сохраняются. Отчёт явно принимает `history_mode`: `as_occurred` либо
`as_known`; неуказанный режим не подменяется fallback. Исправление сохраняет
исходный `posting_at` и получает новый `known_at`.

### Tests

Красный test-first прогон:

```text
pytest -q tests/contracts/test_r1_semantic_contract.py
3 passed, 1 failed — отсутствовал R1 inventory/contract artifact
```

Красный документный gate (`48cbd509`): отсутствовали `as_occurred`,
`as_known`, `known_at` и явный `history_mode` в единственном журнале решений.

Зелёный focused gate после документной реализации:

```text
pytest -q tests/contracts/test_r1_semantic_contract.py tests/test_canon_invariants.py
37 passed
```

Финальный focused semantic/canon/closure/rebase gate после всех R1 tail docs:
`93 passed`.
Фактический full pytest gate оркестратора на commit `b47b02d7`:
`1915 passed, 3 skipped, 35 warnings in 184.48s`; исходный untracked
`current-execution-full-pytest.log` сохранён без изменений.

### Removed paths and residual risks

Удалённые пути: **нет**. R1 только фиксирует допустимых writers и запрещает
новые читатели sealed lineage вне `live_plan_scope.py`; вывод legacy paths
перенесён в R3–R10 mapping/preflight.

Остаточные риски: backdate требует доказать минимальный incremental scope в
R5; schema/persistence пока generation-bound; API/UI ещё не передают новый
`history_mode`; persistence и downstream API/UI migration ещё не проверялись.
Эти риски не разрешают объявлять R6–R11 выполненными.

## R2 evidence

### Commits and files

* `3f5f2914` — test-first контракт изолированного PostgreSQL-контура и
  синтетический набор.
* `ca885897` — implementation: PG-only compose, explicit local DSN guard,
  start/verify scripts, baseline runner, integration migration/rollback tests
  и регистрация integration marker.
* `4eba5cfc` — test-first проверка реального DB-backed FastAPI latency probe
  с sample count/p50/p95.
* `80fe4721` — implementation API probe: детерминированный seed items,
  реальный `GET /api/v1/items/` через FastAPI `TestClient`, warm-up и 9
  измеренных samples.
* `2c748a4a` — test-first обязательные timestamps/idempotence seed и WSL
  runtime entrypoint.
* `3fba61af` — implementation WSL runtime/identity guard и обязательные
  `created_at`/`updated_at` для synthetic items.
* `d29196a4` — test-first WSL `psql` transport для существующего локального
  PostgreSQL rehearsal.
* `c7962afa` — implementation WSL `psql` transport в `pg_rebuild_check.py`.
* `d15a6f24` — test-first exact keeper PID/start identity and root-command
  contract.
* `e629dd63` — implementation root-owned WSL commands and hidden keeper state
  outside the repository.
* `tests/r2/test_r2_local_contract.py` и
  `tests/r2/test_r2_postgres_integration.py` — guard, migration, две сессии и
  rollback-проверки.
* `tests/r2/fixtures/r2_synthetic_seed.json` — фиксированный seed
  `r2-fixed-20260910-v1`: 2 плана, общая деталь, 2 пула, адресная и
  агрегированная закупка, FIFO/backdate, отмена, rework, material custody,
  закрытие и смена MRP без переноса исполнения.
* `docker-compose.r2.yml`, `scripts/r2-postgres.ps1`,
  `scripts/r2-postgres-wsl.ps1`, `scripts/r2-postgres.sh`,
  `backend/app/r2_local_contract.py`, `tools/r2-baseline.py`,
  `tools/pg_rebuild_check.py`, `docs/r2-local-contour.md` — локальный PG-only
  contour без backend/frontend/worker процессов и external hosts.

### Commands and results

Красный test-first прогон до реализации:

```text
pytest -q tests/r2/test_r2_local_contract.py tests/r2/test_r2_postgres_integration.py
4 failed, 1 passed, 2 skipped
```

После реализации focused R2/canon/WSL rehearsal gate на живом DSN
оркестратора:

```text
$env:PRODPLAN_R2_TEST_DSN = $env:PRODPLAN_TEST_PG_URL = $env:PRODPLAN_PG_CHECK_DSN = 'postgresql://r2_user:r2_local_only@127.0.0.1:55441/prodplan_r2'
pytest -q tests/r2/test_r2_local_contract.py tests/r2/test_r2_postgres_integration.py tests/test_canon_invariants.py tests/test_pg_rebuild_check.py tests/test_material_issue_locking.py tests/services/test_reservation_replenishment_core_migration.py
57 passed in 20.61s
```

Красный API-latency test-first прогон до реализации probe:

```text
pytest -q tests/r2/test_r2_local_contract.py tests/r2/test_r2_postgres_integration.py
1 failed, 5 passed, 3 skipped
```

Красный keeper/root-ownership test-first прогон до hardening entrypoint:

```text
pytest -q tests/r2/test_r2_local_contract.py::test_r2_wsl_runtime_owns_root_commands_and_exact_keeper_state
1 failed
```

Проверка синтаксиса compose прошла:

```text
docker compose -f docker-compose.r2.yml config --quiet
exit 0
```

Единая команда запуска/проверки Windows задокументирована как
`pwsh -NoProfile -File .\scripts\r2-postgres.ps1 -Runtime wsl -Action start-verify`.
Фактический результат:

```text
prodplan_r2|r2_user|127.0.0.1
R2 WSL PostgreSQL identity verified: 127.0.0.1:55441/prodplan_r2
```

Docker Desktop остаётся недоступен, но это необязательный альтернативный
runtime; WSL PostgreSQL 16.15 является поддержанным локальным contour.
Keeper state хранится вне репозитория в
`%LOCALAPPDATA%\PRODPLAN\r2-runtime\wsl-keeper.json`; после паузы повторный
`verify` подтвердил тот же PID/start identity. Чужой orchestrator handle не
останавливался. В независимой проверке старый orchestrator WSL session был
остановлен; keeper PID `23396` с сохранённым `start_utc` остался жив, после
чего `verify` и прямое PostgreSQL-соединение на том же DSN прошли.

Environment: локально в WSL Ubuntu установлен PostgreSQL `16.15` как R2
dependency; это не production и не внешний contour.

Migration/round-trip/rehearsal:

```text
python tools/pg_rebuild_check.py --dsn $env:PRODPLAN_PG_CHECK_DSN --stages migrate,round-trip,clear,verify
PASS migrate     20260909_02 (head)
PASS round-trip  head -> 20260726_14 -> head
SKIP clear       refusing destructive clear on a database this run did not create
PASS verify      smoke: executable; known-empty failure; summary projection executable
---- overall: PASS (smoke mode)
```

The clear skip is intentional protection for the named pre-existing local
database; no destructive clear was executed.

Обязательный полный gate с финального R2 implementation-состояния
(`e629dd63`, поверх `c7962afa`; report-only docs commit следует отдельно):

```text
pytest -q
1931 passed, 35 warnings in 204.37s (0:03:24)
```

В полном gate skip отсутствуют: три прежних PostgreSQL проверки и три новых
R2 integration-проверки были выполнены на явном WSL DSN. `pg_rebuild_check`
получил WSL `psql` transport; ни один тест не переключался на
production/default DSN.

`tools/r2-baseline.py` фиксирует seed/объёмы, elapsed time, SQL writes,
temporary table bytes и server identity. После успешной миграции он также
возвращает числовые `api_sample_count`, `api_latency_ms.min`, `p50`, `p95` и
`max` для DB-backed `GET /api/v1/items/?skip=0&limit=100`; `null` fallback
удалён. Два последовательных baseline на WSL PostgreSQL 16.15:

```text
run-1: api_latency_ms.p50=3.863, p95=4.881
run-2: api_latency_ms.p50=3.802, p95=4.421
seed_rows=3|true (rows|timestamps_nonnull)
seed=r2-fixed-20260910-v1, database=prodplan_r2, user=r2_user
```

Повтор idempotent seed сохранил ровно 3 synthetic item rows с ненулевыми
`created_at`/`updated_at`.

Удалённые пути: **нет**. Изменений production persistence, внешних адресов,
SSH/OData, live 1С, deploy или workers нет.

Остаточные риски: destructive `clear` rehearsal намеренно не выполнялся на
уже существующем локальном кластере; Docker runtime не проверен из-за
неработающего Docker Desktop, но WSL runtime и keeper воспроизводимы и
зелёные. R3 началась отдельным промежуточным schema/contract slice; см. ниже.

## R3 evidence — accepted local gate

R3 принято локально после подключения runtime writers и atomic publish
boundaries, focused PostgreSQL gate, migration round-trip и полного pytest с
нулём skips. R4 принята локально в отдельном разделе ниже.

### Commits and files

* `395270c7` — test-first: R3 identity, source completeness, explicit live-MRP
  pointer и frozen-basis contract tests; красный сборочный прогон завершился
  ожидаемым `ModuleNotFoundError` отсутствующего модуля.
* `45e4f184` — implementation: `PhysicalImportPage`, completeness boundary,
  stable `StockLedgerEntry.business_identity`, explicit historical mapping,
  `PlanningLivePointer`, `PlanningRunSuccessor`, frozen-basis generation
  provenance, R3 migration `20260910_01` и ingest identity writer.
* `1ec015ec` — mechanical follow-up: ORM mapping table и закрытие FK/clear
  contract в `tools/sql/clear_rebuildable_ledger_projections.sql`.
* `42c7739c` — additional runtime integration tests for identity, mapping,
  pointer/successor, completeness and frozen provenance.
* `67ce079f` — PostgreSQL integration coverage for R3 schema/visibility and
  100-repeat identity stability.
* `37cec300` — implementation wiring: runtime identity enforcement, physical
  visibility guard, generation/obligation publication pointers and canonical
  frozen-basis writer.
* `36ea6472` — candidate-discard mapping cleanup preserving accepted history.
* `39acb56e` — test-first current-reader regression tests: pointer-only
  resolution, no ancestor traversal, and stale/missing pointer failures.
* `091601df` — test-first PostgreSQL migration backfill regression and updates
  for the explicit pointer contract in existing fixtures.
* `0dacd38d` — implementation: fail-closed current-MRP resolver, pointer-based
  period-plan reader and mutation guard, current-pointer scope API, and
  deterministic migration backfill with ambiguity diagnosis.
* `dc3e4d28` — test-first regression proving the current journal selector uses
  the pointer when the run is anchored to an older physical generation.
* `05188599` — implementation wiring for pointer-aware current journal scope;
  historical generation filtering is no longer a current-MRP selector.
* `6e00c41a` — PostgreSQL current-journal reader now fails closed when a
  plan-bound active pointer is absent.
* `59daccd6` — test-first regressions proving missing fixed-plan pointers fail
  in both `current_live_run_ids` and the current journal after a fact-only
  generation fork.
* `9a647822` — implementation and fixture updates: current scope enumerates
  every fixed business plan and resolves each through `current_live_run`; the
  journal has no dialect-dependent or generation fallback.
* `tests/r3/test_r3_contract.py` — deterministic identity, incomplete/reordered
  page rejection, pointer-only current read и frozen provenance checks.
* `docs/r3-identity-acceptance-contract.md` — boundary/owner summary without
  duplicate formulas.

### Inventory and contract decisions

* Movement identity is `movement:<recorder_type>:<recorder_ref>:<line_no>`;
  generation/import batch remain provenance. `stock_ledger_business_identity_map`
  records every historical copy with explicit `legacy-explicit-backfill`, not a
  latest-row heuristic.
* `PhysicalImportPage` plus `PhysicalImportBatch.expected_page_count`,
  `received_page_count` and `source_complete` make partial, duplicate and
  reordered source pages non-publishable. Existing idempotent re-pull keeps the
  accepted SLE ID; a changed version uses existing supersession.
* Current MRP lookup uses `PlanningLivePointer`; `PlanningRunSuccessor` is
  business history and does not replace technical generation lineage. Frozen
  requirements gain `frozen_basis_generation_id`; current facts cannot rewrite
  that basis.
* Initial add/replacement publication updates pointer and successor in the
  caller-owned transaction; retry is idempotent, while retirement leaves no
  active pointer. Runtime identity collision fails closed; explicit legacy
  migration preflight rejects ambiguous active duplicates before the unique
  index and maps inactive/history copies deterministically.
* Existing fixed plans are backfilled into `PlanningLivePointer` during the
  R3 migration. Missing, retired, stale, non-fixed, or plan-mismatched active
  pointers fail closed; historical rebuild paths retain explicit generation
  lineage only where the read is semantically historical.
* The current scope starts from expected fixed business plans, not from pointer
  rows or current-generation `FIXED_SNAPSHOT` rows. A missing pointer therefore
  cannot silently omit a plan.

### Commands and results

Красный test-first:

```text
pytest -q tests/r3/test_r3_contract.py
ERROR during collection: ModuleNotFoundError: app.services.item_ledger.r3_contract

pytest -q tests/r3/test_r3_current_pointer_readers.py
ERROR during collection: ImportError: cannot import name CurrentMrpResolutionError

pytest -q tests/r3/test_r3_postgres_integration.py::test_r3_postgres_pointer_backfill_maps_existing_fixed_plan
FAILED: migration module had no backfill_live_pointers helper
```

Focused and migration gates:

```text
pytest -q tests/r3/test_r3_contract.py tests/services/test_item_ledger_physical_revision.py tests/services/test_item_ledger_ingest.py
26 passed in 2.81s

python tools/pg_rebuild_check.py --dsn postgresql://r2_user:r2_local_only@127.0.0.1:55441/prodplan_r2 --stages migrate,round-trip,verify
PASS migrate 20260910_01 (head)
PASS round-trip head -> 20260726_14 -> head
PASS verify; overall: PASS (smoke mode)

$env:PRODPLAN_R2_TEST_DSN=$env:PRODPLAN_TEST_PG_URL=$env:PRODPLAN_PG_CHECK_DSN='postgresql://r2_user:r2_local_only@127.0.0.1:55441/prodplan_r2'; pytest -q tests/r3 tests/services/test_period_plan_obligation_refresh_contract.py tests/services/test_mrp_mutation_guard.py tests/services/test_journal_truth_selectors.py
34 passed in 4.07s

$env:PRODPLAN_R2_TEST_DSN=$env:PRODPLAN_TEST_PG_URL=$env:PRODPLAN_PG_CHECK_DSN='postgresql://r2_user:r2_local_only@127.0.0.1:55441/prodplan_r2'; pytest -q tests/r3 tests/services/test_period_plan_obligation_refresh_contract.py tests/services/test_mrp_mutation_guard.py tests/services/test_journal_truth_selectors.py tests/services/test_production_control.py tests/services/test_production_control_journal_shelf_pull.py tests/services/test_paint_weld_chain_close.py
141 passed in 15.59s

$env:PRODPLAN_R2_TEST_DSN='postgresql://r2_user:r2_local_only@127.0.0.1:55441/prodplan_r2'; pytest -q tests/r3/test_r3_postgres_integration.py::test_r3_postgres_pointer_backfill_maps_existing_fixed_plan
3 passed in 0.77s

$env:PRODPLAN_R2_TEST_DSN=$env:PRODPLAN_TEST_PG_URL=$env:PRODPLAN_PG_CHECK_DSN='postgresql://r2_user:r2_local_only@127.0.0.1:55441/prodplan_r2'; pytest -q
1951 passed, 35 warnings in 211.83s (0:03:31); 0 skipped
```

Удалённые пути: **нет**. Сохранён untracked `current-execution-full-pytest.log`;
его содержимое не изменялось.

Остаточные риски: migration round-trip и runtime integration выполнены только
на локальном WSL PostgreSQL 16.15; production contour не проверялся и не
разрешён текущей задачей. Удалённых путей нет, untracked
`current-execution-full-pytest.log` сохранён без изменений.

## R4 evidence — локальная приёмка

R4 имеет статус `принято локально` после focused PG/canon gate, migration smoke
и полного pytest с нулём skips. Реализован один current writer для
адресного/FIFO replenishment: `current_replenishment.py` меняет только
изменившиеся пары, execution-поля и компактный source/revision marker в одной
транзакции. `ReservationConsumptionAllocation` остаётся каноническим
основанием со стабильным `(sle_id, reservation_id)` и `is_current`; generation
используется только как provenance. Supplier `ReservationEvent` не является
вторым текущим владельцем: после completed R4 marker его writer отклоняется.

### Commits and fixtures

* `01e7631d` — test-first canonical scope, explicit empty scope и PostgreSQL
  atomic visibility regressions.
* `e1a140e5` — test-first publication adapter и supplier `ReservationEvent`
  guard; `0a8c54cc` — точная область assertion для PG visibility.
* `67df9d4c` — implementation: canonical scope/source stream separation,
  source-key migration `20260910_04`, production physical-refresh caller,
  legacy-writer guard и accepted-generation adapter.
* `e740aadd` — implementation correction: current writer runs before accepted
  snapshots/work-items; supplier path persists typed provenance only in R4
  mode; obligation refresh uses the same current writer.
* `16c114c2` — implementation correction: `allocation_role` separates
  `material_consumption` from `replenishment_receipt`, with migration
  `20260910_05` and consumer filters.
* `79361d9a` — portable SQLite checksum migration, role-aware metadata gate и
  regenerated OpenAPI contract.
* `0c6fa319` — test fixture isolation for repeated shared PostgreSQL runs.
* `d82a257d` — current R4 state/audit added to the rebuild clear-set, closing
  the migrated-schema FK invariant.
* `tests/services/test_current_replenishment_transaction.py` — stable IDs,
  exact retry/drift, stale revision, foreign pool, empty complete scope,
  rollback boundaries, adapter/guard и real PG visibility.
* `tests/r4/test_r4_postgres_integration.py` — два PostgreSQL соединения и
  concurrency lock/no-double-apply.

### Commands and results

Красный test-first прогон до implementation:

```text
pytest -q tests/services/test_current_replenishment_transaction.py
12 passed, 2 failed, 1 skipped
```

Focused gate с локальным WSL PostgreSQL DSN после implementation:

```text
$env:PRODPLAN_R2_TEST_DSN=$env:PRODPLAN_TEST_PG_URL=$env:PRODPLAN_PG_CHECK_DSN='postgresql://r2_user:r2_local_only@127.0.0.1:55441/prodplan_r2'
pytest -q tests/services/test_current_replenishment_transaction.py tests/r4/test_r4_postgres_integration.py tests/services/test_supplier_receipt_allocation.py tests/services/test_generation_lifecycle.py tests/services/test_obligation_refresh_orchestrator.py tests/routers/test_item_ledger_router.py tests/services/test_reservation_replenishment_core_migration.py tests/test_canon_invariants.py
160 passed in 21.26s
```

Migration smoke на том же DSN:

```text
python tools/pg_rebuild_check.py --dsn $env:PRODPLAN_PG_CHECK_DSN --stages migrate
PASS migrate 20260910_05 (head)
```

Финальный full pytest с implementation commit `d82a257d`:

```text
pytest -q
1971 passed, 35 warnings in 199.30s (0:03:19)
```

В полном gate skips отсутствуют; focused gate также завершился без skips.

Удалённые пути: **нет**. `current-execution-full-pytest.log` — чужой untracked
файл, сохранён без изменений. Production/SSH/OData/live 1С/workers/deploy/push
не использовались.

Остаточные риски: adapter намеренно fail-closed при неоднозначных pool scope;
локальная приёмка не является production rollout и не проверяет production
contour. R6–R11 не начинались.

## R5 evidence — локальная приёмка

R5 имеет статус `принято локально` после focused semantic/legacy/canon gate,
реального PostgreSQL replay gate, migration head `20260910_07` и полного pytest
с нулём skips. Реализован signed replay corrections/returns поверх единого R4
allocator/current writer: explicit `as_occurred`/`as_known`, сохранение
`posting_at`/`known_at`, полная convergence boundary, closed/unknown fail-closed,
mixed exact/FIFO provenance и явный over-return result. Production persistence
не запускалась; OpenAPI surface не менялся, поскольку новый route/DTO не
добавлялся.

### Commits and fixtures

* `9bbd8efe` — test-first базовые R5 fixtures для correction, cancel, return,
  backdate, Decimal и metamorphic replay.
* `ba4972d5` — test-first расширение return modes, addressed/aggregated caps,
  history modes, unknown/closed и foreign pool.
* `e855eaee` — test-first выравнивание ожидаемых результатов с полным
  каноническим replay.
* `066f5e0e` — test-first PostgreSQL/current integration: persisted correction
  audit, exactly-once retry и out-of-order supersession с одной active version.
* `45acb52c` — test-first mixed addressed provenance и over-return surplus.
* `f37565ba` — test-first isolation: PostgreSQL replay fixture retires its
  synthetic facts and asserts no active rows remain for later rebuild rehearsal.
* `07e471b2` — implementation: signed replay/current-writer integration,
  `known_at`, correction audit reason/basis IDs и migrations `20260910_06`,
  `20260910_07` (`mixed` match rule).

Fixtures: `tests/r5/test_r5_correction_returns.py` содержит hand-calculated
addressed/FIFO, exact original/order-line/no-ref returns, backdate time-axis,
out-of-order versions, closed/unknown obligations, foreign pool, Decimal,
convergence and metamorphic cases. `tests/r5/test_r5_current_replay_integration.py`
проверяет persisted correction audit, idempotent retry, stale revision, physical
source guard, PostgreSQL path и supersession visibility.

### Commands and results

Красный test-first regression до implementation correction:

```text
pytest -q tests/r5/test_r5_correction_returns.py tests/r5/test_r5_current_replay_integration.py
2 failed, 13 passed, 1 skipped — mixed provenance и unmatched_return_qty отсутствовали
```

Focused R5/affected/canon gate на локальном PostgreSQL:

```text
$env:PRODPLAN_R2_TEST_DSN=$env:PRODPLAN_TEST_PG_URL='postgresql://r2_user:r2_local_only@127.0.0.1:55441/prodplan_r2'
$env:PRODPLAN_PG_CHECK_DSN='postgresql://r2_user:r2_local_only@127.0.0.1:55442/prodplan_r2'
pytest -q tests/r5/test_r5_correction_returns.py tests/r5/test_r5_current_replay_integration.py tests/services/test_supplier_receipt_allocation.py tests/services/test_current_replenishment_transaction.py tests/services/test_generation_lifecycle.py tests/services/test_obligation_refresh_orchestrator.py tests/services/test_reservation_consumption_allocation_schema.py tests/test_canon_invariants.py
160 passed in 18.42s
```

R5 PostgreSQL replay subset after migration:

```text
pytest -q tests/r5/test_r5_current_replay_integration.py tests/r5/test_r5_correction_returns.py
16 passed in 0.75s
```

Migration/round-trip/verify на чистом именованном локальном WSL PG16 cluster
(`r5gate`, port 55442; основной R2 contour 55441 не изменялся):

```text
python tools/pg_rebuild_check.py --dsn $env:PRODPLAN_PG_CHECK_DSN --stages migrate,round-trip,verify
PASS migrate 20260910_07 (head)
PASS round-trip head -> 20260726_14 -> head
PASS verify smoke: executable; known-empty failure; summary projection executable
```

Финальный последовательный gate с implementation `07e471b2` и fixture-isolation
commit `f37565ba` на чистом local WSL PostgreSQL contour `127.0.0.1:55444`:

```text
$env:PRODPLAN_R2_TEST_DSN=$env:PRODPLAN_TEST_PG_URL=$env:PRODPLAN_PG_CHECK_DSN='postgresql://r2_user:r2_local_only@127.0.0.1:55444/prodplan_r2'
pytest -q
1987 passed, 35 warnings in 201.29s (0:03:21)
```

Последовательная проверка изоляции на том же DSN также прошла:

```text
pytest -q tests/r5/test_r5_current_replay_integration.py
3 passed in 0.86s
python tools/pg_rebuild_check.py --dsn $env:PRODPLAN_PG_CHECK_DSN --stages migrate,round-trip,verify
PASS migrate 20260910_07 (head)
PASS round-trip head -> 20260726_14 -> head
PASS verify smoke: executable; known-empty failure; summary projection executable
```

### Removed paths and residual risks

Удалённые пути: **нет**. `current-execution-full-pytest.log` — чужой untracked
файл, сохранён без изменений. Для финального full gate использованы два
локального WSL PostgreSQL 16.15 cluster `55444`; R5 integration fixture
retire-ит synthetic facts перед завершением теста, поэтому последующий R3
migration round-trip не получает ложных active duplicate identities.
Production/SSH/OData/live 1С/workers/deploy/push не использовались.

Остаточные риски: API/UI не получили новый history-mode route в R5 и продолжают
использовать существующий current read contract; production contour не
проверялся. Основной orchestrator contour 55441 ранее содержал накопленные
integration rows, поэтому rebuild round-trip был доказан на отдельном named
local contour, без очистки или остановки orchestrator DB.

## R6 evidence — локальная приёмка

R6 имеет статус `принято локально` после focused R6/canon/PG gate, migration
round-trip и полного pytest на локальном WSL PostgreSQL 16.15. Реализованы
компактный физический `StockBin` (одна current-строка на полный physical key),
явная accepted-publication boundary, role-separated custody/holds и strict
compact current custody reader. `LedgerGeneration.id` остаётся обязательной
provenance, но не является current identity. GET/read paths только читают
сохранённые current/accepted projection и не запускают rebuild.

### Commits, files and fixtures

* `481f5412` — test-first red gate: physical fold, negative stock, receipt vs
  assigned consumption, transfer/return/organization and custody baseline.
* `e12579b6` — расширенный R6 gate: MVCC publication boundary, current-reader
  no-generation-fallback, late custody baseline, foreign organization.
* `30c1ec12` — implementation: `StockBin.is_current`, migrations
  `20260910_08`/`20260910_09`, candidate/publication writer split, compact
  custody projection, physical writer flags and R6 PostgreSQL test.
* `a012b4f3` — accepted-generation source scope, role-aware material hold and
  custody compatibility correction.
* `eb98e580` — test-first correction gate: stale/mixed current provenance,
  explicit allocation default and no-rebuild compact custody reader.
* `27e6f38f` — implementation: `load_compact_current_material_custody`,
  fail-closed `StockBin` provenance, explicit material allocation default and
  historical freeze scope.
* `2a1fbfa7` — separate historical/building StockBin reader for explicit
  candidate/pinned reads; current readers remain pointer-bound.
* `25ea9914` — global compact-provenance validation and generation/status guard
  for historical reads.
* `52339341` — test-first rewind-baseline retention regression through an
  intermediate accepted projection.
* `ff42ae55` — test-first local post-cutoff custody tail publication regression.
* `0e3b14d7` — implementation of bounded custody baselines, local compact tail
  publication and strict event watermark handling.
* `b9454665` — test-first fail-closed regression for an unseen physical event
  before a later local custody event.
* `45928f50` — test-first real two-session PostgreSQL marker-lock regression.
* `bde23bed` — implementation of pre-INSERT marker serialization, monotonic
  custody watermark and row locking.
* `c04f80e6` — test-first compact StockBin publication regression (old current,
  stale key and active BUILDING staging).
* `7270bb8a` — test-first custody history-compaction regression.
* `d2fd3918` — test-first migration case for a BUILDING-only new physical key.
* `0a8b03e0` — implementation of compact accepted StockBin/custody migration
  pruning and publication cleanup.

Relevant files: `backend/app/models.py`, `backend/app/services/item_ledger/
{physical.py,ingest.py,generation_lifecycle.py}`,
`backend/app/services/{mrp_stock_helpers.py,mrp_freeze.py,
production_material_custody_projection.py,release_feasibility.py}`,
`backend/alembic/versions/20260910_08_r6_compact_stock_bin.py`,
`20260910_09_r6_compact_custody.py`, `tests/r6/`, and affected historical
reader tests. No second stock or custody quantity owner was introduced.

### Commands and results

Красный test-first прогон:

```text
pytest -q tests/r6/test_r6_physical_custody_contract.py
ERROR during collection: ModuleNotFoundError: app.services.item_ledger.current_physical
```

Красный correction gate (`eb98e580`):

```text
pytest -q tests/r6/test_r6_physical_custody_contract.py tests/services/test_release_feasibility.py::test_material_custody_is_not_free_for_a_new_release
4 failed, 6 passed — stale provenance, allocation default and compact loader were not implemented
```

Focused R6/affected gate after final implementation:

```text
$env:PRODPLAN_R2_TEST_DSN=$env:PRODPLAN_TEST_PG_URL=$env:PRODPLAN_PG_CHECK_DSN='postgresql://r2_user:r2_local_only@127.0.0.1:55444/prodplan_r2'
pytest -q tests/r6 tests/r2/test_r2_local_contract.py tests/test_canon_invariants.py tests/services/test_generation_lifecycle.py::test_stock_bin_publication_removes_old_accepted_copy_and_keeps_building_stage tests/services/test_production_material_custody_projection.py tests/services/test_item_ledger_position_generation_truth.py tests/services/test_production_control_journal_snapshot.py::test_work_item_materials_remain_readable_from_the_published_row_generation tests/services/test_release_feasibility.py::test_material_custody_is_not_free_for_a_new_release
94 passed in 11.69s
```

Focused PG/canon gate:

```text
$env:PRODPLAN_R2_TEST_DSN=$env:PRODPLAN_TEST_PG_URL=$env:PRODPLAN_PG_CHECK_DSN='postgresql://r2_user:r2_local_only@127.0.0.1:55444/prodplan_r2'
pytest -q tests/r6/test_r6_postgres_integration.py tests/r2/test_r2_local_contract.py tests/test_canon_invariants.py
42 passed in 7.18s
```

Migration/round-trip/verify:

```text
python tools/pg_rebuild_check.py --dsn $env:PRODPLAN_PG_CHECK_DSN --stages migrate,round-trip,verify
PASS migrate 20260910_09 (head)
PASS round-trip head -> 20260726_14 -> head
PASS verify; overall: PASS (smoke mode)
```

Финальный full pytest на DSN `127.0.0.1:55444` после implementation:

```text
2004 passed, 35 warnings in 204.26s (0:03:24)
```

Удалённые пути: **нет**. `current-execution-full-pytest.log` — чужой untracked
файл, сохранён без изменений. Production/SSH/OData/live 1С/боевые workers/
deploy/push не использовались.

Остаточные риски: accepted/failed StockBin и custody copies удаляются при
migration/promotion; active BUILDING rows и explicit custody rewind baselines
остаются bounded staging/provenance. Локальные issue/terminal custody events
сериализуются до INSERT через planning-truth marker; physical/backdated gaps
fail closed до следующей accepted publication. Live/prod contour намеренно не
проверялся. R8–R11 не начинались.

## R7 evidence — локальная приёмка после correction gate

R7 имеет статус `принято локально` только после correction gate: focused,
двухсессионный PostgreSQL/MVCC gate, seeded migration round-trip и полный
pytest на финальном implementation commit завершились без skip. Retained
обязательства не копируются и не retarget-ятся; current future supply имеет
отдельного compact owner и append-only change audit. Production/SSH/OData/live
1С/боевые workers/deploy/push не использовались.

### Commits, files and contract

* `1a7d7151` — test-first stable `LedgerFutureSupply.current_identity` и
  staged identity contract.
* `a1c0af7d` — test-first semantic specification import: порядок строк,
  Decimal formatting и display noise не создают новую revision/rebase.
* `13d546e4` — implementation: stable future-supply identity/current
  staging, migration `20260910_10`, current GET/material-availability
  guards, numeric specification canonicalization и publication integration.
* `acc7a505`, `f784882f` — test-first retained-run rebase after unrelated
  refreshes and stale-copy exclusion by immutable run anchor.
* `d82853a4` — test-first proof that obligation refresh does not invoke the
  legacy retained-reservation copy writer.
* `340c143d` — test-first stable current-owner correction/return, schema
  metadata and two-session PostgreSQL publication/no-op gate.
* `060a99da` — test-first explicit reverse supplier return: `10 → 8` leaves
  open `2`, then `10 → 3` reopens `7` on the same current row with one audit
  edge and no current-row multiplication.
* `8580aa92` — implementation: `LedgerFutureSupplyCurrent` compact owner,
  `LedgerFutureSupplyCurrentChange` audit, exact-only current publication,
  retained-run anchor readers, and no-copy/no-retarget obligation refresh.
* `91491516` — reversible migration `20260910_11`: downgrade restores legacy
  marker only through an unambiguous identity/generation/capture-batch mapping.
* `b4524df2` — rebuild-clear SQL and production-control fixture updated for
  the compact current owner.

Существующий canonical output path сохранён: `document_net_output.py` →
`assembly_output_core.py` → `assembly_output_persistence.py`; stable
`ProductionPlanExecutionFact` и persisted line accepted/remaining являются
единственным current owner. Existing specification rebase сохраняет matrix и
remaining roots; R7 не создаёт второй output/FIFO engine. Future supply
разделён на immutable generation evidence и pointer-bound compact current rows.
Удалённые пути: **нет**.

Existing regression evidence covers plan `10 → accepted 4 → successor remaining
6` without double-credit, immutable source matrix, foreign retained run
preservation, exact/FIFO output and document return netting, closed/fully
produced rebase, Decimal/tie-break/idempotent replay, future-supply rejection
and closure history, and fail-closed missing/ambiguous source evidence.
BUILDING current rows are staged and invisible; accepted publication switches
the pointer-bound compact owner. One identity survives 100 distinct accepted
technical generations with unchanged ID/update timestamp and no audit churn;
correction `6 → 2 → 7` reuses that ID with one audit edge per business change,
and the explicit reverse-return fixture separately proves `10 → 8 → 3`
(`open 2 → 7`) on the same current row with exactly one additional audit edge.
Explicit close emits one immutable close audit. Equivalent specification
import and repeated rebase/capture are no-ops; genuine specification change
creates one revision request.

### Red, focused and migration evidence

Красный test-first прогон до соответствующих реализаций:

```text
pytest -q tests/services/test_ledger_future_supply_schema.py tests/services/test_future_supply_capture.py::test_capture_assigns_stable_current_identity_and_stays_staged_until_publish
2 failed, 2 passed — current_identity/is_current отсутствовали

pytest -q tests/services/test_spec_component_child_spec_sync.py::test_semantically_equivalent_spec_import_is_idempotent
1 failed — числовые `1.000` и `1` давали разные revision hashes
```

Focused R7/affected suites после correction implementation:

```text
pytest -q tests/r7/test_r7_postgres_integration.py tests/services/test_future_supply_capture.py tests/services/test_ledger_future_supply_schema.py tests/services/test_specification_mrp_rebase.py tests/services/test_obligation_refresh_orchestrator.py tests/services/test_obligation_refresh_publish.py tests/services/test_carry_forward_retained_reservations.py tests/test_ledger_rebuild_operations.py tests/test_canon_invariants.py
135 passed in 23.21s

pytest -q tests/services/test_future_supply_capture.py::test_current_future_supply_reverse_return_keeps_owner_and_audits_once
1 passed in 0.45s
```

Real PostgreSQL two-session/MVCC gate on the named local contour:

```text
$env:PRODPLAN_R2_TEST_DSN=$env:PRODPLAN_TEST_PG_URL=$env:PRODPLAN_PG_CHECK_DSN='postgresql://r2_user:r2_local_only@127.0.0.1:55444/prodplan_r2'
pytest -q tests/r7/test_r7_postgres_integration.py
1 passed in 0.81s
```

The independent reader saw the old pointer/quantity while publication was
uncommitted, then the new quantity on the same current row ID after commit;
two exact retries added no audit rows. The fixture restores a valid prior
pointer (or the explicit NULL known-empty state) and cleans only its named
rows.

Local PostgreSQL migration/round-trip/verify on the named R2 contour:

```text
python tools/pg_rebuild_check.py --dsn postgresql://r2_user:r2_local_only@127.0.0.1:55444/prodplan_r2 --stages migrate,round-trip,verify --no-seed
PASS migrate 20260910_11 (head)
PASS round-trip head -> 20260726_14 -> head
PASS verify; overall: PASS (smoke mode)
```

Seeded current-owner downgrade/upgrade proof (same local DSN, exact named
seed `r7-seeded-roundtrip-20260910`, removed after verification):

```text
alembic upgrade head -> downgrade 20260910_10 -> upgrade head: PASS
legacy staging is_current after downgrade: true
compact current row backfilled after upgrade: present
seed cleanup: exact rows only
```

### Final R7 gate and residual risks

`current-execution-full-pytest.log` — чужой untracked файл, сохранён без
изменений. Финальный full pytest с correction implementation `b4524df2`:

```text
$env:PRODPLAN_R2_TEST_DSN=$env:PRODPLAN_TEST_PG_URL=$env:PRODPLAN_PG_CHECK_DSN='postgresql://r2_user:r2_local_only@127.0.0.1:55444/prodplan_r2'
pytest -q
2012 passed, 35 warnings in 211.28s (0:03:31)
```

Skip отсутствуют. Остаточный риск — production contour намеренно не
проверялся; current publication и generation evidence остаются локально
проверенными на named PostgreSQL contour. Migration downgrade fail-closes при
отсутствующем или неоднозначном staging source; R10 cleanup ещё не начинался.

## R8 evidence — локальная приёмка

R8 закрывает только очередь сборки, readiness, барабан и полки. Публичные
current GET требуют единственный persisted `CurrentExecutionScope` manifest:
он хранит valid-empty состояние, accepted source revision/generation и
сохранённое summary. Отсутствующий, stale, неготовый или нестыкующийся с
`PlanningTruthState` manifest даёт 503; fallback к `PlanningReadSnapshot` или
generation-scoped GET удалён. `CurrentExecutionRow` — compact business owner;
generation ids остаются только provenance.

### Commits, files and invariants

Test-first commits:

* `e8a0433f` — red regression: generation-local queue ids churn readiness/drum
  current rows and audit.
* `5e3f66e7` — red regressions for all four contours (queue/readiness/drum/
  shelf) and current drum ordering.
* `97fbc692` — typed same-date/same-resource priority ordering regression.
* `b05544d6` — reference mutation invalidation behavior and no-op semantics.
* `e1cd8fa9`, `b4a67f4d` — custody invalidation/idempotent retry and unrelated
  queue-scope preservation.

Implementation/fix commits:

* `cec32cc9` — stable queue-owner mapping for readiness/drum, typed canonical
  current drum sort, and semantic no-op guards for rates/resources/shelf
  policies.
* `bb324555` — fixed transfer-custody invalidation revision propagation.
* `40f228c3` — persisted `drum_excluded` current rows and current GET mapping;
  exclusions remain part of the supported DrumSchedule response.
* `e2a8b2cf` — correction evidence: actual generation publisher creates and
  republishes excluded rows; calendar contract names the out-of-band unsafe gap.

Relevant implementation is in
`backend/app/services/item_ledger/current_execution.py`,
`backend/app/routers/production_control.py`,
`backend/app/routers/planning_rates.py`, `backend/app/routers/resources.py`,
and the existing `CurrentExecutionRow/CurrentExecutionScope` migrations
`20260910_12`–`20260910_14`. Legacy route tests now explicitly prove missing
current manifests fail closed. `current-execution-full-pytest.log` remains the
pre-existing untracked file and was not touched.

### Red, focused, PostgreSQL and migration evidence

Красные test-first проверки до реализации:

```text
pytest -q tests/r8/test_r8_current_execution.py::test_r8_generation_local_queue_ids_do_not_churn_current_readiness_or_drum
1 failed — current change/audit count grew from 4 to 6 on generation-local queue ids

pytest -q tests/routers/test_assembly_queue_router.py::test_current_drum_get_sorts_persisted_slots_by_date_resource_priority_and_ordinal
1 failed — returned [10, 20], expected canonical [20, 10]

pytest -q tests/r8/test_r8_invalidation_wiring.py::test_r8_reference_writers_are_idempotent_before_invalidating_on_real_change
1 failed — semantic no-op reference writes invalidated readiness

pytest -q tests/routers/test_assembly_queue_router.py::test_current_drum_get_returns_persisted_excluded_rows
1 failed — current GET returned an empty excluded list before persisted promotion

pytest -q tests/r8/test_r8_invalidation_wiring.py::test_r8_calendar_contract_names_missing_writer_as_explicit_unsafe_gap
1 failed — contract still described automatic fail-closed behavior for an absent writer
```

Focused current/canon/custody/affected gate with the local DSN:

```text
$env:PRODPLAN_R2_TEST_DSN=$env:PRODPLAN_TEST_PG_URL=$env:PRODPLAN_PG_CHECK_DSN='postgresql://r2_user:r2_local_only@127.0.0.1:55444/prodplan_r2'
pytest -q tests/r8 tests/routers/test_assembly_queue_router.py tests/test_canon_invariants.py tests/test_openapi_contract_sync.py tests/test_ledger_rebuild_operations.py tests/services/test_production_material_custody_projection.py tests/services/test_one_c_posted_transfer_sync.py
116 passed in 22.65s

Final R8 focused/canon route gate after excluded-row correction:

```text
$env:PRODPLAN_R2_TEST_DSN=$env:PRODPLAN_TEST_PG_URL=$env:PRODPLAN_PG_CHECK_DSN='postgresql://r2_user:r2_local_only@127.0.0.1:55444/prodplan_r2'
pytest -q tests/r8 tests/routers/test_assembly_queue_router.py tests/test_canon_invariants.py tests/test_openapi_contract_sync.py
76 passed in 13.53s
```
```

Migration and seeded round-trip/verify:

```text
python -m alembic -c alembic.ini upgrade head
INFO ... PostgresqlImpl ... (head 20260910_14)
python tools/pg_rebuild_check.py --dsn $env:PRODPLAN_PG_CHECK_DSN --stages migrate,round-trip,verify
PASS migrate 20260910_14 (head)
PASS round-trip head -> 20260726_14 -> head
PASS verify ... summary projection executable
---- overall: PASS (smoke mode)
```

Финальный full gate на том же DSN и с теми же тремя переменными:

```text
pytest -q
2040 passed, 35 warnings in 216.14s (0:03:36)
```

Skip отсутствуют. Удалённые пути: **нет**. Production/SSH/OData/live 1С,
боевые workers, deploy и push не использовались.

### Остаточные риски

Реальный WorkCalendarDay writer/API в R8-контуре отсутствует. Для будущего
writer зафиксирован обязательный transactional hook
`invalidate_current_execution_for_calendar_change`; out-of-band изменение без
него оставляет ready manifest и не обнаруживается автоматически — это явный
residual unsafe gap. Excluded drum rows теперь сохраняются как `drum_excluded` current
rows и возвращаются прежним API-контрактом; их coverage проверена focused
регрессией. R9 API/UI и R10 cleanup не начинались; production contour
намеренно не проверялся.

## R9 evidence — принято локально

R9 current-reader, action, transport-anchor и fail-closed slices прошли локальный
exit gate. Ниже сохранён исходный test-first inventory; отсутствие persisted
current manifest по-прежнему даёт 503, а legacy snapshot не используется:

| User path | Current owner / identity | Evidence |
|---|---|---|
| `production-control /orders` | `CurrentExecutionRow(production_control_journal)`; journal business identity | `tests/r9/test_r9_current_obligation_views.py`, `tests/r9/test_r9_production_sort.py` |
| `production-control /orders/root-products` | persisted `root_product_options` in the current manifest | `test_r9_root_products_read_current_production_rows` |
| production material/work-item reads | persisted `material_coverage_snapshot` on the current journal row | `test_r9_materials_read_current_payload_without_snapshot_fallback`, missing-manifest 503 regression |
| purchase journal selection/materialize | `CurrentExecutionRow(purchase_control_journal)` identities plus `CurrentExecutionScope.source_revision`; current export batch anchors to scope, legacy snapshot FK remains nullable for historical batches | `tests/r9/test_r9_current_obligation_views.py`, `tests/r9/test_r9_purchase_current_anchor.py`, `tests/services/test_purchase_export_materialization_schema.py` |
| MRP summary/detail/grouped/category/capacity | compact `mrp_result` rows and per-run manifest; missing current scope is 503, with no legacy snapshot fallback | `tests/r9/test_r9_mrp_current_reader.py` |
| MRP production/purchase/rework exports | current row/manifest identity and `source_revision` are returned for CSV/XLSX; rework export no longer references an undefined identity | `test_mrp_export_returns_current_identity_and_revision`, `test_mrp_rework_export_returns_current_identity_and_revision` |
| MRP purchase export-to-1C | stable selected current identities + expected revision are required; identities resolve to current purchase rows, unknown/foreign rows fail closed; router adapter emits a canonical idempotency key | `test_mrp_purchases_to_1c_resolves_current_identity_and_revision`, `test_mrp_purchases_to_1c_rejects_unknown_or_foreign_current_identity` |
| MRP result UI export action | selection stores only `row.current_identity`; missing identity is non-selectable; POST carries `current_identities` and `expected_source_revision` | `MrpResultPage.test.tsx` — 12 passed; `c2e4e7aa`, `38b6c692` |
| period execution | exact resolved current run; no all-run mixing | `backend/app/routers/plan.py`, commit `4b07347c` |
| production row actions | state/quantity/delete/return/material issue/export/from-work-items validate current identities and accepted manifest revision; UI forwards CAS fields and rejects incomplete context | `tests/r9/test_r9_production_action_contract.py`, `ProductionControlPage.test.tsx`, `productionControl.test.ts`; test-first `fb3f873e`, `d2da64d1`, implementation `756e1dfb` |
| production work-item materials / paint-weld / route sheets | current identity + manifest revision; persisted material/route payload only; numeric work-item IDs are current-validated locators; counterpart anchors are exact current proposal identities; GET has no replay/DML and POST marks only current sheet members | `tests/r9/test_r9_production_remaining_current_links.py`, `tests/routers/test_route_sheet_print_read_only.py`, `test_production_remaining_links_openapi_require_current_cas_fields` |

Test-first commits: `d7742fef` (production sort), `492f160f` and `8af02554`
(MRP current reader/fail-closed and metadata parity), `2610d9e7` (MRP HTTP
503), `85a23cba`, `91549375`, `677b3115`, `d0c51a4f` (root/material current
reader regressions), `0bcc5635`, `2d6e8d11` (purchase current anchor/sort/schema
and row transport red
contract), `a323c6ff`, `a922cdf2`, `a92bc147` (complete MRP endpoint
inventory, current export-to-1C identity/CAS and local fake retry contracts).
Implementation commits: `4b07347c`, `59b4880d`,
`1e5d611b`, `3573a758`, `1a87817c`, `de1a9b89`, `288d2ce8`, `0698737f`,
`d04f6d80`, `5271a50e`, `7ee23783`, `715f51bc`, `5c1eca3d`, `6f91171d`,
`03ffdbec`, `756e1dfb`.
Current-link test-first commits are `b64126d5`, `a5509e9b`, `496f7e87`,
`7499319e`, `5b9d3e17`, `d6877dba`, and `c463175a`; implementation commits are
`7a89adfa`, `c63e76b5`, and `3c21002d`. The snapshot-only route characterization was updated in
`dce15e8e` to assert a missing-current 503 rather than preserve the cancelled
fallback.

Focused current-reader gate:

```text
pytest -q tests/r9 tests/services/test_purchase_control_materialization.py \
  tests/services/test_purchase_export_materialization_schema.py \
  tests/services/test_supplier_future_supply.py
42 passed in 5.60s (MRP/current-reader focused rerun)

```
pytest -q tests/services/test_one_c_purchase_order_export.py \
  -k "recovers_exact_posted_batch_without_duplicate or retry"
5 passed, 13 deselected in 0.82s
```

Production action focused gate:

```text
pytest -q tests/r9/test_r9_production_action_contract.py
9 passed in 2.68s
npx vitest run src/services/productionControl.test.ts src/ui/pages/ProductionControlPage.test.tsx --reporter=dot
2 files passed, 55 tests passed
pytest -q tests/services/test_production_order_sync.py -k "fact_cache_keeps_cancellation_separate_from_physical_remaining"
1 passed, 25 deselected in 0.62s
```

Current-link focused gate:

```text
pytest -q tests/r9/test_r9_production_remaining_current_links.py \
  tests/routers/test_route_sheet_print_read_only.py \
  tests/r9/test_r9_production_action_contract.py \
  tests/routers/test_production_control_journal_contract.py \
  tests/services/test_one_c_purchase_order_export.py::test_purchase_order_export_recovers_exact_posted_batch_without_duplicate \
  tests/services/test_production_order_sync.py::test_production_fact_cache_keeps_cancellation_separate_from_physical_remaining
25 passed, 2 warnings in 3.29s
```

Coherent current-read/no-DML slice (test-first commits `6ac7b07f`,
`628b4f4c`, `5728df70`; implementation `ccb39657`):

* `load_current_execution_coherent` locks only the manifest with PostgreSQL
  `FOR SHARE OF current_execution_scope`, then starts a second statement for
  rows. This avoids the READ COMMITTED mixed snapshot (new manifest + old
  rows) and preserves a ready empty scope as a valid result.
* `tests/r9/test_r9_current_coherent_reads.py` covers writer-first and
  reader-first two-session interleavings, including the writer wait and the
  old-or-new boundary.
* `tests/r9/test_r9_current_get_contract_matrix.py` inventories production,
  purchase, period, MRP and assembly current readers, observes SQL writes,
  checks real representative route adapters fail closed at 503 before legacy
  readers, and verifies route-sheet GET with `mark_printed=true` performs no
  marking. The MRP grouped/detail/category/capacity/export matrix is also
  checked against the real current gate and legacy snapshot internals are
  prohibited by raising test doubles.

Focused local result:

```text
pytest -q tests/r9/test_r9_current_coherent_reads.py \
  tests/r9/test_r9_current_get_contract_matrix.py \
  tests/r9/test_r9_current_obligation_views.py \
  tests/r9/test_r9_mrp_current_reader.py \
  tests/r9/test_r9_production_remaining_current_links.py
48 passed, 2 skipped in 4.68s
```

Real local PostgreSQL interleaving/no-DML result on
`127.0.0.1:55444/prodplan_r2`:

```text
PRODPLAN_R2_TEST_DSN=PRODPLAN_TEST_PG_URL=PRODPLAN_PG_CHECK_DSN=<local DSN>
pytest -q tests/r9/test_r9_current_coherent_reads.py \
  tests/r9/test_r9_current_get_contract_matrix.py
8 passed in 2.51s
```

R9 is accepted locally through the current API/UI contract gates. The separate
R11 full acceptance package remains outstanding; this report records the local
R1–R10 release slice only. No production, network, OData, worker, deploy or
push path was used; `current-execution-full-pytest.log` remains untracked and
unchanged.

The impacted snapshot-only route characterization was intentionally changed to
prove a missing current manifest returns 503 even when legacy rows would exist;
no snapshot fallback was restored. Work-item material GETs return only the
persisted publication quantity and fail closed for another quantity rather than
replaying BOM/ledger calculations. Paint/weld actions require the exact current
MRP requirement+item anchor for the welded counterpart before invoking the
internally committing chain service; material-issue CAS accepts that same exact
proposal anchor.

The service-level fake-1C read-back test keeps the external `post_count` at
one after a local SyncLink loss. The router test checks only the stable
current identity/idempotency adapter contract; it is not a second proof of
external read-back.

Frontend static gates on the current checkout:

```text
npm run build   # PASS
npm run lint    # PASS
npx vitest run src/ui/pages/PurchaseControlPage.test.tsx --reporter=dot  # 9 passed
```

Не входят в этот R9/R10 release slice: production export/read-back fault gate
beyond the existing service proof, production PostgreSQL MVCC operations,
Playwright critical path, and deployment rehearsal. OpenAPI was regenerated
from the application schema and `npm run api:types` refreshed transport types;
the focused canon/OpenAPI/link gate passed 43 tests. `current-execution-full-pytest.log`
сохранён и не изменён.
Удалённые runtime paths: route-sheet snapshot reader imports and the cancelled
snapshot-only characterization; R10 additionally removed runtime and
operational-SQL `planning_read_*` paths and dropped the three legacy relations.

## R10 evidence — принято локально

R10 завершена локальным test-first/implementation/correction циклом. R9 current
readers and transport anchors are the accepted prerequisite; R10 did not restore
legacy fallback paths.

### Commits

* Migration preflight/apply and anchor mapping: `dff7eb15`, `c81a2ef0`,
  `94d78403`, `7e98ddd8`, `bb3a49e3`, `df0e721e`, `dd3c2fde`, `98f5ae37`,
  `38d80150`, `82c8f52e`, `e8606948`, `7d0d4679`, `4c394e8c`, `ec702242`,
  `4356dca7`, `955e9f9e`, `08b32c63`, `d986ca45`, `e88a8e12`, `1abfbf46`,
  `32262e42`, `3bff7e93`, `9cef28a8`, `382d76aa`, `45f7e543`, `1521fc5a`,
  `49b3ed21`, `5ae66b93`, `0d1efb5a`, `198c5956`.
* Direct current-owner waves: purchase `0f99262e/c04759c0`, production
  `c1a3c0e6/f08851bb/9bf0815d`, MRP `c6206140/d081532d/c4f556ee`, exact-live
  correction `0543356b/a2f0ea57`, period `1d4d7a16/cb75814c/62e5fe5a`,
  assembly `301e453d/6f5513a2/a150e6c8`, compact checkpoint
  `ee980491/ac803b0b`.
* Runtime legacy removal and lineage corrections: `dcf18a14/c6b8a798`,
  `f413462c/967e4d88`, `ec933969/0bb47263`, `7a59a63c/82ceaf7f`,
  `823bc78a/7fa07939`, `0ab4c976/711c4ec0`, `fe174e94/514faea3`,
  `a022b65e/376a346d`, `83399705`, `c085296e`, `8726ec2f`.
* B2 planning-read drop: RED `1a8eff0a`, implementation `f960846b`,
  PostgreSQL correction `231f9250`.
* Storage reclaim/restore rehearsal: RED `ce6dc190`, implementation
  `e267153b`.
* Operational legacy-path cleanup: RED `c8651910`, corrective RED
  `b08be339`, implementation `2aea1ff1`.

### Scope and removed paths

Migrations `20260911_01_r9_purchase_current_anchor.py`,
`20260911_02_r10_purchase_anchor_cutover.py` and
`20260911_03_r10_drop_planning_read_storage.py` complete the purchase-anchor
cutover and drop `planning_read_snapshot`, `planning_read_row` and
`planning_read_root_member` after accepted-truth/current-scope validation.
Runtime services and operational rebuild SQL now use `CurrentExecutionScope` /
`CurrentExecutionRow`; the active production reader is
`read_current_projection`. Full historical payloads are not retained in
refresh checkpoints. Migration-only conversion/rehearsal tools remain explicit
offline paths.

Generation fork/carry remains intentionally active: obligation fork, physical
refresh fork, retained-reservation carry and future-supply carry preserve stable
lineage and unchanged obligations across accepted replacements. They have real
runtime callers and focused tests; they are not legacy read-model paths.

### Commands and results

Focused current/cleanup gate:

```text
pytest -q tests/r10/test_r10_legacy_path_cleanup.py tests/test_ledger_rebuild_operations.py tests/test_pg_rebuild_check.py tests/services/test_production_control_journal_projection.py tests/routers/test_production_control_journal_contract.py tests/services/test_paint_weld_route_sheet.py tests/routers/test_production_control_execution_sync.py tests/r10/test_r10_production_current_publication.py
73 passed, 1 skipped
```

Generation/fork/carry gate: `102 passed`. Canon/orchestrator/publish gate:
`87 passed`. Local PostgreSQL migration gate: `5 passed`; real publisher gate:
`1 passed in 91.83s`; planning-read drop gate: `8 passed`; storage rehearsal:
`3 passed`. The storage rehearsal verified custom-format `pg_dump`/
`pg_restore --list`, SHA-256, stable IDs/subject values and non-zero physical
`pg_total_relation_size` reclaim after DROP, then restored the disposable schema
and rechecked legacy rows and accepted pointers/scopes.

Final local full gate:

```text
pytest -q
2179 passed, 25 skipped, 41 warnings in 218.56s
```

### Residual risks and boundary

R11 is the final local acceptance/package review; its evidence is recorded
below. No production database, SSH, OData, 1C, workers, deploy or push action
was performed. The untracked `current-execution-full-pytest.log` was preserved
unchanged.

## R11 evidence — принято локально

R11 completed the final test-first acceptance package on the Windows local
checkout. The tested implementation/configuration boundary is `a3c6b1ae`
(this documentation commit follows it). Main-branch drift was integrated in
chronological order as `00283159` (artifact ignore) and `a4ed78fd` (dead
material-issues refresh removal).

### Environment and exact gates

* Windows; Python `3.13.7`; Node `22.19.0`; npm `10.9.3`; PostgreSQL
  `16.15` on the local DSN identity `127.0.0.1:55444/prodplan_r2` as user
  `r2_user` (password is intentionally omitted from this report).
* R11/R10 focused gate: `31 passed`.
* PostgreSQL concurrency/migration/restore gate: `65 passed in 125.36s`.
* R2 baseline seed `r2-fixed-20260910-v1`: API p95 `6.419ms`.
* R11 local report: API p95 `5.95ms`, current-publish p95 `4.204ms`, 100
  no-op repetitions, DML/row/scope/change growth `0`, WAL `32096`, dead tuples
  `0`, two semantic mutations and two audit changes, final retry no-op.
* Frontend: `npm run build` passed; `npm run lint` passed; unit `321 passed`,
  bundle check `3 passed`; Playwright smoke `18 passed`.
* Full local Python gate: `2194 passed, 30 skipped, 41 warnings in 214.48s`.

### A01–A18 evidence matrix

| ID | Concrete evidence | Result |
|---|---|---|
| A01 | `tests/r3/test_r3_runtime_integration.py::test_seed_repeat_100_preserves_one_id_and_identity_mapping` | covered |
| A02 | `tests/r5/test_r5_correction_returns.py::test_addressed_receipt_precedes_fifo_and_aggregated_purchase_cap_is_respected` | covered |
| A03 | `tests/r5/test_r5_correction_returns.py::test_decreasing_m1_replays_shared_pool_and_changes_m2_assignment` | covered |
| A04 | `tests/r3/test_r3_contract.py::test_frozen_basis_has_explicit_generation_provenance` | covered |
| A05 | `tests/services/test_obligation_refresh_orchestrator.py::test_replacement_is_end_to_end_same_plan_saved_remainder_with_history_replay` | covered |
| A06 | `tests/r8/test_r8_postgres_integration.py::test_r8_current_owner_is_atomic_on_two_postgresql_sessions` | covered |
| A07 | `tests/r4/test_r4_postgres_integration.py::test_two_postgresql_sessions_serialize_current_replenishment_without_double_apply` | covered |
| A08 | `tests/r9/test_r9_current_obligation_views.py::test_r9_current_obligation_scope_is_fail_closed_when_not_published` | covered |
| A09 | `tests/r6/test_r6_physical_custody_contract.py::test_conservation_is_role_separate_and_decimal_exact` | covered |
| A10 | `tests/r6/test_r6_physical_custody_contract.py::test_late_custody_event_is_explicitly_visible_for_baseline_rewind` | covered |
| A11 | `tests/r8/test_r8_current_execution.py::test_r8_queue_order_is_oldest_first_with_decimal_and_equal_date_tiebreak` | covered |
| A12 | R9 current GET contract + PostgreSQL atomic visibility + `frontend-erp-shell/tests/smoke/current-execution-path.spec.ts::follows persisted plan → MRP → journal basis → assembly queue links` | covered |
| A13 | `tests/services/test_one_c_purchase_order_export.py::test_purchase_order_export_recovers_exact_posted_batch_without_duplicate` | covered |
| A14 | `tests/r8/test_r8_current_execution.py::test_r8_current_owner_is_stable_across_100_noop_recalculations` | covered |
| A15 | `tests/r10/test_r10_obligation_refresh_compact_checkpoint.py::test_published_retry_reuses_current_scopes_without_saved_read_models` | covered |
| A16 | `tests/r10/test_r10_legacy_path_cleanup.py::test_operational_rebuild_sql_uses_current_owner_after_head` | covered |
| A17 | Two identical disposable-schema storage rehearsals with normalized subject-state comparison, plus R10 drop/restore tests | covered |
| A18 | Fixed pre-measurement budgets, real R2 TestClient API p95, current-publish p95, no-op/WAL/dead-tuple checks | covered |

The machine-readable matrix remains fail-closed: statuses are `covered`, not
unsupported `passed`; final evidence validation requires concrete node/command/
outcome/data records and finite numeric A18 budget metrics.

### Retention, warnings and boundary

Business/audit history remains on the owner/legal horizon with no automatic
deletion. Technical task/debug records retain for 30 days after terminal
incident closure. Migration control copies retain for 90 days after owner
sign-off and verified restore read-back. All deletion is explicit and owner
approved; no automatic deletion is enabled.

The public-contour contamination incident found during early rehearsal was
cleaned by exact predicates; the final R11 disposable-schema sentinel proof
remained unchanged at zero. Remaining warnings are noncritical React `act`
warnings and Python deprecation warnings. Cross-platform screenshots reuse the
reviewed Linux baseline with Windows `maxDiffPixelRatio=0.10`; semantic smoke
assertions remain exact.

R12 is a separate owner command and is not authorized by this report. No
production DB, SSH, OData, 1C, workers, deploy or push action is implied.
