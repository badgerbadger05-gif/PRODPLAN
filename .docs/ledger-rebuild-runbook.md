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
5. Every SKU in `required_assembly_item_codes` has exactly one approved,
   positive `dbr_assembly_rate` and an existing resource. Synthetic candidate
   rates are forbidden in production.
6. The preflight command below returns `preflight-ok` before maintenance
   begins.

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
- preserves `shelf_policy`, `dbr_assembly_rate` and manual
  `forced_order_request`;
- detaches their obsolete generation/run pointers;
- truncates only Ledger/MRP generations and generation-bound projections;
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
- After clear, prefer rerunning the same idempotent manifest.
- If replay cannot be resumed, keep all services stopped. Restore only the
  verified pre-rebuild dump under an explicit recovery decision; never patch a
  partially built generation into accepted state.
- Never copy candidate synthetic rates into production.
