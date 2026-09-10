# Current-execution release report

Дата среза: 2026-09-10. Область: локальные волны R1–R4. Продовые БД, SSH,
OData, боевые workers, deploy и push не использовались.

## Сводка волн

| Волна | Статус на срезе | Доказательство/граница |
|---|---|---|
| R1 — контракт данных, границы и предметные решения | принято локально | test-first `fd57f8fd`, документный gate `48cbd509`, implementation/docs `299ae84b` + follow-up решения; focused gate ниже |
| R2 — локальный PostgreSQL и baseline | принято локально | WSL PostgreSQL 16 runtime, migration/rollback/API baseline/full gate зелёные; Docker остаётся необязательным альтернативным runtime |
| R3 — устойчивые идентичности и принятие физики | принято локально | runtime writers, completeness/publish guards, live-MRP pointer, successor/frozen provenance и migration mapping проверены; focused PG и full gate зелёные |
| R4 — транзакционные основания и текущее исполнение | принято локально | current writer, typed provenance, role separation, rebuild closure, PG atomicity и полный gate зелёные |
| R5 — исправления, отмены и backdate | не начато | incremental persistence scope отложен в зависимую волну |
| R6 — физический Ledger и custody | не начато | только контрактные границы R1 |
| R7 — выпуск плана, MRP и будущие поставки | не начато | только зафиксированы границы successor MRP |
| R8 — барабан, полки и мехцех | не начато | только owner/identity contract |
| R9 — API, UI и обменные ссылки | не начато | UI/backend migration не выполнялась |
| R10 — миграция и удаление старого контура | не начато | migration rehearsal не выполнялся |
| R11 — полная локальная приёмка | не начато | этот report не является R11 release approval |

`принято локально` выставляется только после полного exit gate соответствующей
волны. R1–R4 приняты локально; R5–R11 намеренно не продвигаются.

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
Эти риски не разрешают объявлять R5–R11 выполненными.

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
contour. R5–R11 не начинались.
