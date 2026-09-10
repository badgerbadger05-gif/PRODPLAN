# R2 local PostgreSQL contour

R2 uses a disposable PostgreSQL-only Docker service with an explicit identity:
`127.0.0.1:55441/prodplan_r2`, user `r2_user`, compose project
`prodplan-r2-local`. The compose file starts no backend, frontend or worker and
does not define external hosts. It is never a production connection.

## Start and verify

From the repository root, the single reproducible command is:

```powershell
pwsh -NoProfile -File .\scripts\r2-postgres.ps1 start-verify
```

The script only runs `up`/`stop`; it has no destructive `down -v` path. When
the service is available, set the explicit DSN for tests and migration:

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
temporary table size, SQL write count and server identity. API latency is
explicitly `null` because R2 starts PostgreSQL only; an API baseline belongs to
the later API wave and is not fabricated here. The output and environment must
be stored with the run before any thresholds are tuned.

## Current environment result

On 2026-09-10 the safe checks found Docker CLI installed but its Linux engine
pipe unavailable; no local PostgreSQL service, `psql` or `pg_isready` executable
was available. WSL also could not reach its local service. Therefore the
PostgreSQL migration, two-session rollback test and numeric baseline remain
blocked and are not reported as green. No external or production connection
was attempted.
