CREATE TABLE IF NOT EXISTS planning_run_bucket_modes (
    run_id INTEGER PRIMARY KEY REFERENCES planning_run(run_id) ON DELETE CASCADE,
    use_weekly BOOLEAN NOT NULL,
    legacy_bucket_types JSONB,
    weekly_rows INTEGER NOT NULL DEFAULT 0,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS mrp_bucket_type_legacy (
    entity TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL REFERENCES planning_run(run_id) ON DELETE CASCADE,
    bucket_type TEXT NOT NULL,
    bucket_date DATE NOT NULL,
    PRIMARY KEY (entity, record_id)
);

CREATE INDEX IF NOT EXISTS ix_mrp_bucket_legacy_run_entity
    ON mrp_bucket_type_legacy (run_id, entity);
