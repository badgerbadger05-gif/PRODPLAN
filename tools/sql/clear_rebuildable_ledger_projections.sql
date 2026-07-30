-- Destructively clear only generation-bound Ledger/MRP state before a full
-- historical replay.  Reference data, plans, external-document idempotency,
-- shelf policies, assembly rates and manual forced-order requests are kept.
--
-- Required psql variables:
--   expected_database
--   expected_generation_key
--   confirm = CLEAR_REBUILDABLE_LEDGER_PROJECTIONS
--
-- Example:
--   psql "$DATABASE_URL" -X -v ON_ERROR_STOP=1 \
--     -v expected_database=prodplan_shadow \
--     -v expected_generation_key=fix-period-plan:11:26 \
--     -v confirm=CLEAR_REBUILDABLE_LEDGER_PROJECTIONS \
--     -f tools/sql/clear_rebuildable_ledger_projections.sql

\set ON_ERROR_STOP on

BEGIN;

CREATE TEMP TABLE rebuild_clear_guard ON COMMIT DROP AS
SELECT
    current_database()::text AS actual_database,
    :'expected_database'::text AS expected_database,
    :'expected_generation_key'::text AS expected_generation_key,
    :'confirm'::text AS confirmation,
    pts.current_generation_id,
    lg.generation_key,
    lg.status,
    (
        SELECT count(*)
        FROM pg_stat_activity activity
        WHERE activity.datname = current_database()
          AND activity.pid <> pg_backend_pid()
          AND activity.backend_type = 'client backend'
    )::integer AS other_client_sessions
FROM planning_truth_state pts
JOIN ledger_generation lg
  ON lg.id = pts.current_generation_id
WHERE pts.id = 1;

DO $guard$
DECLARE
    guard_row rebuild_clear_guard%ROWTYPE;
BEGIN
    SELECT * INTO guard_row FROM rebuild_clear_guard;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'planning truth has no current accepted generation';
    END IF;
    IF guard_row.confirmation <> 'CLEAR_REBUILDABLE_LEDGER_PROJECTIONS' THEN
        RAISE EXCEPTION 'destructive confirmation is missing';
    END IF;
    IF guard_row.actual_database <> guard_row.expected_database THEN
        RAISE EXCEPTION
            'database guard failed: actual %, expected %',
            guard_row.actual_database,
            guard_row.expected_database;
    END IF;
    IF guard_row.status <> 'accepted' THEN
        RAISE EXCEPTION
            'current generation % is not accepted',
            guard_row.current_generation_id;
    END IF;
    IF guard_row.generation_key <> guard_row.expected_generation_key THEN
        RAISE EXCEPTION
            'generation guard failed: actual %, expected %',
            guard_row.generation_key,
            guard_row.expected_generation_key;
    END IF;
    IF guard_row.other_client_sessions <> 0 THEN
        RAISE EXCEPTION
            'database still has % other client session(s); stop API/workers first',
            guard_row.other_client_sessions;
    END IF;
END
$guard$;

-- Counts of non-rebuildable inputs must be unchanged by the transaction.
CREATE TEMP TABLE rebuild_keep_counts ON COMMIT DROP AS
SELECT *
FROM (
    VALUES
        ('items', (SELECT count(*) FROM items)),
        ('specifications', (SELECT count(*) FROM specifications)),
        ('production_plan_header', (SELECT count(*) FROM production_plan_header)),
        ('production_plan_line', (SELECT count(*) FROM production_plan_line)),
        ('production_orders', (SELECT count(*) FROM production_orders)),
        ('production_products', (SELECT count(*) FROM production_products)),
        ('production_material_issues', (SELECT count(*) FROM production_material_issues)),
        ('production_order_line_states', (SELECT count(*) FROM production_order_line_states)),
        ('sync_link', (SELECT count(*) FROM sync_link)),
        ('shelf_policy', (SELECT count(*) FROM shelf_policy)),
        ('dbr_assembly_rate', (SELECT count(*) FROM dbr_assembly_rate)),
        ('forced_order_request', (SELECT count(*) FROM forced_order_request))
) AS counts(table_name, row_count);

-- KEEP -> CLEAR references.  The kept business rows remain, but their pointers
-- to the retired rebuildable generation/run are deliberately detached.
-- Resolve KEEP -> CLEAR foreign keys by their structural identity.  Constraint
-- names differ between legacy create-table migrations, later named migrations,
-- SQLAlchemy metadata and PostgreSQL's 63-byte truncation, but the local
-- column and referenced table are the stable contract.
DO $detach_rebuildable_fks$
DECLARE
    expected record;
    constraint_name text;
    constraint_count integer;
