# Current-execution release report

Дата среза: 2026-09-10. Область: локальные волны R1 и R2. Продовые БД, SSH,
OData, боевые workers, deploy и push не использовались.

## Сводка волн

| Волна | Статус на срезе | Доказательство/граница |
|---|---|---|
| R1 — контракт данных, границы и предметные решения | принято локально | test-first `fd57f8fd`, документный gate `48cbd509`, implementation/docs `299ae84b` + follow-up решения; focused gate ниже |
| R2 — локальный PostgreSQL и baseline | принято локально | WSL PostgreSQL 16 runtime, migration/rollback/API baseline/full gate зелёные; Docker остаётся необязательным альтернативным runtime |
| R3 — устойчивые идентичности и принятие физики | не начато | production persistence не менялась |
| R4 — транзакционные основания и текущее исполнение | не начато | runtime writers не переносились |
| R5 — исправления, отмены и backdate | не начато | incremental persistence scope отложен в зависимую волну |
| R6 — физический Ledger и custody | не начато | только контрактные границы R1 |
| R7 — выпуск плана, MRP и будущие поставки | не начато | только зафиксированы границы successor MRP |
| R8 — барабан, полки и мехцех | не начато | только owner/identity contract |
| R9 — API, UI и обменные ссылки | не начато | UI/backend migration не выполнялась |
| R10 — миграция и удаление старого контура | не начато | migration rehearsal не выполнялся |
| R11 — полная локальная приёмка | не начато | этот report не является R11 release approval |

`принято локально` для R1 и R2 выставлено только после их зелёных gates;
R3–R11 намеренно не продвигаются.

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
Эти риски не разрешают объявлять R3–R11 выполненными.

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
зелёные. R3 не начиналась.
