# PRODPLAN production deploy

This is the single deploy runbook for the live PRODPLAN instance.

## Source Of Truth

- Host: `mtzdock.lan`
- Deploy user: `barsukov`
- Project path: `/home/barsukov/prodplan`
- Compose file: `docker-compose.test.yml`
- Compose project: `prodplan`
- Frontend: `http://mtzdock.lan:9010`
- Backend health: `http://mtzdock.lan:8010/health`
- PostgreSQL host port: `55433`
- OData config mount: `/home/barsukov/prodplan/config-test`
- Output mount: `/home/barsukov/prodplan/output-test`

Current live services are:

```text
prodplan-db-1
prodplan-backend-1
prodplan-frontend-1
prodplan-sync-worker-1
prodplan-reconcile-worker-1
```

## Important Rule

Production can be ahead of the local workstation. As of 2026-06-15, production
was on commit `3bcb50af`, while this local checkout was behind it.

Before deploying from any machine, compare the local commit to production. Do
not rebuild production from an older local checkout.

```bash
git rev-parse --short HEAD
ssh barsukov@mtzdock.lan "cd /home/barsukov/prodplan && git rev-parse --short HEAD && git status --short"
```

If production is ahead, first bring the local branch up to the production code
or make the fix from the production branch. Treat production as the freshest
state until Git history proves otherwise.

## Connect

```bash
ssh barsukov@mtzdock.lan
cd /home/barsukov/prodplan
```

Never store SSH passwords or 1C credentials in this repository. Use the local
operator's credential store or the existing server setup.

## Inspect Production

```bash
cd /home/barsukov/prodplan
git status --short
git log -1 --format='%h %ci %s'
docker compose -f docker-compose.test.yml ps
docker ps --format 'table {{.Names}}\t{{.Ports}}\t{{.Status}}'
curl -fsS http://localhost:8010/health
curl -I http://localhost:9010
```

Check the active OData base without printing credentials:

```bash
docker compose -f docker-compose.test.yml exec backend python - <<'PY'
from app.services.odata_config import load_odata_config
cfg = load_odata_config()
print(cfg.get("base_url"))
PY
```

The live production base is expected to be the real `unf` OData base, not
`unf_demo`, unless the team explicitly switches the environment.

## Logs

Use exact service names from compose:

```bash
cd /home/barsukov/prodplan
docker compose -f docker-compose.test.yml logs --tail=200 backend
docker compose -f docker-compose.test.yml logs --tail=200 frontend
docker compose -f docker-compose.test.yml logs --tail=200 sync-worker
docker compose -f docker-compose.test.yml logs --tail=200 reconcile-worker
```

For an incident window:

```bash
docker compose -f docker-compose.test.yml logs --since=2026-06-15T08:00:00 backend sync-worker reconcile-worker
```

## Database

Connect through the container for production diagnostics:

```bash
docker exec -it prodplan-db-1 psql -U prodplan -d prodplan
```

One-shot read-only query example:

```bash
docker exec prodplan-db-1 psql -U prodplan -d prodplan -P pager=off -c "select now();"
```

Avoid direct SQL writes in production unless the corrective action has been
agreed explicitly. For 1C-linked documents, prefer the application flow or a
document-level correction in 1C.

## Update Deploy

Use this when the target commit is known and newer than production:

```bash
cd /home/barsukov/prodplan
git status --short
git fetch --all --prune
git log --oneline --decorate -5
git pull --ff-only

docker compose -f docker-compose.test.yml build
docker compose -f docker-compose.test.yml up -d
docker compose -f docker-compose.test.yml exec backend alembic upgrade head
docker compose -f docker-compose.test.yml ps
curl -fsS http://localhost:8010/health
```

If `git pull --ff-only` refuses, stop and inspect the branch history. Do not
force-reset production without an explicit decision.

## Restart Without Code Changes

```bash
cd /home/barsukov/prodplan
docker compose -f docker-compose.test.yml restart backend frontend sync-worker reconcile-worker
docker compose -f docker-compose.test.yml ps
```

## Rebuild One Service

Backend:

```bash
cd /home/barsukov/prodplan
docker compose -f docker-compose.test.yml build backend sync-worker reconcile-worker
docker compose -f docker-compose.test.yml up -d backend sync-worker reconcile-worker
docker compose -f docker-compose.test.yml exec backend alembic upgrade head
```

Frontend:

```bash
cd /home/barsukov/prodplan
docker compose -f docker-compose.test.yml build frontend
docker compose -f docker-compose.test.yml up -d frontend
curl -I http://localhost:9010
```

## Frontend White Screen

If `http://mtzdock.lan:9010` is blank but `curl -I http://localhost:9010`
returns `200`, inspect nginx logs:

```bash
cd /home/barsukov/prodplan
docker compose -f docker-compose.test.yml logs --tail=120 frontend
```

If nginx cannot read Vite assets, rebuild the frontend image after confirming
the Dockerfile normalizes permissions under `/usr/share/nginx/html`.

## Worker Cadence

- `sync-worker`: calls `/api/v1/sync/auto/tick` about every 120 seconds and
  runs at most one due 1C sync job per tick.
- `reconcile-worker`: calls `/api/v1/plan/reconcile` about every 10800 seconds.

Both workers use the backend service URL inside the compose network:

```text
http://backend:8000
```

## Safety Notes

- Current production uses `docker-compose.test.yml` by history, but it is the
  live production compose file on `mtzdock.lan`.
- `config-test/odata_config.json` is a live secret-bearing config file. Never
  commit it and never paste credentials into logs.
- Child 1C documents must keep their business basis: transfers and manufactures
  must point to the production order; piecework orders must point to the
  manufacture document.
- When investigating a 1C mismatch, start with backend logs, then local DB
  state, then 1C document refs. Do not "fix" linked rows with SQL before
  deciding what happens to the corresponding 1C documents.
