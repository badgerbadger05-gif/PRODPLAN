# PRODPLAN NEXT test production deploy

This runbook is for a separate test production stack. It must not share the
running production database, backend, frontend, or 1C base.

## Isolation

- Compose file: `docker-compose.test.yml`
- Compose project: `prodplan-next-test`
- Frontend port: `9010` by default
- Backend port: `8010` by default
- PostgreSQL host port: `55433` by default
- PostgreSQL database: `prodplan_next` by default
- Data volume: `prodplan-next-test_prodplan_next_postgres_data`
- OData config mount: `./config-test`
- Output mount: `./output-test`

The default test OData base is:

```text
http://mtzw7/unf_demo/odata/standard.odata
```

Do not use the main production OData base:

```text
http://mtzw7/unf/odata/standard.odata
```

Write operations to 1C have an additional backend guard and require an URL that
looks like the demo base (`unf_demo`) unless explicitly overridden in code.
For child 1C documents, write checks must also preserve the business basis:
`ДокументОснование` and `ДокументОснование_Type` must point to the parent
document in the chain (transfer/manufacture from production order; piecework
order from manufacture).

## First deploy

Use `/opt/prodplan-next` if the deploy user has sudo/write access to `/opt`.
On `mtzdock.lan`, the test stack currently runs from:

```text
/home/barsukov/prodplan-next
```

```bash
cd /home/barsukov/prodplan-next
mkdir -p config-test output-test
cp config-test/odata_config.example.json config-test/odata_config.json
nano config-test/odata_config.json
docker compose -f docker-compose.test.yml build
docker compose -f docker-compose.test.yml up -d
docker compose -f docker-compose.test.yml ps
```

Set the demo 1C credentials in `config-test/odata_config.json`. Do not commit
that file. The required `base_url` is:

```text
http://mtzw7/unf_demo/odata/standard.odata
```

Then open:

```text
http://mtzdock.lan:9010
```

If the server cannot reach the npm registry during frontend Docker build, build
the React shell locally, copy `frontend-erp-shell/dist` to the server, and use a
small compose override that points frontend to a prebuilt nginx Dockerfile.
This keeps the deployed UI identical to the locally verified build while
avoiding npm network timeouts on the server.

## Update deploy

```bash
cd /home/barsukov/prodplan-next
git pull
docker compose -f docker-compose.test.yml build
docker compose -f docker-compose.test.yml up -d
docker compose -f docker-compose.test.yml exec backend alembic upgrade head
docker compose -f docker-compose.test.yml ps
```

## Empty database bootstrap

The legacy app still creates the baseline schema with SQLAlchemy metadata on
backend startup. For a completely empty test database, start the backend once,
then stamp Alembic to the current head and seed the active planning config:

```bash
docker compose -f docker-compose.test.yml up -d backend
docker compose -f docker-compose.test.yml exec backend alembic stamp head
docker compose -f docker-compose.test.yml exec backend python - <<'PY'
from app.database import SessionLocal
from app.services.planning_service import DEFAULT_PLANNING_CONFIG, create_planning_config_version, list_planning_configs

db = SessionLocal()
try:
    if list_planning_configs(db, limit=1, offset=0)["total"] == 0:
        create_planning_config_version(
            db,
            DEFAULT_PLANNING_CONFIG,
            comment="initial planning config seed",
            created_by="deploy",
            activate=True,
        )
finally:
    db.close()
PY
```

## Safety checks

```bash
docker compose -f docker-compose.test.yml exec backend python - <<'PY'
from app.services.odata_config import load_odata_config
cfg = load_odata_config()
print(cfg.get("base_url"))
assert "unf_demo" in (cfg.get("base_url") or "").lower()
PY

curl -I http://localhost:9010
curl -I http://localhost:8010/health
```

If the OData URL does not contain `unf_demo`, stop before writing anything to 1C.
If a dry-run payload for a child document does not contain `ДокументОснование`
and `ДокументОснование_Type`, stop before writing anything to 1C.

Optional metadata smoke check:

```bash
curl -s http://localhost:8010/api/v1/odata/test -X POST \
  -H 'Content-Type: application/json' \
  --data @config-test/odata_config.json
```
