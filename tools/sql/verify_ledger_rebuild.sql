\set ON_ERROR_STOP on
\pset pager off

-- Required psql variables:
--   expected_database
--   expected_generation_key
--   expected_cutoff
--   expected_fixed_run_count
--   expected_opening_baseline_at
--
-- This verifier is intentionally read-only and fail-closed.  A missing -v
-- argument fails during the set_config statement before any check can run.

BEGIN TRANSACTION READ ONLY;

SELECT
    set_config(
        'prodplan_verify.expected_database',
        :'expected_database',
        true
    ) AS expected_database,
    set_config(
        'prodplan_verify.expected_generation_key',
        :'expected_generation_key',
        true
    ) AS expected_generation_key,
    set_config(
        'prodplan_verify.expected_cutoff',
        :'expected_cutoff',
        true
    ) AS expected_cutoff,
    set_config(
        'prodplan_verify.expected_fixed_run_count',
        :'expected_fixed_run_count',
        true
    ) AS expected_fixed_run_count,
    set_config(
        'prodplan_verify.expected_opening_baseline_at',
        :'expected_opening_baseline_at',
        true
    ) AS expected_opening_baseline_at
\gset prodplan_verify_

DO $verify$
DECLARE
    v_expected_database text :=
        current_setting('prodplan_verify.expected_database');
    v_expected_generation_key text :=
        current_setting('prodplan_verify.expected_generation_key');
    v_expected_cutoff timestamptz :=
        current_setting('prodplan_verify.expected_cutoff')::timestamptz;
    v_expected_fixed_run_count integer :=
        current_setting('prodplan_verify.expected_fixed_run_count')::integer;
    v_expected_opening_baseline_at timestamp :=
        current_setting('prodplan_verify.expected_opening_baseline_at')::timestamp;
    v_generation_id bigint;
    v_physical_import_batch_id bigint;
    v_generation_cutoff timestamptz;
    v_first_run_id integer;
    v_first_freeze_version integer;
    v_count bigint;
    v_quantity numeric;
