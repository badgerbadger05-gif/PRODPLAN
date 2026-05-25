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

## Local run
```bash
docker compose up -d --build
```

## Health checks
- Backend: `http://localhost:8000/health`
- Frontend: `http://localhost:9000`
