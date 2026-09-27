-- Baseline required before 001_create_transactions.sql.
-- Existing raw-email tables are preserved; this migration never replaces data.
CREATE TABLE IF NOT EXISTS raw_emails (
    id BIGSERIAL PRIMARY KEY,
    gmail_message_id TEXT UNIQUE NOT NULL,
    thread_id TEXT,
    sender TEXT,
    subject TEXT,
    snippet TEXT,
    raw_body TEXT,
    received_at TIMESTAMP WITH TIME ZONE,
    source TEXT DEFAULT 'gmail',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_raw_emails_received_at ON raw_emails(received_at);
