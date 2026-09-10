# R2 local PostgreSQL contour

R2 uses a PostgreSQL-only local runtime with an explicit identity:
`127.0.0.1:55441/prodplan_r2`, user `r2_user`. Docker Compose remains
supported; on Windows the supported WSL runtime uses the named `Ubuntu`
PostgreSQL 16 cluster on port `55441`. Neither runtime starts backend,
frontend or worker processes, defines external hosts, or connects to a
production database.

## Start and verify

From the repository root, the single reproducible Windows command is:

```powershell
pwsh -NoProfile -File .\scripts\r2-postgres.ps1 -Runtime wsl -Action start-verify
```

The WSL entrypoint runs all cluster/provision/stop commands as explicit WSL
root, checks `psql`, `pg_lsclusters`, `pg_createcluster`, `pg_ctlcluster` and
`runuser`, and never runs `apt install`. It idempotently starts/verifies
PostgreSQL 16 on port `55441`, provisions only `r2_user`/`prodplan_r2` when
necessary, and verifies the exact local identity. It also keeps WSL alive with
a hidden `tail -f /dev/null` process; PID, start time, distro and command are
stored only at `%LOCALAPPDATA%\PRODPLAN\r2-runtime\wsl-keeper.json`. Verify
checks that exact PID/start identity, and stop acts only on the verified named
cluster and keeper PID. The Docker command remains available as `-Runtime
docker` and retains its non-destructive `up`/`stop` behavior; it has no
`down -v` path. When the service is available, set the explicit DSN for tests
and migration:

```powershell
$env:PRODPLAN_R2_TEST_DSN = 'postgresql://r2_user:r2_local_only@127.0.0.1:55441/prodplan_r2'
$env:DATABASE_URL = $env:PRODPLAN_R2_TEST_DSN
python -m alembic -c backend/alembic.ini upgrade head
python tools/r2-baseline.py
pytest -q tests/r2/test_r2_local_contract.py tests/r2/test_r2_postgres_integration.py
```

The guard rejects a non-local host, a missing host, the default `postgres`
database, and the application `prodplan` database. Fixtures contain only
synthetic IDs and quantities; they contain no credentials or external URLs.

## Fixed baseline

The baseline seed is `r2-fixed-20260910-v1` from
`tests/r2/fixtures/r2_synthetic_seed.json`: 2 plans, 3 items, 2 pools and 7
movements. `tools/r2-baseline.py` reports elapsed time, inserted rows, the
temporary table size, SQL write count and server identity. It also seeds the
three deterministic item rows and probes the real FastAPI read route
`GET /api/v1/items/?skip=0&limit=100` through `TestClient`, with a warm-up and
9 measured samples. The output contains numeric `api_latency_ms.min`, `p50`,
`p95` and `max` values plus `api_sample_count`; it never fabricates `null`.
The route is representative of the DB-backed item-list read path and performs
no mutation. The output and environment must be stored with the run before any
thresholds are tuned. Two consecutive WSL runs recorded:

```text
PostgreSQL 16.15 / Ubuntu / 127.0.0.1:55441
seed=r2-fixed-20260910-v1, plans=2, movements=7
sql_write_count=12, temp_table_bytes=32768
run-1: api_sample_count=9, p50=3.863ms, p95=4.881ms
run-2: api_sample_count=9, p50=3.802ms, p95=4.421ms
seed_rows=3|true (rows|timestamps_nonnull)
```

## Current environment result

On 2026-09-10 Docker Desktop's Linux engine pipe was unavailable, but the
supported WSL runtime was live and verified. The keeper survived a pause and a
second verify retained the same PID/start identity. Migration, two-session
rollback, numeric baseline and API latency all passed against the explicit
local DSN.
The probe refuses to run without a validated local DSN and migrated `items`
table. No external or production connection was attempted.
