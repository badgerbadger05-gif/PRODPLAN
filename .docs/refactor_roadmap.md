# Refactor Roadmap

## Completed in current cleanup pass

- Local Python 3.11 workflow added through `.venv`.
- Project-level pytest configuration added in `pytest.ini`.
- Backend test dependency gap fixed by adding `httpx`.
- Legacy root diagnostics moved to `tools/diagnostics/`.
- Reports moved to `docs/reports/`.
- OData credentials removed from tracked `config/odata_config.json`; safe example added.
- OData config load/save centralized in `backend/app/services/odata_config.py`.
- Docker Compose backend tests can run with mounted `tests/` and `pytest.ini`.
- PostgreSQL host port defaults to `55432` to avoid common local `5432` conflicts.
- SQLAlchemy declarative base import migrated to the SQLAlchemy 2 location.
- Pydantic response schemas migrated from nested `Config` to `ConfigDict`.
- Shared 1C export helpers extracted to `backend/app/services/one_c_export_common.py`.
- 1C export `SyncLink` lookup/upsert logic centralized behind shared helpers.
- 1C export OData client/config construction centralized while preserving module-local test monkeypatches.
- `frontend-erp-shell` package manager metadata aligned with the actual npm/package-lock workflow.
- Backend dependencies split into runtime (`requirements.txt`) and dev/test (`requirements-dev.txt`).
- Backend Dockerfile supports dev dependency installation through `INSTALL_DEV`; local Compose enables it for test ergonomics.
- Frontend ESLint flat config and `npm run lint` added to the React ERP shell.
- Project test scripts now run backend pytest, frontend lint, and frontend build.
- React ERP shell lint baseline cleaned to zero warnings by stabilizing page data loaders with `useCallback`.
- 1C export real-write loop centralized in `post_export_entries` while keeping domain-specific success/error callbacks in each exporter.
- Playwright Chromium smoke test added for React ERP-shell critical sections.
- Production-control warehouse settings service/router split into dedicated modules while preserving API paths and legacy imports.
- Production-control common helpers and route-sheet rendering split into dedicated modules while preserving legacy wrapper imports.
- Production-control print layer generalized to `production_control_printing.py`; material issue create/read/legacy export split into `production_control_material_issues.py`.
- Production-control material availability/coverage and shared domain helpers split into dedicated modules.
- Production-control produce/leftover return flow split into `production_control_production_flow.py`.
- Production-control journal/order materialization split into `production_control_journal.py`; old service reduced to compatibility facade.

## Next safe refactor phases

1. Remove the production-control compatibility facade when external callers are confirmed migrated, then apply the same split pattern to the next oversized service/router.
2. Continue shrinking 1C export modules by extracting payload builders only where tests already cover the exact payload shape.
3. Add CI documentation/command matrix for backend, frontend, Docker, and smoke checks.
4. Convert remaining mojibake comments/docstrings only after confirming file encoding byte-safety.
5. Add formatter policy if the team wants auto-format enforcement beyond lint/build.
