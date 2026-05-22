-- Date the customer registered the return in Shopify (Return object created_at).
-- NULL for goodwill/standalone refunds (no return object).
-- days_to_refund is now computed from return_date (physical) or refund_date (goodwill).
ALTER TABLE refund_line_items
    ADD COLUMN IF NOT EXISTS return_date DATE;
