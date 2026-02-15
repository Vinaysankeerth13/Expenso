-- Migration: Create transactions table for HDFC Bank
-- Date: 2026-02-15
-- Description: Simple HDFC-focused transaction storage

-- Add bank metadata to raw_emails (safe to run multiple times)
ALTER TABLE raw_emails
ADD COLUMN IF NOT EXISTS bank TEXT DEFAULT 'hdfc';

ALTER TABLE raw_emails
ADD COLUMN IF NOT EXISTS account_last4 TEXT;

-- Create transactions table
CREATE TABLE IF NOT EXISTS transactions (
    id SERIAL PRIMARY KEY,
    transaction_id TEXT UNIQUE NOT NULL,
    raw_email_id TEXT NOT NULL REFERENCES raw_emails(gmail_message_id),

    -- Transaction details
    date DATE,
    amount NUMERIC(10, 2),
    currency TEXT DEFAULT 'INR',
    direction TEXT CHECK (direction IN ('in', 'out', 'unknown')),

    -- Source info (hdfc_upi, hdfc_credit_card, hdfc_debit_card)
    source TEXT,
    status TEXT CHECK (status IN ('success', 'failed', 'attempted', 'unknown')),

    -- Merchant info
    merchant TEXT,
    reference_id TEXT,

    -- Category (for future use)
    category TEXT,
    notes TEXT,

    -- Audit fields
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),

    -- Parser metadata
    parser_version TEXT DEFAULT 'hdfc_v1',
    text_source TEXT DEFAULT 'snippet',
    signals JSONB
);

-- Create indexes for common queries
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date DESC);
CREATE INDEX IF NOT EXISTS idx_transactions_source ON transactions(source);
CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_transactions_merchant ON transactions(merchant);
CREATE INDEX IF NOT EXISTS idx_transactions_raw_email_id ON transactions(raw_email_id);
CREATE INDEX IF NOT EXISTS idx_transactions_created_at ON transactions(created_at DESC);

-- Create merchant_categories table (for future categorization)
CREATE TABLE IF NOT EXISTS merchant_categories (
    id SERIAL PRIMARY KEY,
    merchant_pattern TEXT UNIQUE NOT NULL,
    category TEXT NOT NULL,
    subcategory TEXT,
    confidence FLOAT DEFAULT 1.0,
    source TEXT DEFAULT 'manual',  -- 'manual', 'llm', 'keyword'
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_merchant_categories_pattern ON merchant_categories(merchant_pattern);
CREATE INDEX IF NOT EXISTS idx_merchant_categories_category ON merchant_categories(category);

-- Add comments for documentation
COMMENT ON TABLE transactions IS 'Extracted transaction events from raw emails (HDFC Bank)';
COMMENT ON COLUMN transactions.transaction_id IS 'Unique identifier: {source}_{reference_id or email_id}';
COMMENT ON COLUMN transactions.source IS 'Transaction source: hdfc_upi, hdfc_credit_card, hdfc_debit_card';
COMMENT ON COLUMN transactions.status IS 'Transaction status: success (money moved), failed, attempted, unknown';
COMMENT ON COLUMN transactions.direction IS 'Money flow: in (credited), out (debited), unknown';
COMMENT ON COLUMN transactions.signals IS 'JSON array of extraction signals/keywords used';

COMMENT ON TABLE merchant_categories IS 'Merchant name to category mappings for transaction categorization';
