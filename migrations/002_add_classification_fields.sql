-- Migration: Add classification fields and seed merchant categories
-- Date: 2026-02-21
-- Description: Phase 3 - Transaction classification engine support

-- Add classification tracking columns to transactions
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS classification_source TEXT;
-- values: 'rule', 'llm', 'manual'

ALTER TABLE transactions ADD COLUMN IF NOT EXISTS classification_confidence FLOAT DEFAULT 1.0;

-- Index for querying LLM-classified transactions needing review
CREATE INDEX IF NOT EXISTS idx_transactions_classification_source
    ON transactions(classification_source);

CREATE INDEX IF NOT EXISTS idx_transactions_category
    ON transactions(category);

-- Seed merchant_categories with known patterns
-- These are case-insensitive substring matches against normalized merchant names

-- Food & Dining
INSERT INTO merchant_categories (merchant_pattern, category, source) VALUES
    ('swiggy', 'Food & Dining', 'manual'),
    ('bundl technologies', 'Food & Dining', 'manual'),
    ('golden handi', 'Food & Dining', 'manual'),
    ('chaitanyas food', 'Food & Dining', 'manual'),
    ('quenelle', 'Food & Dining', 'manual'),
    ('airmenus', 'Food & Dining', 'manual'),
    ('district dining', 'Food & Dining', 'manual'),
    ('olive mithai', 'Food & Dining', 'manual'),
    ('ocean of bakes', 'Food & Dining', 'manual'),
    ('brown bear bakery', 'Food & Dining', 'manual'),
    ('namasthe', 'Food & Dining', 'manual'),
    ('chicken pokodi', 'Food & Dining', 'manual'),
    ('domino', 'Food & Dining', 'manual')
ON CONFLICT (merchant_pattern) DO UPDATE SET
    category = EXCLUDED.category,
    source = EXCLUDED.source,
    updated_at = NOW();

-- Groceries
INSERT INTO merchant_categories (merchant_pattern, category, source) VALUES
    ('blinkit', 'Groceries', 'manual'),
    ('instamart', 'Groceries', 'manual'),
    ('bbnow', 'Groceries', 'manual'),
    ('licious', 'Groceries', 'manual'),
    ('kirana', 'Groceries', 'manual'),
    ('innovative retail', 'Groceries', 'manual')
ON CONFLICT (merchant_pattern) DO UPDATE SET
    category = EXCLUDED.category,
    source = EXCLUDED.source,
    updated_at = NOW();

-- Entertainment
INSERT INTO merchant_categories (merchant_pattern, category, source) VALUES
    ('bookmyshow', 'Entertainment', 'manual'),
    ('book my show', 'Entertainment', 'manual'),
    ('amb cinemas', 'Entertainment', 'manual'),
    ('gpr multiplex', 'Entertainment', 'manual'),
    ('district movies', 'Entertainment', 'manual')
ON CONFLICT (merchant_pattern) DO UPDATE SET
    category = EXCLUDED.category,
    source = EXCLUDED.source,
    updated_at = NOW();

-- Subscriptions
INSERT INTO merchant_categories (merchant_pattern, category, source) VALUES
    ('netflix', 'Subscriptions', 'manual'),
    ('apple', 'Subscriptions', 'manual'),
    ('google play', 'Subscriptions', 'manual'),
    ('googleworksp', 'Subscriptions', 'manual'),
    ('claude.ai', 'Subscriptions', 'manual')
ON CONFLICT (merchant_pattern) DO UPDATE SET
    category = EXCLUDED.category,
    source = EXCLUDED.source,
    updated_at = NOW();

-- Shopping
INSERT INTO merchant_categories (merchant_pattern, category, source) VALUES
    ('myntra', 'Shopping', 'manual'),
    ('uniqlo', 'Shopping', 'manual'),
    ('amazon', 'Shopping', 'manual'),
    ('glazed clay', 'Shopping', 'manual'),
    ('musical hub', 'Shopping', 'manual')
ON CONFLICT (merchant_pattern) DO UPDATE SET
    category = EXCLUDED.category,
    source = EXCLUDED.source,
    updated_at = NOW();

-- Transport & Fuel
INSERT INTO merchant_categories (merchant_pattern, category, source) VALUES
    ('ganesh service station', 'Transport & Fuel', 'manual'),
    ('cyberabad filling', 'Transport & Fuel', 'manual'),
    ('irctc', 'Transport & Fuel', 'manual'),
    ('filling station', 'Transport & Fuel', 'manual')
ON CONFLICT (merchant_pattern) DO UPDATE SET
    category = EXCLUDED.category,
    source = EXCLUDED.source,
    updated_at = NOW();

-- Health & Personal
INSERT INTO merchant_categories (merchant_pattern, category, source) VALUES
    ('apollo pharmacy', 'Health & Personal', 'manual'),
    ('medplus', 'Health & Personal', 'manual'),
    ('betrufit', 'Health & Personal', 'manual'),
    ('vibrant salon', 'Health & Personal', 'manual')
ON CONFLICT (merchant_pattern) DO UPDATE SET
    category = EXCLUDED.category,
    source = EXCLUDED.source,
    updated_at = NOW();

-- Gold/Investment
INSERT INTO merchant_categories (merchant_pattern, category, source) VALUES
    ('mmtc pamp', 'Shopping', 'manual')
ON CONFLICT (merchant_pattern) DO UPDATE SET
    category = EXCLUDED.category,
    source = EXCLUDED.source,
    updated_at = NOW();

COMMENT ON COLUMN transactions.classification_source IS 'How the category was assigned: rule, llm, or manual';
COMMENT ON COLUMN transactions.classification_confidence IS 'Confidence score: 1.0 for rules/manual, 0.0-1.0 for LLM';
