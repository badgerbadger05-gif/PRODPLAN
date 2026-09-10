# Current-execution release report

Дата среза: 2026-09-10. Область: локальная волна R1. Продовые БД, SSH,
OData, боевые workers, deploy и push не использовались.

## Сводка волн

| Волна | Статус на срезе | Доказательство/граница |
|---|---|---|
| R1 — контракт данных, границы и предметные решения | принято локально | test-first `fd57f8fd`, документный gate `48cbd509`, implementation/docs `299ae84b` + follow-up решения; focused gate ниже |
| R2 — локальный PostgreSQL и baseline | не начато | не запускалась; отдельная волна |
| R3 — устойчивые идентичности и принятие физики | не начато | production persistence не менялась |
| R4 — транзакционные основания и текущее исполнение | не начато | runtime writers не переносились |
| R5 — исправления, отмены и backdate | не начато | incremental persistence scope отложен в зависимую волну |
| R6 — физический Ledger и custody | не начато | только контрактные границы R1 |
| R7 — выпуск плана, MRP и будущие поставки | не начато | только зафиксированы границы successor MRP |
| R8 — барабан, полки и мехцех | не начато | только owner/identity contract |
| R9 — API, UI и обменные ссылки | не начато | UI/backend migration не выполнялась |
| R10 — миграция и удаление старого контура | не начато | migration rehearsal не выполнялся |
| R11 — полная локальная приёмка | не начато | этот report не является R11 release approval |

`принято локально` для R1 выставлено только после зелёного документного gate;
остальные волны намеренно не продвигаются.

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
Сохранённый full pytest gate current-execution после test-first fixtures:
`1914 passed, 3 skipped, 35 warnings`; исходный untracked
`current-execution-full-pytest.log` сохранён без изменений.

### Removed paths and residual risks

Удалённые пути: **нет**. R1 только фиксирует допустимых writers и запрещает
новые читатели sealed lineage вне `live_plan_scope.py`; вывод legacy paths
перенесён в R3–R10 mapping/preflight.

Остаточные риски: backdate требует доказать минимальный incremental scope в
R5; schema/persistence пока generation-bound; API/UI ещё не передают новый
`history_mode`; миграция и PostgreSQL concurrency не проверялись. Эти риски не
разрешают объявлять R2–R11 выполненными.
