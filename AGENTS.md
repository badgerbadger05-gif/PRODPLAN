# Repository Guidelines

> **ОБЯЗАТЕЛЬНО ПЕРВЫМ: прочитай `.docs/CANON.md`** — конституция структуры
> (канонические ядра, запрет дублей, один источник истины на величину).
> Волна, нарушающая канон, отклоняется на приёмке целиком.

## Project Structure & Module Organization

PRODPLAN is split into a FastAPI backend and a Vite/React frontend. Backend code lives in `backend/app`, with Alembic migrations in `backend/alembic` and worker entrypoints in `backend/sync_worker.py` and `backend/reconcile_worker.py`. Frontend source is in `frontend-erp-shell/src`; static assets are in `frontend-erp-shell/public`. Shared or operational material is kept in `docs`, `scripts`, `tools`, `config`, and `config-test`. Main pytest coverage lives in the root `tests` directory, with additional frontend smoke tests in `frontend-erp-shell/tests`.

## Build, Test, and Development Commands

Backend tests:

```powershell
pytest -q
pytest tests/services/test_production_control.py -q
```

Frontend commands, run from `frontend-erp-shell`:

```powershell
npm run dev      # start Vite on port 9300
npm run build    # TypeScript build plus production Vite bundle
npm run lint     # ESLint
npm run smoke    # Playwright smoke tests
```

Live server stack (the former 8010/9010 stack is retired and must not be
started):

```powershell
scripts/shadow-stack.sh start
scripts/shadow-stack.sh verify
```

## Coding Style & Naming Conventions

Python uses 4-space indentation, type hints where useful, and snake_case for modules, functions, and variables. Keep service logic in `backend/app/services`, request routing in `backend/app/routers`, and database shape changes in Alembic migrations. TypeScript/React uses 2-space indentation, PascalCase components, camelCase values, and domain types under `frontend-erp-shell/src/domain`. Prefer existing helpers and DTO shapes over ad hoc duplicates.

## Planning Truth Invariant

Follow [`.docs/planning-truth-contract.md`](.docs/planning-truth-contract.md).
Accepted Item Ledger generation is the only source of factual stock, movement,
execution and reservation realization/coverage. Plan snapshots are obligations,
not facts. Empty, stale or unaccepted Ledger means unavailable: fail closed,
block dependent calculations/mutations, and never fall back to legacy
aggregates. Pages read saved snapshots; heavy recalculation belongs in workers.

## Frontend Framework Invariant

Follow [`.docs/frontend-framework.md`](.docs/frontend-framework.md). Extend the
shared ERP shell instead of creating parallel DBR/MRP pages for the same
entity. Reuse existing primitives, keep HTTP in `src/services`, derive
transport types from OpenAPI, and do not move backend business formulas into
React. Migrate incrementally when touching a screen; do not start a broad
rewrite without an explicit task.

Frontend is read-only with respect to business math: totals, remaining values,
percentages, statuses, priorities, KPIs, grouping, and allowed actions must
come precomputed from a persisted backend read model. Client code may format
values and manage UI state, but must not reconstruct domain results from rows.

## Testing Guidelines

Pytest is configured by `pytest.ini` to discover `test_*.py` under `tests`, with `backend` on `pythonpath`. Add or update focused service tests for backend behavior changes, especially sync, planning, reservations, and 1C export flows. For UI changes, run `npm run build`; add Playwright coverage when behavior spans navigation or critical user workflows.

## Commit & Pull Request Guidelines

History uses short imperative commit subjects, for example `Add production reservation repair workflow` or `Repair stale MRP workshop bindings`. Keep commits scoped and avoid mixing generated artifacts or local config with product changes. Pull requests should describe the operational impact, list tests run, mention migrations or deploy steps, and include screenshots for visible UI changes.

## Security & Configuration Tips

Do not commit real 1C credentials, database passwords, or machine-specific config. Runtime OData settings belong in `config/odata_config.json` or the mounted production config, not source code. The container stack pins `Europe/Moscow`; preserve `TZ`, `PGTZ`, and Postgres timezone settings when editing compose or Docker files.
