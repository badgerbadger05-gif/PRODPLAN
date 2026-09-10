# R3 — контракт устойчивых идентичностей и принятия физики

Статус: реализация и локальная проверка R3, 2026-09-10. Этот документ
фиксирует границы схемы и проверяемые инварианты; предметные формулы остаются
в `.docs/notes/mrp-decisions-log.md` и `.docs/CANON.md`.

## Схема и владельцы

* `StockLedgerEntry.business_identity` — стабильный ключ движения
  `movement:<recorder_type>:<recorder_ref>:<line_no>`. Поколение и batch —
  только provenance. `stock_ledger_business_identity_map` хранит явное ребро
  для каждой исторической копии; migration mapping не выбирает «последнюю
  строку».
* `PhysicalImportPage` и поля completeness на `PhysicalImportBatch` отделяют
  получение страниц источника от `LedgerGeneration`. Публикация возможна
  только при полном непрерывном наборе страниц в порядке источника.
* `PlanningLivePointer` — единственный прямой current-MRP указатель по плану.
  `PlanningRunSuccessor` хранит бизнес-историю замены. Новый current read
  обращается к pointer и не обходит `parent_generation_id`/`prior_run_id`.
* `MrpFreezeBaseline.frozen_basis_generation_id` дополняет существующую
  provenance batch/cutoff. Изменение текущего факта не переписывает frozen
  basis.

## Test-first и локальный gate

Красный test-first commit: `395270c7` — тесты отсутствующего R3 контракта
(`ModuleNotFoundError: app.services.item_ledger.r3_contract`).

После реализации:

```text
pytest -q tests/r3/test_r3_contract.py
5 passed in 0.61s

pytest -q tests/r3/test_r3_contract.py tests/services/test_item_ledger_physical_revision.py tests/services/test_item_ledger_ingest.py
26 passed in 2.81s

PRODPLAN_R2_TEST_DSN=postgresql://r2_user:r2_local_only@127.0.0.1:55441/prodplan_r2
alembic upgrade head
pytest -q tests/r3/test_r3_contract.py tests/r2/test_r2_postgres_integration.py tests/test_canon_invariants.py
41 passed in 16.61s
```

Проверены: повторный приём сохраняет ID/число фактов, замена создаёт одно
ребро supersession, неполный или переставленный импорт не становится
completed, current MRP читается через pointer, а frozen provenance имеет
отдельное поле. Production/SSH/OData/live 1С/workers/deploy/push не
использовались; удалённых путей нет.

Остаток R3: существующие orchestration writers ещё должны постепенно
заполнять pointer/successor и page receipts; это не переносится в R4 в рамках
этого минимального schema/contract slice.
