-- "Use for all payments to this merchant" also recategorizes the merchant's other
-- automated payments. Those edits point at the edit the user made, so one undo
-- reverts the whole batch. Additive and safe to rerun.
ALTER TABLE classification_edits
    ADD COLUMN IF NOT EXISTS batch_id BIGINT REFERENCES classification_edits(id);
CREATE INDEX IF NOT EXISTS classification_edits_batch_idx
    ON classification_edits(batch_id) WHERE batch_id IS NOT NULL;
