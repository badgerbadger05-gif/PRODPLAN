-- Atomic first-transition wrapper for a pre-Ledger database.
-- Required psql variables:
--   expected_database
--   expected_generation_key = legacy-rejected-20260723-06
--   confirm = CLEAR_INITIAL_LEGACY_REJECTED_PROJECTIONS

\set ON_ERROR_STOP on

BEGIN;

CREATE TEMP TABLE initial_legacy_clear_guard ON COMMIT DROP AS
SELECT
    current_database()::text AS actual_database,
    :'expected_database'::text AS expected_database,
    :'expected_generation_key'::text AS expected_generation_key,
    :'confirm'::text AS confirmation,
    (
        SELECT count(*)
        FROM pg_stat_activity activity
        WHERE activity.datname = current_database()
          AND activity.pid <> pg_backend_pid()
          AND activity.backend_type = 'client backend'
    )::integer AS other_client_sessions;

DO $initial_guard$
DECLARE
    guard_row initial_legacy_clear_guard%ROWTYPE;
    legacy_count integer;
    other_generation_count integer;
    truth_count integer;
BEGIN
    SELECT * INTO STRICT guard_row FROM initial_legacy_clear_guard;
    IF guard_row.confirmation <> 'CLEAR_INITIAL_LEGACY_REJECTED_PROJECTIONS' THEN
        RAISE EXCEPTION 'initial legacy destructive confirmation is missing';
    END IF;
    IF guard_row.actual_database <> guard_row.expected_database THEN
        RAISE EXCEPTION
            'database guard failed: actual %, expected %',
            guard_row.actual_database,
            guard_row.expected_database;
    END IF;
    IF guard_row.expected_generation_key <> 'legacy-rejected-20260723-06' THEN
        RAISE EXCEPTION
            'unexpected initial legacy generation key: %',
            guard_row.expected_generation_key;
    END IF;
    IF guard_row.other_client_sessions <> 0 THEN
        RAISE EXCEPTION
            'database still has % other client session(s); stop API/workers first',
            guard_row.other_client_sessions;
    END IF;

    SELECT count(*) INTO truth_count FROM planning_truth_state;
    IF truth_count <> 0 THEN
        RAISE EXCEPTION
            'initial legacy clear requires an empty planning_truth_state, found % row(s)',
            truth_count;
    END IF;

    SELECT count(*) INTO legacy_count
    FROM ledger_generation
    WHERE generation_key = guard_row.expected_generation_key
      AND status = 'rejected'
      AND source_watermarks::jsonb = '{"source":"pre-lineage-schema"}'::jsonb;
    IF legacy_count <> 1 THEN
        RAISE EXCEPTION
            'expected exactly one rejected pre-lineage generation, found %',
            legacy_count;
    END IF;

    SELECT count(*) INTO other_generation_count
    FROM ledger_generation
    WHERE generation_key <> guard_row.expected_generation_key;
    IF other_generation_count <> 0 THEN
        RAISE EXCEPTION
            'initial legacy clear refuses % additional Ledger generation(s)',
            other_generation_count;
    END IF;
END
$initial_guard$;

-- The accepted pointer exists only inside this uncommitted transaction. The
-- included fail-closed clear validates it and commits both steps atomically.
UPDATE ledger_generation
SET status = 'accepted',
    accepted_at = now(),
    reason = 'Transient bootstrap-clear guard; never published'
WHERE generation_key = :'expected_generation_key'
  AND status = 'rejected';

INSERT INTO planning_truth_state (id, current_generation_id, updated_at)
SELECT 1, id, now()
FROM ledger_generation
WHERE generation_key = :'expected_generation_key'
  AND status = 'accepted';

\set confirm CLEAR_REBUILDABLE_LEDGER_PROJECTIONS
\ir clear_rebuildable_ledger_projections.sql
