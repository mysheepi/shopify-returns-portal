-- Link each refund line item back to the Shopify return object (if one exists).
-- Presence of return_id = merchant created a return request (DHL label issued via Shopify).
-- NULL = standalone refund with no return object = goodwill / partial discount.
ALTER TABLE refund_line_items
    ADD COLUMN IF NOT EXISTS return_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_rli_return_id ON refund_line_items(return_id);
