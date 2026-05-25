# Architecture

## Components
- Frontend (React ERP shell): SPA at port 9000
- Backend (FastAPI): REST API at port 8000
- DB (PostgreSQL): application and planning data
- 1C integration: OData sync

## Code layout
Backend:
- `backend/app/main.py` - app bootstrap
- `backend/app/routers/` - HTTP endpoints
- `backend/app/services/` - business logic
- `backend/app/models.py` - SQLAlchemy models
- `backend/alembic/` - migrations

Frontend:
- `frontend-erp-shell/src/ui/pages/` - pages
- `frontend-erp-shell/src/ui/layout/` - shell layout
- `frontend-erp-shell/src/services/` - typed API services
- `frontend-erp-shell/src/lib/api.ts` - API transport wrapper

## MRP flow
1. Load config and source data
2. Calculate gross/net demand
3. Create production/purchase proposals
4. Run capacity planning
5. Apply pegging and prioritization
6. Save results to `planned_*` and `capacity_load`
