# Frontend ERP Shell Migration Status

## Decision fixed on 2026-05-23
Migration phase is complete for this branch policy:
- Quasar/Vue frontend removed from `next-erp`
- `frontend-erp-shell` is now the single frontend source of truth
- all new UI work must be implemented only in React shell

## Operational consequences
1. `docker-compose.yml` frontend service builds from `./frontend-erp-shell`.
2. Legacy references to `frontend/src/...` in older docs are historical notes only.
3. Any new feature or bugfix in UI must target `frontend-erp-shell/src/...`.

## Branch policy
- `main`: production-stable line
- `next-erp`: active refactor/development line

## Cleanup policy
- Remove legacy artifacts when they are not required by runtime.
- Keep historical documents, but treat Quasar references as archive context.
