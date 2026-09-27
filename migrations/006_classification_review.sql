-- Existing curated patterns remain literal substring rules. New user-learned
-- rules are exact normalized merchant matches; model rules are exact-only.
ALTER TABLE merchant_categories ADD COLUMN IF NOT EXISTS match_mode TEXT NOT NULL
    DEFAULT 'substring' CHECK (match_mode IN ('exact', 'substring'));
UPDATE merchant_categories SET match_mode='exact' WHERE source='llm' AND match_mode != 'exact';

CREATE TABLE IF NOT EXISTS classification_edits (
    id BIGSERIAL PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
    before_state JSONB NOT NULL,
    after_state JSONB NOT NULL,
    rule_pattern TEXT,
    rule_before JSONB,
    rule_after JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    undone_at TIMESTAMPTZ
);