BEGIN
    FOR expected IN
        SELECT *
          FROM (
              VALUES
                  ('planning_truth_state', 'current_generation_id', 'ledger_generation'),
                  ('production_material_issues', 'ledger_generation_id', 'ledger_generation'),
                  ('production_orders', 'source_run_id', 'planning_run'),
                  ('production_plan_line', 'locked_by_run_id', 'planning_run'),
                  ('production_products', 'ledger_generation_id', 'ledger_generation'),
                  ('production_products', 'source_mrp_requirement_id', 'mrp_requirement'),
                  ('production_products', 'source_planned_order_id', 'planned_order'),
                  ('production_order_line_states', 'material_coverage_ledger_generation_id', 'ledger_generation'),
                  ('sync_link', 'ledger_generation_id', 'ledger_generation'),
                  ('forced_order_request', 'run_id', 'planning_run')
          ) AS expected_fk(table_name, column_name, target_table)
    LOOP
        SELECT count(*), min(constraint_row.conname)
          INTO constraint_count, constraint_name
          FROM pg_constraint AS constraint_row
          JOIN pg_attribute AS local_column
            ON local_column.attrelid = constraint_row.conrelid
           AND local_column.attnum = constraint_row.conkey[1]
         WHERE constraint_row.conrelid = expected.table_name::regclass
           AND constraint_row.confrelid = expected.target_table::regclass
           AND constraint_row.contype = 'f'
           AND cardinality(constraint_row.conkey) = 1
           AND local_column.attname = expected.column_name;
        IF constraint_count <> 1 THEN
            RAISE EXCEPTION
                'required FK %.% -> % count is %, expected 1',
                expected.table_name,
                expected.column_name,
                expected.target_table,
                constraint_count;
        END IF;
        EXECUTE format(
            'ALTER TABLE %I DROP CONSTRAINT %I',
            expected.table_name,
            constraint_name
        );
    END LOOP;
END
$detach_rebuildable_fks$;

UPDATE production_orders
SET source_run_id = NULL
WHERE source_run_id IS NOT NULL;

UPDATE production_plan_line
SET locked_by_run_id = NULL
WHERE locked_by_run_id IS NOT NULL;

UPDATE production_products
SET
    ledger_generation_id = NULL,
    source_mrp_requirement_id = NULL,
    source_planned_order_id = NULL
WHERE ledger_generation_id IS NOT NULL
   OR source_mrp_requirement_id IS NOT NULL
   OR source_planned_order_id IS NOT NULL;

UPDATE production_material_issues
SET ledger_generation_id = NULL
WHERE ledger_generation_id IS NOT NULL;

-- Material coverage is a generation-bound read projection kept on an
-- operational line.  Detach both its FK and the derived payload so no status
-- from the retired generation can survive the rebuild.
UPDATE production_order_line_states
SET
    material_coverage_ledger_generation_id = NULL,
    material_coverage_status = NULL,
    material_coverage_label = NULL,
    material_coverage_calculated_at = NULL,
    material_coverage_snapshot = NULL
WHERE material_coverage_ledger_generation_id IS NOT NULL
   OR material_coverage_status IS NOT NULL
   OR material_coverage_label IS NOT NULL
   OR material_coverage_calculated_at IS NOT NULL
   OR material_coverage_snapshot IS NOT NULL;

UPDATE sync_link
SET ledger_generation_id = NULL
WHERE ledger_generation_id IS NOT NULL;

UPDATE forced_order_request
SET run_id = NULL
WHERE run_id IS NOT NULL;

UPDATE planning_truth_state
SET current_generation_id = NULL;

-- No CASCADE: a newly introduced reference to rebuildable state makes this
-- transaction fail instead of silently deleting an unreviewed table.
TRUNCATE
    stock_ledger_entry,
    stock_ledger_anchor,
    stock_ledger_fact_supersession,
    stock_ledger_supplier_receipt_provenance,
    stock_bin,
    ledger_future_supply,
    ledger_build_batch,
    ledger_generation,
    physical_import_batch,
    stock_recorder_pull,
    reservation_event,
    reservation_entry,
    pegging_link,
    mrp_freeze_allocation,
    mrp_freeze_component,
    mrp_freeze_baseline,
    mrp_requirement_bucket,
    mrp_requirement,
    planned_order_stage,
    planned_order,
    planned_purchase,
    planned_rework,
    capacity_load,
    forced_order_result,
    replenishment_work_item,
    planning_run_bucket_modes,
    mrp_bucket_type_legacy,
    planning_run,
    assembly_output_allocation,
    assembly_output_fact_decision,
    assembly_queue_line,
    drum_capacity_gap,
    drum_slot,
    drum_schedule,
    planning_read_root_member,
    planning_read_row,
    planning_read_snapshot,
    closed_plan_snapshot,
    shelf_projection,
    purchase_export_obligation_allocation,
    purchase_export_line_allocation,
    purchase_export_batch
RESTART IDENTITY;

