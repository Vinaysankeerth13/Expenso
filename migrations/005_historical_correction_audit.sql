-- Append-only application receipts for explicitly reviewed historical corrections.
-- Quarantined rows remain in transactions with status='unknown'; successful
-- reporting excludes them, while original values and classification survive.
CREATE TABLE IF NOT EXISTS historical_correction_audit (
    batch_id TEXT NOT NULL,
    raw_email_id TEXT NOT NULL REFERENCES raw_emails(gmail_message_id),
    action TEXT NOT NULL CHECK (action IN ('insert', 'quarantine', 'exclude', 'confirm')),
    reason TEXT NOT NULL,
    before_state JSONB NOT NULL,
    after_state JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (batch_id, raw_email_id)
);
