# How To Work In This Repo

This repository is now split strictly into:
- stable production branch: `main`
- active development branch: `next-erp`

## Current stack
- Backend: FastAPI + SQLAlchemy + Alembic
- Frontend: React + TypeScript + Vite (`frontend-erp-shell`)
- Database: PostgreSQL

Legacy Quasar/Vue frontend has been removed from this branch on 2026-05-23.

## Change rules
1. Any DB model change in `backend/app/models.py` must include an Alembic migration.
2. Any backend business logic change must include or update pytest tests.
3. Any frontend API integration must go through `frontend-erp-shell/src/lib/api.ts` and `frontend-erp-shell/src/services/*`.
4. After finishing a task, update project progress docs.

## Frontend table rules
1. Registry/list screens must keep command buttons in `commandBar`; column filters must live in the table pane and align to the same column `doctype`/`colgroup` as the data table.
2. Do not place unrelated filter grids above a table. If a filter belongs to a column (`Статус`, `Участок`, `Обеспечение`, dates, etc.), render it under that column in a `columnFilterTable`.
3. Keep action buttons and filter inputs on a common row height and baseline. Search/apply controls may span descriptive columns, but should still use the table column geometry.
4. For ordinary tables, define columns as a small doctype near the page (key, title, width/minWidth, grow, align, sortable) and reuse it for filters, headers, and body widths. The descriptive item/detail column should usually be the only growing column.
5. Matrix-like screens (period plan matrix, weekly report) may use a separate matrix blueprint, but must still keep toolbar/filter layout consistent with the registry rules.

## Local run
```bash
docker compose up -d --build
```

## Health checks
- Backend: `http://localhost:8000/health`
- Frontend: `http://localhost:9000`