BEGIN
    IF current_database() IS DISTINCT FROM v_expected_database THEN
        RAISE EXCEPTION
            'database mismatch: expected %, connected to %',
            v_expected_database, current_database();
    END IF;

    IF v_expected_fixed_run_count < 1 THEN
        RAISE EXCEPTION
            'expected_fixed_run_count must be positive, got %',
            v_expected_fixed_run_count;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM planning_truth_state;
    IF v_count <> 1 THEN
        RAISE EXCEPTION
            'planning truth singleton count is %, expected 1', v_count;
    END IF;

    SELECT
        generation.id,
        generation.physical_import_batch_id,
        generation.cutoff
      INTO
        v_generation_id,
        v_physical_import_batch_id,
        v_generation_cutoff
      FROM planning_truth_state AS truth
      JOIN ledger_generation AS generation
        ON generation.id = truth.current_generation_id
     WHERE truth.id = 1
       AND generation.status = 'accepted'
       AND generation.accepted_at IS NOT NULL
       AND generation.generation_key = v_expected_generation_key
       AND generation.cutoff = v_expected_cutoff;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'current truth is absent, unaccepted, or differs from expected key/cutoff';
    END IF;

    SELECT count(*)
      INTO v_count
      FROM physical_import_batch
     WHERE id = v_physical_import_batch_id
       AND status = 'completed'
       AND cutoff <= v_generation_cutoff;
    IF v_count <> 1 THEN
        RAISE EXCEPTION
            'current generation has no completed physical import boundary';
    END IF;

    SELECT count(*)
      INTO v_count
      FROM planning_run
     WHERE status = 'FIXED_SNAPSHOT';
    IF v_count <> v_expected_fixed_run_count THEN
        RAISE EXCEPTION
            'fixed run count is %, expected %',
            v_count, v_expected_fixed_run_count;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM mrp_requirement AS requirement
      JOIN planning_run AS run ON run.run_id = requirement.run_id
     WHERE run.status = 'FIXED_SNAPSHOT';
    IF v_count = 0 THEN
        RAISE EXCEPTION
            'fixed runs have no MRP requirements';
    END IF;

    SELECT count(*)
      INTO v_count
      FROM mrp_requirement AS requirement
      JOIN planning_run AS run ON run.run_id = requirement.run_id
     WHERE run.status = 'FIXED_SNAPSHOT'
       AND (
           requirement.total_required_qty < 0
           OR requirement.net_required_qty < 0
           OR requirement.net_required_qty > requirement.total_required_qty
       );
    IF v_count <> 0 THEN
        RAISE EXCEPTION
            '% fixed-run MRP requirements have invalid gross/net quantities',
            v_count;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM mrp_requirement_bucket AS bucket
      JOIN planning_run AS run ON run.run_id = bucket.run_id
     WHERE run.status = 'FIXED_SNAPSHOT'
       AND (
           bucket.gross_qty < 0
           OR bucket.net_qty < 0
           OR bucket.net_qty > bucket.gross_qty
       );
    IF v_count <> 0 THEN
        RAISE EXCEPTION
            '% fixed-run MRP buckets have invalid gross/net quantities',
            v_count;
    END IF;

    SELECT run_id, active_freeze_version
      INTO v_first_run_id, v_first_freeze_version
      FROM planning_run
     WHERE status = 'FIXED_SNAPSHOT'
       AND source_plan_id = 1;
    IF NOT FOUND
       OR v_first_freeze_version IS NULL THEN
        RAISE EXCEPTION
            'fixed run for source_plan_id=1 is absent or has no active freeze';
    END IF;

    SELECT count(*), coalesce(sum(stock_qty), 0)
      INTO v_count, v_quantity
      FROM mrp_freeze_baseline
     WHERE run_id = v_first_run_id
       AND freeze_version = v_first_freeze_version
       AND baseline_at = v_expected_opening_baseline_at;
    IF v_count = 0 THEN
        RAISE EXCEPTION
            'source_plan_id=1 has no active baseline at expected timestamp %',
            v_expected_opening_baseline_at;
    END IF;
    IF v_quantity <= 0 THEN
        RAISE EXCEPTION
            'source_plan_id=1 baseline stock sum is %, expected positive',
            v_quantity;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM mrp_freeze_baseline
     WHERE run_id = v_first_run_id
       AND freeze_version = v_first_freeze_version
       AND baseline_at IS DISTINCT FROM v_expected_opening_baseline_at;
    IF v_count <> 0 THEN
        RAISE EXCEPTION
            '% source_plan_id=1 active baseline rows have a wrong timestamp',
            v_count;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM reservation_event AS event
      LEFT JOIN reservation_entry AS reservation
        ON reservation.id = event.reservation_id
       AND reservation.ledger_generation_id = v_generation_id
     WHERE event.ledger_generation_id = v_generation_id
       AND reservation.id IS NULL;
    IF v_count <> 0 THEN
        RAISE EXCEPTION
            '% reservation events escape the current generation', v_count;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM reservation_entry
     WHERE ledger_generation_id = v_generation_id;
    IF v_count = 0 THEN
        RAISE EXCEPTION
            'current generation has no reservation entries';
    END IF;

    SELECT count(*)
      INTO v_count
      FROM reservation_entry AS reservation
      LEFT JOIN (
          SELECT
              reservation_id,
              sum(reserved_delta) AS reserved_fold,
              sum(realized_delta) AS realized_fold
            FROM reservation_event
           WHERE ledger_generation_id = v_generation_id
           GROUP BY reservation_id
      ) AS fold ON fold.reservation_id = reservation.id
     WHERE reservation.ledger_generation_id = v_generation_id
       AND (
           coalesce(fold.reserved_fold, 0)
               IS DISTINCT FROM reservation.reserved_qty
           OR coalesce(fold.realized_fold, 0)
               IS DISTINCT FROM reservation.realized_qty
           OR coalesce(fold.realized_fold, 0)
               IS DISTINCT FROM reservation.replenishment_received_qty
           OR reservation.reserved_qty < 0
           OR reservation.replenishment_required_qty < 0
           OR reservation.realized_qty < 0
           OR reservation.realized_qty > reservation.replenishment_required_qty
       );
    IF v_count <> 0 THEN
        RAISE EXCEPTION
            '% reservation rows violate event fold/cache/overfill invariants',
            v_count;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM reservation_event AS event
      LEFT JOIN stock_ledger_entry AS sle
        ON sle.id = event.sle_id
       AND sle.ingest_batch_id <= v_physical_import_batch_id
       AND sle.posting_at <= v_generation_cutoff
       AND NOT EXISTS (
           SELECT 1
             FROM stock_ledger_fact_supersession AS supersession
            WHERE supersession.old_sle_id = sle.id
              AND supersession.import_batch_id <= v_physical_import_batch_id
       )
     WHERE event.ledger_generation_id = v_generation_id
       AND event.realized_delta <> 0
       AND (
           event.sle_id IS NULL
           OR sle.id IS NULL
           OR sle.qty = 0
           OR (sle.qty > 0) IS DISTINCT FROM (event.realized_delta > 0)
           OR abs(event.realized_delta) > abs(sle.qty)
       );
    IF v_count <> 0 THEN
        RAISE EXCEPTION
            '% realization events violate SLE visibility/sign/quantity',
            v_count;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM reservation_event
     WHERE ledger_generation_id = v_generation_id
       AND realized_delta <> 0;
    IF v_count = 0 THEN
        RAISE EXCEPTION
            'current generation has no realization events';
    END IF;

    SELECT count(*)
      INTO v_count
      FROM (
          SELECT sle_id
            FROM reservation_event
           WHERE ledger_generation_id = v_generation_id
             AND realized_delta <> 0
           GROUP BY sle_id
          HAVING count(*) > 1
      ) AS duplicated_sle;
    IF v_count <> 0 THEN
        RAISE EXCEPTION
            '% physical SLE rows are realized more than once', v_count;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM planning_read_snapshot
     WHERE consumer = 'purchase_control_journal'
       AND snapshot_key = 'journal:v1'
       AND ledger_generation_id = v_generation_id
       AND cutoff = v_generation_cutoff
       AND truth_status = 'accepted'
       AND jsonb_typeof(payload::jsonb) = 'object'
       AND jsonb_typeof(payload::jsonb -> 'rows') = 'array';
    IF v_count <> 1 THEN
        RAISE EXCEPTION
            'current accepted purchase journal snapshot count is %, expected 1',
            v_count;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM planning_read_snapshot AS snapshot
      CROSS JOIN LATERAL
          jsonb_array_elements(snapshot.payload::jsonb -> 'rows') AS buy(row)
     WHERE snapshot.consumer = 'purchase_control_journal'
       AND snapshot.snapshot_key = 'journal:v1'
       AND snapshot.ledger_generation_id = v_generation_id
       AND snapshot.truth_status = 'accepted';
    IF v_count = 0 THEN
        RAISE EXCEPTION
            'current purchase journal has no BUY rows to verify';
    END IF;

    SELECT count(*)
      INTO v_count
      FROM planning_read_snapshot AS snapshot
      CROSS JOIN LATERAL
          jsonb_array_elements(snapshot.payload::jsonb -> 'rows') AS buy(row)
     WHERE snapshot.consumer = 'purchase_control_journal'
       AND snapshot.snapshot_key = 'journal:v1'
       AND snapshot.ledger_generation_id = v_generation_id
       AND snapshot.truth_status = 'accepted'
       AND (
           NOT (buy.row ?& ARRAY[
               'row_key',
               'row_generator',
               'required_qty',
               'realized_qty',
               'open_order_covered_qty',
               'to_order_qty',
               'quantity',
               'received_qty',
               'remaining_qty'
           ])
           OR buy.row ->> 'row_generator' <> 'mrp_reservation'
           OR buy.row ->> 'row_key' NOT LIKE 'buy:%'
           OR (buy.row ->> 'required_qty')::numeric < 0
           OR (buy.row ->> 'realized_qty')::numeric < 0
           OR (buy.row ->> 'open_order_covered_qty')::numeric < 0
           OR (buy.row ->> 'to_order_qty')::numeric < 0
           OR abs(
               (buy.row ->> 'required_qty')::numeric
               - (
                   (buy.row ->> 'realized_qty')::numeric
                   + (buy.row ->> 'open_order_covered_qty')::numeric
                   + (buy.row ->> 'to_order_qty')::numeric
               )
           ) > 0.000001
           OR abs(
               (buy.row ->> 'quantity')::numeric
               - (buy.row ->> 'required_qty')::numeric
           ) > 0.000001
           OR abs(
               (buy.row ->> 'received_qty')::numeric
               - (buy.row ->> 'realized_qty')::numeric
           ) > 0.000001
           OR abs(
               (buy.row ->> 'remaining_qty')::numeric
               - (buy.row ->> 'to_order_qty')::numeric
           ) > 0.000001
       );
    IF v_count <> 0 THEN
        RAISE EXCEPTION
            '% purchase journal BUY rows violate equation or aliases', v_count;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM (
          SELECT buy.row ->> 'row_key' AS row_key
            FROM planning_read_snapshot AS snapshot
            CROSS JOIN LATERAL
                jsonb_array_elements(snapshot.payload::jsonb -> 'rows') AS buy(row)
           WHERE snapshot.consumer = 'purchase_control_journal'
             AND snapshot.snapshot_key = 'journal:v1'
             AND snapshot.ledger_generation_id = v_generation_id
             AND snapshot.truth_status = 'accepted'
           GROUP BY buy.row ->> 'row_key'
          HAVING count(*) > 1
      ) AS duplicate_buy_key;
    IF v_count <> 0 THEN
        RAISE EXCEPTION
            '% purchase journal BUY row keys are duplicated', v_count;
    END IF;

    SELECT count(*), coalesce(sum(assembly_remaining_qty), 0)
      INTO v_count, v_quantity
      FROM assembly_queue_line
     WHERE ledger_generation_id = v_generation_id;
    IF v_count = 0 OR v_quantity <= 0 THEN
        RAISE EXCEPTION
            'current assembly queue is empty or has no open quantity';
    END IF;

    SELECT count(*)
      INTO v_count
      FROM assembly_queue_line
     WHERE ledger_generation_id = v_generation_id
       AND assembly_remaining_qty <= 0;
    IF v_count <> 0 THEN
        RAISE EXCEPTION
            '% current assembly queue rows are nonpositive', v_count;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM drum_schedule
     WHERE ledger_generation_id = v_generation_id
       AND status = 'completed';
    IF v_count <> 1 THEN
        RAISE EXCEPTION
            'completed current drum schedule count is %, expected 1', v_count;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM drum_slot AS slot
      JOIN drum_schedule AS schedule
        ON schedule.id = slot.drum_schedule_id
     WHERE schedule.ledger_generation_id = v_generation_id;
    IF v_count = 0 THEN
        RAISE EXCEPTION
            'current drum has no slots';
    END IF;

    SELECT count(*)
      INTO v_count
      FROM drum_slot AS slot
      JOIN drum_schedule AS schedule
        ON schedule.id = slot.drum_schedule_id
     WHERE schedule.ledger_generation_id = v_generation_id
       AND slot.slot_qty <= 0;
    IF v_count <> 0 THEN
        RAISE EXCEPTION
            '% current drum slots are nonpositive', v_count;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM drum_capacity_gap AS gap
      JOIN drum_schedule AS schedule
        ON schedule.id = gap.drum_schedule_id
     WHERE schedule.ledger_generation_id = v_generation_id
       AND gap.gap_qty <= 0;
    IF v_count <> 0 THEN
        RAISE EXCEPTION
            '% current drum gaps are nonpositive', v_count;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM drum_schedule AS schedule
     WHERE schedule.ledger_generation_id = v_generation_id
       AND (
           schedule.slot_row_count <> (
               SELECT count(*)
                 FROM drum_slot
                WHERE drum_schedule_id = schedule.id
           )
           OR schedule.gap_row_count <> (
               SELECT count(*)
                 FROM drum_capacity_gap
                WHERE drum_schedule_id = schedule.id
           )
           OR schedule.total_open_qty IS DISTINCT FROM (
               SELECT coalesce(sum(assembly_remaining_qty), 0)
                 FROM assembly_queue_line
                WHERE ledger_generation_id = v_generation_id
           )
           OR schedule.total_slot_qty IS DISTINCT FROM (
               SELECT coalesce(sum(slot_qty), 0)
                 FROM drum_slot
                WHERE drum_schedule_id = schedule.id
           )
           OR schedule.total_gap_qty IS DISTINCT FROM (
               SELECT coalesce(sum(gap_qty), 0)
                 FROM drum_capacity_gap
                WHERE drum_schedule_id = schedule.id
           )
           OR schedule.total_open_qty
               IS DISTINCT FROM schedule.total_slot_qty + schedule.total_gap_qty
       );
    IF v_count <> 0 THEN
        RAISE EXCEPTION
            'current drum header counts or totals violate conservation';
    END IF;

    SELECT count(*)
      INTO v_count
      FROM assembly_queue_line AS queue
     WHERE queue.ledger_generation_id = v_generation_id
       AND queue.assembly_remaining_qty IS DISTINCT FROM (
           SELECT
               coalesce(sum(piece.qty), 0)
             FROM (
                 SELECT slot.slot_qty AS qty
                   FROM drum_slot AS slot
                   JOIN drum_schedule AS schedule
                     ON schedule.id = slot.drum_schedule_id
                  WHERE schedule.ledger_generation_id = v_generation_id
                    AND slot.assembly_queue_line_id = queue.id
                 UNION ALL
                 SELECT gap.gap_qty AS qty
                   FROM drum_capacity_gap AS gap
                   JOIN drum_schedule AS schedule
                     ON schedule.id = gap.drum_schedule_id
                  WHERE schedule.ledger_generation_id = v_generation_id
                    AND gap.assembly_queue_line_id = queue.id
             ) AS piece
       );
    IF v_count <> 0 THEN
        RAISE EXCEPTION
            '% assembly queue lines violate slot/gap conservation', v_count;
    END IF;
