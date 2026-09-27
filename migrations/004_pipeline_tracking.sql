-- Completion comes from persisted work, never MAX(email date).
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id BIGSERIAL PRIMARY KEY,
    stage TEXT NOT NULL CHECK (stage IN ('ingestion', 'extraction', 'classification')),
    scope TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'success', 'failed', 'incomplete')),
    window_start DATE,
    window_end DATE,
    counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_stage_time ON pipeline_runs(stage, started_at DESC);
CREATE TABLE IF NOT EXISTS ingestion_checkpoints (
    scope TEXT PRIMARY KEY,
    coverage_start DATE NOT NULL,
    covered_through DATE NOT NULL,
    last_success_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id BIGINT NOT NULL REFERENCES pipeline_runs(id),
    CHECK (coverage_start <= covered_through)
);
CREATE TABLE IF NOT EXISTS email_processing_outcomes (
    raw_email_id TEXT PRIMARY KEY REFERENCES raw_emails(gmail_message_id),
    parser_version TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('accepted', 'duplicate', 'review', 'rejected', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    error_code TEXT
);
