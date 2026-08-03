# Production Ledger rebuild runbook

This procedure rebuilds the canonical Item Ledger, frozen MRP obligations and
their generation-bound read models. It does not patch quantities in place and
does not use a legacy aggregate as a fallback.

The commands resolve files from the Git checkout. The checkout may live at any
absolute path on Windows or Linux:

```bash
repo="$(git rev-parse --show-toplevel)"
cd "$repo"
```

On PowerShell the equivalent root discovery is:

```powershell
$Repo = (git rev-parse --show-toplevel).Trim()
Set-Location $Repo
```

The production maintenance commands below run on the Linux Docker host. Set
the compose/env filenames for the selected contour instead of embedding the
host checkout path:

```bash
compose_file="${COMPOSE_FILE:-docker-compose.shadow.yml}"
env_file="${COMPOSE_ENV_FILE:-.env.shadow}"
compose=(docker compose --env-file "$env_file" -f "$compose_file")
manifest="$repo/config/ledger_rebuild_history_20260729.json"
```

## Preconditions

1. The deployed commit is the reviewed rebuild commit.
2. The current DB name, accepted generation key and cutoff have been recorded.
3. A custom-format `pg_dump` and a separate evidence archive exist, have a
   SHA-256 digest and pass `pg_restore --list`.
4. The first physical boundary is
   `2026-05-31T23:59:59.999999+03:00`. Do not replace it with UTC midnight.
5. Ambiguous assembly rates are absent. A SKU without an approved rate remains
   in the assembly queue and is excluded from the drum until a rate is entered;
   this does not block Ledger/MRP rebuild or physical-output attribution.
   Synthetic candidate rates are forbidden in production.
6. The preflight command below returns `preflight-ok` before maintenance
   begins.
7. The first post-migration Ledger generation has an explicitly reviewed
   material-custody baseline. Its `observed_at` must equal the generation
   cutoff. Do not infer or backfill a historical baseline from current mutable
   material-issue statuses. Later generations fold only append-only custody
   events and fail closed when the baseline/manifest is absent.
   The reviewed cells are sealed in the replay manifest under
   `material_custody_baseline_cells`; an empty array is an explicit empty
   baseline, not an inferred default.

Create and verify the backup without relying on a host PostgreSQL install:

```bash
backup_dir="${BACKUP_DIR:?set BACKUP_DIR to an existing evidence directory}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump="$backup_dir/pre-ledger-rebuild-$stamp.dump"

"${compose[@]}" exec -T db sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$dump"
test -s "$dump"
"${compose[@]}" exec -T db pg_restore --list < "$dump" >/dev/null
sha256sum "$dump"
```

Run the rate gate read-only. The backend source is discovered inside the
container through `PRODPLAN_BACKEND_DIR`; the manifest and CLI are mounted
read-only from the checkout:

```bash
"${compose[@]}" run --rm --no-deps \
  -e PRODPLAN_BACKEND_DIR=/app \
  -v "$repo/tools:/workspace/tools:ro" \
  -v "$repo/config:/workspace/config:ro" \
  backend \
  python /workspace/tools/rebuild_ledger_history.py \
  /workspace/config/ledger_rebuild_history_20260729.json \
  --preflight-only
```

If this fails, do not clear the database.

## Rehearsal on a restored dump copy

The SQLite-based tests cannot execute either SQL file: the migration harness
stubs `ALTER TABLE ... ADD CONSTRAINT`, so most KEEP -> CLEAR foreign keys are
invisible there. `tools/pg_rebuild_check.py` runs the migrations and both files
against a real PostgreSQL and reports every stage as PASS/FAIL.

Disposable smoke rehearsal (starts and removes its own `postgres:15` on a free
port, seeds a minimal accepted generation so the clear guard can run, and takes
about fifteen seconds):

```bash
python tools/pg_rebuild_check.py
PRODPLAN_PG_CHECK=1 pytest tests/test_pg_rebuild_check.py   # same run via pytest
```

On a **restored copy** of the production dump — never on production — first
prove that the clear transaction reaches `CLEAR PASS` on the real schema:

```bash
python tools/pg_rebuild_check.py \
  --dsn "postgresql://USER:PASSWORD@HOST:PORT/prodplan_rebuild_copy" \
  --stages migrate,round-trip,clear \
  --allow-destructive-clear \
  --expected-database prodplan_rebuild_copy \
  --expected-generation-key "$expected_generation_key"
```

`--stages clear` commits on the copy, so run it only there. Add
`--psql-container NAME` when psql lives inside a container instead of on the
host. After the replay has finished on the same copy, gate it strictly:

```bash
python tools/pg_rebuild_check.py \
  --dsn "postgresql://USER:PASSWORD@HOST:PORT/prodplan_rebuild_copy" \
  --stages verify --strict \
  --expected-database prodplan_rebuild_copy \
  --expected-generation-key prod-rebuild-20260729-obligation-plan11 \
  --expected-cutoff "2026-07-28 14:30:24+03" \
  --expected-fixed-run-count 10 \
  --expected-opening-baseline-at "2026-05-31 23:59:59.999999"
```

Notes:

