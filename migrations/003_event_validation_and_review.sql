-- Additive migration: historical rows and classification are not rewritten.
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS account_last4 TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS event_kind TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS original_transaction_id TEXT
    REFERENCES transactions(transaction_id);

CREATE TABLE IF NOT EXISTS transaction_notifications (
    raw_email_id TEXT PRIMARY KEY REFERENCES raw_emails(gmail_message_id),
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
    parser_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS extraction_reviews (
    raw_email_id TEXT PRIMARY KEY REFERENCES raw_emails(gmail_message_id),
    parser_version TEXT NOT NULL,
    reasons JSONB NOT NULL,
    candidate JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transactions_event_reference
    ON transactions(source, reference_id, account_last4);

-- NOT VALID preserves legacy rows pending a reviewed backfill, while enforcing
-- the invariant for every new insert/update. Application validation additionally
-- rejects implausible dates and excessive precision before NUMERIC rounds them.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'transactions'::regclass
                     AND conname = 'transactions_valid_success') THEN
        ALTER TABLE transactions ADD CONSTRAINT transactions_valid_success CHECK (
            status IS DISTINCT FROM 'success' OR (
                amount IS NOT NULL AND amount > 0 AND amount <= 99999999.99
                AND date IS NOT NULL AND date >= DATE '2000-01-01'
                AND date <= CURRENT_DATE + 1
                AND currency IS NOT NULL AND currency = 'INR'
                AND direction IS NOT NULL AND direction IN ('in', 'out')
            )
        ) NOT VALID;
    END IF;
END $$;