END
$verify$;

WITH current_generation AS (
    SELECT generation.*
      FROM planning_truth_state AS truth
      JOIN ledger_generation AS generation
        ON generation.id = truth.current_generation_id
     WHERE truth.id = 1
),
fixed AS (
    SELECT count(*) AS run_count
      FROM planning_run
     WHERE status = 'FIXED_SNAPSHOT'
),
mrp AS (
    SELECT
        count(*) AS requirement_count,
        coalesce(sum(requirement.total_required_qty), 0) AS gross_qty,
        coalesce(sum(requirement.net_required_qty), 0) AS net_qty
      FROM mrp_requirement AS requirement
      JOIN planning_run AS run ON run.run_id = requirement.run_id
     WHERE run.status = 'FIXED_SNAPSHOT'
),
baseline AS (
    SELECT coalesce(sum(baseline.stock_qty), 0) AS stock_qty
      FROM mrp_freeze_baseline AS baseline
      JOIN planning_run AS run ON run.run_id = baseline.run_id
     WHERE run.status = 'FIXED_SNAPSHOT'
       AND run.source_plan_id = 1
       AND baseline.freeze_version = run.active_freeze_version
),
reservation AS (
    SELECT
        count(*) AS reservation_count,
        count(*) FILTER (
            WHERE entry.realized_qty <> 0
        ) AS realized_reservation_count
      FROM reservation_entry AS entry
      CROSS JOIN current_generation AS generation
     WHERE entry.ledger_generation_id = generation.id
),
realization AS (
    SELECT
        count(*) AS event_count,
        count(DISTINCT event.sle_id) AS sle_count,
        coalesce(sum(abs(event.realized_delta)), 0) AS allocated_qty
      FROM reservation_event AS event
      CROSS JOIN current_generation AS generation
     WHERE event.ledger_generation_id = generation.id
       AND event.realized_delta <> 0
),
purchase AS (
    SELECT
        count(*) AS buy_row_count,
        coalesce(
            sum((buy.row ->> 'open_order_covered_qty')::numeric),
            0
        ) AS open_order_covered_qty
      FROM planning_read_snapshot AS snapshot
      CROSS JOIN current_generation AS generation
      CROSS JOIN LATERAL
          jsonb_array_elements(snapshot.payload::jsonb -> 'rows') AS buy(row)
     WHERE snapshot.consumer = 'purchase_control_journal'
       AND snapshot.snapshot_key = 'journal:v1'
       AND snapshot.ledger_generation_id = generation.id
       AND snapshot.truth_status = 'accepted'
),
queue AS (
    SELECT
        count(*) AS queue_row_count,
        coalesce(sum(line.assembly_remaining_qty), 0) AS open_qty
      FROM assembly_queue_line AS line
      CROSS JOIN current_generation AS generation
     WHERE line.ledger_generation_id = generation.id
),
drum AS (
    SELECT
        schedule.slot_row_count,
        schedule.gap_row_count
      FROM drum_schedule AS schedule
      CROSS JOIN current_generation AS generation
     WHERE schedule.ledger_generation_id = generation.id
)
SELECT
    current_database() AS database_name,
    generation.id AS generation_id,
    generation.generation_key,
    generation.cutoff,
    fixed.run_count AS fixed_runs,
    mrp.requirement_count AS mrp_rows,
    mrp.gross_qty AS mrp_gross_qty,
    mrp.net_qty AS mrp_net_qty,
    baseline.stock_qty AS opening_stock_qty,
    reservation.reservation_count AS reservations,
    reservation.realized_reservation_count AS realized_reservations,
    realization.event_count AS realize_events,
    realization.sle_count AS realized_sles,
    realization.allocated_qty AS realized_qty,
    purchase.buy_row_count AS buy_rows,
    purchase.open_order_covered_qty,
    queue.queue_row_count AS queue_rows,
    queue.open_qty AS queue_open_qty,
    drum.slot_row_count AS drum_slots,
    drum.gap_row_count AS drum_gaps
  FROM current_generation AS generation
  CROSS JOIN fixed
  CROSS JOIN mrp
  CROSS JOIN baseline
  CROSS JOIN reservation
  CROSS JOIN realization
  CROSS JOIN purchase
  CROSS JOIN queue
  CROSS JOIN drum;

\echo 'PASS: Ledger rebuild invariants verified'

ROLLBACK;