ALTER TABLE planning_truth_state
    ADD CONSTRAINT planning_truth_state_current_generation_id_fkey
    FOREIGN KEY (current_generation_id)
    REFERENCES ledger_generation(id)
    ON DELETE RESTRICT;
ALTER TABLE production_material_issues
    ADD CONSTRAINT fk_prod_mat_issue_ledger_gen
    FOREIGN KEY (ledger_generation_id)
    REFERENCES ledger_generation(id)
    ON DELETE RESTRICT;
ALTER TABLE production_orders
    ADD CONSTRAINT fk_production_orders_source_run_id_planning_run
    FOREIGN KEY (source_run_id)
    REFERENCES planning_run(run_id)
    ON DELETE SET NULL;
ALTER TABLE production_plan_line
    ADD CONSTRAINT production_plan_line_locked_by_run_id_fkey
    FOREIGN KEY (locked_by_run_id)
    REFERENCES planning_run(run_id)
    ON DELETE SET NULL;
ALTER TABLE production_products
    ADD CONSTRAINT fk_production_products_ledger_generation
    FOREIGN KEY (ledger_generation_id)
    REFERENCES ledger_generation(id)
    ON DELETE RESTRICT;
ALTER TABLE production_products
    ADD CONSTRAINT fk_production_products_source_mrp_requirement_id
    FOREIGN KEY (source_mrp_requirement_id)
    REFERENCES mrp_requirement(id)
    ON DELETE SET NULL;
ALTER TABLE production_products
    ADD CONSTRAINT fk_production_products_source_planned_order_id_planned_order
    FOREIGN KEY (source_planned_order_id)
    REFERENCES planned_order(order_id)
    ON DELETE SET NULL;
ALTER TABLE production_order_line_states
    ADD CONSTRAINT fk_prod_line_state_coverage_generation
    FOREIGN KEY (material_coverage_ledger_generation_id)
    REFERENCES ledger_generation(id)
    ON DELETE RESTRICT;
ALTER TABLE sync_link
    ADD CONSTRAINT fk_sync_link_ledger_generation
    FOREIGN KEY (ledger_generation_id)
    REFERENCES ledger_generation(id)
    ON DELETE RESTRICT;
ALTER TABLE forced_order_request
    ADD CONSTRAINT forced_order_request_run_id_fkey
    FOREIGN KEY (run_id)
    REFERENCES planning_run(run_id)
    ON DELETE SET NULL;

DO $verify$
DECLARE
    changed_count integer;
    remaining_count bigint;
BEGIN
    SELECT count(*) INTO changed_count
    FROM rebuild_keep_counts expected
    JOIN LATERAL (
        SELECT CASE expected.table_name
            WHEN 'items' THEN (SELECT count(*) FROM items)
            WHEN 'specifications' THEN (SELECT count(*) FROM specifications)
            WHEN 'production_plan_header' THEN (SELECT count(*) FROM production_plan_header)
            WHEN 'production_plan_line' THEN (SELECT count(*) FROM production_plan_line)
            WHEN 'production_orders' THEN (SELECT count(*) FROM production_orders)
            WHEN 'production_products' THEN (SELECT count(*) FROM production_products)
            WHEN 'production_material_issues' THEN (SELECT count(*) FROM production_material_issues)
            WHEN 'production_order_line_states' THEN (SELECT count(*) FROM production_order_line_states)
            WHEN 'sync_link' THEN (SELECT count(*) FROM sync_link)
            WHEN 'shelf_policy' THEN (SELECT count(*) FROM shelf_policy)
            WHEN 'dbr_assembly_rate' THEN (SELECT count(*) FROM dbr_assembly_rate)
            WHEN 'forced_order_request' THEN (SELECT count(*) FROM forced_order_request)
        END AS row_count
    ) actual ON true
    WHERE actual.row_count <> expected.row_count;

    IF changed_count <> 0 THEN
        RAISE EXCEPTION '% kept table count(s) changed', changed_count;
    END IF;

    SELECT
        (SELECT count(*) FROM planning_run)
      + (SELECT count(*) FROM stock_ledger_entry)
      + (SELECT count(*) FROM ledger_generation)
      + (SELECT count(*) FROM reservation_entry)
      + (SELECT count(*) FROM reservation_event)
      + (SELECT count(*) FROM mrp_requirement)
      + (SELECT count(*) FROM planning_read_snapshot)
    INTO remaining_count;

    IF remaining_count <> 0 THEN
        RAISE EXCEPTION
            'canonical rebuildable state is not empty: % rows remain',
            remaining_count;
    END IF;
END
$verify$;

SELECT
    'CLEAR PASS' AS result,
    actual_database AS database_name,
    current_generation_id AS retired_generation_id,
    generation_key AS retired_generation_key
FROM rebuild_clear_guard;

COMMIT;