- The round trip is `head -> 20260726_14 -> head`. A full downgrade to base is
  impossible by design: `20260726_04` is a destructive canon cleanup whose
  `downgrade()` raises `RuntimeError`.
- Default `--smoke` mode tolerates only the documented emptiness failures of the
  verifier and additionally executes its summary projection, which the aborted
  `DO` block never reaches. `--strict` treats any failure as a failure.
- The manifest name, `expected_generation_key`, `expected_cutoff`,
  `expected_fixed_run_count` and the compose/env defaults used elsewhere in this
  runbook are still the shadow-contour values. Re-measure them on production
  before the maintenance window instead of copying them.

## Maintenance and canonical clear

Stop every database writer. The clear SQL independently refuses to run while
another client session remains:

```bash
"${compose[@]}" --profile automation stop sync-worker backend
```

Run the destructive clear with the exact values observed immediately before
maintenance:

```bash
expected_database="${EXPECTED_DATABASE:?set EXPECTED_DATABASE}"
expected_generation_key="${EXPECTED_GENERATION_KEY:?set EXPECTED_GENERATION_KEY}"

"${compose[@]}" exec -T db sh -lc \
  'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v ON_ERROR_STOP=1 \
    -v expected_database="'"$expected_database"'" \
    -v expected_generation_key="'"$expected_generation_key"'" \
    -v confirm=CLEAR_REBUILDABLE_LEDGER_PROJECTIONS' \
  < "$repo/tools/sql/clear_rebuildable_ledger_projections.sql"
```

The script runs in one transaction and must end with `CLEAR PASS`. It:

- preserves items, specifications, plans and plan lines;
- preserves 1C `sync_link` idempotency rows;
- preserves `shelf_policy` and `dbr_assembly_rate`;
- detaches their obsolete generation/run pointers;
- truncates only Ledger/MRP generations and generation-bound projections;
- clears custody events and projections whose physical provenance names the
  cleared SLE identity space; the replay must establish a new explicit custody
  baseline before publishing its first production journal;
- uses no `TRUNCATE ... CASCADE`;
- rolls back if a new unreviewed FK, wrong database/key or active writer is
  detected.

## Historical replay

Keep the normal API stopped. Run the canonical service orchestrator as a
one-off container. The extended truth age is required only because the replay
intentionally visits historical cutoffs:

```bash
"${compose[@]}" run --rm --no-deps \
  -e PRODPLAN_BACKEND_DIR=/app \
  -e PLANNING_TRUTH_MAX_AGE_SECONDS=315360000 \
  -v "$repo/tools:/workspace/tools:ro" \
  -v "$repo/config:/workspace/config:ro" \
  backend \
  python /workspace/tools/rebuild_ledger_history.py \
  /workspace/config/ledger_rebuild_history_20260729.json
```

The manifest replays plans `1..9, 11` at their original cutoffs. The command is
bounded and resumable: an existing key is reused only when cutoff, parent
lineage and fixed-plan identity match. A mismatch fails closed.

Do not start a second replay process. On an external OData interruption, rerun
the same command and manifest; do not edit rows or invent a new cutoff.

## Acceptance before publication

Run the verifier directly against PostgreSQL while the normal backend remains
stopped:

```bash
"${compose[@]}" exec -T db sh -lc \
  'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v ON_ERROR_STOP=1 \
    -v expected_database="'"$expected_database"'" \
    -v expected_generation_key=prod-rebuild-20260729-obligation-plan11 \
    -v "expected_cutoff=2026-07-28 14:30:24+03" \
    -v expected_fixed_run_count=10 \
    -v "expected_opening_baseline_at=2026-05-31 23:59:59.999999"' \
  < "$repo/tools/sql/verify_ledger_rebuild.sql"
```

Publication is allowed only after `PASS: Ledger rebuild invariants verified`.
The verifier checks:

- accepted truth key/cutoff and completed physical boundary;
- ten unique fixed-plan snapshots;
- non-zero 31 May opening stock and exact baseline timestamp;
- `0 <= net <= gross` in every requirement and bucket;
- reservation event fold, cache and overfill conservation;
- one visible, sign-correct, non-overallocated SLE per realization;
- purchase journal equation and aliases;
- non-empty assembly queue and drum;
- slot/gap/header quantity conservation and positive slots.

After PASS, start the API and frontend, then check health and the saved
purchase/assembly/drum pages:

```bash
"${compose[@]}" up -d backend frontend
"${compose[@]}" ps
```

Automation workers stay off until the accepted generation and UI snapshots
have been reviewed.

## Abort and recovery

- Before `CLEAR PASS`, any SQL failure rolls the transaction back.
- Downgrade across `20260731_01`–`20260731_04` recreates only an empty
  compatibility schema for retired tables. Deleted forced-order, legacy-plan
  and root-product rows can be recovered only from the verified database dump.
- After clear, prefer rerunning the same idempotent manifest.
- If replay cannot be resumed, keep all services stopped. Restore only the
  verified pre-rebuild dump under an explicit recovery decision; never patch a
  partially built generation into accepted state.
- Never copy candidate synthetic rates into production.
